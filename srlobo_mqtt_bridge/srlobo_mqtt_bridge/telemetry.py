"""Raw telemetry publishing. ADR-007 §5 for courts, ADR-019 for doors,
ADR-020's calibration fields folded into the court payload.

Rule (same for both, per ADR-019): publish everything HA exposes for the
device's entities, unfiltered. No named-field allowlist here, that's the
backend's job when it derives court_telemetry/door_telemetry columns from
this payload (ADR-007 §6).
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import AddonOptions
from .discovery import CourtDiscovery, DiscoveryState, DoorDiscovery
from .ha_client import HomeAssistantClient
from .mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)

# ADR-007's own noted risk: high-frequency state changes (e.g. a lux sensor
# updating every second) could flood MQTT. Debounced per court/door index.
DEBOUNCE_S = 2.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateCache:
    """Tracks the latest state+attributes for every entity_id seen. Seeded
    from a discovery snapshot, kept current by every incoming state_changed
    event (even ones we don't own, cheap to cache and saves re-fetching over
    REST on every publish)."""

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, Any]] = {}

    def seed(self, raw_states: list) -> None:
        for state in raw_states:
            entity_id = state.get("entity_id")
            if entity_id:
                self._states[entity_id] = state

    def update(self, entity_id: str, state: Dict[str, Any]) -> None:
        self._states[entity_id] = state

    def get(self, entity_id: str) -> Optional[Dict[str, Any]]:
        return self._states.get(entity_id)


def _member_entities_dict(cache: StateCache, entity_ids: list) -> Dict[str, str]:
    entities: Dict[str, str] = {}
    for entity_id in entity_ids:
        cached = cache.get(entity_id)
        if cached is not None:
            entities[entity_id] = cached.get("state", "unknown")
    return entities


class TelemetryPublisher:
    def __init__(
        self,
        mqtt: BridgeMqttClient,
        ha: HomeAssistantClient,
        options: AddonOptions,
    ) -> None:
        self._mqtt = mqtt
        self._ha = ha
        self._options = options
        self._cache = StateCache()
        self._discovery: Optional[DiscoveryState] = None
        self._debounce_timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_rediscover(self, discovery: DiscoveryState) -> None:
        self._discovery = discovery
        self._cache.seed(discovery.raw_states)
        # Publish an immediate snapshot for everything right after (re)connect,
        # rather than waiting for each entity's next individual state change.
        for index in discovery.courts:
            self._publish_court(index)
        for index in discovery.doors:
            self._publish_door(index)

    def on_state_changed(self, event_data: Dict[str, Any]) -> None:
        entity_id = event_data.get("entity_id")
        new_state = event_data.get("new_state")
        if not entity_id or new_state is None or self._discovery is None:
            return
        self._cache.update(entity_id, new_state)

        target = self._discovery.reverse.get(entity_id)
        if target is None:
            return  # Not one of ours, ignore it (entity_registry principle).
        domain, index = target
        self._debounced_publish(domain, index)

    def _debounced_publish(self, domain: str, index: int) -> None:
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
        if self._discovery is None:
            return
        court = self._discovery.courts.get(index)
        if court is None:
            return
        payload = self._build_court_payload(court)
        self._mqtt.publish_court_telemetry(index, payload)

    def _publish_door(self, index: int) -> None:
        if self._discovery is None:
            return
        door = self._discovery.doors.get(index)
        if door is None:
            return
        payload = self._build_door_payload(door)
        self._mqtt.publish_door_telemetry(index, payload)

    def _build_court_payload(self, court: CourtDiscovery) -> Dict[str, Any]:
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

    def _read_calibration(self, court_index: int) -> Optional[Dict[str, Any]]:
        """Reads reference_lux/reference_power_pct/calibrated_at (ADR-020) off
        the configurable lux-reference entity. Best-effort since the real
        entity name isn't confirmed yet. A missing entity just means "not
        calibrated yet", not an error."""
        entity_id = self._options.lux_reference_entity_template.format(n=court_index)
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
