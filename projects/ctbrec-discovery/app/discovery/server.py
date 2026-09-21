from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import load_config
from .engine import DiscoveryEngine


class App:
    def __init__(self, root: Path, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.config = load_config(config_path)
        self.engine = DiscoveryEngine(self.config, root)

    def safe_settings(self):
        filters = self.engine.config.get("recommendation_filters") if isinstance(self.engine.config.get("recommendation_filters"), dict) else {}
        return {
            "recommendation_filters": {
                "allowed_genders": list(filters.get("allowed_genders") or []),
                "include_unknown_gender": bool(filters.get("include_unknown_gender", True)),
                "include_couples": bool(filters.get("include_couples", False)),
            },
            "live_control_base_url": str(self.engine.config.get("live_control_base_url", "http://127.0.0.1:8792")),
            "mobile_catalog_cache": str(self.engine.config.get("mobile_catalog_cache", "")),
            "note": "WM/credentials are intentionally never returned by this API",
        }

    def update_settings(self, payload):
        current = {}
        if self.config_path.exists():
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            current = raw if isinstance(raw, dict) else {}
        filters = payload.get("recommendation_filters") if isinstance(payload.get("recommendation_filters"), dict) else None
        if filters is not None:
            allowed = [str(x).strip().casefold() for x in (filters.get("allowed_genders") or []) if str(x).strip()]
            current["recommendation_filters"] = {
                "allowed_genders": allowed,
                "include_unknown_gender": bool(filters.get("include_unknown_gender", True)),
                "include_couples": bool(filters.get("include_couples", False)),
            }
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.config_path)
        self.config = load_config(self.config_path)
        # Recommendation filters are safe to apply live; collector topology remains unchanged until restart.
        self.engine.config["recommendation_filters"] = self.config.get("recommendation_filters", {})
        return self.safe_settings()


class Handler(BaseHTTPRequestHandler):
    server_version = f"CTBRecDiscovery/{__version__}"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 2_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_HEAD(self):
        u = urlparse(self.path)
        if u.path in {"", "/", "/index.html"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/api/health":
                return self._json({
                    "ok": True,
                    "version": __version__,
                    "schema_version": 2,
                    "sources": self.app.engine.store.source_states(),
                    "outbox": self.app.engine.store.action_counts(),
                    "collectors": self.app.engine.collector_status()["items"],
                })
            if u.path == "/api/recommendations":
                limit = int(qs.get("limit", ["100"])[0])
                rows = [r.__dict__ for r in self.app.engine.recommendations(limit)]
                return self._json({"items": rows, "count": len(rows)})
            if u.path == "/api/continuations":
                limit = int(qs.get("limit", ["100"])[0])
                rows = self.app.engine.continuations(limit)
                return self._json({"items": rows, "count": len(rows)})
            if u.path == "/api/accounts":
                rows = self.app.engine.store.list_accounts()
                return self._json({"items": rows, "count": len(rows)})
            if u.path == "/api/outbox":
                limit = int(qs.get("limit", ["100"])[0])
                state = qs.get("state", [None])[0]
                rows = self.app.engine.store.list_actions(state=state, limit=limit)
                return self._json({"items": rows, "count": len(rows), "counts": self.app.engine.store.action_counts()})
            if u.path == "/api/integration":
                refresh = qs.get("refresh", ["0"])[0].casefold() in {"1", "true", "yes"}
                return self._json(self.app.engine.integration_status(refresh=refresh))
            if u.path == "/api/collectors":
                return self._json(self.app.engine.collector_status())
            if u.path == "/api/diagnostics":
                return self._json(self.app.engine.diagnostics())
            if u.path == "/api/settings":
                return self._json(self.app.safe_settings())
            if u.path == "/api/sync":
                return self._json(self.app.engine.request_sync())
            if u.path == "/api/sync-status":
                return self._json(self.app.engine.sync_status())
            return self._static(u.path)
        except Exception as exc:
            return self._json({"error": type(exc).__name__, "detail": str(exc)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            data = self._body_json()
            if u.path == "/api/evidence":
                return self._json(self.app.engine.ingest_payload(data), 201)
            if u.path == "/api/feedback":
                return self._json(self.app.engine.set_feedback(str(data.get("identity_id", "")), str(data.get("label", "")), data.get("metadata") if isinstance(data.get("metadata"), dict) else None))
            if u.path == "/api/undo":
                return self._json(self.app.engine.undo_last_feedback())
            if u.path == "/api/merge":
                keep = str(data.get("keep_identity_id", "")); merge = str(data.get("merge_identity_id", ""))
                self.app.engine.store.merge_identities(keep, merge, reason="discovery_ui_confirmed")
                return self._json({"ok": True})
            if u.path == "/api/continuation-decision":
                decision = str(data.get("decision", "")).casefold()
                old_id = str(data.get("old_identity_id", "")); new_id = str(data.get("new_identity_id", ""))
                if decision == "reject":
                    return self._json(self.app.engine.reject_continuation(old_id, new_id))
                raise ValueError("supported decision: reject")
            if u.path == "/api/settings":
                return self._json(self.app.update_settings(data))
            if u.path == "/api/collectors/run":
                name = str(data.get("name", "")).strip()
                if not name:
                    raise ValueError("collector name is required")
                return self._json(self.app.engine.run_collector(name))
            return self._json({"error": "not_found"}, 404)
        except (ValueError, KeyError) as exc:
            return self._json({"error": type(exc).__name__, "detail": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": type(exc).__name__, "detail": str(exc)}, 500)

    def _static(self, path: str):
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        if ".." in Path(rel).parts:
            return self._json({"error":"bad_path"}, 400)
        p = self.app.root / "static" / rel
        if not p.exists() or not p.is_file():
            return self._json({"error":"not_found"}, 404)
        raw = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, no-store, max-age=0, must-revalidate")
        self.end_headers(); self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description="CTBRec Discovery companion service")
    parser.add_argument("--config", default="discovery_config.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    app = App(root, root / args.config)
    host = str(app.config.get("listen_host", "127.0.0.1"))
    port = int(app.config.get("listen_port", 8793))
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    print(f"CTBRec Discovery v{__version__} listening on http://{host}:{port}")
    print("Initial catalog/source sync queued in background; the UI is available immediately.")
    app.engine.start_background()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.engine.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
