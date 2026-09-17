"""Parsing of portal answers, without Home Assistant."""

import json

import pytest

from custom_components.szep_kartya import portal


def answer(payload):
    return portal.parse_answer(json.dumps(payload))


def test_ok_both_pockets():
    a = answer([{'EREDMENY': 'OK', 'UZENET': {'szamla_osszeg9': '+000051572', 'szamla_osszeg8': '+000000000',
                                              'multipont_azonosito': '99999999'}}])
    assert a.kind == portal.OK
    assert a.balances == {'szamla_osszeg9': 51572, 'szamla_osszeg8': 0}
    assert a.message is None


def test_ok_without_active_pocket_is_flagged():
    a = answer([{'EREDMENY': 'OK', 'UZENET': {'szamla_osszeg9': '+000000100'}}])
    assert a.kind == portal.OK and a.balances == {'szamla_osszeg9': 100}
    assert 'szamla_osszeg8' in a.message


@pytest.mark.parametrize('uzenet', [{'szamla_osszeg8': '+1'}, {'szamla_osszeg9': 123}, {'szamla_osszeg9': 'abc'}])
def test_ok_with_missing_or_bad_balance_is_not_zero(uzenet):
    assert answer([{'EREDMENY': 'OK', 'UZENET': uzenet}]).kind == portal.UNEXPECTED


@pytest.mark.parametrize('payload', [[{'EREDMENY': 'RC', 'UZENET': ''}], [{'EREDMENY': 'HI', 'UZENET': 'hibas_recaptcha'}],
                                     ['RC']])
def test_captcha(payload):
    assert answer(payload).kind == portal.CAPTCHA


@pytest.mark.parametrize('reason', ['hibas_kartyaszam_vagy_telekod', 'letiltott_inaktiv_kartya', 'nincs_kartya',
                                    'virtualis_kartya'])
def test_card_rejected(reason):
    a = answer([{'EREDMENY': 'HI', 'UZENET': reason}])
    assert a.kind == portal.CARD_REJECTED and a.reason == reason


def test_legacy_hi_is_a_rejected_code():
    a = answer(['HI'])
    assert a.kind == portal.CARD_REJECTED and a.reason == 'hibas_kartyaszam_vagy_telekod'


@pytest.mark.parametrize('reason', ['api_nem_elerheto', 'otpdirekt_nem_elerheto', 'valami_uj'])
def test_temporary(reason):
    assert answer([{'EREDMENY': 'HI', 'UZENET': reason}]).kind == portal.TEMPORARY


@pytest.mark.parametrize('text', ['<html>502</html>', '[]', '["XY", "msg"]', '{}', '[{"EREDMENY": "HI", "UZENET": {"x": 1}}]'])
def test_unexpected_or_card_does_not_crash(text):
    assert portal.parse_answer(text).kind in (portal.UNEXPECTED, portal.CARD_REJECTED)


def test_masked_hides_card_numbers():
    text = 'unexpected 1234567890123456 and 1234 5678 9012 3456 code 007'
    out = portal.parse_answer(text).message
    assert '5678' not in out and '1234567890123456' not in out and '007' not in out


@pytest.mark.parametrize('value, valid', [('1234567890123456', True), ('123456789012345', False),
                                          ('12345678901234ab', False), ('', False)])
def test_card_number_format(value, valid):
    assert portal.is_valid_card_number(value) is valid
