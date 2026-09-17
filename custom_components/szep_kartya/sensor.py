"""Balance sensors of a SZÉP Kártya, and the import of the old YAML platform."""

from __future__ import annotations

import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SzepKartyaConfigEntry
from .const import (
    CONF_CARD_CODE,
    CONF_CARD_NUMBER,
    CONF_SCAN_HOURS,
    DEFAULT_NAME,
    DEFAULT_SCAN_HOURS,
    DOMAIN,
    ISSUE_DEPRECATED_YAML,
    ISSUE_INVALID_YAML,
    POCKET_ACCOMMODATION,
    POCKET_ACTIVE_HUNGARIANS,
)
from .coordinator import SzepKartyaCoordinator
from .portal import is_valid_card_code, is_valid_card_number

_LOGGER = logging.getLogger(__name__)

UNIT = 'Ft'


def _as_text(value):
    # Never fails: Home Assistant appends the offending value to every schema
    # error it logs, which would put the card number or card code into the log.
    return '' if value is None else str(value).strip()


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_CARD_NUMBER): _as_text,
    vol.Required(CONF_CARD_CODE): _as_text,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
})


async def async_setup_platform(hass: HomeAssistant, config, async_add_entities, discovery_info=None):
    """Import a YAML sensor into a config entry; YAML creates no entities any more."""
    card_number = config[CONF_CARD_NUMBER]
    card_code = config[CONF_CARD_CODE]
    name = config[CONF_NAME]

    problems = []
    if not is_valid_card_number(card_number):
        problems.append('card_number must be exactly 16 digits')
    if not is_valid_card_code(card_code):
        problems.append('card_code must be exactly 3 digits')
    if problems:
        _LOGGER.error('Invalid YAML configuration: %s. (Values not shown.)', '; '.join(problems))
        ir.async_create_issue(
            hass, DOMAIN, f'{ISSUE_INVALID_YAML}_{name}',
            is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INVALID_YAML,
            translation_placeholders={'name': name},
        )
        return

    scan_interval = config.get(CONF_SCAN_INTERVAL)
    hours = scan_interval.total_seconds() / 3600 if scan_interval else DEFAULT_SCAN_HOURS
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={'source': SOURCE_IMPORT},
        data={CONF_CARD_NUMBER: card_number, CONF_CARD_CODE: card_code, CONF_NAME: name,
              CONF_SCAN_HOURS: hours},
    )
    if result.get('type') == 'abort' and result.get('reason') != 'already_configured':
        _LOGGER.error('Importing the YAML configuration of %s failed: %s', name, result.get('reason'))
        return
    ir.async_create_issue(
        hass, DOMAIN, f'{ISSUE_DEPRECATED_YAML}_{card_number[-4:]}',
        is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_DEPRECATED_YAML,
        translation_placeholders={'name': name},
    )


async def async_setup_entry(hass: HomeAssistant, entry: SzepKartyaConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        PocketSensor(coordinator, POCKET_ACCOMMODATION, 'szallashely', primary=True),
        PocketSensor(coordinator, POCKET_ACTIVE_HUNGARIANS, 'aktiv_magyarok'),
        TotalSensor(coordinator),
        LastSuccessSensor(coordinator),
    ])


def device_info(coordinator: SzepKartyaCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        name=coordinator.config_entry.title,
        manufacturer='OTP Bank',
        model=f'SZÉP Kártya *{coordinator.card_id}',
        entry_type=DeviceEntryType.SERVICE,
        configuration_url='https://magan.szepkartya.otpportalok.hu/egyenleglekerdezes/',
    )


class SzepKartyaEntity(CoordinatorEntity[SzepKartyaCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SzepKartyaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        # Same unique IDs as the 1.2.x YAML sensors, so imports keep entity IDs.
        self._attr_unique_id = f'{coordinator.card_id}_{key}'
        self._attr_device_info = device_info(coordinator)

    @property
    def available(self) -> bool:
        # A failed query keeps the last values: going unavailable and back would
        # read as spending and top-up to automations that compare states.
        return self.coordinator.data is not None


class PocketSensor(SzepKartyaEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: SzepKartyaCoordinator, pocket: str, key: str, primary: bool = False) -> None:
        super().__init__(coordinator, key)
        self._pocket = pocket
        self._primary = primary
        self._attr_icon = 'mdi:bed' if primary else 'mdi:run-fast'

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.balances.get(self._pocket)

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return data is not None and not (data.polling_stopped and self.native_value is None)

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self._primary:
            return None
        # Kept from 1.2.x for existing dashboards and automations.
        data = self.coordinator.data
        return {
            'last_success': _iso(data.last_success),
            'last_attempt': _iso(data.last_attempt),
            'last_error': data.last_error,
            'not_before': _iso(data.not_before),
            'rejections': data.rejections,
            'polling_stopped': data.polling_stopped,
            'stale': data.stale,
            'Egyenleg': f'{self.native_value} {UNIT}',
        }


class TotalSensor(SzepKartyaEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 0
    _attr_icon = 'mdi:credit-card-outline'

    def __init__(self, coordinator: SzepKartyaCoordinator) -> None:
        super().__init__(coordinator, 'osszesen')

    @property
    def native_value(self) -> int | None:
        balances = self.coordinator.data.balances
        if POCKET_ACCOMMODATION not in balances:
            return None
        return sum(balances.values())


class LastSuccessSensor(SzepKartyaEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SzepKartyaCoordinator) -> None:
        super().__init__(coordinator, 'utolso_sikeres_lekerdezes')

    @property
    def native_value(self):
        return self.coordinator.data.last_success


def _iso(value):
    return value.isoformat() if value else None
