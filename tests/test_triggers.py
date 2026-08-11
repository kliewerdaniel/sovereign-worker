"""§24 workflow triggers — file_changed / webhook / event.

Fail-closed guarantees tested:
  * unknown trigger kind rejected
  * file_changed requires absolute, non-empty roots
  * webhook unknown path -> 404, wrong method -> 405, bad secret -> 403
  * event bus dispatch fires registered handler
"""

import os
import threading
import time

import pytest

from sworker import trigger as T


def test_normalize_rejects_unknown_kind():
    with pytest.raises(T.TriggerError):
        T.normalize_trigger({"kind": "cron"})


def test_normalize_file_changed_requires_absolute_roots():
    with pytest.raises(T.TriggerError):
        T.normalize_trigger({"kind": "file_changed", "roots": ["relative/path"]})
    with pytest.raises(T.TriggerError):
        T.normalize_trigger({"kind": "file_changed", "roots": []})
    spec = T.normalize_trigger({"kind": "file_changed", "roots": ["/tmp"]})
    assert spec["roots"] == ["/tmp"]


def test_normalize_webhook_requires_path():
    with pytest.raises(T.TriggerError):
        T.normalize_trigger({"kind": "webhook"})
    spec = T.normalize_trigger({"kind": "webhook", "path": "/hook"})
    assert spec["path"] == "/hook"
    assert spec["secret"] == ""  # default empty


def test_resolve_triggers_fail_closed(tmp_path):
    class W:
        triggers = [{"kind": "bogus"}]
    with pytest.raises(T.TriggerError):
        T.resolve_triggers(W())


def test_file_watcher_detects_new_file(tmp_path):
    root = tmp_path / "watch"
    root.mkdir()
    fired = []
    stop = threading.Event()
    w = T.FileWatcher(
        {"kind": "file_changed", "roots": [str(root)], "interval": 0.2},
        lambda trig, paths: fired.append((trig, paths)),
        stop,
    )
    w.start()
    time.sleep(0.5)  # baseline snapshot
    (root / "new.txt").write_text("hello")
    deadline = time.time() + 5
    while not fired and time.time() < deadline:
        time.sleep(0.1)
    stop.set()
    w.stop()
    assert any(p.endswith("new.txt") for _, paths in fired for p in paths)


def test_event_bus_dispatch():
    bus = T.EventBus()
    got = []
    bus.subscribe("doc.ingested", lambda payload: got.append(payload))
    n = bus.publish("doc.ingested", {"src": "x"})
    assert n == 1
    assert got and got[0]["src"] == "x"
    # unsubscribed event fires nothing
    assert bus.publish("nothing") == 0


def test_webhook_receiver_routing_and_gates(tmp_path):
    recv = T.WebhookReceiver(host="127.0.0.1", port=0)
    fired = []
    recv.on_fire = lambda trig, payload: fired.append((trig["path"], payload))
    recv.register({"kind": "webhook", "path": "/ok", "secret": ""})
    recv.register({"kind": "webhook", "path": "/secret", "secret": "pw"})

    import http.client

    # bind to a real free port via a throwaway server instance
    import socketserver
    srv = socketserver.TCPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.server_close()
    recv.port = port

    import threading
    t = threading.Thread(target=recv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)

    def post(path, body=b"{}", secret=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Sworker-Secret"] = secret
        c.request("POST", path, body=body, headers=headers)
        resp = c.getresponse()
        code = resp.status
        c.close()
        return code

    # valid path, no secret
    assert post("/ok", b'{"a":1}') == 202
    # unknown path -> 404
    assert post("/nope") == 404
    # GET -> 405
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    c.request("GET", "/ok")
    assert c.getresponse().status == 405
    c.close()
    # secret-gated path without secret -> 403
    assert post("/secret", b'{}') == 403
    # with secret -> 202
    assert post("/secret", b'{"b":2}', secret="pw") == 202

    recv.shutdown()
    assert any(p == "/ok" for p, _ in fired)
    assert any(p == "/secret" for p, _ in fired)
