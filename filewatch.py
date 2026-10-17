"""Watch folders and fire n8n workflows on file events.

This is the bot's "notice things happened" sense. Drop a PDF into a watched
folder and n8n gets a webhook with the path — so automations can run without
you asking the bot for anything.

Events are debounced (editors write files in several bursts) and filtered by
extension so a build directory doesn't spam the webhook.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from pathlib import Path

import config

_WEBHOOK = getattr(config, "N8N_WATCH_WEBHOOK",
                   "http://127.0.0.1/n8n/webhook/file-event")

_observer = None
_watched: dict[str, dict] = {}
_recent: list[dict] = []
_lock = threading.Lock()


def _post(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            _WEBHOOK, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass          # n8n may be down; the event log still records it


def recent(n: int = 15) -> list[dict]:
    with _lock:
        return _recent[-n:][::-1]


def status() -> dict:
    return {"running": bool(_observer and _observer.is_alive()),
            "watching": list(_watched), "events_seen": len(_recent),
            "webhook": _WEBHOOK}


def start(paths: list[str] | None = None,
          extensions: tuple[str, ...] = (".pdf", ".txt", ".md", ".csv", ".json",
                                         ".png", ".jpg", ".docx", ".py"),
          debounce: float = 2.0) -> dict:
    """Begin watching. Safe to call twice (returns existing status)."""
    global _observer
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"watchdog not installed ({e})"}

    if _observer and _observer.is_alive():
        return {"ok": True, "note": "already running", **status()}

    targets = paths or [str(config.UPLOADS_DIR),
                        str(config.WORKSPACE / "outputs"),
                        str(Path.home() / "Downloads")]

    seen: dict[str, float] = {}

    class Handler(FileSystemEventHandler):
        def _fire(self, kind: str, path: str) -> None:
            if not path.lower().endswith(extensions):
                return
            now = time.time()
            if now - seen.get(path, 0) < debounce:   # editors write in bursts
                return
            seen[path] = now
            ev = {"event": kind, "path": path, "name": Path(path).name,
                  "size": Path(path).stat().st_size if Path(path).exists() else 0,
                  "ts": int(now)}
            with _lock:
                _recent.append(ev)
                del _recent[:-200]
            threading.Thread(target=_post, args=(ev,), daemon=True).start()

        def on_created(self, e):
            if not e.is_directory:
                self._fire("created", e.src_path)

        def on_modified(self, e):
            if not e.is_directory:
                self._fire("modified", e.src_path)

        def on_moved(self, e):
            if not e.is_directory:
                self._fire("moved", e.dest_path)

    _observer = Observer()
    h = Handler()
    ok_paths = []
    for p in targets:
        if Path(p).is_dir():
            _observer.schedule(h, p, recursive=True)
            _watched[p] = {"since": int(time.time())}
            ok_paths.append(p)
    if not ok_paths:
        return {"ok": False, "error": "none of the target folders exist"}
    _observer.daemon = True
    _observer.start()
    return {"ok": True, "watching": ok_paths, "webhook": _WEBHOOK}


def stop() -> dict:
    global _observer
    if _observer:
        _observer.stop()
        _observer = None
        _watched.clear()
        return {"ok": True, "stopped": True}
    return {"ok": True, "note": "was not running"}


async def start_async(*a, **kw) -> dict:
    return await asyncio.to_thread(start, *a, **kw)
