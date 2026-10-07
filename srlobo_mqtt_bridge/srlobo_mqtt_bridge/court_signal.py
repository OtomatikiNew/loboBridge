"""Court light signal (ADR-026): the cloud's desired state for each court.

The bridge never drives the court lights itself. It writes what the cloud
(or, while the cloud is silent, the offline schedule) wants each court to do
to `binary_sensor.pista_{n}`, same contract as SR.Lobo 1.0:

    state:      "on" | "off"           (never anything else)
    brightness: 0-100                  (0 when off)

The club's local HA automation reads that sensor and is the only thing that
drives the real lights (`light.luces_padel_{n}`), so local manual override
with a timer keeps working, with or without internet. `light.luces_padel_{n}`
is only read, for discovery and telemetry.

States written through the REST API are lost when HA Core restarts. The last
signal per court is saved to /data and written again at startup, whenever the
HA connection comes back, and every REFRESH_S. Rewriting an identical state
doesn't fire state_changed, so the refresh doesn't retrigger automations.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from .entity_registry import EntityRegistry, court_number
from .ha_client import HomeAssistantClient, HomeAssistantError
from .persistence import DATA_DIR, read_json, write_json_atomic

logger = logging.getLogger(__name__)

SIGNALS_PATH = f"{DATA_DIR}/court_signals.json"
DEFAULT_ENTITY_TEMPLATE = "binary_sensor.pista_{n}"
REFRESH_S = 60

SOURCE_CLOUD = "cloud"
SOURCE_OFFLINE_SCHEDULE = "offline_schedule"

# ("on", 80) or ("off", 0)
Signal = Tuple[str, int]


def _utc_now_iso() -> str:
    """Returns:
        Current UTC time as ISO-8601.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(state: Optional[str], brightness_pct: Any) -> Optional[Signal]:
    """Turns a command's state/brightness into the exact signal written to HA.

    Args:
        state: "on", "off" or None.
        brightness_pct: 0-100 or None.

    Returns:
        ("on", 0-100), ("off", 0), or None if the command doesn't touch
        on/off/brightness (e.g. a mode-only command).
    """
    if state == "off":
        return ("off", 0)
    if state == "on" or brightness_pct is not None:
        if brightness_pct is None:
            return ("on", 100)
        try:
            value = int(round(float(brightness_pct)))
        except (TypeError, ValueError):
            return ("on", 100)
        value = max(0, min(100, value))
        # Brightness 0 means off for the local automation.
        return ("off", 0) if value == 0 else ("on", value)
    return None


class CourtSignalPublisher:
    """Writes and keeps alive `binary_sensor.pista_{n}` for every court."""

    def __init__(
        self,
        ha: HomeAssistantClient,
        registry: EntityRegistry,
        installation_id: str,
        entity_template: str = DEFAULT_ENTITY_TEMPLATE,
        path: str = SIGNALS_PATH,
        now_iso: Callable[[], str] = _utc_now_iso,
    ) -> None:
        """
        Args:
            ha: HA client.
            registry: This installation's courts.
            installation_id: Saved with the signals, so signals restored from
                another club's backup are ignored.
            entity_template: Entity id template, `{n}` is the 1-based court number.
            path: Persistence file.
            now_iso: UTC clock (tests).
        """
        self._ha = ha
        self._registry = registry
        self._installation_id = installation_id
        self._template = entity_template
        self._path = path
        self._now_iso = now_iso
        self._lock = threading.Lock()
        # index -> {"state", "brightness", "source", "updated_at"}
        self._signals: Dict[int, Dict[str, Any]] = {}
        self._court_names: Dict[int, str] = {}
        self._stop = threading.Event()

    def entity_id(self, index: int) -> str:
        """Args:
            index: 0-based court index.

        Returns:
            The court's signal entity id.
        """
        return self._template.format(n=court_number(index))

    def load(self) -> None:
        """Loads saved signals and writes them to HA straight away, so the
        local automation has its input back before the cloud reconnects."""
        stored = read_json(self._path)
        if not isinstance(stored, dict) or stored.get("installation_id") != self._installation_id:
            if stored is not None:
                logger.warning("Saved court signals are for another installation, ignoring them")
            return
        courts = stored.get("courts")
        if not isinstance(courts, dict):
            return
        valid = self._registry.court_indexes()
        with self._lock:
            for key, value in courts.items():
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if index not in valid or not isinstance(value, dict):
                    continue
                if value.get("state") not in ("on", "off"):
                    continue
                self._signals[index] = value
        logger.info("Restored court signals for %s courts", len(self._signals))
        self.republish()

    def apply(self, index: int, state: Optional[str], brightness_pct: Any, source: str) -> bool:
        """Sets a court's signal from a cloud command or the offline schedule.

        Args:
            index: 0-based court index.
            state: "on", "off" or None.
            brightness_pct: 0-100 or None.
            source: SOURCE_CLOUD or SOURCE_OFFLINE_SCHEDULE.

        Returns:
            True if the payload touched on/off/brightness (whether or not
            the value changed), False if there was nothing to apply.

        Raises:
            HomeAssistantError: If writing to HA fails. The new value is
                still kept and saved, and will be written on the next refresh.
        """
        signal = normalize(state, brightness_pct)
        if signal is None:
            return False
        with self._lock:
            current = self._signals.get(index)
            if (
                current is not None
                and (current.get("state"), current.get("brightness")) == signal
                and current.get("source") == source
            ):
                value = current
            else:
                value = {
                    "state": signal[0],
                    "brightness": signal[1],
                    "source": source,
                    "updated_at": self._now_iso(),
                }
                self._signals[index] = value
                self._save_locked()
        self._write(index, value)
        return True

    def current(self, index: int) -> Optional[Signal]:
        """Args:
            index: 0-based court index.

        Returns:
            The last signal set for the court, or None.
        """
        with self._lock:
            value = self._signals.get(index)
        if value is None:
            return None
        return (value["state"], int(value.get("brightness", 0)))

    def set_court_names(self, names: Dict[int, str]) -> None:
        """Updates the friendly names (from venue/config) and rewrites the
        signals so HA shows the new names.

        Args:
            names: 0-based court index -> court name.
        """
        with self._lock:
            self._court_names = dict(names)
        self.republish()

    def republish(self) -> None:
        """Writes every known signal to HA again. Used at startup, when the
        HA connection comes back (Core restart) and periodically."""
        with self._lock:
            snapshot = dict(self._signals)
        for index, value in snapshot.items():
            try:
                self._write(index, value)
            except HomeAssistantError:
                logger.warning("Could not rewrite court %s signal, will retry", index)

    def start(self) -> None:
        """Starts the periodic refresh thread."""
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        """Stops the periodic refresh thread."""
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_S):
            try:
                self.republish()
            except Exception:  # noqa: BLE001 - the refresh thread must not die
                logger.exception("Court signal refresh failed")

    def _write(self, index: int, value: Dict[str, Any]) -> None:
        """Writes one court's signal to HA. Attributes only change when the
        signal changes, so an identical rewrite doesn't fire state_changed.

        Args:
            index: 0-based court index.
            value: Stored signal.
        """
        name = self._court_names.get(index) or f"Pista {court_number(index)}"
        attributes = {
            "friendly_name": name,
            "brightness": int(value.get("brightness", 0)),
            "source": value.get("source"),
            "updated_at": value.get("updated_at"),
            "court_index": index,
        }
        self._ha.set_state(self.entity_id(index), value["state"], attributes)

    def _save_locked(self) -> None:
        """Saves signals to disk. Caller holds the lock."""
        try:
            write_json_atomic(
                self._path,
                {
                    "installation_id": self._installation_id,
                    "courts": {str(i): v for i, v in self._signals.items()},
                },
            )
        except OSError:
            logger.exception("Could not save court signals")
