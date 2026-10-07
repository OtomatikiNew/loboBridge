"""SR.Lobo dashboard in the club's HA (replaces loboBrain's web UI).

Shows each court's light group, mode and lux reference, the door locks and
the bridge mode sensor. Works without the cloud.

Only the dashboard at DASHBOARD_URL_PATH is ever written. It's regenerated
when courts, doors or names change, so manual edits to it get overwritten.
"""

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional, Set

from .config import AddonOptions
from .discovery import DiscoveryState
from .entity_registry import court_helper_entity_id, court_number
from .ha_client import HomeAssistantClient
from .schedule import MODE_ENTITY_ID
from .venue_config import VenueConfig

logger = logging.getLogger(__name__)

# HA requires a hyphen in dashboard url paths.
DASHBOARD_URL_PATH = "srlobo-club"
DASHBOARD_TITLE = "SR.Lobo"
DASHBOARD_ICON = "mdi:tennis"


def build_dashboard_config(
    court_indexes: Iterable[int],
    door_entities: Dict[int, str],
    existing_entities: Set[str],
    options: AddonOptions,
    venue: Optional[VenueConfig] = None,
) -> Dict[str, Any]:
    """Builds the Lovelace config. Entities missing from HA are skipped.

    Args:
        court_indexes: 0-based court indexes.
        door_entities: Door index to lock entity id.
        existing_entities: Entity ids present in HA.
        options: Add-on options (entity name templates).
        venue: Latest venue/config, for club and court names.

    Returns:
        The dashboard config.
    """
    court_names = venue.court_names if venue else {}
    cards: List[Dict[str, Any]] = []

    for index in sorted(court_indexes):
        number = court_number(index)
        helper = court_helper_entity_id(index)
        name = court_names.get(index) or f"Pista {number}"
        if helper not in existing_entities:
            continue
        court_cards: List[Dict[str, Any]] = [
            {"type": "tile", "entity": helper, "name": name, "features": [{"type": "light-brightness"}]}
        ]
        helpers = [
            template.format(n=number)
            for template in (options.mode_select_entity_template, options.lux_reference_entity_template)
        ]
        present = [entity_id for entity_id in helpers if entity_id in existing_entities]
        if present:
            court_cards.append({"type": "entities", "entities": present})
        cards.append({"type": "vertical-stack", "cards": court_cards})

    for index in sorted(door_entities):
        entity_id = door_entities[index]
        if entity_id in existing_entities:
            cards.append({"type": "tile", "entity": entity_id})

    cards.append({"type": "entities", "title": "SR.Lobo Bridge", "entities": [MODE_ENTITY_ID]})

    return {
        "title": venue.club_name if venue else DASHBOARD_TITLE,
        "views": [{"title": "Pistas", "path": "pistas", "icon": DASHBOARD_ICON, "cards": cards}],
    }


def missing_court_entities(court_indexes: Iterable[int], existing_entities: Set[str], options: AddonOptions) -> List[str]:
    """Blueprint entities (light group, mode, lux reference, calibration)
    that are missing from HA.

    Args:
        court_indexes: 0-based court indexes.
        existing_entities: Entity ids present in HA.
        options: Add-on options (entity name templates).

    Returns:
        Missing entity ids.
    """
    missing: List[str] = []
    for index in sorted(court_indexes):
        number = court_number(index)
        expected = [
            court_helper_entity_id(index),
            options.mode_select_entity_template.format(n=number),
            options.lux_reference_entity_template.format(n=number),
            options.calibration_trigger_entity_template.format(n=number),
        ]
        missing.extend(entity_id for entity_id in expected if entity_id not in existing_entities)
    return missing


class DashboardProvisioner:
    def __init__(self, ha: HomeAssistantClient, options: AddonOptions) -> None:
        """Nothing is written until the first discovery.

        Args:
            ha: HA client.
            options: Add-on options (entity name templates).
        """
        self._ha = ha
        self._options = options
        self._lock = threading.Lock()
        self._discovery: Optional[DiscoveryState] = None
        self._venue: Optional[VenueConfig] = None
        self._saved_config: Optional[Dict[str, Any]] = None
        self._warned_missing: Set[str] = set()

    def update_discovery(self, discovery: DiscoveryState) -> None:
        """Args:
            discovery: Latest discovery result.
        """
        with self._lock:
            self._discovery = discovery
            self._warn_missing_entities(discovery)
            self._sync()

    def update_venue(self, venue: VenueConfig) -> None:
        """Args:
            venue: Latest venue config.
        """
        with self._lock:
            self._venue = venue
            self._sync()

    def _warn_missing_entities(self, discovery: DiscoveryState) -> None:
        """Warns once per missing blueprint entity.

        Args:
            discovery: Latest discovery result.
        """
        existing = _entity_ids(discovery)
        for entity_id in missing_court_entities(discovery.courts.keys(), existing, self._options):
            if entity_id not in self._warned_missing:
                self._warned_missing.add(entity_id)
                logger.warning("%s not found in HA, check the installer blueprint", entity_id)

    def _sync(self) -> None:
        """Saves the dashboard if its config changed."""
        if self._discovery is None:
            return
        config = build_dashboard_config(
            court_indexes=self._discovery.courts.keys(),
            door_entities={index: door.lock_entity_id for index, door in self._discovery.doors.items()},
            existing_entities=_entity_ids(self._discovery),
            options=self._options,
            venue=self._venue,
        )
        if config == self._saved_config:
            return
        try:
            self._ensure_dashboard()
            self._ha.ws_command({"type": "lovelace/config/save", "url_path": DASHBOARD_URL_PATH, "config": config})
        except Exception:  # noqa: BLE001
            logger.warning("Could not update the dashboard", exc_info=True)
            return
        self._saved_config = config
        logger.info("Dashboard /%s updated", DASHBOARD_URL_PATH)

    def _ensure_dashboard(self) -> None:
        """Creates the dashboard if it doesn't exist."""
        dashboards = self._ha.ws_command({"type": "lovelace/dashboards/list"}) or []
        if any(d.get("url_path") == DASHBOARD_URL_PATH for d in dashboards):
            return
        self._ha.ws_command(
            {
                "type": "lovelace/dashboards/create",
                "url_path": DASHBOARD_URL_PATH,
                "title": DASHBOARD_TITLE,
                "icon": DASHBOARD_ICON,
                "show_in_sidebar": True,
                "require_admin": False,
                "mode": "storage",
            }
        )
        logger.info("Dashboard /%s created", DASHBOARD_URL_PATH)


def _entity_ids(discovery: DiscoveryState) -> Set[str]:
    """Args:
        discovery: Discovery result.

    Returns:
        Entity ids present in HA at discovery time.
    """
    return {state["entity_id"] for state in discovery.raw_states if "entity_id" in state}
