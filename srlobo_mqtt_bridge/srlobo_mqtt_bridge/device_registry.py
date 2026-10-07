"""HA device and entity registry helpers (ADR-007, ADR-018).

Integrations like Shelly and Nuki register some sensors as disabled by
default, and disabled entities never show up in GET /api/states, so their
data never reaches telemetry. DisabledEntityEnabler turns those back on,
except ones a user disabled. The device registry also gives us firmware
versions for the state payloads.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .ha_client import HomeAssistantClient, HomeAssistantError

logger = logging.getLogger(__name__)

# disabled_by values we re-enable. "user" is left alone.
_REENABLE_DISABLED_BY = {"integration", "config_entry", "device", "hass"}


@dataclass
class RegistrySnapshot:
    devices: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    entities: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def fetch(cls, ha: HomeAssistantClient) -> "RegistrySnapshot":
        """Reads both registries.

        Args:
            ha: HA client.

        Returns:
            The snapshot.
        """
        devices = {device["id"]: device for device in ha.list_device_registry() if "id" in device}
        return cls(devices=devices, entities=ha.list_entity_registry())

    def device_id_of(self, entity_id: str) -> Optional[str]:
        """Args:
            entity_id: Entity to look up.

        Returns:
            The entity's device id, or None.
        """
        for entity in self.entities:
            if entity.get("entity_id") == entity_id:
                return entity.get("device_id")
        return None

    def firmware_by_entity(self) -> Dict[str, str]:
        """Maps each entity to its device's sw_version, where known.

        Returns:
            Entity id to firmware version.
        """
        result: Dict[str, str] = {}
        for entity in self.entities:
            device = self.devices.get(entity.get("device_id") or "")
            version = device.get("sw_version") if device else None
            if version:
                result[entity["entity_id"]] = str(version)
        return result


def _matches_manufacturer(device: Dict[str, Any], manufacturers: Iterable[str]) -> bool:
    """Case-insensitive substring match, so "Nuki" matches "Nuki Home Solutions GmbH".

    Args:
        device: Device registry entry.
        manufacturers: Configured manufacturer names.

    Returns:
        True if the device matches one of them.
    """
    manufacturer = (device.get("manufacturer") or "").lower()
    return bool(manufacturer) and any(name.lower() in manufacturer for name in manufacturers if name)


class DisabledEntityEnabler:
    def __init__(self, ha: HomeAssistantClient, manufacturers: List[str]) -> None:
        """Args:
            ha: HA client.
            manufacturers: Manufacturers whose devices are always included.
        """
        self._ha = ha
        self._manufacturers = manufacturers

    def run(
        self,
        snapshot: RegistrySnapshot,
        seed_entity_ids: Iterable[str],
        only_device_ids: Optional[Set[str]] = None,
    ) -> int:
        """Re-enables integration-disabled entities on the devices behind
        `seed_entity_ids` and on every device from a configured
        manufacturer, then reloads the affected config entries.

        Args:
            snapshot: Registry snapshot.
            seed_entity_ids: Court light group members and door locks.
            only_device_ids: Limit the pass to these devices (a newly added one).

        Returns:
            How many entities were enabled. Failures are logged and skipped.
        """
        targets: Set[str] = {
            device_id
            for device_id in (snapshot.device_id_of(entity_id) for entity_id in seed_entity_ids)
            if device_id
        }
        targets.update(
            device_id
            for device_id, device in snapshot.devices.items()
            if _matches_manufacturer(device, self._manufacturers)
        )
        if only_device_ids is not None:
            targets &= only_device_ids

        enabled = 0
        entries_to_reload: Set[str] = set()
        for entity in snapshot.entities:
            if entity.get("device_id") not in targets:
                continue
            if entity.get("disabled_by") not in _REENABLE_DISABLED_BY:
                continue
            entity_id = entity.get("entity_id")
            try:
                self._ha.enable_entity(entity_id)
            except HomeAssistantError:
                logger.warning("Could not enable %s", entity_id, exc_info=True)
                continue
            enabled += 1
            logger.info("Enabled %s (disabled_by=%s)", entity_id, entity.get("disabled_by"))
            if entity.get("config_entry_id"):
                entries_to_reload.add(entity["config_entry_id"])

        for entry_id in entries_to_reload:
            try:
                self._ha.reload_config_entry(entry_id)
            except HomeAssistantError:
                # HA reloads by itself a bit after an enable, so this only delays the data.
                logger.warning("Could not reload config entry %s", entry_id, exc_info=True)
        return enabled
