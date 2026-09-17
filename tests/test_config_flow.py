"""Config, reauth and options flows."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.szep_kartya.const import DOMAIN

from .conftest import CARD, CODE, captcha, ok, rejected


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={'source': config_entries.SOURCE_USER})


async def test_user_flow_creates_entry_with_one_query(hass: HomeAssistant, portal_mock) -> None:
    result = await _start(hass)
    assert result['type'] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'Gábor', 'card_number': '1234 5678 9012 3456', 'card_code': CODE})
    await hass.async_block_till_done()
    assert result['type'] is FlowResultType.CREATE_ENTRY
    assert result['title'] == 'Gábor'
    assert result['data'] == {'card_number': CARD, 'card_code': CODE}
    assert result['options'] == {'scan_hours': 4}
    # The test query seeds the entry: setting it up does not query again.
    assert portal_mock.call_count == 1
    state = hass.states.get('sensor.gabor_szallashely_zseb')
    assert state is not None and state.state == '51572'


async def test_user_flow_format_errors_do_not_query(hass: HomeAssistant, portal_mock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': '123', 'card_code': '12'})
    assert result['type'] is FlowResultType.FORM
    assert result['errors'] == {'card_number': 'invalid_card_number', 'card_code': 'invalid_card_code'}
    assert portal_mock.call_count == 0


async def test_user_flow_rejected_and_captcha(hass: HomeAssistant, portal_mock) -> None:
    result = await _start(hass)
    portal_mock.return_value = rejected()
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['errors'] == {'base': 'card_rejected'}
    assert result['description_placeholders']['reason'] == 'hibas_kartyaszam_vagy_telekod'

    portal_mock.return_value = captcha()
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['errors'] == {'base': 'captcha'}

    portal_mock.side_effect = OSError('boom')
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['errors'] == {'base': 'cannot_connect'}


async def test_duplicate_card_aborts_before_query(hass: HomeAssistant, portal_mock) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=CARD, data={'card_number': CARD, 'card_code': CODE}).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['type'] is FlowResultType.ABORT and result['reason'] == 'already_configured'
    assert portal_mock.call_count == 0


async def test_reauth_updates_code_and_restarts_polling(hass: HomeAssistant, portal_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title='Gábor', unique_id=CARD,
                            data={'card_number': CARD, 'card_code': '999'}, options={'scan_hours': 4})
    entry.add_to_hass(hass)
    portal_mock.return_value = rejected()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.data.polling_stopped
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]['context']['source'] == config_entries.SOURCE_REAUTH

    portal_mock.return_value = ok(40000)
    result = await hass.config_entries.flow.async_configure(flows[0]['flow_id'], {'card_code': CODE})
    await hass.async_block_till_done()
    assert result['type'] is FlowResultType.ABORT and result['reason'] == 'reauth_successful'
    assert entry.data['card_code'] == CODE
    new = entry.runtime_data
    assert not new.data.polling_stopped and new.data.balances['szamla_osszeg9'] == 40000


async def test_options_change_interval(hass: HomeAssistant, portal_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title='Gábor', unique_id=CARD,
                            data={'card_number': CARD, 'card_code': CODE}, options={'scan_hours': 4})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result['flow_id'], {'scan_hours': 12})
    await hass.async_block_till_done()
    assert result['type'] is FlowResultType.CREATE_ENTRY
    assert entry.runtime_data.update_interval.total_seconds() == 12 * 3600


async def test_import_flow_keeps_scan_interval(hass: HomeAssistant, portal_mock) -> None:
    with patch('custom_components.szep_kartya.async_setup_entry', return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={'source': config_entries.SOURCE_IMPORT},
            data={'card_number': CARD, 'card_code': CODE, 'name': 'SZÉP Kártya', 'scan_hours': 12.0})
    assert result['type'] is FlowResultType.CREATE_ENTRY
    assert result['options'] == {'scan_hours': 12}
    assert portal_mock.call_count == 0
