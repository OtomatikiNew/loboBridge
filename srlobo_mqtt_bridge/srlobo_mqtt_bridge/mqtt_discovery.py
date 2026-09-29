"""Home Assistant native MQTT Discovery, on the club's own LOCAL Home
Assistant MQTT broker. That's a different broker and a different purpose
than mqtt_client.py's connection to SR.Lobo Cloud.

Scope decision, since nothing else specifies one: publish exactly one
entity, a "SR.Lobo Bridge" connectivity binary_sensor, so an installer or
on-site technician can see whether the bridge add-on itself is alive and
connected, directly from HA's own dashboard, without needing SR.Lobo Cloud
access. Deliberately not mirroring court/door state into synthetic sensors
here. The real underlying entities (the light group, the lock) are already
visible in HA's own UI, since the bridge controls them directly rather than
creating them, so duplicating that into fake sensors would be redundant.

Assumes the club's Home Assistant installation runs a local MQTT broker
(almost always the official Mosquitto add-on). config.yaml declares
`services: [mqtt:want]` so HA Supervisor auto-injects this add-on's
connection details (MQTT_HOST etc.) as environment variables if one exists.
"want" (not "need") means the add-on must keep working if it's absent: if no
local broker is configured, this entire module quietly no-ops rather than
failing bridge startup.
"""

import json
import logging
import os
from typing import Dict, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"


def _local_broker_env() -> Optional[Dict[str, str]]:
    host = os.environ.get("MQTT_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": os.environ.get("MQTT_PORT", "1883"),
        "username": os.environ.get("MQTT_USERNAME", ""),
        "password": os.environ.get("MQTT_PASSWORD", ""),
    }


def publish_bridge_connectivity_discovery(installation_id: str) -> None:
    """Connects to the local HA MQTT broker (if the `mqtt:want` service is
    available) and publishes a retained MQTT Discovery config plus an
    initial "on" state for the bridge connectivity sensor, with a Last Will
    Testament that flips it to "off" if the add-on's process dies without a
    clean shutdown. No-ops entirely if no local broker is configured, and
    never raises, since this is a nice-to-have, not connection-critical
    the way bootstrap/mqtt_client.py is.
    """
    env = _local_broker_env()
    if env is None:
        logger.info("No local MQTT broker service available (mqtt:want), skipping HA MQTT Discovery")
        return

    object_id = f"srlobo_{installation_id}_bridge"
    state_topic = f"{DISCOVERY_PREFIX}/binary_sensor/{object_id}/state"
    config_topic = f"{DISCOVERY_PREFIX}/binary_sensor/{object_id}/config"
    config_payload = {
        "name": "SR.Lobo Bridge",
        "unique_id": object_id,
        "device_class": "connectivity",
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": {
            "identifiers": [f"srlobo_{installation_id}"],
            "name": "SR.Lobo automation_bridge",
            "manufacturer": "Otomatiki",
            "model": "srlobo_mqtt_bridge",
        },
    }

    try:
        client = mqtt.Client(client_id=f"srlobo-discovery-{installation_id}")
        if env["username"]:
            client.username_pw_set(env["username"], env["password"])
        client.will_set(state_topic, payload="OFF", qos=1, retain=True)
        client.connect(env["host"], int(env["port"]), keepalive=60)
        client.loop_start()
        client.publish(config_topic, json.dumps(config_payload), qos=1, retain=True)
        client.publish(state_topic, "ON", qos=1, retain=True)
        logger.info("Published HA MQTT Discovery config for bridge connectivity sensor")
    except Exception:
        logger.warning("Could not publish HA MQTT Discovery to local broker (continuing without it)", exc_info=True)
