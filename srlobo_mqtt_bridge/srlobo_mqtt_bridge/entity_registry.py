"""Single source of truth for "does automation_bridge own this entity".

Per lessons-from-lobobrain.md #1: loboBrain had a domain filter
(`if 'binary_sensor' in entity_id`) that deleted every matching entity
fleet-wide on restart, including entities that had nothing to do with it.
Any cleanup/reconciliation code here has to go through is_owned() instead of
inventing its own domain/name check.

Built once at startup from bootstrap. discovery.py registers each
court/door's real member entities as it finds them.
"""

from typing import Dict, Set

from .config import BootstrapConfig


def court_helper_entity_id(index: int) -> str:
    """Fixed naming convention (ADR-007 §1) for the installer-created light
    group helper. No config needed beyond the index."""
    return f"light.luces_padel_{index}"


class EntityRegistry:
    def __init__(self, bootstrap: BootstrapConfig) -> None:
        self._court_indexes = {court.index for court in bootstrap.courts}
        self._door_indexes = {door.index for door in bootstrap.doors}
        self._door_entity_by_index: Dict[int, str] = {
            door.index: door.entity_id for door in bootstrap.doors if door.entity_id
        }
        self._owned: Set[str] = {court_helper_entity_id(i) for i in self._court_indexes}
        self._owned.update(self._door_entity_by_index.values())

    def court_indexes(self) -> Set[int]:
        return set(self._court_indexes)

    def door_indexes(self) -> Set[int]:
        return set(self._door_indexes)

    def door_entity_id(self, index: int) -> str:
        entity_id = self._door_entity_by_index.get(index)
        if not entity_id:
            raise KeyError(f"No HA entity_id configured for door index {index}")
        return entity_id

    def register_member_entity(self, entity_id: str) -> None:
        """Extends ownership past the top-level helper/lock once discovery
        finds the actual member entities (Shelly lights, sensors, the lock)."""
        self._owned.add(entity_id)

    def is_owned(self, entity_id: str) -> bool:
        return entity_id in self._owned
