"""Setup, polling behaviour, YAML migration, several cards and diagnostics."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.szep_kartya.const import DOMAIN
from custom_components.szep_kartya.diagnostics import async_get_config_entry_diagnostics

from .conftest import CARD, CARD2, CODE, captcha, ok, rejected


def _entry(hass, card=CARD, title='Gábor', hours=4):
    entry = MockConfigEntry(domain=DOMAIN, title=title, unique_id=card,
                            data={'card_number': card, 'card_code': CODE}, options={'scan_hours': hours})
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _entity_id(hass, unique_id, domain='sensor'):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


async def _poll(hass, freezer, hours):
    freezer.tick(timedelta(hours=hours))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_setup_creates_entities(hass: HomeAssistant, portal_mock) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    assert hass.states.get(_entity_id(hass, '3456_szallashely')).state == '51572'
    assert hass.states.get(_entity_id(hass, '3456_aktiv_magyarok')).state == '0'
    assert hass.states.get(_entity_id(hass, '3456_osszesen')).state == '51572'
    assert hass.states.get(_entity_id(hass, '3456_utolso_sikeres_lekerdezes')).state not in ('unknown', 'unavailable')
    assert hass.states.get(_entity_id(hass, '3456_lekerdezesi_problema', 'binary_sensor')).state == 'off'
    assert portal_mock.call_count == 1


async def test_failure_keeps_balance_and_captcha_backs_off(hass: HomeAssistant, portal_mock,
                                                           freezer: FrozenDateTimeFactory) -> None:
    entry = _entry(hass)
    coordinator = await _setup(hass, entry)
    main = _entity_id(hass, '3456_szallashely')

    portal_mock.return_value = captcha()
    await _poll(hass, freezer, 4)
    assert portal_mock.call_count == 2
    assert hass.states.get(main).state == '51572'
    assert hass.states.get(_entity_id(hass, '3456_lekerdezesi_problema', 'binary_sensor')).state == 'on'
    assert coordinator.data.not_before is not None

    await _poll(hass, freezer, 4)
    assert portal_mock.call_count == 2, 'skipped during the 8 hour backoff'
    portal_mock.return_value = ok(50000)
    await _poll(hass, freezer, 4)
    assert portal_mock.call_count == 3
    assert hass.states.get(main).state == '50000'
    assert coordinator.data.last_error is None


async def test_rejected_code_stops_and_starts_reauth(hass: HomeAssistant, portal_mock, freezer) -> None:
    entry = _entry(hass)
    coordinator = await _setup(hass, entry)
    portal_mock.return_value = rejected()
    await _poll(hass, freezer, 4)
    assert coordinator.data.polling_stopped
    assert any(f['context']['source'] == 'reauth' for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN))
    await _poll(hass, freezer, 24)
    assert portal_mock.call_count == 2, 'no retry with a rejected code'
    assert hass.states.get(_entity_id(hass, '3456_szallashely')).state == '51572'


async def test_nincs_kartya_is_tolerated_for_a_working_card(hass: HomeAssistant, portal_mock, freezer) -> None:
    entry = _entry(hass)
    coordinator = await _setup(hass, entry)
    portal_mock.return_value = rejected('nincs_kartya')
    for expected in (1, 2):
        await _poll(hass, freezer, 24)
        assert not coordinator.data.polling_stopped and coordinator.data.rejections == expected
    await _poll(hass, freezer, 24)
    assert coordinator.data.polling_stopped


async def test_state_survives_restart_without_new_query(hass: HomeAssistant, portal_mock, freezer) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    assert portal_mock.call_count == 1
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert portal_mock.call_count == 1, 'within 15 minutes of the last query'
    assert hass.states.get(_entity_id(hass, '3456_szallashely')).state == '51572'
    freezer.tick(timedelta(minutes=16))
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert portal_mock.call_count == 2


async def test_two_cards_share_a_captcha_backoff(hass: HomeAssistant, portal_mock, freezer) -> None:
    first, second = _entry(hass), _entry(hass, CARD2, 'Anyu')
    await _setup(hass, first)  # sets up every entry of the domain
    assert second.runtime_data is not None
    assert portal_mock.call_count == 2
    assert _entity_id(hass, '3456_szallashely') != _entity_id(hass, '4321_szallashely')

    def by_card(card_number, card_code):
        return captcha() if card_number == CARD else ok(1000)
    portal_mock.side_effect = by_card
    freezer.tick(timedelta(minutes=16))
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    assert portal_mock.call_count == 3 and first.runtime_data.data.not_before is not None
    await hass.config_entries.async_reload(second.entry_id)
    await hass.async_block_till_done()
    assert portal_mock.call_count == 3, "the second card waits for the first card's captcha backoff"
    assert second.runtime_data.data.not_before == first.runtime_data.data.not_before


async def test_yaml_is_imported_and_keeps_entity_ids_and_history(hass: HomeAssistant, portal_mock) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create('sensor', DOMAIN, '3456_szallashely', suggested_object_id='szep_kartya')
    registry.async_get_or_create('sensor', DOMAIN, '3456_aktiv_magyarok', suggested_object_id='szep_kartya_aktiv_magyarok')
    last_attempt = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    mock_restore_cache(hass, [
        State('sensor.szep_kartya', '51572', {'last_success': last_attempt, 'last_attempt': last_attempt,
                                              'rejections': 0, 'polling_stopped': True}),
        State('sensor.szep_kartya_aktiv_magyarok', '300', {}),
    ])

    assert await async_setup_component(hass, 'sensor', {'sensor': [{
        'platform': DOMAIN, 'card_number': CARD, 'card_code': CODE, 'name': 'SZÉP Kártya',
        'scan_interval': {'hours': 12}}]})
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1 and entries[0].options == {'scan_hours': 12}
    assert hass.states.get('sensor.szep_kartya').state == '51572'
    assert hass.states.get('sensor.szep_kartya_aktiv_magyarok').state == '300'
    assert registry.async_get('sensor.szep_kartya').config_entry_id == entries[0].entry_id
    assert portal_mock.call_count == 0, 'last query was 5 minutes ago'
    assert not entries[0].runtime_data.data.polling_stopped
    assert ir.async_get(hass).async_get_issue(DOMAIN, 'deprecated_yaml_3456') is not None


async def test_invalid_yaml_never_shows_values(hass: HomeAssistant, portal_mock, caplog) -> None:
    assert await async_setup_component(hass, 'sensor', {'sensor': [{
        'platform': DOMAIN, 'card_number': '123456789012345', 'card_code': 7, 'name': 'Hibás'}]})
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert '123456789012345' not in caplog.text
    assert ir.async_get(hass).async_get_issue(DOMAIN, 'invalid_yaml_Hibás') is not None


async def test_diagnostics_redact_card_data(hass: HomeAssistant, portal_mock) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)
    text = str(await async_get_config_entry_diagnostics(hass, entry))
    assert CARD not in text and "'card_code': '007'" not in text
    assert '51572' in text
