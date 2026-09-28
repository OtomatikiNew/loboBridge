"""MQTT connection and the topic contract from docs/architecture/mqtt-contract.md
(ADR-002, extended by ADR-007/013/015/018/019/020).

Credentials always come from the bootstrap response, never hardcoded and
never shared across installations (ADR-017). This module just uses whatever
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


class BridgeMqttClient:
    def __init__(self, mqtt_config: MqttConfig, installation_id: str) -> None:
        self._config = mqtt_config
        self.base_topic = mqtt_config.base_topic.strip("/")
        self._installation_id = installation_id
        self._client = mqtt.Client(client_id=mqtt_config.client_id or f"srlobo-ha-{installation_id}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        if mqtt_config.username:
            # Preferred path (ADR-017): username/password via an AWS IoT
            # custom authorizer. Never logged, see logging_setup.redact.
            self._client.username_pw_set(mqtt_config.username, mqtt_config.password)
        if mqtt_config.tls:
            if mqtt_config.ca_cert or mqtt_config.client_cert or mqtt_config.client_key:
                # X.509 fallback (ADR-017), only if backend sends certs
                # instead of username/password.
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
        self._schedule_handler: Optional[Callable[[Dict[str, Any]], None]] = None

    def on_court_command(self, handler: CourtCommandHandler) -> None:
        self._court_command_handler = handler

    def on_door_command(self, handler: DoorCommandHandler) -> None:
        self._door_command_handler = handler

    def on_venue_config(self, handler: VenueConfigHandler) -> None:
        self._venue_config_handler = handler

    def connect(self) -> None:
        logger.info("Connecting to MQTT broker %s:%s", self._config.broker, self._config.port)
        self._client.connect(self._config.broker, self._config.port, keepalive=60)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
        if rc != 0:
            logger.error("MQTT connection failed with result code %s", rc)
            return
        logger.info("Connected to MQTT broker")
        topics = [
            f"{self.base_topic}/courts/+/command",
            f"{self.base_topic}/doors/+/command",
            f"{self.base_topic}/venue/config",
        ]
        for topic in topics:
            client.subscribe(topic, qos=1)
            logger.info("Subscribed to %s", topic)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%s), paho will auto-reconnect", rc)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
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
        if len(parts) == 3 and parts[0] == "courts" and parts[2] == "command":
            if self._court_command_handler:
                self._court_command_handler(int(parts[1]), payload)
            return
        if len(parts) == 3 and parts[0] == "doors" and parts[2] == "command":
            if self._door_command_handler:
                self._door_command_handler(int(parts[1]), payload)
            return
        logger.debug("Ignoring message on unrecognized topic %s", topic)

    # --- publish helpers, matching docs/architecture/mqtt-contract.md exactly ---

    def _publish(self, suffix: str, payload: Dict[str, Any], retain: bool, qos: int = 1) -> None:
        topic = f"{self.base_topic}/{suffix}"
        self._client.publish(topic, payload=json.dumps(payload), qos=qos, retain=retain)

    def publish_court_telemetry(self, index: int, payload: Dict[str, Any]) -> None:
        self._publish(f"courts/{index}/telemetry", payload, retain=True)

    def publish_door_telemetry(self, index: int, payload: Dict[str, Any]) -> None:
        self._publish(f"doors/{index}/telemetry", payload, retain=True)

    def publish_court_state(self, index: int, payload: Dict[str, Any]) -> None:
        self._publish(f"courts/{index}/state", payload, retain=True)

    def publish_door_state(self, index: int, payload: Dict[str, Any]) -> None:
        self._publish(f"doors/{index}/state", payload, retain=True)

    def publish_court_ack(self, index: int, payload: Dict[str, Any]) -> None:
        self._publish(f"courts/{index}/ack", payload, retain=False)

    def publish_bridge_status(self, payload: Dict[str, Any]) -> None:
        self._publish("bridge/status", payload, retain=True)
