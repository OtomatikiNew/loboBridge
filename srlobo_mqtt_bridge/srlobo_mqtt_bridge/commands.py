"""Court/door command execution.

On/off + brightness (courts) and open/close/unlock (doors) call real HA
services (light.*, lock.*) directly. Mode-switching and calibration touch HA
helper entities with no documented naming convention anywhere, so those go
through the configurable entity-name templates in AddonOptions instead of a
hardcoded guess.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import AddonOptions
from .entity_registry import court_helper_entity_id, court_number, EntityRegistry
from .ha_client import HomeAssistantClient, HomeAssistantError
from .mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)

_DOOR_ACTION_TO_SERVICE = {
    "open": ("lock", "open"),
    "close": ("lock", "lock"),
    "unlock": ("lock", "unlock"),
}


def _utc_now_iso() -> str:
    """Returns the current UTC time formatted as an ISO-8601 string.

    Returns:
        The current UTC timestamp, e.g. "2026-01-01T00:00:00Z".
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommandHandler:
    """Executes court/door commands received over MQTT against Home
    Assistant and publishes the resulting command ack."""

    def __init__(
        self,
        ha: HomeAssistantClient,
        mqtt: BridgeMqttClient,
        registry: EntityRegistry,
        options: AddonOptions,
    ) -> None:
        """Stores the collaborators needed to execute and acknowledge commands.

        Args:
            ha: Client used to call Home Assistant services.
            mqtt: Client used to publish command acks.
            registry: Registry used to validate court/door indexes and resolve door entity ids.
            options: Add-on options, including the configurable entity-name templates.
        """
        self._ha = ha
        self._mqtt = mqtt
        self._registry = registry
        self._options = options
        # Last executed action per door, for doors/{n}/state last_action.
        # The lock itself doesn't report this.
        self._last_door_actions: Dict[int, str] = {}

    def handle_court_command(self, index: int, payload: Dict[str, Any]) -> None:
        """Handles an incoming court command, executing it against HA and
        publishing an ack for it.

        Args:
            index: 0-based court index the command targets.
            payload: Decoded command payload from the courts/{index}/command topic.
        """
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
        """Handles an incoming door command by calling the matching HA lock
        service. Doors have no ack topic, unlike courts.

        Args:
            index: 0-based door index the command targets.
            payload: Decoded command payload from the doors/{index}/command topic.
        """
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
            self._last_door_actions[index] = action
        except HomeAssistantError:
            logger.exception("Door %s command %s failed", index, action)

    def last_door_action(self, index: int) -> Optional[str]:
        """Returns the last action executed for a door since startup.

        Args:
            index: 0-based door index.

        Returns:
            "open"/"close"/"unlock", or None.
        """
        return self._last_door_actions.get(index)

    def _control_court(self, index: int, payload: Dict[str, Any]) -> None:
        """Applies the on/off/brightness and mode/lux-target parts of a
        court command, if present in the payload.

        Args:
            index: 0-based court index being controlled.
            payload: Decoded court command payload.
        """
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

        # A LUX_LOOP activation command carries a one-time lux_target seed
        # value. Only present when the cloud actually wants to (re)seed it,
        # so a plain mode-only command for an already-configured court can
        # omit it.
        lux_target = payload.get("lux_target")
        if lux_target is not None:
            self._seed_lux_target(index, lux_target)

    def _set_mode(self, index: int, mode: str) -> None:
        """Sets a court's mode-select helper entity to the given mode.

        Args:
            index: 0-based court index.
            mode: Mode option to select.
        """
        entity_id = self._options.mode_select_entity_template.format(n=court_number(index))
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
        """Seeds a court's lux-reference helper entity with a one-time
        target value carried by a LUX_LOOP activation command.

        Args:
            index: 0-based court index.
            lux_target: Target lux value to seed.
        """
        entity_id = self._options.lux_reference_entity_template.format(n=court_number(index))
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
        """Triggers a court's calibration input_button, optionally with a
        power percentage.

        Args:
            index: 0-based court index.
            payload: Decoded court command payload; may carry calibration_power_pct.
        """
        calibration_power_pct = payload.get("calibration_power_pct")
        entity_id = self._options.calibration_trigger_entity_template.format(n=court_number(index))
        data = {"power_pct": calibration_power_pct} if calibration_power_pct is not None else {}
        self._ha.call_service("input_button", "press", entity_id, data)
        logger.info("Court %s: triggered calibration via %s", index, entity_id)

    def _publish_ack(self, index: int, command_id: Optional[str], status: str, error: Optional[str]) -> None:
        """Publishes a court command ack.

        Args:
            index: 0-based court index the command targeted.
            command_id: Id of the command being acknowledged, as sent by the caller.
            status: Outcome of the command, e.g. "executed" or "failed".
            error: Error message if the command failed, otherwise None.
        """
        self._mqtt.publish_court_ack(
            index,
            {
                "command_id": command_id,
                "status": status,
                "error": error,
                "timestamp": _utc_now_iso(),
            },
        )
