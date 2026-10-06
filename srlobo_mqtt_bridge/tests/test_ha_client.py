import json
from unittest.mock import MagicMock, patch

from srlobo_mqtt_bridge.ha_client import HAStateListener


def _listener(device_handler=None):
    with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "t"}):
        return HAStateListener(on_event=MagicMock(), on_reconnect=MagicMock(), on_device_registry_updated=device_handler)


def test_subscribes_to_device_registry_updated_on_the_same_connection():
    ws = MagicMock()
    ws.recv.side_effect = [
        json.dumps({"id": 2, "type": "result", "success": True}),
        json.dumps({"type": "event", "event": {"event_type": "state_changed"}}),  # interleaved, dropped
        json.dumps({"id": 1, "type": "result", "success": True}),
    ]
    _listener(MagicMock())._subscribe(ws)
    sent = [json.loads(call.args[0])["event_type"] for call in ws.send.call_args_list]
    assert sent == ["state_changed", "device_registry_updated"]


def test_without_device_handler_only_state_changed_is_subscribed():
    ws = MagicMock()
    ws.recv.side_effect = [json.dumps({"id": 1, "type": "result", "success": True})]
    _listener(None)._subscribe(ws)
    assert ws.send.call_count == 1
