"""Polling, backoff and persistence for one SZÉP Kártya."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import async_get as async_get_restore_state
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import portal
from .const import (
    BACKOFF_MAX,
    BACKOFF_START,
    CONF_CARD_CODE,
    CONF_CARD_NUMBER,
    CONF_SCAN_HOURS,
    DEFAULT_SCAN_HOURS,
    DOMAIN,
    GLOBAL_QUERY_GAP_SECONDS,
    MIN_QUERY_GAP,
    POCKET_ACCOMMODATION,
    POCKET_ACTIVE_HUNGARIANS,
    REJECTION_LIMIT,
    STALE_AFTER,
    STORE_VERSION,
    TOLERATED_REASONS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CardState:
    """Everything that has to survive a restart for one card."""

    balances: dict[str, int] = field(default_factory=dict)
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    last_error: str | None = None
    not_before: datetime | None = None
    backoff_hours: float = BACKOFF_START.total_seconds() / 3600
    rejections: int = 0
    polling_stopped: bool = False

    @property
    def stale(self) -> bool:
        return self.last_success is None or dt_util.utcnow() - self.last_success > STALE_AFTER

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ('last_success', 'last_attempt', 'not_before'):
            data[key] = data[key].isoformat() if data[key] else None
        return data

    @classmethod
    def from_dict(cls, data: dict, now: datetime) -> CardState:
        def when(key, allow_future=False):
            value = dt_util.parse_datetime(str(data.get(key) or ''))
            if value is None or value.tzinfo is None:
                return None
            # A time in the future (clock moved back) would block every query.
            if not allow_future and value > now:
                return None
            return value

        def number(key, default, kind):
            try:
                return max(0, kind(data.get(key, default)))
            except (TypeError, ValueError):
                return default

        balances = {}
        for key, value in (data.get('balances') or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                balances[key] = value
        not_before = when('not_before', allow_future=True)
        if not_before is not None:
            not_before = min(not_before, now + BACKOFF_MAX)
        return cls(
            balances=balances,
            last_success=when('last_success'),
            last_attempt=when('last_attempt'),
            last_error=data.get('last_error') if isinstance(data.get('last_error'), str) else None,
            not_before=not_before,
            backoff_hours=number('backoff_hours', BACKOFF_START.total_seconds() / 3600, float),
            rejections=number('rejections', 0, int),
            polling_stopped=bool(data.get('polling_stopped', False)),
        )


class PortalGate:
    """Serialises portal queries of all cards and shares captcha backoffs."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_query: float | None = None
        self.not_before: datetime | None = None

    async def async_wait_turn(self) -> None:
        if self.last_query is not None:
            wait = GLOBAL_QUERY_GAP_SECONDS - (time.monotonic() - self.last_query)
            if wait > 0:
                await asyncio.sleep(wait)

    def mark_query(self) -> None:
        self.last_query = time.monotonic()


def get_gate(hass: HomeAssistant) -> PortalGate:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if 'gate' not in domain_data:
        domain_data['gate'] = PortalGate()
    return domain_data['gate']


def seed_state(hass: HomeAssistant, card_number: str, state: CardState) -> None:
    """Hand the result of a config flow's test query to the new entry."""
    hass.data.setdefault(DOMAIN, {}).setdefault('seeds', {})[card_number] = state


async def async_query_once(hass: HomeAssistant, card_number: str, card_code: str) -> portal.Answer | None:
    """One gated query for a config flow. None while a captcha backoff runs."""
    gate = get_gate(hass)
    async with gate.lock:
        if gate.not_before and dt_util.utcnow() < gate.not_before:
            return None
        await gate.async_wait_turn()
        try:
            return await hass.async_add_executor_job(portal.query_balance, card_number, card_code)
        finally:
            gate.mark_query()


def _store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f'{DOMAIN}.{entry_id}')


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    await _store(hass, entry_id).async_remove()


def card_id(card_number: str) -> str:
    # The last 4 digits are what cards show publicly. Unique IDs of 1.2.x used
    # the same, so entities imported from YAML keep their entity IDs.
    return card_number[-4:]


class SzepKartyaCoordinator(DataUpdateCoordinator[CardState]):
    """Queries one card, gently, and remembers what happened."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        hours = entry.options.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f'{DOMAIN} {entry.title}',
            update_interval=timedelta(hours=hours),
        )
        self._card_number: str = entry.data[CONF_CARD_NUMBER]
        self._card_code: str = entry.data[CONF_CARD_CODE]
        self._store = _store(hass, entry.entry_id)
        self.card_id = card_id(self._card_number)

    async def async_load(self) -> None:
        now = dt_util.utcnow()
        seeds = self.hass.data.get(DOMAIN, {}).get('seeds', {})
        seeded = seeds.pop(self._card_number, None)
        stored = await self._store.async_load()
        if seeded is not None:
            state = seeded
        elif stored is not None:
            state = CardState.from_dict(stored, now)
        else:
            state = self._state_from_yaml_entities(now)
        self.data = state
        await self._async_save(state)

    def _state_from_yaml_entities(self, now: datetime) -> CardState:
        """Take over the balance and query history of the 1.2.x YAML sensors."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id('sensor', DOMAIN, f'{self.card_id}_szallashely')
        if entity_id is None:
            return CardState()
        restored = async_get_restore_state(self.hass).last_states.get(entity_id)
        if restored is None:
            return CardState()
        attributes = dict(restored.state.attributes)
        state = CardState.from_dict(attributes, now)
        # 1.2.x stopped until restart; here a stop waits for new credentials,
        # so do not carry it over.
        state.polling_stopped = False
        try:
            state.balances[POCKET_ACCOMMODATION] = int(float(restored.state.state))
        except (TypeError, ValueError):
            pass
        active_id = registry.async_get_entity_id('sensor', DOMAIN, f'{self.card_id}_aktiv_magyarok')
        active = async_get_restore_state(self.hass).last_states.get(active_id) if active_id else None
        if active is not None:
            try:
                state.balances[POCKET_ACTIVE_HUNGARIANS] = int(float(active.state.state))
            except (TypeError, ValueError):
                pass
        _LOGGER.info('Took over the balance and query history of %s', entity_id)
        return state

    async def _async_save(self, state: CardState) -> None:
        await self._store.async_save(state.to_dict())

    async def _async_update_data(self) -> CardState:
        state = replace(self.data or CardState(), balances=dict((self.data or CardState()).balances))
        now = dt_util.utcnow()
        if state.polling_stopped:
            return state
        if state.last_attempt and now - state.last_attempt < MIN_QUERY_GAP:
            _LOGGER.debug('%s: skipping query, last one at %s', self.config_entry.title, state.last_attempt)
            return state
        # Polling rounds arrive at about the backoff length; do not skip a round
        # over a few seconds of scheduling jitter.
        if state.not_before and now + MIN_QUERY_GAP < state.not_before:
            _LOGGER.debug('%s: skipping query until %s', self.config_entry.title, state.not_before)
            return state

        gate = get_gate(self.hass)
        async with gate.lock:
            now = dt_util.utcnow()
            if gate.not_before and now + MIN_QUERY_GAP < gate.not_before:
                # Another card got a captcha: the portal is likely limiting us all.
                state.not_before = gate.not_before
                _LOGGER.debug('%s: skipping query until %s (portal backoff)', self.config_entry.title,
                              gate.not_before)
                await self._async_save(state)
                return state
            await gate.async_wait_turn()
            now = dt_util.utcnow()
            state.not_before = None
            state.last_attempt = now
            try:
                answer = await self.hass.async_add_executor_job(
                    portal.query_balance, self._card_number, self._card_code)
            except Exception as err:  # noqa: BLE001 - any failure keeps the last balance
                self._fail(state, f'Balance update failed: {err!r}')
            else:
                self._apply(state, answer, now, gate)
            finally:
                gate.mark_query()

        await self._async_save(state)
        return state

    def _fail(self, state: CardState, message: str) -> None:
        state.last_error = message
        _LOGGER.error('%s: %s', self.config_entry.title, message)

    def _back_off(self, state: CardState, now: datetime) -> None:
        state.not_before = now + timedelta(hours=state.backoff_hours)
        state.backoff_hours = min(state.backoff_hours * 2, BACKOFF_MAX.total_seconds() / 3600)

    def _apply(self, state: CardState, answer: portal.Answer, now: datetime, gate: PortalGate) -> None:
        if answer.kind == portal.OK:
            state.balances.update(answer.balances)
            state.last_success = now
            state.last_error = answer.message
            if answer.message:
                _LOGGER.warning('%s: %s', self.config_entry.title, answer.message)
            state.backoff_hours = BACKOFF_START.total_seconds() / 3600
            state.rejections = 0
            gate.not_before = None
            return

        if answer.kind == portal.CAPTCHA:
            self._back_off(state, now)
            gate.not_before = max(filter(None, (gate.not_before, state.not_before)))
            self._fail(state, f'{answer.message}; next query not before {state.not_before.isoformat()}')
            return

        if answer.kind == portal.CARD_REJECTED:
            state.rejections += 1
            if (answer.reason in TOLERATED_REASONS and state.last_success is not None
                    and state.rejections < REJECTION_LIMIT):
                self._back_off(state, now)
                self._fail(state, f'{answer.message}, {state.rejections} of {REJECTION_LIMIT} in a row; '
                                  f'the card worked before, so this may be temporary. '
                                  f'Next query not before {state.not_before.isoformat()}')
                return
            state.polling_stopped = True
            self._fail(state, f'{answer.message}; polling stopped until the card code is entered again')
            self.config_entry.async_start_reauth(self.hass)
            return

        self._fail(state, answer.message or 'Unknown portal answer')
