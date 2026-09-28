"""Dynamic entity discovery (ADR-007 §2, extended to doors by ADR-019).

Finds every real HA entity behind a court/door's hardware, not just the
top-level helper/lock, so telemetry.py can publish everything unfiltered.
Runs at startup and again on every WebSocket reconnect (ADR-007 §2) since a
Core or add-on restart can change what's actually present.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import BootstrapConfig
from .entity_registry import EntityRegistry, court_helper_entity_id
from .ha_client import HomeAssistantClient

logger = logging.getLogger(__name__)

# Domains that are control/diagnostic surfaces, never telemetry, regardless of
# whether their entity_id happens to share a device's prefix (ADR-007 §2 step 2).
_EXCLUDED_DOMAINS = {"button", "update"}


@dataclass
class MemberDevice:
    member_entity_id: str
    entity_ids: List[str] = field(default_factory=list)


@dataclass
class CourtDiscovery:
    index: int
    helper_entity_id: str
    devices: List[MemberDevice] = field(default_factory=list)


@dataclass
class DoorDiscovery:
    index: int
    lock_entity_id: str
    devices: List[MemberDevice] = field(default_factory=list)


@dataclass
class DiscoveryState:
    courts: Dict[int, CourtDiscovery] = field(default_factory=dict)
    doors: Dict[int, DoorDiscovery] = field(default_factory=dict)
    # entity_id -> ("court" | "door", index), for fast dispatch on state_changed.
    reverse: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    # Snapshot at discovery time. Seeds telemetry.py's cache so a payload
    # right after (re)connect doesn't have to wait for every entity to fire
    # its own state_changed first.
    raw_states: List[Dict] = field(default_factory=list)


def _is_telemetry_entity(entity_id: str) -> bool:
    domain = entity_id.split(".", 1)[0]
    if domain in _EXCLUDED_DOMAINS:
        return False
    if "_entrada_" in entity_id:
        # Digital-relay input wiring diagnostics, not telemetry (ADR-007 context table).
        return False
    return True


class Discoverer:
    def __init__(self, ha: HomeAssistantClient, registry: EntityRegistry, bootstrap: BootstrapConfig) -> None:
        self._ha = ha
        self._registry = registry
        self._bootstrap = bootstrap

    def discover_all(self) -> DiscoveryState:
        all_states = self._ha.get_states()
        state = DiscoveryState(raw_states=all_states)

        for court in self._bootstrap.courts:
            helper_entity_id = court_helper_entity_id(court.index)
            devices = self._discover_devices(helper_entity_id, all_states)
            state.courts[court.index] = CourtDiscovery(
                index=court.index, helper_entity_id=helper_entity_id, devices=devices
            )
            state.reverse[helper_entity_id] = ("court", court.index)
            for device in devices:
                for entity_id in device.entity_ids:
                    state.reverse[entity_id] = ("court", court.index)
                    self._registry.register_member_entity(entity_id)

        for door in self._bootstrap.doors:
            try:
                lock_entity_id = self._registry.door_entity_id(door.index)
            except KeyError:
                logger.warning("No entity_id configured for door index %s, skipping discovery", door.index)
                continue
            devices = self._discover_devices(lock_entity_id, all_states)
            state.doors[door.index] = DoorDiscovery(
                index=door.index, lock_entity_id=lock_entity_id, devices=devices
            )
            state.reverse[lock_entity_id] = ("door", door.index)
            for device in devices:
                for entity_id in device.entity_ids:
                    state.reverse[entity_id] = ("door", door.index)
                    self._registry.register_member_entity(entity_id)

        return state

    def _discover_devices(self, helper_entity_id: str, all_states: List[Dict]) -> List[MemberDevice]:
        """Given a top-level helper (a court's light group) or a standalone
        entity (a door's lock), finds member entities the same way ADR-007 §2
        describes: a light group's `attributes.entity_id` lists its members;
        anything else is treated as its own single member. For each member,
        find sibling entities sharing its device prefix."""
        helper_state = self._ha.get_state(helper_entity_id)
        if helper_state is None:
            logger.warning("Entity %s not found, skipping discovery for it", helper_entity_id)
            return []

        member_entity_ids = helper_state.get("attributes", {}).get("entity_id") or [helper_entity_id]

        devices: List[MemberDevice] = []
        for member_entity_id in member_entity_ids:
            devices.append(self._discover_device(member_entity_id, all_states))
        return devices

    @staticmethod
    def _discover_device(member_entity_id: str, all_states: List[Dict]) -> MemberDevice:
        domain, _, suffix = member_entity_id.partition(".")
        prefix = suffix
        related = [
            state["entity_id"]
            for state in all_states
            if "entity_id" in state
            and state["entity_id"].split(".", 1)[-1].startswith(prefix)
            and _is_telemetry_entity(state["entity_id"])
        ]
        if member_entity_id not in related:
            related.append(member_entity_id)
        return MemberDevice(member_entity_id=member_entity_id, entity_ids=related)
