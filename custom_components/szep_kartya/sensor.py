import json
import logging
import re
import threading
import time
from datetime import timedelta

import requests
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    RestoreSensor,
    SensorDeviceClass,
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
REQUEST_DEADLINE = 60
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Minimum gap between two queries. Both sensors of a polling round share one
# query, and a burst of Home Assistant restarts sends only one (the last attempt
# is restored). Frequent queries make the portal answer with captchas and even
# with card errors for a working card.
MIN_QUERY_GAP = timedelta(minutes=15)
# After a captcha or a card error the next query waits at least this long,
# doubling up to a day.
CAPTCHA_BACKOFF_START = timedelta(hours=8)
CAPTCHA_BACKOFF_MAX = timedelta(hours=24)
# No successful query for this long sets the 'stale' attribute.
STALE_AFTER = timedelta(hours=48)

# 'HI' reasons that blame the card. Retrying a wrong card code risks locking the
# card, but the portal has also answered nincs_kartya for a working card right
# after a burst of queries. So polling stops at once only for a card that never
# had a successful query; otherwise after REJECTION_LIMIT answers in a row.
CARD_REASONS = {
    'hibas_kartyaszam_vagy_telekod',
    'letiltott_inaktiv_kartya',
    'nincs_kartya',
    'virtualis_kartya',
}
CAPTCHA_REASONS = {'hibas_recaptcha'}
REJECTION_LIMIT = 3

# Balance fields of the quick balance response, as labelled by the portal's
# own balance page (data-data attributes).
POCKET_ACCOMMODATION = 'szamla_osszeg9'  # Szálláshely zseb
POCKET_ACTIVE_HUNGARIANS = 'szamla_osszeg8'  # Aktív Magyarok zseb

ISSUE_CARD_REJECTED = 'card_rejected'
ISSUE_INVALID_CONFIG = 'invalid_config'


def _as_text(value):
    # Never fails: Home Assistant appends the offending value to every schema
    # error it logs, which would put the card number or card code into the log.
    # The format is checked in async_setup_platform instead.
    return '' if value is None else str(value).strip()


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CARD_NUMBER): _as_text,
        vol.Required(CONF_CARD_CODE): _as_text,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    card_number = config[CONF_CARD_NUMBER]
    card_code = config[CONF_CARD_CODE]
    name = config[CONF_NAME]

    # One issue per YAML entry, so a valid entry does not clear another's issue.
    issue_id = f'{ISSUE_INVALID_CONFIG}_{name}'
    problems = []
    if not (card_number.isdigit() and len(card_number) == 16):
        problems.append('card_number must be exactly 16 digits')
    if not (card_code.isdigit() and len(card_code) == 3):
        problems.append('card_code must be exactly 3 digits')
    if problems:
        _LOGGER.error('Invalid configuration: %s. Quote the values in secrets.yaml '
                      'so leading zeroes are kept. (Values not shown.)', '; '.join(problems))
        ir.async_create_issue(
            hass, DOMAIN, issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INVALID_CONFIG,
            translation_placeholders={'problems': '; '.join(problems)},
        )
        return
    ir.async_delete_issue(hass, DOMAIN, issue_id)

    # The last 4 digits are what cards show publicly. A hash of the whole
    # number could be brute forced from the entity registry.
    card_id = card_number[-4:]
    client = SzepKartyaClient(hass, card_number, card_code, card_id)
    sensors = [
        SzepKartyaSensor(client, name, f'{card_id}_szallashely', POCKET_ACCOMMODATION, primary=True),
        SzepKartyaSensor(client, f'{name} Aktív Magyarok', f'{card_id}_aktiv_magyarok', POCKET_ACTIVE_HUNGARIANS),
    ]

    # No update before adding: a failed fetch used to abort platform setup, and
    # the entities restore their last state first (see async_added_to_hass).
    async_add_entities(sensors)


def parse_balance(input_string) -> int:
    if not isinstance(input_string, str):
        raise ValueError(f'balance is not a string: {type(input_string).__name__}')
    text = input_string.strip()
    if text == '':
        return 0
    return int(text.replace('+', ''))


def _iso(value):
    return value.isoformat() if value else None


def _masked(text: str) -> str:
    """Start of a response body for the log, with numbers hidden."""
    return re.sub(r'\d(?:[\d \-]*\d){2,}', '<num>', text[:200])


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
        self.not_before = None
        self._backoff = CAPTCHA_BACKOFF_START
        self._rejections = 0

    def refresh(self):
        with self._lock:
            now = dt_util.utcnow()
            if self.stopped:
                return
            if self.last_attempt and now - self.last_attempt < MIN_QUERY_GAP:
                return
            # Polling rounds arrive at about the backoff length; do not skip a
            # round over a few seconds of scheduling jitter.
            if self.not_before and now + MIN_QUERY_GAP < self.not_before:
                _LOGGER.debug('Skipping balance query until %s (captcha backoff)', self.not_before)
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
        deadline = time.monotonic() + REQUEST_DEADLINE
        with requests.Session() as session:
            with session.get(URL_HTML, timeout=REQUEST_TIMEOUT,
                             allow_redirects=False, stream=True) as response:
                html = self._get_text(response, deadline)
            match = re.search(r"ajax_token = '([a-z0-9]{64})'", html)
            if not match:
                raise ValueError("Can't find ajax_token on the portal page")

            data = {
                's_azonosito_k': self._card_number,
                's_telekod_k': self._card_code,
                'ajax_token': match.group(1),
                's_captcha': '',
            }
            with session.post(URL_API, data=data, timeout=REQUEST_TIMEOUT,
                              allow_redirects=False, stream=True) as response:
                status = response.status_code
                text = self._get_text(response, deadline)

        try:
            payload = json.loads(text)
        except ValueError:
            self._fail(f'Unexpected balance response (HTTP {status}): {_masked(text)}')
            return
        self._handle(payload, status, text, now)

    @staticmethod
    def _get_text(response, deadline) -> str:
        if response.status_code != 200:
            raise ValueError(f'HTTP {response.status_code} from {response.url}')
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError('Portal response too large')
            if time.monotonic() > deadline:
                raise ValueError('Portal response too slow')
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
            if POCKET_ACCOMMODATION not in message:
                # A missing field must not read as a zero balance.
                self._fail(f'Balance response without {POCKET_ACCOMMODATION}: '
                           f'fields {sorted(message)}')
                return
            balances = {POCKET_ACCOMMODATION: parse_balance(message[POCKET_ACCOMMODATION])}
            self.last_error = None
            if POCKET_ACTIVE_HUNGARIANS in message:
                balances[POCKET_ACTIVE_HUNGARIANS] = parse_balance(message[POCKET_ACTIVE_HUNGARIANS])
            else:
                # Keep the last value, but do not let it look current.
                self.last_error = f'Balance response without {POCKET_ACTIVE_HUNGARIANS}'
                _LOGGER.warning(self.last_error)
            self.balances = balances
            self.last_success = now
            self.not_before = None
            self._backoff = CAPTCHA_BACKOFF_START
            self._rejections = 0
            ir.delete_issue(self.hass, DOMAIN, self._issue_id)
            return

        if result == 'RC' or reason in CAPTCHA_REASONS:
            self._back_off(now)
            self._fail(f'Captcha protection kicked in (too many requests); '
                       f'next query not before {self.not_before.isoformat()}')
            return

        if result == 'HI' and (reason is None or reason in CARD_REASONS):
            reason = reason or 'wrong card number or card code'
            self._rejections += 1
            if self.last_success is not None and self._rejections < REJECTION_LIMIT:
                self._back_off(now)
                self._fail(f'The portal rejected the card ({reason}), {self._rejections} of '
                           f'{REJECTION_LIMIT} in a row; the card worked before, so this may be '
                           f'temporary. Next query not before {self.not_before.isoformat()}')
                return
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

    def _back_off(self, now):
        self.not_before = now + self._backoff
        self._backoff = min(self._backoff * 2, CAPTCHA_BACKOFF_MAX)


class SzepKartyaSensor(RestoreSensor):
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
        self._attr_native_value = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Restore before the first query: the last balance, so automations
        # comparing old and new states do not see unknown -> value, and the
        # query history, so a restart neither forgets that the card worked nor
        # skips a captcha backoff.
        if self._attr_native_value is None:
            last = await self.async_get_last_sensor_data()
            if last is not None and last.native_value is not None:
                try:
                    self._attr_native_value = int(last.native_value)
                except (TypeError, ValueError):
                    pass
        last_state = await self.async_get_last_state()
        if last_state is not None:
            client = self._client
            for attribute in ('last_success', 'last_attempt', 'not_before'):
                if getattr(client, attribute) is None:
                    restored = dt_util.parse_datetime(str(last_state.attributes.get(attribute) or ''))
                    if restored is not None:
                        setattr(client, attribute, restored)
        self._update_attributes()
        self.async_schedule_update_ha_state(True)

    def update(self):
        # Never raise: an exception in the first update makes HA drop the entity.
        try:
            self._client.refresh()
        except Exception as err:
            _LOGGER.error('Balance update failed: %r', err)

        value = self._client.balances.get(self._pocket)
        if value is not None:
            self._attr_native_value = value
        self._update_attributes()

    def _update_attributes(self):
        client = self._client
        # A failed query keeps the last balance: going unavailable and back would
        # read as spending and top-up to automations that compare states.
        self._attr_available = not (client.stopped and self._attr_native_value is None)
        stale = client.last_success is None or dt_util.utcnow() - client.last_success > STALE_AFTER
        attributes = {
            'last_success': _iso(client.last_success),
            'last_attempt': _iso(client.last_attempt),
            'last_error': client.last_error,
            'not_before': _iso(client.not_before),
            'polling_stopped': client.stopped,
            'stale': stale,
        }
        if self._primary:
            attributes['Egyenleg'] = f'{self._attr_native_value} {DEFAULT_UNIT}'
        self._attr_extra_state_attributes = attributes
