from srlobo_mqtt_bridge.logging_setup import redact


def test_redact_masks_known_secret_keys():
    payload = {"srlobo_token": "abc123", "installation_id": "club_1"}
    assert redact(payload) == {"srlobo_token": "[REDACTED]", "installation_id": "club_1"}


def test_redact_masks_nested_mqtt_credentials():
    payload = {"mqtt": {"broker": "mqtt.example", "username": "u", "password": "p"}}
    result = redact(payload)
    assert result["mqtt"]["password"] == "[REDACTED]"
    assert result["mqtt"]["broker"] == "mqtt.example"  # not a secret key, passes through
    assert result["mqtt"]["username"] == "u"  # username alone isn't in SENSITIVE_KEYS


def test_redact_masks_inside_lists():
    payload = [{"token": "t1"}, {"token": "t2"}]
    assert redact(payload) == [{"token": "[REDACTED]"}, {"token": "[REDACTED]"}]


def test_redact_passes_through_non_dict_values():
    assert redact("plain string") == "plain string"
    assert redact(42) == 42
    assert redact(None) is None
