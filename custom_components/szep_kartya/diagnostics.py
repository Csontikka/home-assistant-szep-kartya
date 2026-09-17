"""Diagnostics for SZÉP Kártya, without card data."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SzepKartyaConfigEntry
from .const import CONF_CARD_CODE, CONF_CARD_NUMBER

TO_REDACT = {CONF_CARD_NUMBER, CONF_CARD_CODE, 'unique_id'}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SzepKartyaConfigEntry) -> dict[str, Any]:
    entry_dict = async_redact_data(entry.as_dict(), TO_REDACT)
    return {
        'entry': entry_dict,
        'state': _state(entry),
    }


def _state(entry: SzepKartyaConfigEntry) -> dict | None:
    coordinator = getattr(entry, 'runtime_data', None)
    data = getattr(coordinator, 'data', None)
    return data.to_dict() if data else None
