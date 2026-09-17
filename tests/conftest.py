"""Fixtures for SZÉP Kártya tests."""

from unittest.mock import patch

import pytest

from custom_components.szep_kartya import portal

CARD = '1234567890123456'
CARD2 = '6543210987654321'
CODE = '007'


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_global_gap():
    with patch('custom_components.szep_kartya.coordinator.GLOBAL_QUERY_GAP_SECONDS', 0):
        yield


def ok(accommodation=51572, active=0):
    return portal.Answer(portal.OK, balances={'szamla_osszeg9': accommodation, 'szamla_osszeg8': active})


def rejected(reason='hibas_kartyaszam_vagy_telekod'):
    return portal.Answer(portal.CARD_REJECTED, reason=reason, message=f'The portal rejected the card ({reason})')


def captcha():
    return portal.Answer(portal.CAPTCHA, message='Captcha protection kicked in (too many requests)')


@pytest.fixture
def portal_mock():
    with patch('custom_components.szep_kartya.portal.query_balance', return_value=ok()) as mock:
        yield mock
