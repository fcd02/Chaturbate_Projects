#!/usr/bin/env python3
"""Create a privacy-conscious CTBRec Mobile Reviewer diagnostics ZIP.

Run from the installed CTBRec_Mobile_Reviewer folder while the server is running
if possible. The bundle intentionally EXCLUDES Recu browser/session cookies and
redacts obvious PIN/secret/token/cookie/password fields from mobile_config.json.
It may still contain model names and local filesystem paths in catalog/state/log
files; review before sharing if that matters for your use case.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
SENSITIVE_FRAGMENTS = ("pin", "secret", "token", "cookie", "password", "credential", "authorization")
SAFE_RUNTIME_FILES = [
    "mobile_reviewer.log",
    "mobile_catalog_cache.json",
    "mobile_ready_work_index.json",
    "mobile_library_mosaic_state.json",
    "mobile_non_nsfw_state.json",
    "mobile_non_nsfw_index.json",
    "mobile_action_queue.json",
    "mobile_hidden_models.json",
    "mobile_model_metadata.json",
    "recording_roots.txt",
]
CODE_FILES = [
    "ctbrec_mobile_server.py",
    "ctbrec_mobile_model_admin.py",
    "ctbrec_live_bridge_client.py",
    "static/app.js",
    "static/rapid.js",
    "static/service-worker.js",
    "static/index.html",
]


def redact(value: Any, key: str = "") -> Any:
    key_l = key.casefold()
    if any(fragment in key_l for fragment in SENSITIVE_FRAGMENTS):
        if value in (None, "", [], {}):
            return value
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def run_command(command: list[str], timeout: int = 12) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, creationflags=(0x08000000 if os.name == "nt" else 0))
        return f"$ {' '.join(command)}\nexit={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n"
    except Exception as exc:
        return f"$ {' '.join(command)}\nERROR: {exc}\n"


def fetch_probe(url: str, timeout: int = 20) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            payload = None
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                pass
            summary: dict[str, Any] = {
                "ok": True,
                "status": int(getattr(response, "status", 200)),
                "elapsed_ms": elapsed,
                "bytes": len(body),
                "content_type": response.headers.get("Content-Type", ""),
            }
            if isinstance(payload, dict):
                if "models" in payload and isinstance(payload["models"], list):
                    summary["model_count"] = len(payload["models"])
                    summary["first_models"] = [str(row.get("name", "")) for row in payload["models"][:10] if isinstance(row, dict)]
                    summary["request_ms"] = payload.get("request_ms")
                else:
                    summary["json"] = redact(payload)
            return summary
    except Exception as exc:
        return {"ok": False, "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1), "error": repr(exc)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = APP_DIR / f"CTBRec_Mobile_Reviewer_Diagnostics_{stamp}.zip"
    with tempfile.TemporaryDirectory(prefix="ctbrec_mobile_diag_") as tmp_raw:
        tmp = Path(tmp_raw)
        summary: dict[str, Any] = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app_dir": str(APP_DIR),
            "python": sys.version,
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "files": {},
            "local_http_probes": {},
        }

        config = APP_DIR / "mobile_config.json"
        if config.exists():
            try:
                payload = json.loads(config.read_text(encoding="utf-8"))
                (tmp / "mobile_config_REDACTED.json").write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                (tmp / "mobile_config_REDACTION_ERROR.txt").write_text(str(exc), encoding="utf-8")

        runtime_dir = tmp / "runtime"
        runtime_dir.mkdir()
        for name in SAFE_RUNTIME_FILES:
            source = APP_DIR / name
            if not source.exists() or not source.is_file():
                summary["files"][name] = {"exists": False}
                continue
            try:
                stat = source.stat()
                summary["files"][name] = {"exists": True, "bytes": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")}
                # Avoid a diagnostics ZIP exploding because of an unexpectedly huge log/cache.
                if stat.st_size <= 80 * 1024 * 1024:
                    shutil.copy2(source, runtime_dir / name)
                else:
                    summary["files"][name]["not_copied_reason"] = "larger than 80 MiB"
            except Exception as exc:
                summary["files"][name] = {"exists": True, "error": str(exc)}

        checksums = {}
        for name in CODE_FILES:
            source = APP_DIR / name
            if source.exists() and source.is_file():
                try:
                    checksums[name] = {"bytes": source.stat().st_size, "sha256": sha256(source)}
                except Exception as exc:
                    checksums[name] = {"error": str(exc)}
        (tmp / "code_checksums.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")

        base = "http://127.0.0.1:8787"
        for label, path in [
            ("auth", "/api/auth"),
            ("diagnostics_status", "/api/diagnostics/status"),
            ("catalog_original_all_drives", "/api/catalog?mode=original&filter=&drives="),
            ("catalog_review_all_drives", "/api/catalog?mode=review&filter=&drives="),
            ("catalog_easy_all_drives", "/api/catalog?mode=original&filter=easy&drives="),
        ]:
            summary["local_http_probes"][label] = fetch_probe(base + path)

        commands_dir = tmp / "commands"
        commands_dir.mkdir()
        command_sets = []
        if os.name == "nt":
            command_sets.extend([
                ("netstat_8787.txt", ["cmd", "/c", "netstat -ano | findstr :8787"]),
                ("python_tasks.txt", ["cmd", "/c", "tasklist | findstr /I python"]),
                ("tailscale_status.txt", ["cmd", "/c", "tailscale status"]),
                ("tailscale_serve_status.txt", ["cmd", "/c", "tailscale serve status"]),
            ])
        for filename, command in command_sets:
            (commands_dir / filename).write_text(run_command(command), encoding="utf-8", errors="replace")

        (tmp / "SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (tmp / "README.txt").write_text(
            "Upload this entire ZIP to ChatGPT for CTBRec Mobile Reviewer diagnosis.\n"
            "Recu session/browser-profile data are intentionally excluded. Obvious PIN/secret/token/cookie/password fields in mobile_config.json are redacted.\n"
            "The bundle can still contain model names and local paths in runtime caches/logs.\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in sorted(tmp.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(tmp))
    print(f"Created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
