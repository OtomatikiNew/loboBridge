"""Wires bootstrap, MQTT, HA REST/WebSocket, command execution, telemetry,
venue config and health reporting together. See docs/DEVELOPER_NOTES.md and
docs/architecture/local-automation-layer.md (srlobo-2.0 repo) for the design
this implements.
"""

import logging
import threading

from .bootstrap import SrLoboBootstrapClient
from .commands import CommandHandler
from .config import load_options
from .discovery import Discoverer
from .entity_registry import EntityRegistry
from .ha_client import HAStateListener, HomeAssistantClient
from .health import HealthReporter
from .logging_setup import setup_logging
from .mqtt_client import BridgeMqttClient
from .mqtt_discovery import publish_bridge_connectivity_discovery
from .telemetry import TelemetryPublisher
from .venue_config import VenueConfigHandler

logger = logging.getLogger(__name__)


def main() -> None:
    options = load_options()
    setup_logging(options.log_level)

    bootstrap = SrLoboBootstrapClient(
        api_url=options.srlobo_api_url,
        bootstrap_path=options.bootstrap_path,
        token=options.srlobo_token,
    ).fetch()
    logger.info(
        "Bootstrap loaded for installation %s with %s courts and %s doors",
        bootstrap.installation_id,
        len(bootstrap.courts),
        len(bootstrap.doors),
    )

    registry = EntityRegistry(bootstrap)
    ha = HomeAssistantClient()
    mqtt = BridgeMqttClient(bootstrap.mqtt, bootstrap.installation_id)

    commands = CommandHandler(ha, mqtt, registry, options)
    mqtt.on_court_command(commands.handle_court_command)
    mqtt.on_door_command(commands.handle_door_command)

    venue_config = VenueConfigHandler(ha)
    mqtt.on_venue_config(venue_config.handle)

    telemetry = TelemetryPublisher(mqtt, ha, options)
    discoverer = Discoverer(ha, registry, bootstrap)
    health = HealthReporter(mqtt, ha, options)

    def on_reconnect() -> None:
        health.record_reconnect()
        discovery_state = discoverer.discover_all()
        telemetry.on_rediscover(discovery_state)

    listener = HAStateListener(on_event=telemetry.on_state_changed, on_reconnect=on_reconnect)

    mqtt.connect()
    mqtt.loop_start()
    listener.start()
    health.start()
    publish_bridge_connectivity_discovery(bootstrap.installation_id)

    logger.info("automation_bridge running")
    threading.Event().wait()  # Runs forever; every subsystem above manages its own thread.


if __name__ == "__main__":
    main()
