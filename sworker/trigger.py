"""§24 workflow triggers — opt-in automation that launches a worker.

Three trigger kinds, all fail-closed and zero third-party deps:

  * ``file_changed`` — polls one or more roots (stdlib only; no watchdog) and
    fires when a file is created/modified. Debounced per (root) so a burst of
    writes coalesces into one fire.
  * ``webhook`` — a tiny stdlib ``http.server`` receiver that routes an
    inbound POST to a registered worker by path. Unknown paths/methods are
    rejected with 404/405 (fail closed).
  * ``event`` — an in-process ``EventBus`` so other subsystems (scheduler,
    connectors) can fire a worker on a named internal event.

Triggers never become a path to authority: they only call ``engine.run`` with
the trigger name recorded, exactly like a manual run. The engine still applies
policy + gate. A disabled worker refuses to start regardless of trigger.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional

from .config import WorkerConfig  # type: ignore

VALID_KINDS = ("file_changed", "webhook", "event")


class TriggerError(ValueError):
    """§24 — a trigger config is invalid (fail closed)."""


def normalize_trigger(spec: Dict[str, Any]) -> Dict[str, Any]:
    """§24 — validate a single trigger spec; raise TriggerError if malformed."""
    kind = spec.get("kind")
    if kind not in VALID_KINDS:
        raise TriggerError(f"unknown trigger kind {kind!r}; must be one of {VALID_KINDS}")
    out: Dict[str, Any] = dict(spec)
    if kind == "file_changed":
        roots = spec.get("roots")
        if not isinstance(roots, list) or not roots:
            raise TriggerError("file_changed trigger requires non-empty 'roots' list")
        for r in roots:
            if not os.path.isabs(str(r)):
                raise TriggerError(f"file_changed root must be absolute: {r!r}")
        out["roots"] = [str(r) for r in roots]
        out.setdefault("interval", 5.0)
    elif kind == "webhook":
        path = spec.get("path")
        if not path or not str(path).startswith("/"):
            raise TriggerError("webhook trigger requires 'path' starting with '/'")
        out["path"] = str(path)
        out.setdefault("secret", "")
    elif kind == "event":
        name = spec.get("name")
        if not name:
            raise TriggerError("event trigger requires 'name'")
        out["name"] = str(name)
    return out


def resolve_triggers(worker: "WorkerConfig") -> List[Dict[str, Any]]:
    """§24 — validate every trigger on a worker; fail closed on any bad spec."""
    out = []
    for raw in getattr(worker, "triggers", []) or []:
        out.append(normalize_trigger(raw))
    return out


def snapshot_roots(roots: List[str]) -> Dict[str, str]:
    """Return {path: sha256} for files currently under roots (one level deep)."""
    snap: Dict[str, str] = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full):
                try:
                    with open(full, "rb") as fh:
                        snap[full] = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    continue
    return snap


class FileWatcher(threading.Thread):
    """§24 — poll roots; call ``on_fire(trigger, changed_paths)`` when they change."""

    def __init__(
        self,
        trigger: Dict[str, Any],
        on_fire: Callable[[Dict[str, Any], List[str]], None],
        stop: Optional[threading.Event] = None,
    ):
        super().__init__(daemon=True)
        self.trigger = trigger
        self.on_fire = on_fire
        self._stop = stop or threading.Event()
        self._last: Dict[str, str] = {}
        self._primed = False

    def run(self) -> None:
        interval = float(self.trigger.get("interval", 5.0))
        while not self._stop.is_set():
            cur = snapshot_roots(self.trigger["roots"])
            changed = [p for p in cur if cur.get(p) != self._last.get(p)]
            if self._primed and changed:  # skip the first snapshot (baseline)
                self.on_fire(self.trigger, changed)
            self._last = cur
            self._primed = True
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()


class EventBus:
    """§24 — in-process named-event pub/sub used to fire workers on events."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}

    def subscribe(self, name: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        self._subs.setdefault(name, []).append(handler)

    def publish(self, name: str, payload: Optional[Dict[str, Any]] = None) -> int:
        handlers = self._subs.get(name, [])
        for h in handlers:
            h(payload or {})
        return len(handlers)


class _WebhookHandler(BaseHTTPRequestHandler):
    def _dispatch(self):
        bus: "WebhookReceiver" = self.server.receiver  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        trigger = bus.route.get(path)
        if self.command != "POST":
            self.send_response(405)
            self.end_headers()
            self.wfile.write(b'{"error":"method not allowed"}')
            return
        if trigger is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"no such webhook"}')
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        secret = trigger.get("secret", "")
        if secret:
            provided = self.headers.get("X-Sworker-Secret", "")
            if provided != secret:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error":"forbidden"}')
                return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", "replace")}
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')
        bus.on_fire(trigger, payload)

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_GET(self):  # noqa: N802
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b'{"error":"method not allowed"}')

    def do_PUT(self):  # noqa: N802
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b'{"error":"method not allowed"}')

    def log_message(self, *args):  # silence default stderr logging
        return


class WebhookReceiver:
    """§24 — stdlib HTTP server routing POSTs to registered worker triggers."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.host = host
        self.port = port
        self.route: Dict[str, Dict[str, Any]] = {}
        self.on_fire: Callable[[Dict[str, Any], Dict[str, Any]], None] = lambda t, p: None
        self._server: Optional[HTTPServer] = None

    def register(self, trigger: Dict[str, Any]) -> None:
        self.route[trigger["path"]] = trigger

    def serve_forever(self) -> None:
        self._server = HTTPServer((self.host, self.port), _WebhookHandler)
        self._server.receiver = self  # type: ignore[attr-defined]
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
