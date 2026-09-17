"""Config flow for SZÉP Kártya."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import portal
from .const import (
    CONF_CARD_CODE,
    CONF_CARD_NUMBER,
    CONF_SCAN_HOURS,
    DEFAULT_NAME,
    DEFAULT_SCAN_HOURS,
    DOMAIN,
    MAX_SCAN_HOURS,
    MIN_SCAN_HOURS,
)
from .coordinator import CardState, GateBackoff, GateBusy, async_query_once, seed_state

_LOGGER = logging.getLogger(__name__)

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _clean(value: Any) -> str:
    return '' if value is None else str(value).replace(' ', '').replace('-', '').strip()


async def _async_check_card(flow: ConfigFlow, card_number: str, card_code: str) -> tuple[str | None, dict]:
    """Test the card once. Returns (error key, placeholders)."""
    try:
        answer = await async_query_once(flow.hass, card_number, card_code)
    except GateBackoff:
        return 'captcha', {}
    except GateBusy:
        return 'busy', {}
    except Exception as err:  # noqa: BLE001 - shown to the user as a connection problem
        _LOGGER.warning('Test query failed: %r', err)
        return 'cannot_connect', {}
    if answer.kind == portal.CAPTCHA:
        return 'captcha', {}
    if answer.kind == portal.CARD_REJECTED:
        return 'card_rejected', {'reason': answer.reason or ''}
    if answer.kind != portal.OK:
        _LOGGER.warning('Test query: %s', answer.message)
        return 'portal_error', {}
    now = dt_util.utcnow()
    seed_state(flow.hass, card_number, CardState(
        balances=answer.balances, last_success=now, last_attempt=now, last_error=answer.message))
    return None, {}


class SzepKartyaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a card, import YAML, and ask for the card code again."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {'reason': ''}
        if user_input is not None:
            card_number = _clean(user_input[CONF_CARD_NUMBER])
            card_code = _clean(user_input[CONF_CARD_CODE])
            name = (user_input.get(CONF_NAME) or '').strip() or DEFAULT_NAME
            if not portal.is_valid_card_number(card_number):
                errors[CONF_CARD_NUMBER] = 'invalid_card_number'
            if not portal.is_valid_card_code(card_code):
                errors[CONF_CARD_CODE] = 'invalid_card_code'
            if not errors:
                await self.async_set_unique_id(card_number)
                self._abort_if_unique_id_configured()
                if self._last4_taken(card_number):
                    errors['base'] = 'last4_conflict'
            if not errors:
                error, placeholders_update = await _async_check_card(self, card_number, card_code)
                placeholders.update(placeholders_update)
                if error:
                    errors['base'] = error
                else:
                    return self.async_create_entry(
                        title=name,
                        data={CONF_CARD_NUMBER: card_number, CONF_CARD_CODE: card_code},
                        options={CONF_SCAN_HOURS: DEFAULT_SCAN_HOURS},
                    )

        defaults = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Required(CONF_CARD_NUMBER): _SECRET,
            vol.Required(CONF_CARD_CODE): _SECRET,
        })
        return self.async_show_form(step_id='user', data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Move a YAML sensor to a config entry, without a test query."""
        card_number = _clean(import_data[CONF_CARD_NUMBER])
        await self.async_set_unique_id(card_number)
        self._abort_if_unique_id_configured()
        if self._last4_taken(card_number):
            return self.async_abort(reason='last4_conflict')
        hours = import_data.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS)
        hours = min(MAX_SCAN_HOURS, max(MIN_SCAN_HOURS, int(round(hours))))
        return self.async_create_entry(
            title=import_data.get(CONF_NAME) or DEFAULT_NAME,
            data={CONF_CARD_NUMBER: card_number, CONF_CARD_CODE: _clean(import_data[CONF_CARD_CODE])},
            options={CONF_SCAN_HOURS: hours},
        )

    def _last4_taken(self, card_number: str) -> bool:
        # Entity unique IDs use the last 4 digits (as 1.2.x did); two cards
        # sharing them would take over each other's entities.
        return any(
            entry.unique_id != card_number and (entry.unique_id or '')[-4:] == card_number[-4:]
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders = {'reason': '', 'name': entry.title, 'last4': entry.data[CONF_CARD_NUMBER][-4:]}
        if user_input is not None:
            card_code = _clean(user_input[CONF_CARD_CODE])
            if not portal.is_valid_card_code(card_code):
                errors[CONF_CARD_CODE] = 'invalid_card_code'
            else:
                error, placeholders_update = await _async_check_card(
                    self, entry.data[CONF_CARD_NUMBER], card_code)
                placeholders.update(placeholders_update)
                if error:
                    errors['base'] = error
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_CARD_CODE: card_code})

        return self.async_show_form(
            step_id='reauth_confirm',
            data_schema=vol.Schema({vol.Required(CONF_CARD_CODE): _SECRET}),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SzepKartyaOptionsFlow:
        return SzepKartyaOptionsFlow()


class SzepKartyaOptionsFlow(OptionsFlowWithReload):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_HOURS: int(user_input[CONF_SCAN_HOURS])})
        current = self.config_entry.options.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS)
        schema = vol.Schema({
            vol.Required(CONF_SCAN_HOURS, default=current): NumberSelector(NumberSelectorConfig(
                min=MIN_SCAN_HOURS, max=MAX_SCAN_HOURS, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement='óra')),
        })
        return self.async_show_form(step_id='init', data_schema=schema)
