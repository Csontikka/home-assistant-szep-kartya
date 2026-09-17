"""Problem sensor of a SZÉP Kártya."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SzepKartyaConfigEntry
from .sensor import SzepKartyaEntity, _iso


async def async_setup_entry(hass: HomeAssistant, entry: SzepKartyaConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([ProblemSensor(entry.runtime_data)])


class ProblemSensor(SzepKartyaEntity, BinarySensorEntity):
    """On when the last query failed, polling stopped, or the data is stale."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, 'lekerdezesi_problema')

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return bool(data.polling_stopped or data.last_error or data.stale)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            'last_error': data.last_error,
            'last_attempt': _iso(data.last_attempt),
            'not_before': _iso(data.not_before),
            'rejections': data.rejections,
            'polling_stopped': data.polling_stopped,
            'stale': data.stale,
        }
