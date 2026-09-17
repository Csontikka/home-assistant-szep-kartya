"""Blocking client for the OTP SZÉP Kártya quick balance query."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import requests

from .const import (
    CAPTCHA_REASONS,
    CARD_REASONS,
    POCKET_ACCOMMODATION,
    POCKET_ACTIVE_HUNGARIANS,
)

URL_HTML = 'https://magan.szepkartya.otpportalok.hu/fooldal/'
URL_API = 'https://magan.szepkartya.otpportalok.hu/ajax/gyorsegyenleg/'
REQUEST_TIMEOUT = 30
REQUEST_DEADLINE = 60
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

OK = 'ok'
CAPTCHA = 'captcha'
CARD_REJECTED = 'card_rejected'
TEMPORARY = 'temporary'
UNEXPECTED = 'unexpected'


@dataclass
class Answer:
    """What the portal said, reduced to what the integration acts on."""

    kind: str
    reason: str | None = None
    balances: dict[str, int] = field(default_factory=dict)
    # English, safe for the log: never contains card data.
    message: str | None = None


def is_valid_card_number(value: str) -> bool:
    return value.isdigit() and len(value) == 16


def is_valid_card_code(value: str) -> bool:
    return value.isdigit() and len(value) == 3


def parse_balance(value) -> int:
    if not isinstance(value, str):
        raise ValueError(f'balance is not a string: {type(value).__name__}')
    text = value.strip()
    if text == '':
        return 0
    return int(text.replace('+', ''))


def masked(text: str) -> str:
    """Start of a response body for the log, with numbers hidden."""
    return re.sub(r'\d(?:[\d \-]*\d){2,}', '<num>', text[:200])


def query_balance(card_number: str, card_code: str) -> Answer:
    """Run one quick balance query. Raises on network and HTTP errors."""
    deadline = time.monotonic() + REQUEST_DEADLINE
    with requests.Session() as session:
        with session.get(URL_HTML, timeout=REQUEST_TIMEOUT,
                         allow_redirects=False, stream=True) as response:
            html = _read_text(response, deadline)
        match = re.search(r"ajax_token = '([a-z0-9]{64})'", html)
        if not match:
            raise ValueError("Can't find ajax_token on the portal page")

        data = {
            's_azonosito_k': card_number,
            's_telekod_k': card_code,
            'ajax_token': match.group(1),
            's_captcha': '',
        }
        with session.post(URL_API, data=data, timeout=REQUEST_TIMEOUT,
                          allow_redirects=False, stream=True) as response:
            status = response.status_code
            text = _read_text(response, deadline)
    return parse_answer(text, status)


def _read_text(response, deadline) -> str:
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


def parse_answer(text: str, status: int = 200) -> Answer:
    unexpected = Answer(UNEXPECTED, message=f'Unexpected balance response (HTTP {status}): {masked(text)}')
    try:
        payload = json.loads(text)
    except ValueError:
        return unexpected

    first = payload[0] if isinstance(payload, list) and payload else payload
    # The portal answers [{"EREDMENY": "OK"|"HI"|"RC", "UZENET": ...}].
    # Older answers were bare codes such as ["HI"]; keep understanding them.
    if isinstance(first, dict):
        result, message = first.get('EREDMENY'), first.get('UZENET')
    elif first in ('OK', 'HI', 'RC'):
        result, message = first, None
    else:
        return unexpected
    reason = message if isinstance(message, str) else None

    if result == 'OK' and isinstance(message, dict):
        if POCKET_ACCOMMODATION not in message:
            # A missing field must not read as a zero balance.
            return Answer(UNEXPECTED, message=f'Balance response without {POCKET_ACCOMMODATION}: '
                                              f'fields {sorted(message)}')
        try:
            balances = {POCKET_ACCOMMODATION: parse_balance(message[POCKET_ACCOMMODATION])}
            if POCKET_ACTIVE_HUNGARIANS in message:
                balances[POCKET_ACTIVE_HUNGARIANS] = parse_balance(message[POCKET_ACTIVE_HUNGARIANS])
        except ValueError as err:
            return Answer(UNEXPECTED, message=f'Unreadable balance: {err}')
        missing = None if POCKET_ACTIVE_HUNGARIANS in balances else \
            f'Balance response without {POCKET_ACTIVE_HUNGARIANS}'
        return Answer(OK, balances=balances, message=missing)

    if result == 'RC' or reason in CAPTCHA_REASONS:
        return Answer(CAPTCHA, reason=reason, message='Captcha protection kicked in (too many requests)')

    if result == 'HI' and (reason is None or reason in CARD_REASONS):
        reason = reason or 'hibas_kartyaszam_vagy_telekod'
        return Answer(CARD_REJECTED, reason=reason, message=f'The portal rejected the card ({reason})')

    if result == 'HI':
        # Temporary portal side problems, e.g. otpdirekt_nem_elerheto.
        return Answer(TEMPORARY, reason=reason,
                      message=f'The portal could not answer the balance query ({reason})')
    return unexpected
