"""The SZÉP Kártya integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import SzepKartyaCoordinator, async_remove_store

type SzepKartyaConfigEntry = ConfigEntry[SzepKartyaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SzepKartyaConfigEntry) -> bool:
    coordinator = SzepKartyaCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.data.polling_stopped:
        # The reauth flow of a stopped card does not survive a restart.
        entry.async_start_reauth(hass)

    # Query in the background: setup must not wait for the portal, and the
    # coordinator itself decides whether a query is due.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f'{entry.title} first refresh')
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SzepKartyaConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SzepKartyaConfigEntry) -> None:
    await async_remove_store(hass, entry.entry_id)

