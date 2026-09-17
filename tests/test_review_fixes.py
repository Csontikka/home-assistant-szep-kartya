"""Regression tests for the 2.0 review findings."""

from datetime import timedelta

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.szep_kartya.const import DOMAIN
from custom_components.szep_kartya.coordinator import get_gate

from .conftest import CARD, CARD2, CODE, captcha, ok


def _entry(hass, card=CARD, title='Gábor'):
    entry = MockConfigEntry(domain=DOMAIN, title=title, unique_id=card,
                            data={'card_number': card, 'card_code': CODE}, options={'scan_hours': 4})
    entry.add_to_hass(hass)
    return entry


def _store(hass_storage, entry, **data):
    hass_storage[f'{DOMAIN}.{entry.entry_id}'] = {
        'version': 1, 'minor_version': 1, 'key': f'{DOMAIN}.{entry.entry_id}', 'data': data}


async def test_stopped_card_asks_for_the_code_again_after_restart(hass: HomeAssistant, portal_mock,
                                                                  hass_storage) -> None:
    entry = _entry(hass)
    _store(hass_storage, entry, balances={'szamla_osszeg9': 51572}, polling_stopped=True,
           last_error='The portal rejected the card (hibas_kartyaszam_vagy_telekod)')
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f['context']['source'] for f in flows] == [config_entries.SOURCE_REAUTH]
    assert portal_mock.call_count == 0


async def test_attempt_is_stored_before_the_query(hass: HomeAssistant, portal_mock, hass_storage) -> None:
    entry = _entry(hass)
    seen = {}

    def query(card_number, card_code):
        seen['stored'] = hass_storage[f'{DOMAIN}.{entry.entry_id}']['data'].get('last_attempt')
        return ok()
    portal_mock.side_effect = query
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen['stored'] is not None


async def test_same_last_four_digits_are_refused(hass: HomeAssistant, portal_mock) -> None:
    _entry(hass, card='1111222233334444')
    result = await hass.config_entries.flow.async_init(DOMAIN, context={'source': config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': '9999888877774444', 'card_code': CODE})
    assert result['errors'] == {'base': 'last4_conflict'}
    assert portal_mock.call_count == 0
    hass.config_entries.flow.async_abort(result['flow_id'])

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={'source': config_entries.SOURCE_IMPORT},
        data={'card_number': '9999888877774444', 'card_code': CODE, 'name': 'x'})
    assert result['type'] is FlowResultType.ABORT and result['reason'] == 'last4_conflict'


async def test_flow_captcha_makes_every_query_wait(hass: HomeAssistant, portal_mock) -> None:
    portal_mock.return_value = captcha()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={'source': config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['errors'] == {'base': 'captcha'}
    assert get_gate(hass).not_before > dt_util.utcnow() + timedelta(hours=7)

    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
    assert result['errors'] == {'base': 'captcha'}
    assert portal_mock.call_count == 1, 'no second query during the backoff'


async def test_flow_does_not_wait_for_a_busy_gate(hass: HomeAssistant, portal_mock) -> None:
    gate = get_gate(hass)
    await gate.lock.acquire()
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={'source': config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result['flow_id'], {'name': 'x', 'card_number': CARD, 'card_code': CODE})
        assert result['errors'] == {'base': 'busy'}
    finally:
        gate.lock.release()
    assert portal_mock.call_count == 0


async def test_second_yaml_import_is_a_no_op(hass: HomeAssistant, portal_mock) -> None:
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={'source': config_entries.SOURCE_IMPORT},
            data={'card_number': CARD, 'card_code': CODE, 'name': 'SZÉP Kártya'})
        await hass.async_block_till_done()
        assert result['type'] is expected
    assert result['reason'] == 'already_configured'
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_keeps_a_pocket_missing_from_the_test_answer(hass: HomeAssistant, portal_mock,
                                                                  hass_storage) -> None:
    entry = _entry(hass)
    _store(hass_storage, entry, balances={'szamla_osszeg9': 1, 'szamla_osszeg8': 300}, polling_stopped=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    from custom_components.szep_kartya import portal
    portal_mock.return_value = portal.Answer(portal.OK, balances={'szamla_osszeg9': 500})
    result = await hass.config_entries.flow.async_configure(flow['flow_id'], {'card_code': CODE})
    await hass.async_block_till_done()
    assert result['reason'] == 'reauth_successful'
    assert entry.runtime_data.data.balances == {'szamla_osszeg9': 500, 'szamla_osszeg8': 300}
    assert portal_mock.call_count == 1, 'the reload after reauth does not query again'


async def test_two_cards_different_last_four_are_fine(hass: HomeAssistant, portal_mock) -> None:
    _entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={'source': config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result['flow_id'], {'name': 'Anyu', 'card_number': CARD2, 'card_code': CODE})
    assert result['type'] is FlowResultType.CREATE_ENTRY


async def test_corrupt_backoff_is_clamped(hass: HomeAssistant, portal_mock, hass_storage) -> None:
    entry = _entry(hass)
    _store(hass_storage, entry, backoff_hours=0, rejections=-5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.data.backoff_hours == 8
    assert entry.runtime_data.data.rejections == 0
