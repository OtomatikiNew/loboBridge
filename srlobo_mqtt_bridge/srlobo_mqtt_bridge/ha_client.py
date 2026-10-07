"""Home Assistant access, both the Supervisor REST API and the WebSocket
event stream.

Goes through http://supervisor/core (or http://supervisor/supervisor for
add-on/Supervisor info) with SUPERVISOR_TOKEN, never homeassistant.local.
Every HTTP call and WS op has a timeout.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket  # from websocket-client

logger = logging.getLogger(__name__)

CORE_API = "http://supervisor/core/api"
SUPERVISOR_API = "http://supervisor/supervisor"
CORE_WS_URL = "ws://supervisor/core/websocket"

HTTP_TIMEOUT_S = 10
WS_CONNECT_TIMEOUT_S = 10
WS_RECONNECT_DELAY_S = 10


class HomeAssistantError(RuntimeError):
    """Raised when a Home Assistant REST or WebSocket operation fails."""


def _supervisor_token() -> str:
    """Reads the Supervisor-issued auth token from the environment.

    Returns:
        The supervisor token.

    Raises:
        HomeAssistantError: If SUPERVISOR_TOKEN is not set.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise HomeAssistantError(
            "SUPERVISOR_TOKEN is not available. Make sure config.yaml has homeassistant_api: true"
        )
    return token


def _ws_authenticate(ws: "websocket.WebSocket", token: str) -> None:
    """Completes the HA WebSocket auth handshake.

    Args:
        ws: Open WebSocket connection to authenticate on.
        token: Supervisor token to authenticate with.

    Raises:
        HomeAssistantError: If the handshake sequence or auth itself fails.
    """
    first = json.loads(ws.recv())
    if first.get("type") != "auth_required":
        raise HomeAssistantError(f"Unexpected first WebSocket message: {first}")
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth_result = json.loads(ws.recv())
    if auth_result.get("type") != "auth_ok":
        raise HomeAssistantError("HA WebSocket authentication failed")


class HomeAssistantClient:
    """Synchronous REST access to HA Core and the Supervisor, plus service calls."""

    def __init__(self) -> None:
        """Reads the supervisor token and builds the auth headers used by
        every request this client makes.

        Raises:
            HomeAssistantError: If SUPERVISOR_TOKEN is not set.
        """
        self._token = _supervisor_token()
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Reads an entity's current state.

        Read-only; don't use this to gate a write that can create the
        entity itself.

        Args:
            entity_id: HA entity id to look up.

        Returns:
            The entity's state dict, or None if it doesn't exist (404).
        """
        url = f"{CORE_API}/states/{entity_id}"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def get_states(self) -> List[Dict[str, Any]]:
        """Reads the full current state of every HA entity.

        Returns:
            A list of entity state dicts.
        """
        url = f"{CORE_API}/states"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: List[Dict[str, Any]] = response.json()
        return result

    def call_service(
        self, domain: str, service: str, entity_id: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Calls a real HA service (light.turn_on etc). This is how we control
        real devices, not by writing synthetic state via POST /states.

        Args:
            domain: HA service domain, e.g. "light".
            service: Service to call within the domain, e.g. "turn_on".
            entity_id: Entity id the service call targets.
            data: Extra service data to send alongside entity_id.

        Raises:
            HomeAssistantError: If the service call does not succeed.
        """
        url = f"{CORE_API}/services/{domain}/{service}"
        body = {"entity_id": entity_id, **(data or {})}
        response = requests.post(url, headers=self._headers, json=body, timeout=HTTP_TIMEOUT_S)
        if response.status_code not in (200, 201):
            raise HomeAssistantError(
                f"Service call {domain}.{service} on {entity_id} failed: "
                f"{response.status_code} {response.text}"
            )

    def set_state(self, entity_id: str, state: str, attributes: Dict[str, Any]) -> None:
        """Writes synthetic state via POST /states. Only for the bridge's own
        diagnostic entities (sensor.lobobridge_mode etc), not as a stand-in
        for a real device's source of truth. Anything written this way
        doesn't survive a Core restart.

        Args:
            entity_id: Entity id to write state for.
            state: New state value.
            attributes: Attributes dict to attach to the state.

        Raises:
            HomeAssistantError: If the write does not succeed.
        """
        url = f"{CORE_API}/states/{entity_id}"
        response = requests.post(
            url,
            headers=self._headers,
            json={"state": state, "attributes": attributes},
            timeout=HTTP_TIMEOUT_S,
        )
        if response.status_code not in (200, 201):
            raise HomeAssistantError(f"Failed to update {entity_id}: {response.status_code} {response.text}")

    def get_core_info(self) -> Dict[str, Any]:
        """Reads HA Core's own config (version, location, timezone etc).

        Returns:
            The core config dict.
        """
        url = f"{CORE_API}/config"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def get_supervisor_info(self) -> Dict[str, Any]:
        """Reads the Supervisor's own info (version, backup status etc).

        Returns:
            The supervisor info dict.
        """
        url = f"{SUPERVISOR_API}/info"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def set_core_config(self, latitude: float, longitude: float, time_zone: str, language: str) -> None:
        """Sets HA's own core location/timezone/language config, removing
        one manual step per install.

        Args:
            latitude: Venue latitude.
            longitude: Venue longitude.
            time_zone: Venue IANA timezone name.
            language: Venue UI language code.

        Raises:
            HomeAssistantError: If the write does not succeed.
        """
        url = f"{CORE_API}/config/core/config"
        body = {"latitude": latitude, "longitude": longitude, "time_zone": time_zone, "language": language}
        response = requests.post(url, headers=self._headers, json=body, timeout=HTTP_TIMEOUT_S)
        if response.status_code not in (200, 201):
            raise HomeAssistantError(f"Failed to set HA core config: {response.status_code} {response.text}")

    def ws_command(self, message: Dict[str, Any]) -> Any:
        """Runs one WebSocket command on a new connection, for APIs with no
        REST equivalent (lovelace/*, registries). Kept off the listener's
        connection so a slow command doesn't hold up events.

        Args:
            message: Command without "id", e.g. {"type": "lovelace/dashboards/list"}.

        Returns:
            The command's result.

        Raises:
            HomeAssistantError: On auth failure or success=false.
        """
        ws = websocket.create_connection(CORE_WS_URL, timeout=WS_CONNECT_TIMEOUT_S)
        try:
            _ws_authenticate(ws, self._token)
            ws.send(json.dumps({"id": 1, **message}))
            while True:
                reply = json.loads(ws.recv())
                if reply.get("id") == 1 and reply.get("type") == "result":
                    break
        except websocket.WebSocketException as exc:
            raise HomeAssistantError(f"WebSocket command {message.get('type')} failed: {exc}") from exc
        finally:
            ws.close()
        if not reply.get("success"):
            raise HomeAssistantError(f"WebSocket command {message.get('type')} failed: {reply.get('error')}")
        return reply.get("result")

    def list_device_registry(self) -> List[Dict[str, Any]]:
        """Returns:
            HA's device registry entries.
        """
        return self.ws_command({"type": "config/device_registry/list"}) or []

    def list_entity_registry(self) -> List[Dict[str, Any]]:
        """Returns:
            HA's entity registry entries, disabled ones included.
        """
        return self.ws_command({"type": "config/entity_registry/list"}) or []

    def enable_entity(self, entity_id: str) -> None:
        """Enables a disabled entity.

        Args:
            entity_id: Entity to enable.
        """
        self.ws_command({"type": "config/entity_registry/update", "entity_id": entity_id, "disabled_by": None})

    def reload_config_entry(self, entry_id: str) -> None:
        """Reloads a config entry so newly enabled entities start reporting.

        Args:
            entry_id: Config entry to reload.

        Raises:
            HomeAssistantError: If the reload fails.
        """
        url = f"{CORE_API}/config/config_entries/entry/{entry_id}/reload"
        response = requests.post(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        if response.status_code not in (200, 201):
            raise HomeAssistantError(f"Reloading config entry failed: {response.status_code} {response.text}")


OnEventCallback = Callable[[Dict[str, Any]], None]


class HAStateListener:
    """Persistent WebSocket connection to HA Core, subscribed to
    state_changed.

    Calls on_reconnect first on every (re)connect (callers use this to
    re-run entity discovery per ADR-007 §2), then dispatches every
    state_changed event to on_event.
    """

    def __init__(
        self,
        on_event: OnEventCallback,
        on_reconnect: Callable[[], None],
        on_device_registry_updated: Optional[OnEventCallback] = None,
    ) -> None:
        """Stores the callbacks to invoke on connect and on each event.

        Args:
            on_event: Called with the event data of every state_changed event.
            on_reconnect: Called right after each successful (re)connect, before any events are dispatched.
            on_device_registry_updated: Called with the data of every
                device_registry_updated event (ADR-007, new devices).
        """
        self._token = _supervisor_token()
        self._on_event = on_event
        self._on_reconnect = on_reconnect
        self._on_device_registry_updated = on_device_registry_updated
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts the listener's background connection thread."""
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the background connection thread to stop."""
        self._stop.set()

    def _run_forever(self) -> None:
        """Keeps the WebSocket connection alive, reconnecting with a fixed
        delay after any failure, until stopped."""
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:
                logger.exception("HA WebSocket connection failed, reconnecting")
            if self._stop.is_set():
                return
            time.sleep(WS_RECONNECT_DELAY_S)

    def _run_once(self) -> None:
        """Opens one WebSocket connection, authenticates, subscribes to
        state_changed, and dispatches events until the connection drops or
        a stop is requested."""
        ws = websocket.create_connection(CORE_WS_URL, timeout=WS_CONNECT_TIMEOUT_S)
        try:
            self._authenticate(ws)
            self._subscribe(ws)
            self._on_reconnect()
            logger.info("HA WebSocket connected and subscribed to state_changed")
            while not self._stop.is_set():
                raw = ws.recv()
                if not raw:
                    continue
                message = json.loads(raw)
                if message.get("type") == "event":
                    event = message.get("event", {})
                    event_type = event.get("event_type")
                    if event_type == "state_changed":
                        self._on_event(event.get("data", {}))
                    elif event_type == "device_registry_updated" and self._on_device_registry_updated:
                        self._on_device_registry_updated(event.get("data", {}))
        finally:
            ws.close()

    def _authenticate(self, ws: "websocket.WebSocket") -> None:
        """Completes the HA WebSocket auth handshake.

        Args:
            ws: Open WebSocket connection to authenticate on.

        Raises:
            HomeAssistantError: If the handshake sequence or auth itself fails.
        """
        _ws_authenticate(ws, self._token)

    def _subscribe(self, ws: "websocket.WebSocket") -> None:
        """Subscribes to state_changed and, if a handler is set,
        device_registry_updated. Events that arrive before both are
        confirmed are dropped; discovery runs right after anyway.

        Args:
            ws: Open, authenticated WebSocket connection.

        Raises:
            HomeAssistantError: If a subscribe request is not acknowledged as successful.
        """
        subscriptions = {1: "state_changed"}
        if self._on_device_registry_updated:
            subscriptions[2] = "device_registry_updated"
        for sub_id, event_type in subscriptions.items():
            ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": event_type}))

        pending = set(subscriptions)
        while pending:
            message = json.loads(ws.recv())
            if message.get("type") != "result" or message.get("id") not in pending:
                continue
            if not message.get("success"):
                raise HomeAssistantError(f"Failed to subscribe to {subscriptions[message['id']]}: {message}")
            pending.discard(message["id"])
