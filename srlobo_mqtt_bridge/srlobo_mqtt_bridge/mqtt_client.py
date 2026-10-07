"""MQTT connection and the topic contract.

Credentials always come from the bootstrap response, never hardcoded and
never shared across installations. This module just uses whatever
MqttConfig it's given.
"""

import json
import logging
import ssl
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

from .config import MqttConfig

logger = logging.getLogger(__name__)

CourtCommandHandler = Callable[[int, Dict[str, Any]], None]
DoorCommandHandler = Callable[[int, Dict[str, Any]], None]
VenueConfigHandler = Callable[[Dict[str, Any]], None]
ScheduleHandler = Callable[[Dict[str, Any]], None]
CloudSignalHandler = Callable[[], None]


class BridgeMqttClient:
    """Wraps the paho MQTT client with SR.Lobo Cloud's topic contract:
    court/door command subscriptions and telemetry/state/ack/status
    publishing."""

    def __init__(self, mqtt_config: MqttConfig, installation_id: str) -> None:
        """Configures the underlying paho client's auth/TLS and callbacks
        from the given per-installation MQTT config.

        Args:
            mqtt_config: Per-installation MQTT connection details from bootstrap.
            installation_id: This installation's id, used to derive a default client id.
        """
        self._config = mqtt_config
        self.base_topic = mqtt_config.base_topic.strip("/")
        self._installation_id = installation_id
        self._client = mqtt.Client(client_id=mqtt_config.client_id or f"srlobo-ha-{installation_id}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        if mqtt_config.username:
            # Preferred path: username/password via an AWS IoT custom
            # authorizer. Never logged, see logging_setup.redact.
            self._client.username_pw_set(mqtt_config.username, mqtt_config.password)
        if mqtt_config.tls:
            if mqtt_config.ca_cert or mqtt_config.client_cert or mqtt_config.client_key:
                # X.509 fallback, only if backend sends certs instead of
                # username/password.
                self._client.tls_set(
                    ca_certs=mqtt_config.ca_cert,
                    certfile=mqtt_config.client_cert,
                    keyfile=mqtt_config.client_key,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT,
                )
            else:
                self._client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)

        self._court_command_handler: Optional[CourtCommandHandler] = None
        self._door_command_handler: Optional[DoorCommandHandler] = None
        self._venue_config_handler: Optional[VenueConfigHandler] = None
        self._schedule_handler: Optional[ScheduleHandler] = None
        self._cloud_signal_handler: Optional[CloudSignalHandler] = None

    def on_court_command(self, handler: CourtCommandHandler) -> None:
        """Registers the callback invoked for incoming court commands.

        Args:
            handler: Called with (court_index, payload) for each court command.
        """
        self._court_command_handler = handler

    def on_door_command(self, handler: DoorCommandHandler) -> None:
        """Registers the callback invoked for incoming door commands.

        Args:
            handler: Called with (door_index, payload) for each door command.
        """
        self._door_command_handler = handler

    def on_venue_config(self, handler: VenueConfigHandler) -> None:
        """Registers the callback invoked for incoming venue/config messages.

        Args:
            handler: Called with the decoded venue/config payload.
        """
        self._venue_config_handler = handler

    def on_schedule(self, handler: ScheduleHandler) -> None:
        """Registers the callback for the retained offline schedule (ADR-013).

        Args:
            handler: Called with the decoded schedule payload.
        """
        self._schedule_handler = handler

    def on_cloud_signal(self, handler: CloudSignalHandler) -> None:
        """Registers the callback for messages that show the cloud is up:
        the heartbeat and court/door commands. Retained topics don't count,
        the broker replays them on resubscribe even if the backend is down.

        Args:
            handler: Called with no arguments.
        """
        self._cloud_signal_handler = handler

    def connect(self) -> None:
        """Starts connecting without blocking, so the bridge still starts
        when the broker is unreachable. paho retries once loop_start() runs."""
        logger.info("Connecting to MQTT broker %s:%s", self._config.broker, self._config.port)
        self._client.connect_async(self._config.broker, self._config.port, keepalive=60)

    def loop_start(self) -> None:
        """Starts paho's background network loop thread."""
        self._client.loop_start()

    def loop_forever(self) -> None:
        """Runs paho's network loop on the calling thread, blocking forever."""
        self._client.loop_forever()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
        """paho callback: subscribes to the command/config topics once
        connected.

        Args:
            client: The paho client instance (same as self._client).
            userdata: Unused paho userdata.
            flags: Unused paho connect flags.
            rc: Connection result code; subscribes only if 0 (success).
        """
        if rc != 0:
            logger.error("MQTT connection failed with result code %s", rc)
            return
        logger.info("Connected to MQTT broker")
        topics = [
            f"{self.base_topic}/courts/+/command",
            f"{self.base_topic}/doors/+/command",
            f"{self.base_topic}/venue/config",
            f"{self.base_topic}/schedule",
            f"{self.base_topic}/cloud/heartbeat",
        ]
        for topic in topics:
            client.subscribe(topic, qos=1)
            logger.info("Subscribed to %s", topic)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """paho callback: logs unexpected disconnects (paho auto-reconnects
        on its own).

        Args:
            client: Unused paho client instance.
            userdata: Unused paho userdata.
            rc: Disconnect result code; a warning is logged if non-zero.
        """
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%s), paho will auto-reconnect", rc)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        """paho callback: decodes an incoming message's JSON payload and
        dispatches it by topic.

        Args:
            client: Unused paho client instance.
            userdata: Unused paho userdata.
            message: The received MQTT message.
        """
        topic = message.topic
        try:
            payload = json.loads(message.payload.decode("utf-8") or "{}")
        except Exception:
            logger.exception("Invalid JSON payload on %s", topic)
            return
        try:
            self._dispatch(topic, payload)
        except Exception:
            logger.exception("Failed to process message on %s", topic)

    def _dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        """Routes a decoded message to the registered handler matching its
        topic shape.

        Args:
            topic: Full MQTT topic the message arrived on.
            payload: Decoded JSON payload.
        """
        relative = topic[len(self.base_topic):].strip("/")
        parts = relative.split("/") if relative else []

        if parts == ["venue", "config"]:
            if self._venue_config_handler:
                self._venue_config_handler(payload)
            return
        if parts == ["schedule"]:
            if self._schedule_handler:
                self._schedule_handler(payload)
            return
        if parts == ["cloud", "heartbeat"]:
            self._signal_cloud_alive()
            return
        if len(parts) == 3 and parts[0] == "courts" and parts[2] == "command":
            self._signal_cloud_alive()
            if self._court_command_handler:
                self._court_command_handler(int(parts[1]), payload)
            return
        if len(parts) == 3 and parts[0] == "doors" and parts[2] == "command":
            self._signal_cloud_alive()
            if self._door_command_handler:
                self._door_command_handler(int(parts[1]), payload)
            return
        logger.debug("Ignoring message on unrecognized topic %s", topic)

    def _signal_cloud_alive(self) -> None:
        """Calls the cloud-signal handler, if set."""
        if self._cloud_signal_handler:
            self._cloud_signal_handler()

    # --- publish helpers ---

    def _publish(self, suffix: str, payload: Dict[str, Any], retain: bool, qos: int = 1) -> None:
        """Serializes and publishes a payload under base_topic/suffix.

        Args:
            suffix: Topic path appended to base_topic.
            payload: JSON-serializable payload to publish.
            retain: Whether the broker should retain this message.
            qos: MQTT QoS level to publish with.
        """
        topic = f"{self.base_topic}/{suffix}"
        self._client.publish(topic, payload=json.dumps(payload), qos=qos, retain=retain)

    def publish_court_telemetry(self, index: int, payload: Dict[str, Any]) -> None:
        """Publishes a court's raw telemetry payload.

        Args:
            index: 0-based court index.
            payload: Telemetry payload to publish.
        """
        self._publish(f"courts/{index}/telemetry", payload, retain=True)

    def publish_door_telemetry(self, index: int, payload: Dict[str, Any]) -> None:
        """Publishes a door's raw telemetry payload.

        Args:
            index: 0-based door index.
            payload: Telemetry payload to publish.
        """
        self._publish(f"doors/{index}/telemetry", payload, retain=True)

    def publish_court_state(self, index: int, payload: Dict[str, Any]) -> None:
        """Publishes a court's state payload.

        Args:
            index: 0-based court index.
            payload: State payload to publish.
        """
        self._publish(f"courts/{index}/state", payload, retain=True)

    def publish_door_state(self, index: int, payload: Dict[str, Any]) -> None:
        """Publishes a door's state payload.

        Args:
            index: 0-based door index.
            payload: State payload to publish.
        """
        self._publish(f"doors/{index}/state", payload, retain=True)

    def publish_court_ack(self, index: int, payload: Dict[str, Any]) -> None:
        """Publishes a court command's ack payload.

        Args:
            index: 0-based court index.
            payload: Ack payload to publish.
        """
        self._publish(f"courts/{index}/ack", payload, retain=False)

    def publish_bridge_status(self, payload: Dict[str, Any]) -> None:
        """Publishes the bridge's own health status payload.

        Args:
            payload: Health status payload to publish.
        """
        self._publish("bridge/status", payload, retain=True)
