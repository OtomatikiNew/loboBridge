"""Offline lighting schedule (ADR-013).

The cloud publishes a retained 7-day schedule on srlobo/{id}/schedule. We
validate it, save it to /data/schedule.json and only use it once the cloud
has been silent for STALE_AFTER_S. Cloud freshness comes from the heartbeat
and commands, see BridgeMqttClient.on_cloud_signal.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .court_signal import SOURCE_OFFLINE_SCHEDULE, CourtSignalPublisher
from .entity_registry import EntityRegistry
from .ha_client import HomeAssistantClient, HomeAssistantError
from .persistence import DATA_DIR, read_json, write_json_atomic

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 1
SCHEDULE_PATH = f"{DATA_DIR}/schedule.json"
STALE_AFTER_S = 5 * 60
EVALUATE_INTERVAL_S = 15
DIAGNOSTIC_REFRESH_S = 60
MODE_ENTITY_ID = "sensor.lobobridge_mode"

MODE_CLOUD = "cloud"
MODE_OFFLINE_FALLBACK = "offline_fallback"
MODE_SCHEDULE_EXPIRED = "schedule_expired"

# ("on", 75.0) or ("off", None)
CourtState = Tuple[str, Optional[float]]
_OFF: CourtState = ("off", None)


def _utc_now() -> datetime:
    """Returns:
        Current time, timezone-aware UTC.
    """
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Args:
        value: Aware datetime, or None.

    Returns:
        e.g. "2026-08-19T13:30:00Z", or None.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> Optional[datetime]:
    """Parses an ISO-8601 UTC timestamp. Timestamps without a timezone or
    with a non-zero offset are rejected (ADR-013).

    Args:
        value: Raw value from the payload.

    Returns:
        The datetime, or None if invalid.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return parsed


@dataclass
class Interval:
    start: datetime
    end: datetime
    brightness_pct: float


@dataclass
class Schedule:
    generated_at: datetime
    valid_until: datetime
    courts: Dict[int, List[Interval]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: datetime) -> bool:
        """Args:
            now: Current UTC time.

        Returns:
            True if valid_until has passed.
        """
        return self.valid_until <= now

    def desired_state(self, index: int, now: datetime) -> CourtState:
        """What the court should be doing at `now`. Outside all intervals means off.

        Args:
            index: 0-based court index.
            now: Current UTC time.

        Returns:
            ("on", brightness_pct) or ("off", None).
        """
        for interval in self.courts.get(index, []):
            if interval.start <= now < interval.end:
                if interval.brightness_pct > 0:
                    return ("on", interval.brightness_pct)
                return _OFF
        return _OFF


def validate_schedule(
    payload: Any, known_courts: Set[int], now: datetime, allow_expired: bool = False
) -> Optional[Schedule]:
    """Validates a snapshot against the ADR-013 rules. Any error rejects
    the whole snapshot.

    Args:
        payload: Decoded schedule payload.
        known_courts: Court indexes from bootstrap.
        now: Current UTC time.
        allow_expired: Accept a past valid_until (used when loading from disk).

    Returns:
        The schedule, or None if invalid.
    """
    if not isinstance(payload, dict):
        logger.error("Rejected schedule: payload is not a JSON object")
        return None
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        logger.error("Rejected schedule: unsupported schema_version %r", payload.get("schema_version"))
        return None

    generated_at = parse_utc(payload.get("generated_at"))
    valid_until = parse_utc(payload.get("valid_until"))
    if generated_at is None or valid_until is None:
        logger.error("Rejected schedule: generated_at/valid_until must be UTC ISO-8601 timestamps")
        return None
    if not allow_expired and valid_until <= now:
        logger.error("Rejected schedule: valid_until %s is not in the future", payload.get("valid_until"))
        return None

    courts_raw = payload.get("courts")
    if not isinstance(courts_raw, dict):
        logger.error("Rejected schedule: courts must be an object keyed by court index")
        return None

    courts: Dict[int, List[Interval]] = {}
    for index_str, intervals_raw in courts_raw.items():
        try:
            index = int(index_str)
        except (TypeError, ValueError):
            logger.error("Rejected schedule: court key %r is not an index", index_str)
            return None
        if index not in known_courts:
            logger.error("Rejected schedule: court index %s is not known to this installation", index)
            return None
        if not isinstance(intervals_raw, list):
            logger.error("Rejected schedule: court %s intervals must be a list", index)
            return None

        intervals: List[Interval] = []
        for item in intervals_raw:
            interval = _parse_interval(index, item)
            if interval is None:
                return None
            intervals.append(interval)

        intervals.sort(key=lambda i: i.start)
        for previous, current in zip(intervals, intervals[1:]):
            if current.start < previous.end:
                logger.error("Rejected schedule: court %s has overlapping intervals", index)
                return None
        courts[index] = intervals

    return Schedule(generated_at=generated_at, valid_until=valid_until, courts=courts, raw=payload)


def _parse_interval(index: int, item: Any) -> Optional[Interval]:
    """Args:
        index: Court index, for log messages.
        item: Raw interval.

    Returns:
        The interval, or None if invalid.
    """
    if not isinstance(item, dict):
        logger.error("Rejected schedule: court %s has a non-object interval", index)
        return None
    start = parse_utc(item.get("from"))
    end = parse_utc(item.get("to"))
    if start is None or end is None:
        logger.error("Rejected schedule: court %s interval from/to must be UTC ISO-8601", index)
        return None
    if end <= start:
        logger.error("Rejected schedule: court %s has an interval with to <= from", index)
        return None
    brightness = item.get("brightness_pct")
    if isinstance(brightness, bool) or not isinstance(brightness, (int, float)) or not 0 <= brightness <= 100:
        logger.error("Rejected schedule: court %s brightness_pct must be a number from 0 to 100", index)
        return None
    return Interval(start=start, end=end, brightness_pct=float(brightness))


class OfflineScheduler:
    """Keeps the schedule and drives the courts from it while the cloud is silent."""

    def __init__(
        self,
        ha: HomeAssistantClient,
        registry: EntityRegistry,
        installation_id: str,
        signals: CourtSignalPublisher,
        path: str = SCHEDULE_PATH,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Startup counts as hearing from the cloud, so a restart doesn't
        take over the lights straight away.

        Args:
            ha: HA client.
            registry: This installation's courts.
            installation_id: Saved with the schedule file, so a schedule
                restored from another club's backup is ignored.
            signals: Court signals; the schedule drives the courts through
                them, never the lights directly (ADR-026).
            path: Schedule file.
            monotonic: Monotonic clock (tests).
            now: UTC clock (tests).
        """
        self._ha = ha
        self._registry = registry
        self._installation_id = installation_id
        self._signals = signals
        self._path = path
        self._monotonic = monotonic
        self._now = now
        self._lock = threading.Lock()
        self._schedule: Optional[Schedule] = None
        self._last_cloud_signal = monotonic()
        self._last_cloud_signal_utc: Optional[datetime] = None
        self._last_schedule_sync_utc: Optional[datetime] = None
        self._mode: Optional[str] = None
        self._applied: Dict[int, CourtState] = {}
        self._last_diagnostic_write: Optional[float] = None
        self._ha_timezone: Optional[str] = None
        self._stop = threading.Event()

    @property
    def mode(self) -> Optional[str]:
        """Returns:
            Mode from the last evaluation, or None.
        """
        return self._mode

    def load(self) -> None:
        """Loads the saved schedule at startup. Expired ones are loaded too,
        so the sensor can show schedule_expired."""
        stored = read_json(self._path)
        if not isinstance(stored, dict):
            return
        if stored.get("installation_id") != self._installation_id:
            logger.warning("Saved schedule is for another installation, ignoring it")
            return
        schedule = validate_schedule(
            stored.get("schedule"), self._registry.court_indexes(), self._now(), allow_expired=True
        )
        if schedule is None:
            logger.warning("Saved schedule is invalid, ignoring it")
            return
        with self._lock:
            self._schedule = schedule
        logger.info("Loaded saved schedule, valid until %s", _iso(schedule.valid_until))

    def handle_schedule(self, payload: Dict[str, Any]) -> None:
        """Validates and saves a new snapshot. On failure the previous one is kept.

        Args:
            payload: Decoded schedule payload.
        """
        now = self._now()
        schedule = validate_schedule(payload, self._registry.court_indexes(), now)
        if schedule is None:
            logger.error("Invalid schedule, keeping the previous one")
            return
        try:
            write_json_atomic(self._path, {"installation_id": self._installation_id, "schedule": payload})
        except OSError:
            logger.exception("Could not save schedule, keeping the previous one")
            return
        with self._lock:
            self._schedule = schedule
            self._last_schedule_sync_utc = now
        logger.info("Schedule updated: %s courts, valid until %s", len(schedule.courts), _iso(schedule.valid_until))

    def mark_cloud_alive(self) -> None:
        """Called from the MQTT thread on a heartbeat or command."""
        with self._lock:
            self._last_cloud_signal = self._monotonic()
            self._last_cloud_signal_utc = self._now()

    def start(self) -> None:
        """Starts the evaluation thread."""
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        """Stops the evaluation thread."""
        self._stop.set()

    def _loop(self) -> None:
        """Evaluates every EVALUATE_INTERVAL_S until stopped."""
        while not self._stop.is_set():
            try:
                self.evaluate()
            except Exception:
                logger.exception("Offline schedule evaluation failed")
            self._stop.wait(EVALUATE_INTERVAL_S)

    def evaluate(self) -> str:
        """Picks the mode, drives the courts if the cloud is silent and
        updates the sensor.

        Returns:
            cloud, offline_fallback or schedule_expired.
        """
        now = self._now()
        with self._lock:
            stale = self._monotonic() - self._last_cloud_signal > STALE_AFTER_S
            schedule = self._schedule

        if not stale:
            mode = MODE_CLOUD
        elif schedule is not None and not schedule.is_expired(now):
            mode = MODE_OFFLINE_FALLBACK
        else:
            mode = MODE_SCHEDULE_EXPIRED

        mode_changed = mode != self._mode
        if mode_changed:
            logger.warning("Lighting mode changed: %s -> %s", self._mode, mode)
            self._mode = mode

        if mode == MODE_CLOUD:
            # The cloud may change the lights from here on, so the next
            # fallback has to set every court again.
            self._applied.clear()
        elif mode == MODE_OFFLINE_FALLBACK and schedule is not None:
            for index in schedule.courts:
                self._apply(index, schedule.desired_state(index, now))
        elif schedule is not None:
            # Expired schedule: turn off the courts it covered (ADR-013).
            # Courts not in the schedule (MANUAL) are left alone.
            for index in schedule.courts:
                self._apply(index, _OFF)

        self._refresh_diagnostic(mode, schedule, force=mode_changed)
        return mode

    def _apply(self, index: int, desired: CourtState) -> None:
        """Sets a court's signal (binary_sensor.pista_{n}), but only when
        `desired` differs from what we last set, so a local change isn't
        overwritten every cycle. The local automation drives the lights.

        Args:
            index: 0-based court index.
            desired: State to set.
        """
        if self._applied.get(index) == desired:
            return
        state, brightness_pct = desired
        try:
            self._signals.apply(index, state, brightness_pct, SOURCE_OFFLINE_SCHEDULE)
        except HomeAssistantError:
            logger.exception("Could not set court %s signal to %s", index, desired)
            return  # retried next evaluation
        self._applied[index] = desired
        logger.info("Offline schedule: court %s (%s) -> %s", index, self._signals.entity_id(index), desired)

    def _refresh_diagnostic(self, mode: str, schedule: Optional[Schedule], force: bool) -> None:
        """Writes sensor.lobobridge_mode on mode changes and every
        DIAGNOSTIC_REFRESH_S. REST-created states are lost on a Core restart,
        the periodic write brings it back.

        Args:
            mode: Current mode.
            schedule: Current schedule, if any.
            force: Write even if the interval hasn't passed.
        """
        now_mono = self._monotonic()
        if (
            not force
            and self._last_diagnostic_write is not None
            and now_mono - self._last_diagnostic_write < DIAGNOSTIC_REFRESH_S
        ):
            return
        with self._lock:
            last_cloud = self._last_cloud_signal_utc
            last_sync = self._last_schedule_sync_utc
        attributes = {
            "friendly_name": "SR.Lobo Bridge mode",
            "icon": "mdi:cloud-check" if mode == MODE_CLOUD else "mdi:cloud-off-outline",
            "last_cloud_message_utc": _iso(last_cloud),
            "last_schedule_sync_utc": _iso(last_sync),
            "schedule_valid_until_utc": _iso(schedule.valid_until) if schedule else None,
            "ha_timezone": self._get_ha_timezone(),
        }
        try:
            self._ha.set_state(MODE_ENTITY_ID, mode, attributes)
            self._last_diagnostic_write = now_mono
        except Exception:
            logger.warning("Could not update %s", MODE_ENTITY_ID, exc_info=True)

    def _get_ha_timezone(self) -> Optional[str]:
        """HA's timezone, for the sensor attributes only. Cached after the first read.

        Returns:
            The timezone name, or None.
        """
        if self._ha_timezone is None:
            try:
                self._ha_timezone = self._ha.get_core_info().get("time_zone")
            except Exception:
                logger.debug("Could not read HA timezone", exc_info=True)
        return self._ha_timezone
