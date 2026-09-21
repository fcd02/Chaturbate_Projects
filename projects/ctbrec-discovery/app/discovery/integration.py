from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .importers import _iter_records, _name
from .models import normalize_username


class ReviewerIntegration:
    """Read-only integration probe for the existing Mobile Reviewer.

    Mutations remain in Discovery's durable local outbox until an exact verified
    Reviewer/live-bridge adapter is available. Health checks are deliberately tiny.
    """

    def __init__(self, config: Dict[str, Any], base_dir: Path):
        self.config = config
        self.base_dir = base_dir

    def _resolve(self, raw: str) -> Optional[Path]:
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else self.base_dir / p

    def catalog_file_status(self) -> Dict[str, Any]:
        p = self._resolve(str(self.config.get("mobile_catalog_cache", "")))
        if not p:
            return {"configured": False}
        out: Dict[str, Any] = {"configured": True, "path": str(p), "exists": p.exists()}
        if not p.exists():
            return out
        try:
            st = p.stat()
            out.update({"size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns})
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                out["shape"] = "list"
                out["top_level_count"] = len(data)
            elif isinstance(data, dict):
                out["shape"] = "object"
                out["top_level_count"] = len(data)
                out["top_level_keys_sample"] = list(data)[:12]
                catalog = data.get("catalog")
                if isinstance(catalog, (dict, list)):
                    out["catalog_outer_bucket_count"] = len(catalog)
                    if isinstance(catalog, dict):
                        out["catalog_outer_keys_sample"] = list(catalog)[:12]
            else:
                out["shape"] = type(data).__name__

            # Use the same schema-adaptive walker as the importer so Status cannot repeat
            # the misleading v0.4.0 "catalog_entry_count: 3" outer-bucket count.
            unique = set()
            row_groups = 0
            for fallback, record in _iter_records(data):
                row_groups += 1
                username = _name(fallback, record)
                if username:
                    norm = normalize_username(username)
                    if norm:
                        unique.add(norm)
            out["catalog_model_row_groups"] = row_groups
            out["catalog_unique_model_count"] = len(unique)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def probe_reviewer(self) -> Dict[str, Any]:
        base = str(self.config.get("reviewer_base_url", "http://127.0.0.1:8787")).rstrip("/")
        # Existing configs may still carry v0.4's 1.25 second timeout. Give the tiny local
        # auth endpoint at least 2 seconds so momentary Reviewer disk work does not become a
        # false "offline" diagnosis.
        configured = float(self.config.get("reviewer_probe_timeout_seconds", 2.5))
        timeout = max(2.0, min(5.0, configured))
        result: Dict[str, Any] = {"base_url": base, "reachable": False, "auth_required": False, "probe_method": "GET /api/auth"}

        def request(path: str, method: str = "GET"):
            req = urllib.request.Request(base + path, method=method, headers={"User-Agent": "CTBRecDiscovery/0.4.1"})
            return urllib.request.urlopen(req, timeout=timeout)

        try:
            # /api/auth is a deliberately lightweight endpoint in supported Mobile Reviewer
            # builds and avoids the v0.4.0 mistake of falling back to a whole-shell GET.
            with request("/api/auth", "GET") as resp:
                resp.read(4096)
                result.update({"reachable": True, "status": int(resp.status), "server": resp.headers.get("Server")})
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                result.update({"reachable": True, "status": int(exc.code), "auth_required": True, "server": exc.headers.get("Server") if exc.headers else None})
                return result
            if exc.code not in {404, 405, 501}:
                # Any other HTTP response proves a listener is there; only auth status has special meaning.
                result.update({"reachable": True, "status": int(exc.code), "server": exc.headers.get("Server") if exc.headers else None})
                return result
            result["api_auth_status"] = int(exc.code)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            result["api_auth_error"] = f"{type(exc).__name__}: {exc}"

        # Compatibility fallback for a very old build with no /api/auth. HEAD transfers no body.
        result["probe_method"] = "HEAD / fallback"
        try:
            with request("/", "HEAD") as resp:
                result.update({"reachable": True, "status": int(resp.status), "server": resp.headers.get("Server")})
        except urllib.error.HTTPError as exc:
            result.update({"reachable": True, "status": int(exc.code), "auth_required": exc.code in {401, 403}, "server": exc.headers.get("Server") if exc.headers else None})
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    def status(self) -> Dict[str, Any]:
        return {"reviewer": self.probe_reviewer(), "catalog_file": self.catalog_file_status(), "mutation_mode": "durable_local_outbox_only"}
