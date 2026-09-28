from unittest.mock import MagicMock

from srlobo_mqtt_bridge.venue_config import VenueConfigHandler

VALID_PAYLOAD = {
    "schema_version": 1,
    "generated_at": "2026-09-05T10:00:00Z",
    "club_name": "Aytopepino Padel Club",
    "address": "Calle Example 12, Madrid",
    "latitude": 40.4168,
    "longitude": -3.7038,
    "timezone": "Europe/Madrid",
    "language": "es",
    "booking_system": "playtomic",
    "courts": {"1": {"name": "Pista Central"}, "2": {"name": "Pista 2"}},
}


def test_valid_payload_is_applied_to_ha_core_config():
    ha = MagicMock()
    handler = VenueConfigHandler(ha)

    handler.handle(VALID_PAYLOAD)

    ha.set_core_config.assert_called_once_with(
        latitude=40.4168, longitude=-3.7038, time_zone="Europe/Madrid", language="es"
    )
    assert handler.current.club_name == "Aytopepino Padel Club"
    assert handler.current.court_names == {1: "Pista Central", 2: "Pista 2"}


def test_unsupported_schema_version_is_rejected_and_previous_kept():
    ha = MagicMock()
    handler = VenueConfigHandler(ha)
    handler.handle(VALID_PAYLOAD)
    ha.reset_mock()

    handler.handle({**VALID_PAYLOAD, "schema_version": 99})

    ha.set_core_config.assert_not_called()
    assert handler.current.club_name == "Aytopepino Padel Club"  # unchanged


def test_missing_required_field_is_rejected():
    ha = MagicMock()
    handler = VenueConfigHandler(ha)
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "timezone"}

    handler.handle(payload)

    ha.set_core_config.assert_not_called()
    assert handler.current is None


def test_non_numeric_coordinates_are_rejected():
    ha = MagicMock()
    handler = VenueConfigHandler(ha)
    handler.handle({**VALID_PAYLOAD, "latitude": "not-a-number"})
    ha.set_core_config.assert_not_called()


def test_malformed_court_entry_is_skipped_not_fatal():
    ha = MagicMock()
    handler = VenueConfigHandler(ha)
    payload = {**VALID_PAYLOAD, "courts": {"1": {"name": "Pista Central"}, "bad": {}}}

    handler.handle(payload)

    assert handler.current.court_names == {1: "Pista Central"}
