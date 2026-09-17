import hashlib
import json
import logging
import re
import threading
from datetime import timedelta

import requests
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'szep_kartya'

CONF_CARD_NUMBER = 'card_number'
CONF_CARD_CODE = 'card_code'

DEFAULT_NAME = 'SZÉP Kártya'
DEFAULT_UNIT = 'Ft'
DEFAULT_ICON = 'mdi:credit-card-outline'

# The portal asks for a captcha when polled often. Used when the YAML config
# has no scan_interval; the sensor platform default would be 30 seconds.
SCAN_INTERVAL = timedelta(hours=4)

URL_HTML = 'https://magan.szepkartya.otpportalok.hu/fooldal/'
URL_API = 'https://magan.szepkartya.otpportalok.hu/ajax/gyorsegyenleg/'
REQUEST_TIMEOUT = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Both sensors update in the same polling round; they share one query.
SHARED_RESULT_MAX_AGE = timedelta(minutes=5)
# After a captcha the next query waits at least this long, doubling up to a day.
CAPTCHA_BACKOFF_START = timedelta(hours=8)
CAPTCHA_BACKOFF_MAX = timedelta(hours=24)
# Keep the last balance through short outages; only go unavailable after this.
STALE_AFTER = timedelta(hours=48)

# 'HI' reasons that will not fix themselves. Retrying a rejected card code
# risks locking the card, so polling stops until Home Assistant restarts.
PERMANENT_REASONS = {
    'hibas_kartyaszam_vagy_telekod',
    'letiltott_inaktiv_kartya',
    'nincs_kartya',
    'virtualis_kartya',
}
CAPTCHA_REASONS = {'hibas_recaptcha'}

# Balance fields of the quick balance response, as labelled by the portal's
# own balance page (data-data attributes).
POCKET_ACCOMMODATION = 'szamla_osszeg9'  # Szálláshely zseb
POCKET_ACTIVE_HUNGARIANS = 'szamla_osszeg8'  # Aktív Magyarok zseb

ISSUE_CARD_REJECTED = 'card_rejected'


def _digits(length):
    """Validate a digit string without echoing the value.

    Home Assistant logs the offending value of a failed schema check, which
    would put the card number or card code into the log.
    """
    def validate(value):
        text = str(value).strip()
        if not (text.isdigit() and len(text) == length):
            raise vol.Invalid(f'must be exactly {length} digits, quoted as a string (value not shown)')
        return text
    return validate


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CARD_NUMBER): _digits(16),
        vol.Required(CONF_CARD_CODE): _digits(3),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    card_number = config[CONF_CARD_NUMBER]
    name = config[CONF_NAME]
    card_id = hashlib.sha256(card_number.encode()).hexdigest()[:12]

    client = SzepKartyaClient(hass, card_number, config[CONF_CARD_CODE], card_id)
    sensors = [
        SzepKartyaSensor(client, name, f'{card_id}_szallashely', POCKET_ACCOMMODATION, primary=True),
        SzepKartyaSensor(client, f'{name} Aktív Magyarok', f'{card_id}_aktiv_magyarok', POCKET_ACTIVE_HUNGARIANS),
    ]

    # Add the entities first and let HA run the first update. A failed fetch
    # used to abort platform setup, and HA never retries that.
    async_add_entities(sensors, True)


def parse_balance(input_string) -> int:
    text = str(input_string).strip()
    if text == '':
        return 0
    return int(text.replace('+', ''))


def _masked(text: str) -> str:
    """Start of a response body for the log, with long digit runs hidden."""
    return re.sub(r'\d{6,}', '<num>', text[:200])


class SzepKartyaClient:
    """One quick balance query per polling round, shared by the sensors."""

    def __init__(self, hass, card_number: str, card_code: str, card_id: str):
        self.hass = hass
        self._card_number = card_number
        self._card_code = card_code
        self._issue_id = f'{ISSUE_CARD_REJECTED}_{card_id}'
        self._lock = threading.Lock()

        self.balances: dict = {}
        self.last_attempt = None
        self.last_success = None
        self.last_error = None
        self.stopped = False
        self._not_before = None
        self._backoff = CAPTCHA_BACKOFF_START

    def refresh(self):
        with self._lock:
            now = dt_util.utcnow()
            if self.stopped:
                return
            if self.last_attempt and now - self.last_attempt < SHARED_RESULT_MAX_AGE:
                return
            if self._not_before and now < self._not_before:
                _LOGGER.debug('Skipping balance query until %s (captcha backoff)', self._not_before)
                return
            self.last_attempt = now
            try:
                self._query(now)
            except Exception as err:
                self._fail(f'Balance update failed: {err!r}')

    def _fail(self, message: str):
        self.last_error = message
        _LOGGER.error(message)

    def _query(self, now):
        with requests.Session() as session:
            html = self._get_text(session.get(URL_HTML, timeout=REQUEST_TIMEOUT,
                                              allow_redirects=False, stream=True))
            match = re.search(r"ajax_token = '([a-z0-9]{64})'", html)
            if not match:
                raise ValueError("Can't find ajax_token on the portal page")

            data = {
                's_azonosito_k': self._card_number,
                's_telekod_k': self._card_code,
                'ajax_token': match.group(1),
                's_captcha': '',
            }
            response = session.post(URL_API, data=data, timeout=REQUEST_TIMEOUT,
                                    allow_redirects=False, stream=True)
            text = self._get_text(response)

        try:
            payload = json.loads(text)
        except ValueError:
            self._fail(f'Unexpected balance response (HTTP {response.status_code}): {_masked(text)}')
            return
        self._handle(payload, response.status_code, text, now)

    @staticmethod
    def _get_text(response) -> str:
        if response.status_code != 200:
            raise ValueError(f'HTTP {response.status_code} from {response.url}')
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError('Portal response too large')
        return body.decode(response.encoding or 'utf-8', errors='replace')

    def _handle(self, payload, status, text, now):
        first = payload[0] if isinstance(payload, list) and payload else payload

        # The portal answers [{"EREDMENY": "OK"|"HI"|"RC", "UZENET": ...}].
        # Older answers were bare codes such as ["HI"]; keep understanding them.
        if isinstance(first, dict):
            result, message = first.get('EREDMENY'), first.get('UZENET')
        elif first in ('OK', 'HI', 'RC'):
            result, message = first, None
        else:
            result, message = None, None
        reason = message if isinstance(message, str) else None

        if result == 'OK' and isinstance(message, dict):
            self.balances = {
                key: parse_balance(message.get(key, ''))
                for key in (POCKET_ACCOMMODATION, POCKET_ACTIVE_HUNGARIANS)
            }
            self.last_success = now
            self.last_error = None
            self._not_before = None
            self._backoff = CAPTCHA_BACKOFF_START
            ir.delete_issue(self.hass, DOMAIN, self._issue_id)
            return

        if result == 'RC' or reason in CAPTCHA_REASONS:
            self._not_before = now + self._backoff
            self._fail(f'Captcha protection kicked in (too many requests); '
                       f'next query not before {self._not_before.isoformat()}')
            self._backoff = min(self._backoff * 2, CAPTCHA_BACKOFF_MAX)
            return

        if result == 'HI' and (reason is None or reason in PERMANENT_REASONS):
            reason = reason or 'wrong card number or card code'
            self.stopped = True
            self._fail(f'The portal rejected the card ({reason}); '
                       f'polling stopped until Home Assistant restarts')
            ir.create_issue(
                self.hass, DOMAIN, self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_CARD_REJECTED,
                translation_placeholders={'reason': reason},
            )
            return

        if result == 'HI':
            # Temporary portal side problems, e.g. otpdirekt_nem_elerheto.
            self._fail(f'The portal could not answer the balance query ({reason})')
            return

        self._fail(f'Unexpected balance response (HTTP {status}): {_masked(text)}')


class SzepKartyaSensor(SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = DEFAULT_UNIT
    _attr_suggested_display_precision = 0
    _attr_icon = DEFAULT_ICON

    def __init__(self, client: SzepKartyaClient, name: str, unique_id: str, pocket: str,
                 primary: bool = False):
        self._client = client
        self._pocket = pocket
        self._primary = primary
        self._attr_name = name
        self._attr_unique_id = unique_id

    def update(self):
        # Never raise: an exception in the first update makes HA drop the entity.
        try:
            self._client.refresh()
        except Exception as err:
            _LOGGER.error('Balance update failed: %r', err)

        client = self._client
        self._attr_native_value = client.balances.get(self._pocket)
        # Short outages keep the last balance, so automations that compare
        # old and new states do not see a fake drop to unavailable and back.
        self._attr_available = not (
            client.last_success is None and client.stopped
        ) and not (
            client.last_success is not None
            and dt_util.utcnow() - client.last_success > STALE_AFTER
        )

        attributes = {
            'last_success': client.last_success.isoformat() if client.last_success else None,
            'last_error': client.last_error,
        }
        if self._primary:
            attributes['Egyenleg'] = f'{self._attr_native_value} {DEFAULT_UNIT}'
        self._attr_extra_state_attributes = attributes
