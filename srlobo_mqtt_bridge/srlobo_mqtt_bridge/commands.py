"""Court/door command execution.

On/off + brightness (courts) and open/close/unlock (doors) are fully
specified by the ADRs, and call real HA services (light.*, lock.*), so no
guessing needed there. Mode-switching and calibration touch HA helper
entities with no documented naming convention anywhere (checked
blueprints_HA, not in there either), so those go through the configurable
entity-name templates in AddonOptions instead of a hardcoded guess.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import AddonOptions
from .entity_registry import court_helper_entity_id, EntityRegistry
from .ha_client import HomeAssistantClient, HomeAssistantError
from .mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)

_DOOR_ACTION_TO_SERVICE = {
    "open": ("lock", "open"),
    "close": ("lock", "lock"),
    "unlock": ("lock", "unlock"),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommandHandler:
    def __init__(
        self,
        ha: HomeAssistantClient,
        mqtt: BridgeMqttClient,
        registry: EntityRegistry,
        options: AddonOptions,
    ) -> None:
        self._ha = ha
        self._mqtt = mqtt
        self._registry = registry
        self._options = options

    def handle_court_command(self, index: int, payload: Dict[str, Any]) -> None:
        if index not in self._registry.court_indexes():
            logger.warning("Received command for unknown court index %s", index)
            return

        command_id = payload.get("command_id")
        try:
            if payload.get("action") == "calibrate":
                self._calibrate_court(index, payload)
            else:
                self._control_court(index, payload)
            self._publish_ack(index, command_id, status="executed", error=None)
        except HomeAssistantError as exc:
            logger.error("Court %s command failed: %s", index, exc)
            self._publish_ack(index, command_id, status="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - must still ack "failed", not crash the handler
            logger.exception("Unexpected error handling court %s command", index)
            self._publish_ack(index, command_id, status="failed", error=str(exc))

    def handle_door_command(self, index: int, payload: Dict[str, Any]) -> None:
        if index not in self._registry.door_indexes():
            logger.warning("Received command for unknown door index %s", index)
            return
        action = payload.get("action")
        service = _DOOR_ACTION_TO_SERVICE.get(action)
        if service is None:
            logger.warning("Unknown door action %r for door %s", action, index)
            return
        try:
            entity_id = self._registry.door_entity_id(index)
        except KeyError as exc:
            logger.error("Cannot execute door %s command: %s", index, exc)
            return
        domain, ha_service = service
        try:
            self._ha.call_service(domain, ha_service, entity_id)
            logger.info("Door %s: executed %s (%s.%s on %s)", index, action, domain, ha_service, entity_id)
        except HomeAssistantError:
            logger.exception("Door %s command %s failed", index, action)
        # No ack topic for doors, ADR-002 §5.2 only defines one for courts.

    def _control_court(self, index: int, payload: Dict[str, Any]) -> None:
        entity_id = court_helper_entity_id(index)
        state = payload.get("state")
        brightness_pct = payload.get("brightness_pct")

        if state == "off":
            self._ha.call_service("light", "turn_off", entity_id)
        elif state == "on" or brightness_pct is not None:
            data = {"brightness_pct": brightness_pct} if brightness_pct is not None else {}
            self._ha.call_service("light", "turn_on", entity_id, data)

        mode = payload.get("mode")
        if mode is not None:
            self._set_mode(index, mode)

        # ADR-002's 2026-09-18 amendment: a LUX_LOOP activation command
        # carries a one-time lux_target seed value. Only present when the
        # cloud actually wants to (re)seed it, so a plain mode-only command
        # for an already-configured court can omit it.
        lux_target = payload.get("lux_target")
        if lux_target is not None:
            self._seed_lux_target(index, lux_target)

    def _set_mode(self, index: int, mode: str) -> None:
        entity_id = self._options.mode_select_entity_template.format(n=index)
        try:
            self._ha.call_service("input_select", "select_option", entity_id, {"option": mode})
        except HomeAssistantError:
            # Entity name is unconfirmed (see module docstring). Logged, not
            # fatal, since on/off from the same command already went through.
            logger.warning(
                "Could not set mode %s on %s (court %s), entity name unconfirmed against the real HA blueprint",
                mode,
                entity_id,
                index,
            )

    def _seed_lux_target(self, index: int, lux_target: Any) -> None:
        entity_id = self._options.lux_reference_entity_template.format(n=index)
        try:
            self._ha.call_service("input_number", "set_value", entity_id, {"value": lux_target})
            logger.info("Court %s: seeded lux_target=%s on %s", index, lux_target, entity_id)
        except HomeAssistantError:
            # Same entity-name caveat as _set_mode: logged, not fatal.
            logger.warning(
                "Could not seed lux_target on %s (court %s), entity name unconfirmed against the real HA blueprint",
                entity_id,
                index,
            )

    def _calibrate_court(self, index: int, payload: Dict[str, Any]) -> None:
        calibration_power_pct = payload.get("calibration_power_pct")
        entity_id = self._options.calibration_trigger_entity_template.format(n=index)
        data = {"power_pct": calibration_power_pct} if calibration_power_pct is not None else {}
        self._ha.call_service("input_button", "press", entity_id, data)
        logger.info("Court %s: triggered calibration via %s", index, entity_id)

    def _publish_ack(self, index: int, command_id: Optional[str], status: str, error: Optional[str]) -> None:
        self._mqtt.publish_court_ack(
            index,
            {
                "command_id": command_id,
                "status": status,
                "error": error,
                "timestamp": _utc_now_iso(),
            },
        )
