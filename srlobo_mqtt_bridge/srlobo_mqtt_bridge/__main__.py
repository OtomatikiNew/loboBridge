import json
import logging
import os
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
import requests

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_API = "http://supervisor/core/api"


@dataclass
class MqttConfig:
    broker: str
    port: int = 8883
    username: Optional[str] = None
    password: Optional[str] = None
    client_id: Optional[str] = None
    base_topic: str = ""
    tls: bool = True
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    client_key: Optional[str] = None


@dataclass
class SrLoboEntity:
    index: int
    entity_id: str
    friendly_name: str
    device_class: str
    source_id: Optional[str] = None
    source_system: Optional[str] = None


@dataclass
class BootstrapConfig:
    installation_id: str
    source_system: Optional[str]
    mqtt: MqttConfig
    courts: List[SrLoboEntity] = field(default_factory=list)
    doors: List[SrLoboEntity] = field(default_factory=list)


class HomeAssistantClient:
    def __init__(self) -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is not available. Make sure config.yaml has homeassistant_api: true")
        self.base_url = SUPERVISOR_API.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def set_state(self, entity_id: str, state: str, attributes: Dict[str, Any]) -> None:
        url = f"{self.base_url}/states/{entity_id}"
        response = requests.post(url, headers=self.headers, json={"state": state, "attributes": attributes}, timeout=10)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Failed to update {entity_id}: {response.status_code} {response.text}")


class SrLoboBootstrapClient:
    def __init__(self, api_url: str, bootstrap_path: str, token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.bootstrap_path = bootstrap_path
        self.token = token

    def fetch(self) -> BootstrapConfig:
        url = f"{self.api_url}{self.bootstrap_path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        return self._parse(payload)

    def _parse(self, payload: Dict[str, Any]) -> BootstrapConfig:
        mqtt_payload = payload.get("mqtt", {})
        mqtt_config = MqttConfig(
            broker=mqtt_payload["broker"],
            port=int(mqtt_payload.get("port", 8883)),
            username=mqtt_payload.get("username"),
            password=mqtt_payload.get("password"),
            client_id=mqtt_payload.get("client_id"),
            base_topic=mqtt_payload["base_topic"].strip("/"),
            tls=bool(mqtt_payload.get("tls", True)),
            ca_cert=mqtt_payload.get("ca_cert"),
            client_cert=mqtt_payload.get("client_cert"),
            client_key=mqtt_payload.get("client_key"),
        )

        club = payload.get("club", {})
        source_system = club.get("source_system") or club.get("mode") or payload.get("source_system")
        installation_id = payload.get("installation_id") or club.get("uuid") or mqtt_config.base_topic.split("/")[-1]

        courts = [self._parse_entity(item, "pista", "light", source_system) for item in payload.get("courts", [])]
        doors = [self._parse_entity(item, "puerta", "door", source_system) for item in payload.get("doors", [])]

        return BootstrapConfig(
            installation_id=installation_id,
            source_system=source_system,
            mqtt=mqtt_config,
            courts=courts,
            doors=doors,
        )

    @staticmethod
    def _parse_entity(item: Dict[str, Any], prefix: str, default_device_class: str, source_system: Optional[str]) -> SrLoboEntity:
        index = int(item["index"])
        entity_suffix = item.get("entity_id") or f"{prefix}_{index}"
        entity_suffix = entity_suffix.replace("binary_sensor.", "")
        return SrLoboEntity(
            index=index,
            entity_id=f"binary_sensor.{entity_suffix}",
            friendly_name=item.get("friendly_name") or item.get("name") or f"{prefix.capitalize()} {index}",
            device_class=item.get("device_class") or default_device_class,
            source_id=str(item.get("source_id") or item.get("internal_id") or item.get("id") or ""),
            source_system=item.get("source_system") or source_system,
        )


class SrLoboBridge:
    def __init__(self, bootstrap: BootstrapConfig, ha: HomeAssistantClient) -> None:
        self.bootstrap = bootstrap
        self.ha = ha
        self.base_topic = bootstrap.mqtt.base_topic.strip("/")
        self.courts_by_index = {entity.index: entity for entity in bootstrap.courts}
        self.doors_by_index = {entity.index: entity for entity in bootstrap.doors}
        self.client = mqtt.Client(client_id=bootstrap.mqtt.client_id or f"srlobo-ha-{bootstrap.installation_id}")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        if bootstrap.mqtt.username:
            self.client.username_pw_set(bootstrap.mqtt.username, bootstrap.mqtt.password)
        if bootstrap.mqtt.tls:
            if bootstrap.mqtt.ca_cert or bootstrap.mqtt.client_cert or bootstrap.mqtt.client_key:
                self.client.tls_set(
                    ca_certs=bootstrap.mqtt.ca_cert,
                    certfile=bootstrap.mqtt.client_cert,
                    keyfile=bootstrap.mqtt.client_key,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT,
                )
            else:
                self.client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def start(self) -> None:
        self.create_initial_entities()
        logging.info("Connecting to MQTT broker %s:%s", self.bootstrap.mqtt.broker, self.bootstrap.mqtt.port)
        self.client.connect(self.bootstrap.mqtt.broker, self.bootstrap.mqtt.port, keepalive=60)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self.client.loop_forever()

    def create_initial_entities(self) -> None:
        for court in self.bootstrap.courts:
            self._update_court(court.index, {})
        for door in self.bootstrap.doors:
            self._update_door(door.index, {})

    def on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
        if rc != 0:
            logging.error("MQTT connection failed with result code %s", rc)
            return
        logging.info("Connected to MQTT")
        topics = [
            f"{self.base_topic}/config",
            f"{self.base_topic}/status",
            f"{self.base_topic}/courts/+/state",
            f"{self.base_topic}/courts/+/availability",
            f"{self.base_topic}/doors/+/state",
            f"{self.base_topic}/doors/+/availability",
        ]
        for topic in topics:
            client.subscribe(topic)
            logging.info("Subscribed to %s", topic)
        self._publish_availability(True)

    def on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        topic = message.topic
        try:
            payload = json.loads(message.payload.decode("utf-8") or "{}")
        except Exception:
            logging.exception("Invalid JSON payload on %s", topic)
            return

        try:
            self._dispatch(topic, payload)
        except Exception:
            logging.exception("Failed to process message on %s", topic)

    def _dispatch(self, topic: str, payload: Dict[str, Any]) -> None:
        relative = topic[len(self.base_topic):].strip("/")
        parts = relative.split("/") if relative else []
        if parts == ["status"]:
            self._update_status(payload)
        elif parts == ["config"]:
            logging.info("Received config message. Runtime config updates are currently logged only.")
        elif len(parts) == 3 and parts[0] == "courts" and parts[2] in ("state", "availability"):
            self._update_court(int(parts[1]), payload)
        elif len(parts) == 3 and parts[0] == "doors" and parts[2] in ("state", "availability"):
            self._update_door(int(parts[1]), payload)
        else:
            logging.debug("Ignoring topic %s", topic)

    def _update_court(self, index: int, payload: Dict[str, Any]) -> None:
        entity = self.courts_by_index.get(index)
        if not entity:
            logging.warning("Received court state for unknown court index %s", index)
            return
        state = self._court_state(payload)
        attributes = {
            "friendly_name": entity.friendly_name,
            "device_class": entity.device_class,
            "srlobo_index": index,
            "srlobo_entity_id": entity.entity_id,
            "source_system": entity.source_system,
            "source_id": entity.source_id,
            "brightness": int(payload.get("brightness_pct", payload.get("brightness", 0)) or 0),
            "brightness_pct": int(payload.get("brightness_pct", payload.get("brightness", 0)) or 0),
            "meta_state": payload.get("state", state),
            "reservation_active": bool(payload.get("reservation_active", payload.get("occupied", state == "on"))),
            "automatic_mode": payload.get("automatic_mode"),
            "manual_mode": payload.get("manual_mode"),
            "limited_mode": payload.get("limited_mode"),
            "min_level": payload.get("min_level"),
            "max_level": payload.get("max_level"),
            "online": payload.get("online", True),
            "updated_at": payload.get("updated_at"),
        }
        self.ha.set_state(entity.entity_id, state, attributes)
        logging.info("Updated %s to %s", entity.entity_id, state)

    def _update_door(self, index: int, payload: Dict[str, Any]) -> None:
        entity = self.doors_by_index.get(index)
        if not entity:
            logging.warning("Received door state for unknown door index %s", index)
            return
        state = self._door_state(payload)
        attributes = {
            "friendly_name": entity.friendly_name,
            "device_class": entity.device_class,
            "srlobo_index": index,
            "srlobo_entity_id": entity.entity_id,
            "source_system": entity.source_system,
            "source_id": entity.source_id,
            "locked": payload.get("locked"),
            "automatic_mode": payload.get("automatic_mode"),
            "online": payload.get("online", True),
            "updated_at": payload.get("updated_at"),
        }
        self.ha.set_state(entity.entity_id, state, attributes)
        logging.info("Updated %s to %s", entity.entity_id, state)

    def _update_status(self, payload: Dict[str, Any]) -> None:
        attributes = dict(payload)
        attributes["friendly_name"] = "SrLobo club status"
        self.ha.set_state("binary_sensor.srlobo_club_online", "on" if payload.get("online", True) else "off", attributes)

    @staticmethod
    def _court_state(payload: Dict[str, Any]) -> str:
        raw_state = str(payload.get("state", "")).lower()
        if raw_state in ("on", "off"):
            return raw_state
        if payload.get("occupied") is True or payload.get("reservation_active") is True:
            return "on"
        if int(payload.get("brightness_pct", payload.get("brightness", 0)) or 0) > 0:
            return "on"
        return "off"

    @staticmethod
    def _door_state(payload: Dict[str, Any]) -> str:
        raw_state = str(payload.get("state", "")).lower()
        if raw_state in ("open", "opened", "on", "true"):
            return "on"
        if raw_state in ("closed", "close", "off", "false"):
            return "off"
        if payload.get("open") is True:
            return "on"
        return "off"

    def _publish_availability(self, online: bool) -> None:
        payload = json.dumps({"addon_online": online, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.client.publish(f"{self.base_topic}/addon/availability", payload=payload, qos=1, retain=True)

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self._publish_availability(True)
            except Exception:
                logging.exception("Failed to publish heartbeat")
            time.sleep(60)


def load_options() -> Dict[str, Any]:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    options = load_options()
    setup_logging(options.get("log_level", "info"))
    token = options.get("srlobo_token")
    if not token:
        raise RuntimeError("Missing required option: srlobo_token")

    bootstrap = SrLoboBootstrapClient(
        api_url=options.get("srlobo_api_url", "https://srlobo.otomatiki.xyz"),
        bootstrap_path=options.get("bootstrap_path", "/api/homeassistant/bootstrap"),
        token=token,
    ).fetch()

    logging.info("Bootstrap loaded for installation %s with %s courts and %s doors", bootstrap.installation_id, len(bootstrap.courts), len(bootstrap.doors))
    ha = HomeAssistantClient()
    SrLoboBridge(bootstrap, ha).start()


if __name__ == "__main__":
    main()
