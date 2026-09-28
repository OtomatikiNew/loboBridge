"""Home Assistant access, both the Supervisor REST API and the WebSocket
event stream.

Goes through http://supervisor/core (or http://supervisor/supervisor for
add-on/Supervisor info) with SUPERVISOR_TOKEN, never homeassistant.local
(lessons-from-lobobrain.md #2). Every HTTP call and WS op has a timeout
(lessons-from-lobobrain.md #6).
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
    pass


def _supervisor_token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise HomeAssistantError(
            "SUPERVISOR_TOKEN is not available. Make sure config.yaml has homeassistant_api: true"
        )
    return token


class HomeAssistantClient:
    """Synchronous REST access to HA Core and the Supervisor, plus service calls."""

    def __init__(self) -> None:
        self._token = _supervisor_token()
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Returns the entity's state dict, or None if it doesn't exist (404).
        Read-only, don't use this to gate a write that can create the entity
        itself (lessons-from-lobobrain.md #3)."""
        url = f"{CORE_API}/states/{entity_id}"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def get_states(self) -> List[Dict[str, Any]]:
        url = f"{CORE_API}/states"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: List[Dict[str, Any]] = response.json()
        return result

    def call_service(
        self, domain: str, service: str, entity_id: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Calls a real HA service (light.turn_on etc). This is how we control
        real devices, not by writing synthetic state via POST /states."""
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
        doesn't survive a Core restart (lessons-from-lobobrain.md #4)."""
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
        url = f"{CORE_API}/config"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def get_supervisor_info(self) -> Dict[str, Any]:
        url = f"{SUPERVISOR_API}/info"
        response = requests.get(url, headers=self._headers, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        result: Dict[str, Any] = response.json()
        return result

    def set_core_config(self, latitude: float, longitude: float, time_zone: str, language: str) -> None:
        """Sets HA's own core location/timezone/language config (ADR-015).
        The ADR leaves whether to do this as an implementation choice, doing
        it removes one more manual step per install."""
        url = f"{CORE_API}/config/core/config"
        body = {"latitude": latitude, "longitude": longitude, "time_zone": time_zone, "language": language}
        response = requests.post(url, headers=self._headers, json=body, timeout=HTTP_TIMEOUT_S)
        if response.status_code not in (200, 201):
            raise HomeAssistantError(f"Failed to set HA core config: {response.status_code} {response.text}")


OnEventCallback = Callable[[Dict[str, Any]], None]


class HAStateListener:
    """Persistent WebSocket connection to HA Core, subscribed to
    state_changed.

    Calls on_reconnect first on every (re)connect (callers use this to
    re-run entity discovery per ADR-007 §2), then dispatches every
    state_changed event to on_event.
    """

    def __init__(self, on_event: OnEventCallback, on_reconnect: Callable[[], None]) -> None:
        self._token = _supervisor_token()
        self._on_event = on_event
        self._on_reconnect = on_reconnect
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:
                logger.exception("HA WebSocket connection failed, reconnecting")
            if self._stop.is_set():
                return
            time.sleep(WS_RECONNECT_DELAY_S)

    def _run_once(self) -> None:
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
                    if event.get("event_type") == "state_changed":
                        self._on_event(event.get("data", {}))
        finally:
            ws.close()

    def _authenticate(self, ws: "websocket.WebSocket") -> None:
        first = json.loads(ws.recv())
        if first.get("type") != "auth_required":
            raise HomeAssistantError(f"Unexpected first WebSocket message: {first}")
        ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise HomeAssistantError("HA WebSocket authentication failed")

    def _subscribe(self, ws: "websocket.WebSocket") -> None:
        ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        result = json.loads(ws.recv())
        if not result.get("success"):
            raise HomeAssistantError(f"Failed to subscribe to state_changed: {result}")
