"""Single source of truth for "does automation_bridge own this entity".

Any cleanup/reconciliation code here must go through is_owned() instead of
inventing its own domain/name check: a blanket domain/name filter can match
entities this bridge doesn't actually own and delete them fleet-wide.

Built once at startup from bootstrap. discovery.py registers each
court/door's real member entities as it finds them.
"""

from typing import Dict, Set

from .config import BootstrapConfig


def court_number(index: int) -> int:
    """MQTT/bootstrap court indexes are 0-based (ADR-002) but HA entities
    are numbered from 1 (`light.luces_padel_1` is the first court).

    Args:
        index: 0-based court index.

    Returns:
        The court number used in HA entity names.
    """
    return index + 1


def court_helper_entity_id(index: int) -> str:
    """Fixed naming convention for the installer-created light group
    helper. No config needed beyond the index.

    Args:
        index: 0-based court index.

    Returns:
        The light group helper's entity id.
    """
    return f"light.luces_padel_{court_number(index)}"


class EntityRegistry:
    """Tracks which HA entities this bridge owns, for a given installation."""

    def __init__(self, bootstrap: BootstrapConfig) -> None:
        """Builds the initial ownership set from the bootstrap config's
        courts and doors.

        Args:
            bootstrap: Bootstrap config listing the installation's courts and doors.
        """
        self._court_indexes = {court.index for court in bootstrap.courts}
        self._door_indexes = {door.index for door in bootstrap.doors}
        self._door_entity_by_index: Dict[int, str] = {
            door.index: door.entity_id for door in bootstrap.doors if door.entity_id
        }
        self._owned: Set[str] = {court_helper_entity_id(i) for i in self._court_indexes}
        self._owned.update(self._door_entity_by_index.values())

    def court_indexes(self) -> Set[int]:
        """Returns:
            A copy of the set of known court indexes.
        """
        return set(self._court_indexes)

    def door_indexes(self) -> Set[int]:
        """Returns:
            A copy of the set of known door indexes.
        """
        return set(self._door_indexes)

    def door_entity_id(self, index: int) -> str:
        """Looks up the configured HA lock entity id for a door.

        Args:
            index: 0-based door index.

        Returns:
            The door's HA lock entity id.

        Raises:
            KeyError: If no entity_id is configured for this door index.
        """
        entity_id = self._door_entity_by_index.get(index)
        if not entity_id:
            raise KeyError(f"No HA entity_id configured for door index {index}")
        return entity_id

    def register_member_entity(self, entity_id: str) -> None:
        """Extends ownership past the top-level helper/lock once discovery
        finds the actual member entities (Shelly lights, sensors, the lock).

        Args:
            entity_id: HA entity id to add to the owned set.
        """
        self._owned.add(entity_id)

    def is_owned(self, entity_id: str) -> bool:
        """Checks whether this bridge owns the given entity.

        Args:
            entity_id: HA entity id to check.

        Returns:
            True if the entity is owned by this bridge.
        """
        return entity_id in self._owned
