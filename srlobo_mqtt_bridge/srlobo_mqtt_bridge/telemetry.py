"""Raw telemetry publishing for courts and doors.

Rule (same for both): publish everything HA exposes for the device's
entities, unfiltered. No named-field allowlist here, that's the backend's
job when it derives court_telemetry/door_telemetry columns from this
payload.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .commands import CommandHandler
from .config import AddonOptions
from .discovery import CourtDiscovery, DiscoveryState, DoorDiscovery
from .entity_registry import court_number
from .ha_client import HomeAssistantClient
from .mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)

# High-frequency state changes (e.g. a lux sensor updating every second)
# could flood MQTT. Debounced per court/door index.
DEBOUNCE_S = 2.0


def _utc_now_iso() -> str:
    """Returns the current UTC time formatted as an ISO-8601 string.

    Returns:
        The current UTC timestamp, e.g. "2026-01-01T00:00:00Z".
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateCache:
    """Tracks the latest state+attributes for every entity_id seen. Seeded
    from a discovery snapshot, kept current by every incoming state_changed
    event (even ones we don't own, cheap to cache and saves re-fetching over
    REST on every publish)."""

    def __init__(self) -> None:
        """Initializes an empty cache."""
        self._states: Dict[str, Dict[str, Any]] = {}

    def seed(self, raw_states: list) -> None:
        """Loads a discovery snapshot's states into the cache.

        Args:
            raw_states: List of HA entity state dicts to seed the cache with.
        """
        for state in raw_states:
            entity_id = state.get("entity_id")
            if entity_id:
                self._states[entity_id] = state

    def update(self, entity_id: str, state: Dict[str, Any]) -> None:
        """Updates the cached state for one entity.

        Args:
            entity_id: Entity id to update.
            state: New state dict for the entity.
        """
        self._states[entity_id] = state

    def get(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Looks up an entity's cached state.

        Args:
            entity_id: Entity id to look up.

        Returns:
            The cached state dict, or None if nothing is cached for it.
        """
        return self._states.get(entity_id)


# Entity id fragments for the lux and Wi-Fi AP readings.
_LUX_ENTITY_HINTS = ("lux", "illuminance")
_AP_ENTITY_HINTS = ("ssid", "wifi_ap")
_VALID_MODES = ("AUTO", "MANUAL", "LUX_LOOP")


def _firmware_version(firmware: Dict[str, str], member_entity_ids: List[str]) -> Optional[str]:
    """firmware_version for a state payload (ADR-018). If the devices run
    different versions, all of them are listed.

    Args:
        firmware: Entity id to firmware version.
        member_entity_ids: The court's or door's member entities.

    Returns:
        The version(s), comma-separated, or None.
    """
    versions = sorted({firmware[entity_id] for entity_id in member_entity_ids if entity_id in firmware})
    return ", ".join(versions) if versions else None


def _find_lux(entities: Dict[str, str]) -> Optional[float]:
    """First numeric reading from an entity whose id looks like a lux sensor.

    Args:
        entities: Entity id to state, for a court's member devices.

    Returns:
        The lux value, or None.
    """
    for entity_id, value in entities.items():
        if any(hint in entity_id for hint in _LUX_ENTITY_HINTS):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _find_wifi_ap(entities: Dict[str, str]) -> Optional[str]:
    """Wi-Fi AP name from the first entity whose id looks like one.

    Args:
        entities: Entity id to state, for a court's member devices.

    Returns:
        The AP name, or None.
    """
    for entity_id, value in entities.items():
        if any(hint in entity_id for hint in _AP_ENTITY_HINTS) and value not in ("unknown", "unavailable", ""):
            return value
    return None


def _member_entities_dict(cache: StateCache, entity_ids: list) -> Dict[str, str]:
    """Builds an entity_id -> state map for a device's member entities,
    from whatever is currently cached.

    Args:
        cache: State cache to read from.
        entity_ids: Entity ids to look up.

    Returns:
        A dict of entity_id to its current state string, omitting any
        entity with nothing cached for it.
    """
    entities: Dict[str, str] = {}
    for entity_id in entity_ids:
        cached = cache.get(entity_id)
        if cached is not None:
            entities[entity_id] = cached.get("state", "unknown")
    return entities


class TelemetryPublisher:
    """Publishes unfiltered court/door telemetry over MQTT, debounced per
    index, driven by discovery snapshots and HA state_changed events."""

    def __init__(
        self,
        mqtt: BridgeMqttClient,
        ha: HomeAssistantClient,
        options: AddonOptions,
        commands: CommandHandler,
    ) -> None:
        """Stores the collaborators needed to build and publish telemetry.

        Args:
            mqtt: Client used to publish telemetry payloads.
            ha: Client used to read calibration helper entity state.
            options: Add-on options, used to resolve the lux-reference entity template.
            commands: Source of each door's last executed action.
        """
        self._mqtt = mqtt
        self._ha = ha
        self._options = options
        self._commands = commands
        self._cache = StateCache()
        self._firmware: Dict[str, str] = {}
        self._discovery: Optional[DiscoveryState] = None
        self._debounce_timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def set_firmware(self, firmware: Dict[str, str]) -> None:
        """Sets the entity id to firmware version map, refreshed on every
        rediscovery.

        Args:
            firmware: Entity id to firmware version.
        """
        self._firmware = dict(firmware)

    def on_rediscover(self, discovery: DiscoveryState) -> None:
        """Applies a fresh discovery snapshot and immediately publishes
        telemetry for every known court/door from it.

        Args:
            discovery: Latest discovery state to seed the cache from.
        """
        self._discovery = discovery
        self._cache.seed(discovery.raw_states)
        # Publish an immediate snapshot for everything right after (re)connect,
        # rather than waiting for each entity's next individual state change.
        for index in discovery.courts:
            self._publish_court(index)
        for index in discovery.doors:
            self._publish_door(index)

    def on_state_changed(self, event_data: Dict[str, Any]) -> None:
        """Updates the cache for any state_changed event and schedules a
        debounced publish if the entity belongs to a known court/door.

        Args:
            event_data: The state_changed event's "data" field from HA.
        """
        entity_id = event_data.get("entity_id")
        new_state = event_data.get("new_state")
        if not entity_id or new_state is None or self._discovery is None:
            return
        self._cache.update(entity_id, new_state)

        target = self._discovery.reverse.get(entity_id)
        if target is None:
            index = self._local_court_index(entity_id)
            if index is None:
                return  # Not one of ours, ignore it (entity_registry principle).
            target = ("court", index)
        domain, index = target
        self._debounced_publish(domain, index)

    def _local_court_index(self, entity_id: str) -> Optional[int]:
        """Court index for the local helpers that change what a court does
        without touching its lights: the auto/manual switch and the mode
        select. A change there republishes the court's state.

        Args:
            entity_id: HA entity id from a state_changed event.

        Returns:
            0-based court index, or None if it isn't one of them.
        """
        if self._discovery is None:
            return None
        for index in self._discovery.courts:
            number = court_number(index)
            if entity_id in (
                self._options.local_auto_manual_entity_template.format(n=number),
                self._options.mode_select_entity_template.format(n=number),
                self._options.court_signal_entity_template.format(n=number),
            ):
                return index
        return None

    def _debounced_publish(self, domain: str, index: int) -> None:
        """Schedules a publish for a court/door after DEBOUNCE_S, resetting
        any pending timer for the same index.

        Args:
            domain: "court" or "door".
            index: 0-based court or door index.
        """
        key = f"{domain}:{index}"
        with self._lock:
            existing = self._debounce_timers.get(key)
            if existing is not None:
                existing.cancel()
            publish = self._publish_court if domain == "court" else self._publish_door
            timer = threading.Timer(DEBOUNCE_S, publish, args=(index,))
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _publish_court(self, index: int) -> None:
        """Publishes telemetry and state for one court, if still known.

        Args:
            index: 0-based court index.
        """
        if self._discovery is None:
            return
        court = self._discovery.courts.get(index)
        if court is None:
            return
        payload = self._build_court_payload(court)
        self._mqtt.publish_court_telemetry(index, payload)
        self._mqtt.publish_court_state(index, self._build_court_state_payload(payload))

    def _publish_door(self, index: int) -> None:
        """Publishes telemetry and state for one door, if still known.

        Args:
            index: 0-based door index.
        """
        if self._discovery is None:
            return
        door = self._discovery.doors.get(index)
        if door is None:
            return
        payload = self._build_door_payload(door)
        self._mqtt.publish_door_telemetry(index, payload)
        self._mqtt.publish_door_state(index, self._build_door_state_payload(index, payload))

    def _build_court_payload(self, court: CourtDiscovery) -> Dict[str, Any]:
        """Assembles a court's telemetry payload from cached state plus
        calibration data.

        Args:
            court: Discovery info for the court to build a payload for.

        Returns:
            The telemetry payload to publish.
        """
        helper_state = self._cache.get(court.helper_entity_id) or {}
        brightness = helper_state.get("attributes", {}).get("brightness")
        brightness_pct = round(brightness / 255 * 100) if isinstance(brightness, (int, float)) else None

        payload: Dict[str, Any] = {
            "court_index": court.index,
            "helper": {
                "entity_id": court.helper_entity_id,
                "state": helper_state.get("state", "unknown"),
                "brightness_pct": brightness_pct,
            },
            "devices": [
                {
                    "member_entity_id": device.member_entity_id,
                    "entities": _member_entities_dict(self._cache, device.entity_ids),
                }
                for device in court.devices
            ],
            "updated_at": _utc_now_iso(),
        }

        calibration = self._read_calibration(court.index)
        if calibration:
            payload.update(calibration)
        return payload

    def _build_door_payload(self, door: DoorDiscovery) -> Dict[str, Any]:
        """Assembles a door's telemetry payload from cached state.

        Args:
            door: Discovery info for the door to build a payload for.

        Returns:
            The telemetry payload to publish.
        """
        lock_state = self._cache.get(door.lock_entity_id) or {}
        return {
            "door_index": door.index,
            "lock": {
                "entity_id": door.lock_entity_id,
                "state": lock_state.get("state", "unknown"),
            },
            "devices": [
                {
                    "member_entity_id": device.member_entity_id,
                    "entities": _member_entities_dict(self._cache, device.entity_ids),
                }
                for device in door.devices
            ],
            "updated_at": _utc_now_iso(),
        }

    def _build_court_state_payload(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """Builds courts/{n}/state from the telemetry payload. Keep the
        rules in sync with the backend's courtTelemetryDerivation.ts.

        Args:
            telemetry: The court's telemetry payload (needs court_index).

        Returns:
            The state payload.
        """
        helper = telemetry.get("helper", {}) or {}
        light_on = helper.get("state") == "on"
        brightness_pct = helper.get("brightness_pct")

        power_w: Optional[float] = None
        has_power = False
        saw_unavailable = False
        worst_rssi: Optional[int] = None
        all_entities: Dict[str, str] = {}

        for device in telemetry.get("devices", []):
            for entity_id, value in (device.get("entities") or {}).items():
                all_entities[entity_id] = value
                if value == "unavailable":
                    saw_unavailable = True
                if "_potencia" in entity_id:
                    try:
                        power_w = (power_w or 0) + float(value)
                        has_power = True
                    except (TypeError, ValueError):
                        pass
                if "rssi" in entity_id or "signal" in entity_id:
                    try:
                        rssi = int(value)
                        if worst_rssi is None or rssi < worst_rssi:
                            worst_rssi = rssi
                    except (TypeError, ValueError):
                        pass

        return {
            "light_on": light_on,
            "brightness_pct": brightness_pct if isinstance(brightness_pct, (int, float)) else None,
            "power_w": power_w if has_power else None,
            "lux_measured": _find_lux(all_entities),
            "wifi_rssi": worst_rssi,
            "wifi_ap": _find_wifi_ap(all_entities),
            "mode": self._device_reported_mode(telemetry.get("court_index")),
            # ADR-026: whether the club has the court in local manual, and the
            # signal the bridge last gave the local automation. Additive,
            # optional fields (ADR-025 Decision 7).
            "local_mode": self._local_mode(telemetry.get("court_index")),
            "signal": self._signal(telemetry.get("court_index")),
            "shelly_online": not saw_unavailable,
            "firmware_version": _firmware_version(
                self._firmware, [device.get("member_entity_id") for device in telemetry.get("devices", [])]
            ),
            "updated_at": _utc_now_iso(),
        }

    def _local_mode(self, court_index: Optional[int]) -> Optional[str]:
        """Local auto/manual switch, as used by the club's light automation:
        on = auto (follows the signal), off = manual override.

        Args:
            court_index: 0-based court index.

        Returns:
            "auto", "manual", or None if the switch doesn't exist or is unavailable.
        """
        if court_index is None:
            return None
        cached = self._cache.get(
            self._options.local_auto_manual_entity_template.format(n=court_number(court_index))
        )
        if cached is None:
            return None
        state = cached.get("state")
        if state == "on":
            return "auto"
        if state == "off":
            return "manual"
        return None

    def _signal(self, court_index: Optional[int]) -> Optional[Dict[str, Any]]:
        """The court signal as HA currently has it.

        Args:
            court_index: 0-based court index.

        Returns:
            {"state", "brightness"}, or None if HA doesn't have it.
        """
        if court_index is None:
            return None
        cached = self._cache.get(self._options.court_signal_entity_template.format(n=court_number(court_index)))
        if cached is None or cached.get("state") not in ("on", "off"):
            return None
        brightness = (cached.get("attributes") or {}).get("brightness")
        return {
            "state": cached.get("state"),
            "brightness": brightness if isinstance(brightness, (int, float)) else None,
        }

    def _device_reported_mode(self, court_index: Optional[int]) -> Optional[str]:
        """Mode from the court's mode select helper. Values that aren't
        AUTO/MANUAL/LUX_LOOP are reported as None.

        Args:
            court_index: 0-based court index.

        Returns:
            The mode, or None.
        """
        if court_index is None:
            return None
        cached = self._cache.get(self._options.mode_select_entity_template.format(n=court_number(court_index)))
        if cached is None:
            return None
        state = cached.get("state")
        return state if state in _VALID_MODES else None

    def _build_door_state_payload(self, index: int, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """Builds doors/{n}/state from the telemetry payload plus the last
        executed action.

        Args:
            index: 0-based door index.
            telemetry: The door's telemetry payload.

        Returns:
            The state payload.
        """
        lock = telemetry.get("lock", {}) or {}
        state = lock.get("state")
        locked = True if state == "locked" else False if state == "unlocked" else None

        saw_unavailable = any(
            value == "unavailable"
            for device in telemetry.get("devices", [])
            for value in (device.get("entities") or {}).values()
        )

        return {
            "locked": locked,
            "online": (not saw_unavailable) if lock else None,
            "last_action": self._commands.last_door_action(index),
            "firmware_version": _firmware_version(
                self._firmware,
                [lock.get("entity_id")] + [device.get("member_entity_id") for device in telemetry.get("devices", [])],
            ),
            "updated_at": _utc_now_iso(),
        }

    def _read_calibration(self, court_index: int) -> Optional[Dict[str, Any]]:
        """Reads reference_lux/reference_power_pct/calibrated_at off the
        configurable lux-reference entity. Best-effort since the real
        entity name isn't confirmed yet. A missing entity just means "not
        calibrated yet", not an error.

        Args:
            court_index: 0-based court index.

        Returns:
            A dict with reference_lux/reference_power_pct/calibrated_at, or
            None if the entity is missing or its state isn't numeric.
        """
        entity_id = self._options.lux_reference_entity_template.format(n=court_number(court_index))
        state = self._ha.get_state(entity_id)
        if state is None:
            return None
        try:
            reference_lux = float(state.get("state"))
        except (TypeError, ValueError):
            return None
        attributes = state.get("attributes", {})
        return {
            "reference_lux": reference_lux,
            "reference_power_pct": attributes.get("reference_power_pct"),
            "calibrated_at": attributes.get("calibrated_at") or state.get("last_changed"),
        }
