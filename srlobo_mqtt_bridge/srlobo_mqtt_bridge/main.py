"""Wires bootstrap, MQTT, HA REST/WebSocket, command execution, telemetry,
venue config, the offline schedule, the local dashboard and health
reporting together.
"""

import logging
import threading

from .bootstrap import SrLoboBootstrapClient
from .commands import CommandHandler
from .court_signal import CourtSignalPublisher
from .config import load_options
from .dashboard import DashboardProvisioner
from .device_registry import DisabledEntityEnabler
from .discovery import Discoverer
from .entity_registry import EntityRegistry
from .ha_client import HAStateListener, HomeAssistantClient
from .health import HealthReporter
from .logging_setup import setup_logging
from .mqtt_client import BridgeMqttClient
from .mqtt_discovery import publish_bridge_connectivity_discovery
from .persistence import DATA_DIR
from .rediscovery import RediscoveryCoordinator
from .schedule import OfflineScheduler
from .telemetry import TelemetryPublisher
from .venue_config import VenueConfigHandler

logger = logging.getLogger(__name__)

BOOTSTRAP_CACHE_PATH = f"{DATA_DIR}/bootstrap_cache.json"


def main() -> None:
    """Bridge entrypoint: loads config, bootstraps the installation, wires
    up every subsystem, and then blocks forever while they run on their
    own threads."""
    options = load_options()
    setup_logging(options.log_level)

    bootstrap = SrLoboBootstrapClient(
        api_url=options.srlobo_api_url,
        bootstrap_path=options.bootstrap_path,
        token=options.srlobo_token,
        cache_path=BOOTSTRAP_CACHE_PATH,
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

    # ADR-026: the bridge never drives the court lights; it writes each
    # court's signal and the club's local automation drives the lights.
    signals = CourtSignalPublisher(
        ha, registry, bootstrap.installation_id, entity_template=options.court_signal_entity_template
    )
    signals.load()

    commands = CommandHandler(ha, mqtt, registry, options, signals)
    mqtt.on_court_command(commands.handle_court_command)
    mqtt.on_door_command(commands.handle_door_command)

    scheduler = OfflineScheduler(ha, registry, bootstrap.installation_id, signals)
    scheduler.load()
    mqtt.on_schedule(scheduler.handle_schedule)
    mqtt.on_cloud_signal(scheduler.mark_cloud_alive)

    dashboard = DashboardProvisioner(ha, options)

    def on_venue_applied(config) -> None:
        dashboard.update_venue(config)
        signals.set_court_names(config.court_names)

    venue_config = VenueConfigHandler(ha, on_applied=on_venue_applied)
    mqtt.on_venue_config(venue_config.handle)

    telemetry = TelemetryPublisher(mqtt, ha, options, commands)
    discoverer = Discoverer(ha, registry, bootstrap)
    health = HealthReporter(mqtt, ha, options)

    enabler = DisabledEntityEnabler(ha, options.reenable_manufacturers)
    rediscovery = RediscoveryCoordinator(ha, discoverer, enabler, telemetry, dashboard, health)

    listener = HAStateListener(
        on_event=telemetry.on_state_changed,
        on_reconnect=lambda: (signals.republish(), rediscovery.on_reconnect()),
        on_device_registry_updated=rediscovery.on_device_registry_updated,
    )

    mqtt.connect()
    mqtt.loop_start()
    listener.start()
    health.start()
    scheduler.start()
    signals.start()
    publish_bridge_connectivity_discovery(bootstrap.installation_id)

    logger.info("automation_bridge running")
    threading.Event().wait()  # Runs forever; every subsystem above manages its own thread.


if __name__ == "__main__":
    main()
