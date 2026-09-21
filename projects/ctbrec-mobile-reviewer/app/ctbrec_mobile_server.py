#!/usr/bin/env python3
"""Phone-first CTBRec mosaic reviewer.

The HTTP server listens only on 127.0.0.1. Use Tailscale Serve to make it
available privately to the user's iPhone over HTTPS. This file deliberately
reuses the uploaded desktop scripts for chunking, mosaic generation, and file
movement so the mobile workflow follows the same rules.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import importlib.util
import io
import json
import math
import mimetypes
import ntpath
import os
import queue as queue_module
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import websocket
except Exception:
    websocket = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "mobile_config.json"
CATALOG_PATH = APP_DIR / "mobile_catalog_cache.json"
LAYOUT_CACHE_PATH = APP_DIR / "mobile_mosaic_layout_cache.json"
LOG_PATH = APP_DIR / "mobile_reviewer.log"
PID_PATH = APP_DIR / "mobile_server.pid"
RECU_SESSION_PATH = APP_DIR / "mobile_recu_session.json"
RECU_PROFILE_DIR = APP_DIR / "recu_browser_profile"
DELETE_MOSAIC_ROOT = APP_DIR / "mobile_delete_mosaics"
ACTION_QUEUE_PATH = APP_DIR / "mobile_action_queue.json"
LIBRARY_STATE_PATH = APP_DIR / "mobile_library_mosaic_state.json"
LIBRARY_PLAN_PATH = APP_DIR / "mobile_library_mosaic_plan.json"
READY_INDEX_PATH = APP_DIR / "mobile_ready_work_index.json"
OFFLINE_SYNC_PATH = APP_DIR / "mobile_offline_sync_history.json"
MOSAIC_SCRIPT = APP_DIR / "ctbrec_mosaic_sort_lite.py"
REVIEW_SCRIPT = APP_DIR / "ctbrec_review_folder_sort_lite.py"
NSFW_CLEANUP_SCRIPT = APP_DIR / "ctbrec_nsfw_cleanup.py"
KEEP_LAST_SCRIPT = APP_DIR / "ctbrec_keep_last.py"
MODEL_ADMIN_SCRIPT = APP_DIR / "ctbrec_mobile_model_admin.py"
LIVE_BRIDGE_SCRIPT = APP_DIR / "ctbrec_live_bridge_client.py"
NSFW_INDEX_PATH = APP_DIR / "mobile_non_nsfw_index.json"
NSFW_STATE_PATH = APP_DIR / "mobile_non_nsfw_state.json"
STATIC_DIR = APP_DIR / "static"
PREVIEW_HLS_CACHE_DIR = Path(tempfile.gettempdir()) / "ctbrec_mobile_preview_hls"
PREVIEW_SEEKABLE_CACHE_DIR = Path(tempfile.gettempdir()) / "ctbrec_mobile_preview_seekable"
VIDEO_EXTS = {
    ".mp4", ".ts", ".mpegts", ".m2ts", ".mts", ".mov", ".mkv",
    ".m4v", ".avi", ".webm",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "roots_file": "recording_roots.txt",
    "port": 8787,
    "scan_on_start": True,
    "access_pin": "",
    "secret_key": "",
    "ffmpeg_path": "",
    "mosaic_settings": {},
    "review_settings": {},
    "recu": {
        "enabled": True,
        "browser_debug_port": 9223,
        "chrome_path": "",
        "base_url": "https://recu.me",
        "auto_refresh_current": True,
        # v2.14 Chrome-backed incremental scanner. Listing pages use
        # Network.loadNetworkResource through the already-verified Chrome
        # profile, up to 12 at once, without rendering each page in a tab.
        "concurrent_requests": 12,
        "network_retry_attempts": 4,
        "rate_limit_base_seconds": 1.5,
        "navigation_fallback": True,
    },
    "background_mosaics": {
        "original": {"enabled": True, "upcoming_count": 3, "idle_only": True, "idle_minutes": 10},
        "review": {"enabled": True, "upcoming_count": 3, "idle_only": True, "idle_minutes": 10},
    },
    "library_background": {
        "enabled": True,
        "original_enabled": True,
        "review_enabled": True,
        "idle_minutes": 10,
        "rescan_minutes": 120,
        "pause_when_active": True,
        # Liveness guardrails: a single corrupt/transient model can never pin
        # the largest-first pass forever. These are total attempts per model
        # chunk before the worker records the problem and advances to the next
        # model.
        "mosaic_retry_attempts": 3,
        "mosaic_retry_delay_seconds": 2,
        # If the library worker itself crashes outside a per-model guard, retry
        # the saved pass cursor after a short unattended backoff.
        "error_retry_minutes": 5,
    },
    "non_nsfw_cleanup": {
        "enabled": True,
        "idle_minutes": 10,
        "rescan_minutes": 240,
        "pause_when_active": True,
        "sample_every_seconds": 180,
        "detector_model": "640m",
        "confidence_threshold": 0.45,
        "borderline_confidence": 0.20,
        "batch_size": 3,
        # v2.13 extracts several exact sample timestamps in one ffmpeg process
        # before batched NudeNet inference, reducing process-launch overhead.
        "frame_extract_timeout_seconds": 45,
        "frame_extract_batch_size": 6,
        "columns": 4,
        "tile_width": 260,
        "max_tiles_per_image": 120,
        "jpeg_quality": 84,
        "recent_write_grace_seconds": 120,
        "explicit_classes": [
            "ANUS_EXPOSED",
            "BUTTOCKS_EXPOSED",
            "FEMALE_BREAST_EXPOSED",
            "FEMALE_GENITALIA_EXPOSED",
            "MALE_GENITALIA_EXPOSED"
        ]
    },
    "keep_last": {
        "file_path": "keeplasts.txt",
        "gap_minutes": 30,
        "recent_write_grace_seconds": 120
    },
    "background_scheduler": {
        "priority": "mosaics_first"
    },
    "action_queue": {
        "initial_retry_seconds": 20,
        "maximum_retry_seconds": 300,
        "maximum_attempts": 3,
        "batch_size": 64,
        # Precise Review frame-cut jobs are deliberately deferred until the PC
        # has been idle for this long. They still outrank whole-library mosaic
        # generation and Non-NSFW scanning once eligible.
        "frame_cut_idle_minutes": 10,
    },
    "model_admin": {
        "enabled": True,
        "auto_discover": True,
        "models_json_paths": [],
    },
    "live_bridge": {
        "enabled": True,
        "auto_discover": True,
        "controller_config_path": "",
        "ctbrec_dir": "",
        "bridge_port": 8791,
        "heartbeat_seconds": 2.0,
        "snapshot_poll_seconds": 5.0,
        "offline_grace_seconds": 10.0,
    },
    "speed_mode": {
        "ready_suggestions": 8,
        "offline_pack_chunks": 12,
        "offline_pack_max_mb": 750,
        # Only validate a tiny warm set before returning a tapped model to the
        # phone.  Remaining ready chunks are hydrated by the priority worker.
        "open_fast_initial_chunks": 3,
        # Deprecated v2.13.0 transport knob retained only for config compatibility.
        # v2.13.3 intentionally does not gzip API JSON at this Python layer.
        "json_gzip_threshold_bytes": 4096,
    },
    "video_preview": {
        "prefer_hls_on_mobile": True,
        "hls_segment_seconds": 8,
        "max_width": 960,
        "video_crf": 28,
        "audio_bitrate_kbps": 96,
        "cache_hours": 18,
        "seekable_cache_max_gb": 8,
    },
}

def _looks_like_v213_settings_form_corruption(raw: Any) -> bool:
    """Recognize the exact v2.13.x uninitialized-settings overwrite fingerprint.

    v2.13.0-v2.13.2 could call saveSettings() as a side effect of unrelated
    actions (Launch Recu / run background work) before the settings GET had
    populated the form.  The browser then POSTed HTML minima/unchecked boxes
    over the user's real configuration.  Be intentionally conservative: repair
    only the distinctive multi-section fingerprint seen in field diagnostics.
    """
    if not isinstance(raw, dict):
        return False
    try:
        mosaic = raw.get("mosaic_settings", {}) if isinstance(raw.get("mosaic_settings"), dict) else {}
        review = raw.get("review_settings", {}) if isinstance(raw.get("review_settings"), dict) else {}
        recu = raw.get("recu", {}) if isinstance(raw.get("recu"), dict) else {}
        bg = raw.get("background_mosaics", {}) if isinstance(raw.get("background_mosaics"), dict) else {}
        obg = bg.get("original", {}) if isinstance(bg.get("original"), dict) else {}
        rbg = bg.get("review", {}) if isinstance(bg.get("review"), dict) else {}
        library = raw.get("library_background", {}) if isinstance(raw.get("library_background"), dict) else {}
        nsfw = raw.get("non_nsfw_cleanup", {}) if isinstance(raw.get("non_nsfw_cleanup"), dict) else {}
        admin = raw.get("model_admin", {}) if isinstance(raw.get("model_admin"), dict) else {}
        bridge = raw.get("live_bridge", {}) if isinstance(raw.get("live_bridge"), dict) else {}
        action = raw.get("action_queue", {}) if isinstance(raw.get("action_queue"), dict) else {}
        markers = [
            int(mosaic.get("sample_every_seconds", -1)) == 15,
            int(mosaic.get("columns", -1)) == 1,
            int(mosaic.get("tile_width", -1)) == 160,
            int(mosaic.get("max_total_frames", -1)) == 1,
            mosaic.get("use_existing_mosaics") is False,
            int(review.get("sample_every_seconds", -1)) == 15,
            int(review.get("columns", -1)) == 1,
            int(review.get("tile_width", -1)) == 160,
            int(review.get("max_total_frames", -1)) == 1,
            review.get("use_existing_mosaics") is False,
            recu.get("enabled") is False and int(recu.get("browser_debug_port", -1)) == 1024,
            obg.get("enabled") is False and int(obg.get("upcoming_count", -1)) == 0,
            rbg.get("enabled") is False and int(rbg.get("upcoming_count", -1)) == 0,
            library.get("enabled") is False and library.get("original_enabled") is False and library.get("review_enabled") is False,
            nsfw.get("enabled") is False and int(nsfw.get("sample_every_seconds", -1)) == 30 and int(nsfw.get("columns", -1)) == 1,
            admin.get("enabled") is False and admin.get("auto_discover") is False,
            bridge.get("enabled") is False and bridge.get("auto_discover") is False,
            float(action.get("frame_cut_idle_minutes", -1)) == 0.0,
            mosaic.get("recu_enabled") is True and recu.get("enabled") is False,
        ]
        # Require both the characteristic mosaic minima and a broad majority of
        # the cross-section markers.  This makes accidental repair of a genuinely
        # customized configuration extraordinarily unlikely.
        return all(markers[:10]) and sum(1 for value in markers if value) >= 16
    except Exception:
        return False


def _repair_v213_settings_form_corruption(config: Dict[str, Any]) -> None:
    """Restore only settings known to have been overwritten by the bad form POST."""
    mosaic = dict(config.get("mosaic_settings", {}) if isinstance(config.get("mosaic_settings"), dict) else {})
    mosaic.update({
        "sample_every_seconds": 300, "columns": 3, "tile_width": 480,
        "max_total_frames": 240, "use_existing_mosaics": True, "recu_enabled": True,
    })
    config["mosaic_settings"] = mosaic
    review = dict(config.get("review_settings", {}) if isinstance(config.get("review_settings"), dict) else {})
    review.update({
        "sample_every_seconds": 180, "columns": 3, "tile_width": 480,
        "max_total_frames": 300, "use_existing_mosaics": True,
    })
    config["review_settings"] = review

    recu = dict(config.get("recu", {}) if isinstance(config.get("recu"), dict) else {})
    recu.update({"enabled": True, "browser_debug_port": 9223, "auto_refresh_current": True})
    recu["base_url"] = str(recu.get("base_url", "https://recu.me")).strip().rstrip("/") or "https://recu.me"
    config["recu"] = recu

    config["background_mosaics"] = json.loads(json.dumps(DEFAULT_CONFIG["background_mosaics"]))
    config["library_background"] = json.loads(json.dumps(DEFAULT_CONFIG["library_background"]))
    config["non_nsfw_cleanup"] = json.loads(json.dumps(DEFAULT_CONFIG["non_nsfw_cleanup"]))

    action = dict(config.get("action_queue", {}) if isinstance(config.get("action_queue"), dict) else {})
    action["frame_cut_idle_minutes"] = 10
    config["action_queue"] = action

    admin = dict(config.get("model_admin", {}) if isinstance(config.get("model_admin"), dict) else {})
    admin["enabled"] = True
    admin["auto_discover"] = True
    admin.setdefault("models_json_paths", [])
    config["model_admin"] = admin

    bridge = dict(config.get("live_bridge", {}) if isinstance(config.get("live_bridge"), dict) else {})
    bridge["enabled"] = True
    bridge["auto_discover"] = True
    if int(bridge.get("bridge_port", 8791) or 8791) == 1024:
        bridge["bridge_port"] = 8791
    config["live_bridge"] = bridge



def append_log(message: str) -> None:
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomic JSON write with retries for transient Windows file locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    last = None
    for delay in (0.0, 0.05, 0.10, 0.20, 0.40, 0.80):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    if last is not None:
        raise last


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses inspect sys.modules while the file is executing.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def human_size(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} PB"


def drive_label(path: Path) -> str:
    drive, _tail = ntpath.splitdrive(str(path))
    return drive.upper() if drive else (path.anchor or "LOCAL").rstrip("\\/").upper() or "LOCAL"


def normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def resolve_path(raw: str) -> Path:
    value = os.path.expandvars(os.path.expanduser(str(raw or "").strip().strip('"')))
    path = Path(value)
    return path if path.is_absolute() else APP_DIR / path


def parse_roots(path: Path) -> List[Path]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    output: List[Path] = []
    seen: Set[str] = set()
    for raw_line in text.splitlines():
        clean = raw_line.strip()
        if not clean or clean.startswith("#"):
            continue
        clean = re.split(r"\s+#", clean, maxsplit=1)[0].strip()
        for token in re.split(r"[;,]", clean):
            token = token.strip().strip('"')
            if not token:
                continue
            root = Path(os.path.expandvars(os.path.expanduser(token)))
            key = normalized(root)
            if key not in seen:
                seen.add(key)
                output.append(root)
    return output


def parse_extensions(value: str) -> Set[str]:
    parts = re.split(r"[,;\s]+", str(value or "").strip().lower())
    found = {(part if part.startswith(".") else "." + part) for part in parts if part}
    return {ext for ext in found if ext in VIDEO_EXTS} or {".mp4", ".ts"}


def direct_video_byte_breakdown(folder: Path, extensions: Set[str]) -> Tuple[int, int]:
    """Return (all configured video bytes, exact .ts-extension bytes).

    The catalog already walks every direct recording once.  Keeping the TS
    subtotal in that same pass makes the TS-only model ordering effectively
    free and avoids a second library crawl when the phone changes sort mode.
    """
    total = 0
    ts_total = 0
    try:
        with os.scandir(folder) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    suffix = path.suffix.lower()
                    if suffix in extensions:
                        size = int(entry.stat(follow_symlinks=False).st_size)
                        total += size
                        if suffix == ".ts":
                            ts_total += size
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total, ts_total


def direct_video_bytes(folder: Path, extensions: Set[str]) -> int:
    return direct_video_byte_breakdown(folder, extensions)[0]


def video_content_type(path: Path) -> str:
    """Stable media MIME mapping independent of the Windows registry.

    Python's mimetypes database can classify `.ts` as Qt Linguist text on some
    systems.  With `nosniff`, that makes an otherwise playable transport stream
    fail in the browser.  Use explicit video types for all supported media.
    """
    suffix = path.suffix.casefold()
    explicit = {
        ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
        ".ts": "video/mp2t", ".mpegts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
        ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".webm": "video/webm",
    }
    return explicit.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def recursive_video_bytes(folder: Path, extensions: Set[str]) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = Path(root) / name
                if path.suffix.lower() not in extensions:
                    continue
                try:
                    total += int(path.stat().st_size)
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def configured_deletion_roots(roots: Sequence[Path]) -> Dict[str, Path]:
    output: Dict[str, Path] = {}
    for root in roots:
        drive = drive_label(root)
        drive_prefix, _tail = ntpath.splitdrive(str(root))
        if drive_prefix:
            output.setdefault(drive, Path(drive_prefix + os.sep) / "MARKED_FOR_DELETION")
        else:
            output.setdefault(drive, root.parent / "MARKED_FOR_DELETION")
    return output


def computer_idle_seconds() -> Optional[float]:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except Exception:
        return None


def find_chrome(configured: str = "") -> Optional[Path]:
    candidates: List[Path] = []
    if str(configured or "").strip():
        candidates.append(resolve_path(str(configured)))
    for raw in (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if raw:
            candidates.append(Path(raw) / "Google" / "Chrome" / "Application" / "chrome.exe")
    found = shutil.which("chrome") or shutil.which("chrome.exe") or shutil.which("google-chrome")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def safe_model_folder_name(value: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "model")).strip(" ._")
    return (clean or "model")[:120]


@dataclass
class DeletedVideo:
    path: Path
    start: datetime
    size: int
    mtime: float
    duration: float

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=max(0.1, float(self.duration)))


@dataclass
class DeletedChunk:
    folder: Path
    model_folder: Path
    files: List[DeletedVideo]
    start: datetime
    end: datetime
    source_bytes: int
    signature: str
    mosaics: List[Path] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(max(0.1, float(file.duration)) for file in self.files)


class RecuReauthRequired(RuntimeError):
    """The verified Recu browser/session must be refreshed by the user."""

    reauth_required = True


class RecuBackgroundFetchUnavailable(RuntimeError):
    """CDP background resource loading is unsupported/refused; navigation may fallback."""


class RecuBrowserBridge:
    """Dedicated Chrome profile used for human Cloudflare verification and CDP fetches."""

    def __init__(self, state: "MobileReviewerState") -> None:
        self.state = state
        self.lock = threading.RLock()
        self.process: Optional[subprocess.Popen[Any]] = None
        self._message_id = 0
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_until = 0.0
        # v2.14.1 transport health: authentication state and transport state are
        # intentionally separate.  A browser can be perfectly verified while
        # the experimental Network.loadNetworkResource path is rejected by the
        # site/Chrome.  Never turn that transport failure into a user re-auth
        # prompt unless the reliable browser-navigation probe fails too.
        self._transport_lock = threading.RLock()
        self._preferred_transport = "auto"
        self._transport_reason = "not probed yet"

    @property
    def settings(self) -> Dict[str, Any]:
        raw = self.state.config.get("recu", {})
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def port(self) -> int:
        return max(1024, min(65535, int(self.settings.get("browser_debug_port", 9223))))

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url", "https://recu.me")).strip().rstrip("/") or "https://recu.me"

    def endpoint_json(self, path: str, method: str = "GET") -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(request, timeout=4) as response:
            return json.loads(response.read().decode("utf-8"))

    def is_running(self) -> bool:
        try:
            data = self.endpoint_json("/json/version")
            return bool(data.get("webSocketDebuggerUrl"))
        except Exception:
            return False

    def launch(self) -> Dict[str, Any]:
        with self.lock:
            if self.is_running():
                return {"running": True, "message": "The dedicated Recu verification browser is already running."}
            chrome = find_chrome(str(self.settings.get("chrome_path", "")))
            if chrome is None:
                raise RuntimeError("Google Chrome was not found. Set its path in Mobile Settings.")
            RECU_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            command = [
                str(chrome),
                f"--remote-debugging-port={self.port}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-allow-origins=*",
                f"--user-data-dir={RECU_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                self.base_url,
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.process = subprocess.Popen(command, creationflags=creationflags)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.is_running():
                    return {
                        "running": True,
                        "message": "Chrome opened on the PC. Complete Recu verification through Chrome Remote Desktop, then return and capture the session.",
                    }
                time.sleep(0.35)
            raise RuntimeError("Chrome opened but its local debugging endpoint did not become ready.")

    def _browser_ws_url(self) -> str:
        data = self.endpoint_json("/json/version")
        value = str(data.get("webSocketDebuggerUrl", ""))
        if not value:
            raise RuntimeError("The Recu browser debugging endpoint is unavailable.")
        return value

    def _call_ws(self, ws: Any, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            payload = json.loads(ws.recv())
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise RuntimeError(str(payload["error"].get("message", payload["error"])))
                return dict(payload.get("result", {}))
        raise RuntimeError(f"Chrome did not answer {method}.")

    def capture_session(self) -> Dict[str, Any]:
        """Capture the exact dedicated-Chrome Recu session after explicit user confirmation.

        v2.14.3 deliberately mirrors the known-working Recu Clip Model Counter
        v2.4.1 methodology: the *user* decides when the visible Chrome page has
        passed Cloudflare, then Capture reads Browser.getVersion +
        Storage.getCookies.  Capture itself does **not** inspect the DOM, guess
        whether a normal public Recu page is a login/challenge page, navigate the
        tab, or require a background HTTP probe to succeed.

        This matters because normal Recu pages can legitimately contain
        Cloudflare helper/script markup and Sign In UI even when the public site
        is fully accessible.  Those markers caused the v2.14.x false verification
        loop seen in the field.  Actual scans still detect the four challenge
        markers used by the working counter and can fall back from cookie HTTP to
        real Chrome navigation when needed.
        """
        if websocket is None:
            raise RuntimeError("websocket-client is not installed. Run install_mobile_reviewer.bat again.")
        with self.lock:
            if not self.is_running():
                raise RuntimeError("Start the dedicated Recu browser first.")

            ws = websocket.create_connection(
                self._browser_ws_url(),
                timeout=30,
                origin=f"http://127.0.0.1:{self.port}",
            )
            try:
                version = self._call_ws(ws, "Browser.getVersion")
                cookies_result = self._call_ws(ws, "Storage.getCookies")
            finally:
                ws.close()

            cookies = []
            for item in cookies_result.get("cookies", []):
                domain = str(item.get("domain", "")).casefold().lstrip(".")
                if domain == "recu.me" or domain.endswith(".recu.me"):
                    cookies.append(item)

            cookie_header = "; ".join(
                f"{item.get('name')}={item.get('value')}"
                for item in cookies
                if item.get("name")
            )
            names = sorted({str(item.get("name", "")) for item in cookies if item.get("name")})
            captured_at = datetime.now().isoformat(timespec="seconds")
            preferred = "cookie_http" if cookie_header else "navigation"
            reason = (
                "explicit user-confirmed Chrome capture; persistent Cookie/User-Agent HTTP primary"
                if cookie_header
                else "explicit user-confirmed Chrome capture; no Recu cookies exported, navigation primary"
            )
            payload = {
                "schema_version": 2143,
                "cookie": cookie_header,
                "cookie_names": names,
                "user_agent": str(version.get("userAgent", "")),
                "captured_at": captured_at,
                "validated_at": captured_at,
                "user_confirmed_at": captured_at,
                "validation_transport": "explicit_user_confirmation",
                "browser_product": str(version.get("product", "")),
                "preferred_transport": preferred,
                "transport_reason": reason,
            }

            atomic_write_json(RECU_SESSION_PATH, payload)
            try:
                os.chmod(RECU_SESSION_PATH, 0o600)
            except OSError:
                pass
            self.state.apply_recu_session(payload)
            self._set_transport(preferred, reason)
            append_log(
                f"Recu capture accepted by explicit user confirmation: cookies={len(names)}; "
                f"transport={preferred}; no DOM/login heuristic or navigation was run during capture."
            )

            if cookie_header:
                message = (
                    f"Captured {len(names)} Recu cookie(s) from the dedicated Chrome profile. "
                    "Capture was accepted exactly as shown in Chrome; the scanner will try the persistent "
                    "Cookie/User-Agent path first and fall back to that same Chrome session if needed."
                )
            else:
                message = (
                    "Captured the explicitly confirmed Recu Chrome session. Chrome exported no Recu cookies, "
                    "so scans will use the verified Chrome navigation fallback instead of rejecting Capture."
                )
            return {
                "captured": True,
                "verified": True,
                "cookie_names": names,
                "captured_at": captured_at,
                "validated_at": captured_at,
                "preferred_transport": preferred,
                "probe_ok": None,
                "message": message,
            }

    def _page_target(self) -> Dict[str, Any]:
        targets = self.endpoint_json("/json/list")
        for target in targets:
            if target.get("type") == "page" and str(target.get("url", "")).startswith(self.base_url):
                return target
        for target in targets:
            if target.get("type") == "page":
                return target
        encoded = urllib.parse.quote("about:blank", safe="")
        return self.endpoint_json(f"/json/new?{encoded}", method="PUT")

    # Exact challenge markers used by the field-proven Recu Clip Model Counter
    # v2.4.1.  Do not broaden this with generic Cloudflare script/footer text:
    # normal accessible Recu pages can contain those strings.
    RECU_CHALLENGE_TOKENS = (
        "cf-chl-",
        "verify you are human",
        "just a moment",
        "checking your browser",
    )

    @classmethod
    def _challenge_marker(cls, html: str) -> str:
        lowered = str(html or "").casefold()
        for token in cls.RECU_CHALLENGE_TOKENS:
            if token in lowered:
                return token
        return ""

    @classmethod
    def _looks_like_challenge(cls, html: str) -> bool:
        return bool(cls._challenge_marker(html))

    @staticmethod
    def _looks_like_login(html: str, final_url: str = "") -> bool:
        # Recu's public pages normally display Sign In controls and can contain
        # dormant password/login markup.  That is NOT evidence that Cloudflare
        # verification failed.  Only an actual navigation to a login route is
        # considered a login redirect.
        try:
            path = urllib.parse.urlparse(str(final_url or "")).path.casefold().rstrip("/")
        except Exception:
            path = str(final_url or "").casefold()
        return path in {"/login", "/signin", "/account/login", "/account/signin"}

    @staticmethod
    def _has_listing(html: str) -> bool:
        lowered = str(html or "").casefold()
        return (
            "video-thumb" in lowered
            or re.search(r"href=[\"'][^\"']*/(?:[^/\"']+/)?video/\d+/play", str(html or ""), re.IGNORECASE) is not None
        )

    @staticmethod
    def _has_authenticated_shell(html: str) -> bool:
        """Recognize a normal signed-in Recu page even when a listing is empty.

        Saved real Recu pages contain /account/signout and a Sign Out control.
        This lets us distinguish a legitimate zero-result kink page from the
        challenge/login/redirect states that actually require re-verification.
        """
        lowered = str(html or "").casefold()
        return (
            "/account/signout" in lowered
            or ("top-signin-signout-button" in lowered and "sign out" in lowered)
            or ('id="dropdown-user-menu"' in lowered and "my account" in lowered and "sign out" in lowered)
        )

    @staticmethod
    def _has_normal_recu_shell(html: str) -> bool:
        lowered = str(html or "").casefold()
        return any(token in lowered for token in (
            "the ultimate chaturbate archive",
            "the biggest chaturbate archive",
            "most bookmarked recordings",
            'data-testid="search-desktop"',
            "js-performer-search-suggest",
            "header-main__logo",
        ))

    def _set_transport(self, name: str, reason: str) -> None:
        with self._transport_lock:
            self._preferred_transport = str(name or "auto")
            self._transport_reason = str(reason or "")

    def transport_status(self) -> Dict[str, str]:
        with self._transport_lock:
            return {
                "preferred_transport": self._preferred_transport,
                "transport_reason": self._transport_reason,
            }

    def _validate_recu_html(
        self,
        url: str,
        html: str,
        status: int = 200,
        final_url: str = "",
        expect_listing: bool = False,
    ) -> str:
        if int(status or 0) == 429:
            raise RuntimeError(f"Recu rate limited the request for {url} (HTTP 429).")
        if int(status or 0) in {401, 403}:
            raise RecuReauthRequired(
                f"Recu authentication is no longer valid (HTTP {status}). Open the verification browser and re-authenticate."
            )
        if int(status or 0) == 404:
            raise RecuReauthRequired(
                "Recu returned an unexpected 404 for a listing page. Re-authenticate in the verification browser."
            )
        if self._looks_like_challenge(html):
            raise RecuReauthRequired(
                "Recu is showing a browser verification page. Complete verification in the dedicated Chrome window, then capture the session again."
            )
        if self._looks_like_login(html, final_url) and not self._has_authenticated_shell(html):
            raise RecuReauthRequired(
                "Recu redirected to login. Sign in again in the dedicated Chrome window, then capture the session."
            )
        if expect_listing and not self._has_listing(html):
            # A valid authenticated Recu page can legitimately contain zero
            # matching recordings. v2.14.0 treated every such page as expired
            # authentication, which caused the exact "captured cookies ->
            # verification needed" loop seen in the field. Only an unverified
            # shell is auth-suspicious; callers then confirm via real browser
            # navigation before surfacing a re-auth prompt.
            if not (self._has_authenticated_shell(html) or self._has_normal_recu_shell(html)):
                raise RecuReauthRequired(
                    "Recu returned a listing page without clip tiles and without a recognizable normal Recu shell. "
                    "The browser session must be checked before treating this as an empty model."
                )
        if not str(html or "").strip():
            raise RecuReauthRequired(
                "Recu returned an empty page. Re-authenticate in the verification browser."
            )
        return html

    def _call_target(self, ws: Any, method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
        with self.lock:
            self._message_id += 1
            message_id = self._message_id
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + max(5, int(timeout))
        while time.monotonic() < deadline:
            payload = json.loads(ws.recv())
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise RuntimeError(str(payload["error"].get("message", payload["error"])))
                return dict(payload.get("result", {}))
        raise RuntimeError(f"Chrome did not answer {method}.")

    def _read_cdp_stream(self, ws: Any, handle: str, timeout: int) -> str:
        chunks: List[bytes] = []
        try:
            while True:
                result = self._call_target(ws, "IO.read", {"handle": handle, "size": 1024 * 1024}, timeout=timeout)
                data = str(result.get("data", ""))
                if data:
                    if bool(result.get("base64Encoded")):
                        chunks.append(base64.b64decode(data))
                    else:
                        chunks.append(data.encode("utf-8", errors="replace"))
                if bool(result.get("eof")):
                    break
        finally:
            try:
                self._call_target(ws, "IO.close", {"handle": handle}, timeout=5)
            except Exception:
                pass
        return b"".join(chunks).decode("utf-8", errors="replace")

    @staticmethod
    def _retry_after_seconds(headers: Any) -> float:
        if isinstance(headers, dict):
            value = headers.get("retry-after") or headers.get("Retry-After") or ""
            try:
                return max(0.0, min(120.0, float(value)))
            except Exception:
                return 0.0
        return 0.0

    def fetch_html_cookie_http(self, url: str, timeout: int = 30, expect_listing: bool = False) -> str:
        """Fast captured-cookie HTTP path used when Chrome/CDP background loading is unsuitable.

        This deliberately reuses the proven pre-v2.14 Recu HTTP client and the
        exact Cookie/User-Agent captured from the verified Chrome profile.
        Transport rejection here is still *not* proof that browser verification
        expired; callers confirm with verified navigation before asking the user
        to re-authenticate.
        """
        settings = dict(self.state.mosaic_settings)
        settings["recu_request_timeout_seconds"] = max(5, int(timeout))
        html = self.state._original_recu_fetch(url, settings, threading.Event(), 1)
        return self._validate_recu_html(
            url, html, status=200, final_url=url, expect_listing=expect_listing
        )

    def fetch_many_cookie_http(
        self,
        urls: Sequence[str],
        expect_listing: bool = True,
        max_workers: int = 12,
        timeout: int = 30,
    ) -> Dict[str, str]:
        unique = list(dict.fromkeys(str(value) for value in urls if str(value)))
        if not unique:
            return {}
        workers = max(1, min(24, int(max_workers), len(unique)))
        output: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recu-http") as pool:
            futures = {
                pool.submit(self.fetch_html_cookie_http, url, timeout, expect_listing): url
                for url in unique
            }
            for future, url in list(futures.items()):
                output[url] = future.result()
        return output

    def fetch_html_background(self, url: str, timeout: int = 30, expect_listing: bool = False) -> str:
        """Load one Recu resource through Chrome's authenticated network stack."""
        if websocket is None:
            raise RecuBackgroundFetchUnavailable("websocket-client is not installed.")
        if not self.is_running():
            raise RecuBackgroundFetchUnavailable("The verified Recu Chrome session is not running.")
        settings = self.settings
        attempts = max(1, min(8, int(settings.get("network_retry_attempts", 4))))
        base_backoff = max(0.25, float(settings.get("rate_limit_base_seconds", 1.5)))
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            with self._rate_limit_lock:
                shared_wait = max(0.0, self._rate_limit_until - time.monotonic())
            if shared_wait > 0:
                time.sleep(min(shared_wait, 120.0))
            target = self._page_target()
            ws_url = str(target.get("webSocketDebuggerUrl", ""))
            if not ws_url:
                raise RecuBackgroundFetchUnavailable("No Chrome page target is available.")
            ws = websocket.create_connection(
                ws_url,
                timeout=max(10, timeout),
                origin=f"http://127.0.0.1:{self.port}",
            )
            try:
                self._call_target(ws, "Network.enable", timeout=timeout)
                frame_tree = self._call_target(ws, "Page.getFrameTree", timeout=timeout)
                frame = dict(frame_tree.get("frameTree", {}).get("frame", {}))
                frame_id = str(frame.get("id", ""))
                params: Dict[str, Any] = {
                    "url": url,
                    "options": {"disableCache": True, "includeCredentials": True},
                }
                if frame_id:
                    params["frameId"] = frame_id
                try:
                    result = self._call_target(ws, "Network.loadNetworkResource", params, timeout=timeout)
                except Exception as exc:
                    message = str(exc)
                    if "wasn't found" in message or "not found" in message or "Method" in message:
                        raise RecuBackgroundFetchUnavailable(
                            "This Chrome build refused Network.loadNetworkResource."
                        ) from exc
                    raise
                resource = dict(result.get("resource", {}))
                status = int(resource.get("httpStatusCode", 0) or 0)
                headers = resource.get("headers", {})
                if status == 429:
                    retry_after = self._retry_after_seconds(headers)
                    delay = retry_after or min(60.0, base_backoff * (2 ** (attempt - 1)))
                    with self._rate_limit_lock:
                        self._rate_limit_until = max(self._rate_limit_until, time.monotonic() + delay)
                    last_error = RuntimeError(f"HTTP 429 for {url}")
                    if attempt < attempts:
                        time.sleep(delay)
                        continue
                    raise last_error
                if not bool(resource.get("success", True)) and not status:
                    raise RuntimeError(str(resource.get("netErrorName") or resource.get("netError") or "Chrome resource load failed"))
                stream = str(resource.get("stream", ""))
                if not stream:
                    raise RecuBackgroundFetchUnavailable(
                        "Chrome loaded the Recu resource but did not expose a readable response stream."
                    )
                html = self._read_cdp_stream(ws, stream, timeout)
                return self._validate_recu_html(url, html, status=status or 200, final_url=url, expect_listing=expect_listing)
            except RecuReauthRequired:
                raise
            except RecuBackgroundFetchUnavailable:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(15.0, base_backoff * attempt))
                    continue
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        raise RuntimeError(str(last_error or f"Could not load {url} through Chrome."))

    def fetch_many_background(
        self,
        urls: Sequence[str],
        expect_listing: bool = True,
        max_workers: int = 12,
        timeout: int = 30,
    ) -> Dict[str, str]:
        """Fetch Recu listing pages concurrently through verified Chrome."""
        unique = list(dict.fromkeys(str(value) for value in urls if str(value)))
        if not unique:
            return {}
        workers = max(1, min(24, int(max_workers), len(unique)))
        output: Dict[str, str] = {}
        errors: List[Tuple[str, Exception]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recu-cdp") as pool:
            futures = {
                pool.submit(self.fetch_html_background, url, timeout, expect_listing): url
                for url in unique
            }
            for future, url in list(futures.items()):
                try:
                    output[url] = future.result()
                except RecuReauthRequired:
                    for other in futures:
                        other.cancel()
                    raise
                except Exception as exc:
                    errors.append((url, exc))
        if errors:
            unavailable = [exc for _url, exc in errors if isinstance(exc, RecuBackgroundFetchUnavailable)]
            if unavailable:
                raise unavailable[0]
            url, exc = errors[0]
            raise RuntimeError(f"{url}: {exc}")
        return output

    def fetch_html_navigation(self, url: str, timeout: int = 40, expect_listing: bool = False) -> str:
        """Reliability fallback: verified Chrome navigation with heavy assets blocked."""
        if websocket is None:
            raise RuntimeError("websocket-client is not installed.")
        with self.lock:
            if not self.is_running():
                raise RuntimeError("The verified Recu browser is not running.")
            target = self._page_target()
            ws_url = str(target.get("webSocketDebuggerUrl", ""))
            if not ws_url:
                raise RuntimeError("No Chrome page target is available.")
            ws = websocket.create_connection(
                ws_url,
                timeout=max(10, timeout),
                origin=f"http://127.0.0.1:{self.port}",
            )
            try:
                self._call_ws(ws, "Page.enable")
                self._call_ws(ws, "Runtime.enable")
                try:
                    self._call_ws(ws, "Network.enable")
                    self._call_ws(ws, "Network.setBlockedURLs", {"urls": [
                        "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.ico",
                        "*.mp4", "*.webm", "*.m3u8", "*.ts", "*.m4s",
                        "*.woff", "*.woff2", "*.ttf", "*.otf",
                    ]})
                except Exception:
                    pass
                self._call_ws(ws, "Page.navigate", {"url": url})
                deadline = time.monotonic() + max(10, timeout)
                html = ""
                final_url = url
                while time.monotonic() < deadline:
                    time.sleep(0.20)
                    result = self._call_ws(
                        ws,
                        "Runtime.evaluate",
                        {
                            "expression": "JSON.stringify({html:(document.documentElement?document.documentElement.outerHTML:''),href:location.href,ready:document.readyState})",
                            "returnByValue": True,
                        },
                    )
                    raw = str(result.get("result", {}).get("value", "") or "")
                    try:
                        snapshot = json.loads(raw)
                    except Exception:
                        snapshot = {"html": raw, "href": final_url, "ready": ""}
                    html = str(snapshot.get("html", ""))
                    final_url = str(snapshot.get("href", final_url))
                    challenge_marker = self._challenge_marker(html)
                    login_redirect = self._looks_like_login(html, final_url)
                    if challenge_marker or login_redirect:
                        reason = f"challenge marker {challenge_marker!r}" if challenge_marker else f"login redirect to {final_url}"
                        append_log(f"Recu browser navigation classified auth as invalid: {reason}; url={url}")
                        raise RecuReauthRequired(
                            "Recu verification is required in the dedicated Chrome window."
                            if challenge_marker else
                            "Recu redirected to a login page in the dedicated Chrome window."
                        )
                    listing_ready = self._has_listing(html)
                    ready = str(snapshot.get("ready", "")) in {"interactive", "complete"}
                    if (expect_listing and listing_ready) or (not expect_listing and len(html) > 500 and ready):
                        try:
                            self._call_ws(ws, "Page.stopLoading")
                        except Exception:
                            pass
                        return self._validate_recu_html(
                            url, html, status=200, final_url=final_url, expect_listing=expect_listing
                        )
                return self._validate_recu_html(
                    url, html, status=200, final_url=final_url, expect_listing=expect_listing
                )
            finally:
                ws.close()

    def _navigation_fallback_one(
        self,
        url: str,
        timeout: int,
        expect_listing: bool,
        *,
        failed_transport: str,
        failed_error: Exception,
    ) -> str:
        """Authoritative auth check after a fast transport fails.

        Only verified Chrome navigation is allowed to turn an auth-suspect fast
        response into a real RecuReauthRequired.  If navigation succeeds, the
        browser session is good and the failed transport is simply demoted.
        """
        append_log(
            f"Recu {failed_transport} transport failed/suspected auth; confirming with verified navigation: "
            f"{type(failed_error).__name__}: {failed_error}"
        )
        html = self.fetch_html_navigation(url, timeout=timeout, expect_listing=expect_listing)
        self._set_transport(
            "navigation",
            f"{failed_transport} transport was unsuitable; verified navigation succeeded",
        )
        append_log(
            f"Recu authentication remained valid; {failed_transport} was demoted and navigation succeeded for {url}"
        )
        return html

    def fetch_html(self, url: str, timeout: int = 40, expect_listing: bool = False) -> str:
        """Adaptive Recu fetch with auth state separated from transport state.

        Priority is the proven fast transport for this captured session:
        CDP Network.loadNetworkResource when it has passed a probe, otherwise
        captured-cookie HTTP.  Any auth-looking failure is confirmed through
        the exact verified Chrome navigation path before the UI is ever told to
        re-authenticate.
        """
        with self._transport_lock:
            preferred = self._preferred_transport

        # If capture selected navigation, do not repeatedly retry transports that
        # already failed during the same verified browser session.
        if preferred == "navigation":
            return self.fetch_html_navigation(url, timeout=timeout, expect_listing=expect_listing)

        if preferred == "cookie_http":
            try:
                return self.fetch_html_cookie_http(url, timeout=timeout, expect_listing=expect_listing)
            except Exception as http_error:
                if not bool(self.settings.get("navigation_fallback", True)):
                    raise
                return self._navigation_fallback_one(
                    url, timeout, expect_listing, failed_transport="captured-cookie HTTP", failed_error=http_error
                )

        # auto/cdp: try the requested high-throughput Chrome network-stack path.
        try:
            html = self.fetch_html_background(url, timeout=timeout, expect_listing=expect_listing)
            if preferred == "auto":
                self._set_transport("cdp", "Network.loadNetworkResource succeeded")
            return html
        except Exception as cdp_error:
            # Before falling all the way back to navigation, try the proven raw
            # HTTP path with the just-captured Cookie/User-Agent.  This preserves
            # 12-way throughput on installations where CDP is the incompatible
            # piece rather than the Recu session itself.
            try:
                html = self.fetch_html_cookie_http(url, timeout=timeout, expect_listing=expect_listing)
                self._set_transport(
                    "cookie_http",
                    f"CDP was unsuitable ({type(cdp_error).__name__}); captured-cookie HTTP succeeded",
                )
                append_log(
                    f"Recu CDP transport demoted; captured-cookie HTTP succeeded for {url}: "
                    f"{type(cdp_error).__name__}: {cdp_error}"
                )
                return html
            except Exception as http_error:
                if not bool(self.settings.get("navigation_fallback", True)):
                    # Preserve the most relevant original transport error.
                    raise cdp_error
                append_log(
                    f"Recu CDP and captured-cookie HTTP both failed; using verified navigation. "
                    f"CDP={type(cdp_error).__name__}: {cdp_error}; "
                    f"HTTP={type(http_error).__name__}: {http_error}"
                )
                # Crucial v2.14.1 behavior: if navigation succeeds, do NOT surface
                # the earlier RecuReauthRequired from CDP/HTTP.
                return self._navigation_fallback_one(
                    url, timeout, expect_listing, failed_transport="fast background", failed_error=http_error
                )

    def _navigation_fallback_many(
        self,
        urls: Sequence[str],
        timeout: int,
        expect_listing: bool,
        *,
        failed_transport: str,
        failed_error: Exception,
    ) -> Dict[str, str]:
        append_log(
            f"Recu {failed_transport} batch failed/suspected auth; confirming sequentially with verified navigation: "
            f"{type(failed_error).__name__}: {failed_error}"
        )
        output: Dict[str, str] = {}
        for url in urls:
            output[str(url)] = self.fetch_html_navigation(
                str(url), timeout=timeout, expect_listing=expect_listing
            )
        self._set_transport(
            "navigation",
            f"{failed_transport} batch was unsuitable; verified navigation succeeded",
        )
        append_log(
            f"Recu authentication remained valid; {failed_transport} batch was demoted after navigation fallback succeeded."
        )
        return output

    def fetch_many(self, urls: Sequence[str], expect_listing: bool = True, max_workers: int = 12) -> Dict[str, str]:
        """Adaptive 12-way Recu batch with reliable browser confirmation.

        v2.14.0 had a logic hole: RecuReauthRequired from a CDP worker was
        immediately re-raised, so the promised verified-navigation fallback was
        skipped precisely when it mattered most.  This method never declares
        re-authentication solely from a fast transport.
        """
        unique = list(dict.fromkeys(str(value) for value in urls if str(value)))
        if not unique:
            return {}
        timeout = max(10, int(self.settings.get("request_timeout_seconds", 30) or 30))
        with self._transport_lock:
            preferred = self._preferred_transport

        if preferred == "navigation":
            return {
                url: self.fetch_html_navigation(url, timeout=timeout, expect_listing=expect_listing)
                for url in unique
            }

        if preferred == "cookie_http":
            try:
                return self.fetch_many_cookie_http(
                    unique, expect_listing=expect_listing, max_workers=max_workers, timeout=timeout
                )
            except Exception as http_error:
                if not bool(self.settings.get("navigation_fallback", True)):
                    raise
                return self._navigation_fallback_many(
                    unique, timeout, expect_listing,
                    failed_transport="captured-cookie HTTP", failed_error=http_error,
                )

        try:
            result = self.fetch_many_background(
                unique,
                expect_listing=expect_listing,
                max_workers=max_workers,
                timeout=timeout,
            )
            if preferred == "auto":
                self._set_transport("cdp", "Network.loadNetworkResource batch succeeded")
            return result
        except Exception as cdp_error:
            try:
                result = self.fetch_many_cookie_http(
                    unique, expect_listing=expect_listing, max_workers=max_workers, timeout=timeout
                )
                self._set_transport(
                    "cookie_http",
                    f"CDP batch was unsuitable ({type(cdp_error).__name__}); captured-cookie HTTP succeeded",
                )
                append_log(
                    f"Recu CDP batch demoted; captured-cookie HTTP succeeded: "
                    f"{type(cdp_error).__name__}: {cdp_error}"
                )
                return result
            except Exception as http_error:
                if not bool(self.settings.get("navigation_fallback", True)):
                    raise cdp_error
                append_log(
                    f"Recu CDP and captured-cookie HTTP batch both failed; confirming via navigation. "
                    f"CDP={type(cdp_error).__name__}: {cdp_error}; "
                    f"HTTP={type(http_error).__name__}: {http_error}"
                )
                return self._navigation_fallback_many(
                    unique, timeout, expect_listing,
                    failed_transport="fast background", failed_error=http_error,
                )


def build_deletion_chunks(
    model_name: str,
    model_folders: Sequence[Path],
    settings: Dict[str, Any],
    review_module: Any,
    duration_cache: Any,
    progress: Callable[[str], None],
) -> List[DeletedChunk]:
    extensions = parse_extensions(settings.get("extensions", "mp4,ts"))
    ffmpeg, ffprobe = review_module.resolve_ffmpeg(settings)
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found. Set its path in Mobile Settings.")
    rows: List[Tuple[Path, os.stat_result]] = []
    for folder in model_folders:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = Path(root) / name
                if path.suffix.lower() not in extensions:
                    continue
                try:
                    rows.append((path, path.stat()))
                except OSError:
                    continue
    videos: List[DeletedVideo] = []
    total = len(rows)
    for index, (path, stat) in enumerate(rows, start=1):
        progress(f"Reading marked-for-deletion file {index}/{max(1, total)}")
        duration = review_module.probe_duration(
            path,
            int(stat.st_size),
            float(stat.st_mtime),
            ffmpeg,
            ffprobe,
            duration_cache,
        )
        videos.append(
            DeletedVideo(
                path=path,
                start=review_module.parse_start(path, stat.st_mtime),
                size=int(stat.st_size),
                mtime=float(stat.st_mtime),
                duration=float(duration),
            )
        )
    videos.sort(key=lambda item: (item.start, normalized(item.path)))
    gap = timedelta(minutes=max(0, int(settings.get("chunk_gap_minutes", 30))))
    groups: List[List[DeletedVideo]] = []
    current: List[DeletedVideo] = []
    latest_end: Optional[datetime] = None
    for video in videos:
        if not current:
            current = [video]
            latest_end = video.end
        elif latest_end is not None and video.start - latest_end <= gap:
            current.append(video)
            latest_end = max(latest_end, video.end)
        else:
            groups.append(current)
            current = [video]
            latest_end = video.end
    if current:
        groups.append(current)
    mosaic_folder = DELETE_MOSAIC_ROOT / safe_model_folder_name(model_name)
    mosaic_folder.mkdir(parents=True, exist_ok=True)
    chunks: List[DeletedChunk] = []
    for group in groups:
        signature = review_module.chunk_signature(group)
        chunks.append(
            DeletedChunk(
                folder=model_folders[0] if model_folders else mosaic_folder,
                model_folder=mosaic_folder,
                files=group,
                start=min(item.start for item in group),
                end=max(item.end for item in group),
                source_bytes=sum(item.size for item in group),
                signature=signature,
            )
        )
    chunks.sort(key=lambda chunk: (-chunk.source_bytes, chunk.start, chunk.signature))
    duration_cache.save()
    return chunks


class TaskCancelled(RuntimeError):
    """Cooperative cancellation for PC tasks and durable queue jobs."""


class ActionDeferred(RuntimeError):
    """Non-failure pause for an idle-only durable action that must yield."""


@dataclass
class TaskRecord:
    id: str
    label: str
    status: str = "queued"
    progress: str = "Queued."
    current: int = 0
    total: int = 0
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    interactive_priority: bool = False

    def payload(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "progress": self.progress,
            "current": self.current,
            "total": self.total,
            "result": self.result,
            "error": self.error,
            "cancel_requested": bool(self.cancel_requested),
            "interactive_priority": bool(self.interactive_priority),
            "cancellable": self.status in {"queued", "running"},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MobileReviewerState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ctbrec-mobile")
        self.interactive_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctbrec-mobile-interactive")
        # v2.9 sort-first lane: model-click preparation may have several previously
        # requested models waiting, but only the currently selected model is
        # allowed to enter heavy I/O first. Using a separate pool means a newly
        # clicked model is never trapped behind an older all-model preparation
        # task in the single-thread interactive executor.
        self.priority_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctbrec-mobile-sort-first")
        self.prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctbrec-mobile-prefetch")
        self.library_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctbrec-mobile-library")
        self.config = dict(DEFAULT_CONFIG)
        raw = load_json(CONFIG_PATH, {})
        if isinstance(raw, dict):
            self.config.update(raw)
        for nested_key in ("recu", "background_mosaics", "library_background", "non_nsfw_cleanup", "keep_last", "background_scheduler", "action_queue", "speed_mode", "video_preview", "model_admin", "live_bridge"):
            merged = dict(DEFAULT_CONFIG[nested_key])
            current_nested = self.config.get(nested_key, {})
            if isinstance(current_nested, dict):
                merged.update(current_nested)
            self.config[nested_key] = merged
        changed = False
        self.config_repair_notice = ""
        # v2.13.3 field repair: v2.13.0-v2.13.2 could serialize an
        # uninitialized Settings form as a side effect of unrelated buttons.
        # Repair ONLY the exact multi-section fingerprint observed in the real
        # diagnostics, and preserve the bad payload as a local backup first.
        if _looks_like_v213_settings_form_corruption(raw):
            backup_name = f"mobile_config.pre_v2_13_3_settings_repair_{datetime.now():%Y%m%d_%H%M%S}.json"
            backup_path = APP_DIR / backup_name
            try:
                atomic_write_json(backup_path, raw)
            except Exception as exc:
                append_log(f"Could not write pre-v2.13.3 settings-repair backup: {exc}")
            _repair_v213_settings_form_corruption(self.config)
            self.config_repair_notice = (
                "Recovered the v2.13.x uninitialized-settings overwrite. "
                f"The overwritten configuration was backed up as {backup_name}."
            )
            append_log(self.config_repair_notice)
            changed = True
        # v2.8 accuracy migration: only replace the exact legacy v2.7 defaults,
        # and only when the old config has no detector-model choice yet. Any
        # user-customized interval/threshold survives unchanged.
        legacy_nsfw = raw.get("non_nsfw_cleanup", {}) if isinstance(raw, dict) else {}
        if isinstance(legacy_nsfw, dict) and "detector_model" not in legacy_nsfw:
            current_nsfw = self.config["non_nsfw_cleanup"]
            if int(current_nsfw.get("sample_every_seconds", 300)) == 300:
                current_nsfw["sample_every_seconds"] = 180
            if abs(float(current_nsfw.get("confidence_threshold", 0.55)) - 0.55) < 1e-9:
                current_nsfw["confidence_threshold"] = 0.45
            if int(current_nsfw.get("batch_size", 6)) == 6:
                current_nsfw["batch_size"] = 3
        # v2.10 queue migration: change only the exact v2.9.1 defaults.
        # Deliberately customized retry settings are preserved.
        legacy_action = raw.get("action_queue", {}) if isinstance(raw, dict) else {}
        if isinstance(legacy_action, dict):
            aq = self.config.get("action_queue", {})
            if int(legacy_action.get("maximum_attempts", 8) or 8) == 8 and int(aq.get("maximum_attempts", 8) or 8) == 8:
                aq["maximum_attempts"] = 3; changed = True
            if int(legacy_action.get("maximum_retry_seconds", 900) or 900) == 900 and int(aq.get("maximum_retry_seconds", 900) or 900) == 900:
                aq["maximum_retry_seconds"] = 300; changed = True
        if not str(self.config.get("access_pin", "")).strip():
            self.config["access_pin"] = f"{secrets.randbelow(1_000_000):06d}"
            changed = True
        if not str(self.config.get("secret_key", "")).strip():
            self.config["secret_key"] = secrets.token_hex(32)
            changed = True
        if changed:
            atomic_write_json(CONFIG_PATH, self.config)

        self.mosaic = load_module("ctbrec_mobile_mosaic_module", MOSAIC_SCRIPT)
        self.review = load_module("ctbrec_mobile_review_module", REVIEW_SCRIPT)
        self.nsfw = load_module("ctbrec_mobile_nsfw_module", NSFW_CLEANUP_SCRIPT)
        self.keep_last = load_module("ctbrec_mobile_keep_last_module", KEEP_LAST_SCRIPT)
        self.live_bridge_module = load_module("ctbrec_mobile_live_bridge_module", LIVE_BRIDGE_SCRIPT)
        self.live_bridge = self.live_bridge_module.LiveCTBRecBridgeClient(APP_DIR, self.config.get("live_bridge", {}))
        self.model_admin_module = load_module("ctbrec_mobile_model_admin_module", MODEL_ADMIN_SCRIPT)
        self.model_admin = self.model_admin_module.MobileModelAdmin(APP_DIR, self.config.get("model_admin", {}), live_bridge=self.live_bridge)
        self.mosaic_settings = dict(self.mosaic.DEFAULT_SETTINGS)
        self.mosaic_settings.update(self.config.get("mosaic_settings", {}) or {})
        self.review_settings = dict(self.review.DEFAULT_SETTINGS)
        self.review_settings.update(self.config.get("review_settings", {}) or {})
        ffmpeg_path = str(self.config.get("ffmpeg_path", "")).strip()
        if ffmpeg_path:
            self.mosaic_settings["ffmpeg_path"] = ffmpeg_path
            self.review_settings["ffmpeg_path"] = ffmpeg_path
        recu_config = self.config.get("recu", {})
        self.mosaic_settings["recu_enabled"] = bool(recu_config.get("enabled", True))
        self.mosaic_settings["recu_base_url"] = str(recu_config.get("base_url", "https://recu.me"))
        self.mosaic_settings["auto_open_mosaic"] = False
        self.review_settings["auto_open_mosaic"] = False
        self.review_settings["confirm_moves"] = False

        self.roots_path = resolve_path(str(self.config.get("roots_file", "recording_roots.txt")))
        self.roots = parse_roots(self.roots_path)
        self.catalog: Dict[str, List[Dict[str, Any]]] = {"original": [], "review": [], "deletion": []}
        self.catalog_status: Dict[str, Any] = {
            "status": "not_started", "progress": "Not scanned yet.", "updated_at": ""
        }
        cached = load_json(CATALOG_PATH, {})
        if isinstance(cached, dict) and isinstance(cached.get("catalog"), dict):
            for mode in ("original", "review", "deletion"):
                rows = cached["catalog"].get(mode, [])
                if isinstance(rows, list):
                    self.catalog[mode] = rows
            self.catalog_status = {
                "status": "ready",
                "progress": "Loaded cached disk-size catalog.",
                "updated_at": str(cached.get("updated_at", "")),
            }

        self.tasks: Dict[str, TaskRecord] = {}
        self.queues: Dict[str, Dict[str, Any]] = {}
        # v2.13 latency caches.  These cache only derived metadata; the source
        # catalog/ready index/sidecars remain authoritative.
        self.catalog_view_cache: Dict[Tuple[str, Tuple[str, ...], str], Tuple[float, str, str, Tuple[Any, ...], List[Dict[str, Any]]]] = {}
        self.ready_count_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, str, Dict[str, int]]] = {}
        self.last_catalog_request: Dict[str, Any] = {}
        self.validated_layout_cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.delete_confirmations: Dict[str, Dict[str, Any]] = {}
        self.prefetch_futures: Dict[str, Any] = {}
        self.interactive_futures: Dict[str, Any] = {}
        self.mosaic_locks: Dict[str, threading.RLock] = {}
        self.preview_hls_locks: Dict[str, threading.Lock] = {}
        self.preview_seekable_locks: Dict[str, threading.Lock] = {}
        self.preview_duration_cache: Dict[str, Tuple[int, float, float]] = {}
        self.preview_transcode_semaphore = threading.Semaphore(1)
        self.preview_cache_last_prune = 0.0
        # Bounded in-memory trace for one-click video-preview support exports.
        # Media bytes are never copied into diagnostics.
        self.preview_diagnostics: Dict[str, Dict[str, Any]] = {}
        self.preview_diagnostics_order: List[str] = []
        self.heavy_io_lock = threading.Lock()
        self.interactive_demand = threading.Event()
        # The phone is a thin decision client. While a sorting screen is alive,
        # durable file moves/deletions wait; mosaic creation is allowed to keep
        # feeding the sorter. The lease expires automatically if the phone app
        # disappears without sending an explicit stop heartbeat.
        self.sort_session_active_until = 0.0
        # Queue currently visible on the phone. Older in-memory queues must not
        # keep prefetching forever after the phone has moved on or gone away.
        self.active_sort_queue_id = ""
        self.priority_model_key = ""
        self.priority_model_epoch = 0
        self.priority_generation_tasks: Dict[str, str] = {}
        self.priority_generation_targets: Dict[str, Set[str]] = {}
        self.library_force_requested = threading.Event()
        self.library_cancel_requested = threading.Event()
        self.library_future: Optional[Any] = None
        raw_library_state = load_json(LIBRARY_STATE_PATH, {})
        self.library_status: Dict[str, Any] = raw_library_state if isinstance(raw_library_state, dict) else {}
        self.library_status.setdefault("state", "idle")
        self.library_status.setdefault("message", "Whole-library generation has not started.")
        self.library_status.setdefault("updated_at", "")
        self.library_status.setdefault("generated", 0)
        self.library_status.setdefault("reused", 0)
        self.library_status.setdefault("errors", 0)

        raw_nsfw_state = load_json(NSFW_STATE_PATH, {})
        self.nsfw_status: Dict[str, Any] = raw_nsfw_state if isinstance(raw_nsfw_state, dict) else {}
        self.nsfw_status.setdefault("state", "idle")
        self.nsfw_status.setdefault("message", "Non-NSFW cleanup analysis has not started.")
        self.nsfw_status.setdefault("updated_at", "")
        self.nsfw_status.setdefault("models_scanned", 0)
        self.nsfw_status.setdefault("candidate_bytes", 0)
        self.nsfw_status.setdefault("errors", 0)
        raw_nsfw_index = load_json(NSFW_INDEX_PATH, {"version": 1, "models": {}})
        self.nsfw_index: Dict[str, Any] = raw_nsfw_index if isinstance(raw_nsfw_index, dict) else {"version": 1, "models": {}}
        self.nsfw_index.setdefault("version", 1)
        self.nsfw_index.setdefault("models", {})
        self.nsfw_future: Optional[Any] = None
        self.nsfw_force_requested = threading.Event()
        self.nsfw_cancel_requested = threading.Event()
        self.nsfw_process: Optional[subprocess.Popen[Any]] = None

        raw_ready = load_json(READY_INDEX_PATH, {"version": 1, "snapshots": {}})
        self.ready_index: Dict[str, Any] = raw_ready if isinstance(raw_ready, dict) else {"version": 1, "snapshots": {}}
        self.ready_index.setdefault("version", 1)
        self.ready_index.setdefault("snapshots", {})
        self.offline_media_lookup: Dict[str, List[str]] = {}
        for _snapshot in self.ready_index.get("snapshots", {}).values():
            if not isinstance(_snapshot, dict):
                continue
            _mode = str(_snapshot.get("mode", ""))
            for _row in _snapshot.get("chunks", []):
                if not isinstance(_row, dict):
                    continue
                _wid = hashlib.sha256(f"{_mode}\0{_row.get('signature', '')}".encode("utf-8")).hexdigest()[:24]
                self.offline_media_lookup[_wid] = [str(value) for value in _row.get("mosaics", [])]
        raw_offline = load_json(OFFLINE_SYNC_PATH, {"client_actions": {}})
        self.offline_sync_history: Dict[str, Any] = raw_offline if isinstance(raw_offline, dict) else {"client_actions": {}}
        self.offline_sync_history.setdefault("client_actions", {})
        raw_actions = load_json(ACTION_QUEUE_PATH, {"jobs": []})
        self.action_queue: Dict[str, Any] = raw_actions if isinstance(raw_actions, dict) else {"jobs": []}
        jobs = self.action_queue.setdefault("jobs", [])
        if not isinstance(jobs, list):
            self.action_queue["jobs"] = []
        for job in self.action_queue["jobs"]:
            if isinstance(job, dict) and job.get("status") == "running":
                job["status"] = "queued"
                job["message"] = "Recovered after server restart."
        self.action_wakeup = threading.Event()
        self.action_running_id = ""
        self._migrate_action_queue_jobs()
        self._save_action_queue()
        raw_layouts = load_json(LAYOUT_CACHE_PATH, {"layouts": {}})
        self.layout_cache: Dict[str, Any] = raw_layouts if isinstance(raw_layouts, dict) else {"layouts": {}}
        self.layout_cache.setdefault("layouts", {})
        self.mosaic_duration_cache = self.mosaic.DurationCache(self.mosaic.DURATION_CACHE_PATH)
        self.mosaic_manifest = self.mosaic.MosaicManifest(self.mosaic.MANIFEST_PATH)
        self.review_duration_cache = self.review.DurationCache(self.review.DURATION_CACHE_PATH)
        self.recu_cache = self.mosaic.RecuMetadataCache(self.mosaic.RECU_CACHE_PATH)
        self.recu_browser = RecuBrowserBridge(self)
        self._original_recu_fetch = self.mosaic.fetch_recu_html
        self.mosaic.fetch_recu_html = self._recu_fetch_bridge
        self.apply_recu_session(load_json(RECU_SESSION_PATH, {}))
        self.session_generated_signatures: Set[str] = set()
        self.stop_event = threading.Event()
        self.action_thread = threading.Thread(
            target=self._action_loop,
            name="ctbrec-mobile-actions",
            daemon=True,
        )
        self.action_thread.start()
        self.maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="ctbrec-mobile-maintenance",
            daemon=True,
        )
        self.maintenance_thread.start()
        if bool(self.config.get("scan_on_start", True)):
            self.start_catalog_scan()

    @property
    def auth_cookie_value(self) -> str:
        secret = str(self.config.get("secret_key", "")).encode("utf-8")
        return hmac.new(secret, b"ctbrec-mobile-authorized", hashlib.sha256).hexdigest()

    @property
    def device_token_value(self) -> str:
        """Durable paired-device credential used to heal lost iOS PWA cookies.

        It is installation-bound and derived from the same local secret as the
        HttpOnly cookie, but with a separate HMAC context.  The browser stores
        it only after a successful PIN pairing.  API calls can then restore a
        lost cookie without asking the user for the PIN again.
        """
        secret = str(self.config.get("secret_key", "")).encode("utf-8")
        return hmac.new(secret, b"ctbrec-mobile-device-v1", hashlib.sha256).hexdigest()

    def save_config(self) -> None:
        atomic_write_json(CONFIG_PATH, self.config)

    def apply_recu_session(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        cookie = str(payload.get("cookie", "")).strip()
        user_agent = str(payload.get("user_agent", "")).strip()
        if cookie:
            self.mosaic_settings["recu_cookie"] = cookie
        if user_agent:
            self.mosaic_settings["recu_user_agent"] = user_agent
        preferred = str(payload.get("preferred_transport", "")).strip()
        schema_version = int(payload.get("schema_version", 0) or 0)
        # v2.14.2 intentionally migrates every older captured session back to
        # the proven persistent Cookie/User-Agent implementation.  CDP remains
        # available only as an explicit fallback/diagnostic, not as the default
        # auth path that can create false verification loops.
        if schema_version < 2143:
            preferred = "cookie_http" if cookie else "navigation"
            reason = "v2.14.3 migration to explicit-confirmation Recu session semantics"
        else:
            reason = str(payload.get("transport_reason", "restored from captured session"))
        if preferred not in {"cdp", "cookie_http", "navigation"}:
            preferred = "cookie_http" if cookie else "navigation"
        if preferred == "cookie_http" and not cookie:
            preferred = "navigation"
            reason = "captured session has no exported Recu cookie; using Chrome navigation"
        try:
            self.recu_browser._set_transport(preferred, reason)
        except Exception:
            pass
        if cookie or user_agent:
            try:
                self.mosaic.reset_recu_http_sessions()
            except Exception:
                pass

    def recu_status(self) -> Dict[str, Any]:
        session = load_json(RECU_SESSION_PATH, {})
        recu_config = self.config.get("recu", {})
        transport = self.recu_browser.transport_status()
        return {
            "enabled": bool(recu_config.get("enabled", True)),
            "browser_running": self.recu_browser.is_running(),
            "session_captured": bool(isinstance(session, dict) and (session.get("captured_at") or session.get("cookie"))),
            "session_verified": bool(isinstance(session, dict) and session.get("validated_at")),
            "captured_at": str(session.get("captured_at", "")) if isinstance(session, dict) else "",
            "validated_at": str(session.get("validated_at", "")) if isinstance(session, dict) else "",
            "cookie_names": list(session.get("cookie_names", [])) if isinstance(session, dict) else [],
            "base_url": str(recu_config.get("base_url", "https://recu.me")),
            "chrome_found": bool(find_chrome(str(recu_config.get("chrome_path", "")))),
            "preferred_transport": str(transport.get("preferred_transport", "auto")),
            "transport_reason": str(transport.get("transport_reason", "")),
        }

    @staticmethod
    def _recu_error_looks_auth_related(error: Any) -> bool:
        lowered = str(error or "").casefold()
        return any(token in lowered for token in (
            "http 401", "http 403", "http 404",
            "verify you are human", "cloudflare", "cf-chl-",
            "just a moment", "login", "log in", "sign in",
            "verification", "challenge", "unexpectedly returned no clip",
        ))

    def _ensure_recu_browser_navigation(self, url: str, timeout: int) -> str:
        """Start/attach the dedicated profile and prove access by real navigation.

        This is deliberately used only as a reliability/authentication fallback.
        A copied-cookie/raw-HTTP rejection is not authoritative because Recu/CF
        can bind access to the browser context.  If the browser profile is still
        valid, return the requested page without asking the user to verify again.
        """
        if not self.recu_browser.is_running():
            self.recu_browser.launch()
        html = self.recu_browser.fetch_html_navigation(
            self.mosaic.canonicalize_recu_url(url, str(self.mosaic_settings.get("recu_base_url", "https://recu.me"))),
            timeout=max(20, int(timeout)),
            expect_listing=False,
        )
        self.recu_browser._set_transport(
            "navigation",
            "captured-cookie/background transport was rejected; verified browser navigation succeeded",
        )
        return html

    def _recu_fetch_bridge(
        self,
        url: str,
        settings: Dict[str, Any],
        cancel: threading.Event,
        attempts: int = 2,
    ) -> str:
        """Primary Recu fetch bridge using the proven pre-v2.14 HTTP method.

        The dedicated Chrome profile is used to *capture* real browser cookies.
        Normal scraping then goes through the long-standing persistent HTTP
        implementation (canonical redirects, exact Cookie/User-Agent profile,
        nearby-profile recovery, remembered working profile, bounded 403
        cooldown).  Browser navigation is only a last reliability fallback; it
        is never the first thing Capture or a normal scan does.
        """
        if cancel.is_set():
            raise RuntimeError("Recu scraping cancelled.")
        timeout = max(20, int(settings.get("recu_request_timeout_seconds", 20)) + 15)
        canonical = self.mosaic.canonicalize_recu_url(
            url, str(settings.get("recu_base_url", "https://recu.me"))
        )
        try:
            return self.recu_browser.fetch_html_cookie_http(
                canonical, timeout=timeout, expect_listing=False
            )
        except Exception as exc:
            if not self._recu_error_looks_auth_related(exc):
                raise
            append_log(
                f"Persistent-cookie Recu fetch looked auth-related; confirming only as fallback in the dedicated browser: "
                f"{type(exc).__name__}: {exc}"
            )
            try:
                return self._ensure_recu_browser_navigation(canonical, timeout)
            except RecuReauthRequired:
                raise
            except Exception as nav_exc:
                raise RuntimeError(
                    f"Recu persistent-session fetch failed ({exc}) and the browser fallback could not be used ({nav_exc})."
                ) from nav_exc

    def clear_recu_session(self) -> Dict[str, Any]:
        try:
            RECU_SESSION_PATH.unlink()
        except OSError:
            pass
        self.mosaic_settings["recu_cookie"] = ""
        self.mosaic.reset_recu_http_sessions()
        self.recu_browser._set_transport("auto", "session cleared")
        return {"cleared": True, "message": "The captured Recu session was removed."}

    def test_recu_session(self) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            progress("Loading the Recu home page through the verified browser/session…", 0, 1)
            html = self._recu_fetch_bridge(
                str(self.mosaic_settings.get("recu_base_url", "https://recu.me")),
                dict(self.mosaic_settings),
                threading.Event(),
                1,
            )
            lowered = html.casefold()
            if len(html) < 500 or any(token in lowered for token in ("verify you are human", "just a moment", "cf-chl-")):
                raise RuntimeError("Recu still returned a verification page. Reopen the verification browser and complete the check.")
            progress("Recu access verified.", 1, 1)
            return {"ok": True, "message": "Recu access is working from the PC browser session."}
        return self.create_task("Test Recu session", worker)

    def _source_identity_from_descriptors(self, files: Sequence[Dict[str, Any]]) -> str:
        """Order-independent identity for the exact underlying source set."""
        rows: List[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("source", item.get("path", ""))).strip()
            if not raw:
                continue
            path_key = os.path.normcase(os.path.abspath(raw)) if os.name == "nt" else os.path.abspath(raw)
            try: size = int(item.get("bytes", item.get("size", -1)) or -1)
            except Exception: size = -1
            try: mtime = float(item.get("mtime", -1) or -1)
            except Exception: mtime = -1.0
            rows.append(f"{path_key}\0{size}\0{mtime:.6f}")
        if not rows:
            return ""
        raw = "\n".join(sorted(set(rows))).encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(raw).hexdigest()

    def _source_identity_for_chunk(self, chunk: Any) -> str:
        files = [{
            "source": str(video.path), "bytes": int(video.size), "mtime": float(video.mtime)
        } for video in getattr(chunk, "files", [])]
        return self._source_identity_from_descriptors(files)

    def _migrate_action_queue_jobs(self) -> bool:
        changed = False
        with self.lock:
            for job in self.action_queue.get("jobs", []):
                if not isinstance(job, dict):
                    continue
                if str(job.get("status", "")) == "blocked":
                    job["status"] = "failed"
                    job["message"] = str(job.get("message", "")) or "Legacy blocked job migrated to failed history."
                    changed = True
                if not str(job.get("source_identity", "")):
                    identity = self._source_identity_from_descriptors(job.get("files", []) if isinstance(job.get("files"), list) else [])
                    if identity:
                        job["source_identity"] = identity
                        changed = True
        return changed

    def _claimed_source_identities(self) -> Set[str]:
        reusable = {"cancelled", "undone", "failed", "retired"}
        with self.lock:
            jobs = list(self.action_queue.get("jobs", []))
        return {
            str(job.get("source_identity", ""))
            for job in jobs
            if isinstance(job, dict)
            and str(job.get("source_identity", ""))
            and str(job.get("status", "")) not in reusable
        }

    def _chunk_is_claimed(self, chunk: Any) -> bool:
        identity = self._source_identity_for_chunk(chunk)
        return bool(identity and identity in self._claimed_source_identities())

    def _existing_job_for_source_identity(self, identity: str) -> Optional[Dict[str, Any]]:
        if not identity:
            return None
        reusable = {"cancelled", "undone", "failed", "retired", "blocked"}
        with self.lock:
            for job in self.action_queue.get("jobs", []):
                if not isinstance(job, dict):
                    continue
                if str(job.get("source_identity", "")) == identity and str(job.get("status", "")) not in reusable:
                    return job
        return None

    def _save_action_queue(self) -> None:
        with self.lock:
            jobs = self.action_queue.setdefault("jobs", [])
            # Retain incomplete work plus a bounded recent history.
            terminal = {"done", "cancelled", "undone", "failed", "retired"}
            incomplete = [job for job in jobs if isinstance(job, dict) and job.get("status") not in terminal]
            completed = [job for job in jobs if isinstance(job, dict) and job.get("status") in terminal][-250:]
            self.action_queue["jobs"] = incomplete + completed
            self.action_queue["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(ACTION_QUEUE_PATH, self.action_queue)

    def action_queue_summary(self) -> Dict[str, Any]:
        with self.lock:
            jobs = [dict(job) for job in self.action_queue.get("jobs", []) if isinstance(job, dict)]
            running_id = self.action_running_id
        pending = [job for job in jobs if job.get("status") in {"queued", "retrying", "running"}]
        retrying = [job for job in pending if job.get("status") == "retrying"]
        failed = [job for job in jobs if job.get("status") in {"failed", "retired", "blocked"}]
        latest = max(jobs, key=lambda job: str(job.get("created_at", "")), default={})
        return {
            "pending": len(pending),
            "retrying": len(retrying),
            "blocked": 0,
            "failed": len(failed),
            "running": bool(running_id),
            "running_id": running_id,
            "last_message": str(latest.get("message", "No queued sorting instructions.")),
            "last_error": str(latest.get("error", "")),
            "updated_at": str(self.action_queue.get("updated_at", "")),
        }

    def action_queue_jobs_payload(self) -> List[Dict[str, Any]]:
        """Return editable queue rows in actual execution order."""
        with self.lock:
            rows = [dict(job) for job in self.action_queue.get("jobs", []) if isinstance(job, dict)]
        visible = [job for job in rows if job.get("status") in {"queued", "retrying", "running", "failed", "retired", "blocked"}]
        output: List[Dict[str, Any]] = []
        for position, job in enumerate(visible, start=1):
            kind = str(job.get("kind", "chunk_moves"))
            mode = str(job.get("mode", ""))
            label = {
                "permanent_delete_models": "Permanent delete model(s)",
                "undo_chunk": "Undo previous sorting",
                "cleanup_moves": "Non-NSFW cleanup decisions",
                "deletion_restore": "Restore from deletion",
                "keep_last_all": "Run Keep Last all",
                "keep_last_model": "Run Keep Last model",
                "ignore_model_folders": "Ignore model → deletion",
                "review_frame_cuts": "Precise Review frame cut (idle)",
            }.get(kind, f"{mode.title() or 'Sorting'} decisions")
            output.append({
                "id": str(job.get("id", "")),
                "position": position,
                "kind": kind,
                "mode": mode,
                "model": str(job.get("model", ", ".join(job.get("model_names", [])[:3]) if isinstance(job.get("model_names"), list) else "")),
                "label": label,
                "status": str(job.get("status", "")),
                "message": str(job.get("message", "")),
                "error": str(job.get("error", "")),
                "created_at": str(job.get("created_at", "")),
                "cancel_requested": bool(job.get("cancel_requested", False)),
            })
        return output

    def reorder_action_job(self, job_id: str, direction: str) -> Dict[str, Any]:
        direction = str(direction or "top").casefold()
        with self.lock:
            jobs = self.action_queue.setdefault("jobs", [])
            index = next((i for i, job in enumerate(jobs) if isinstance(job, dict) and str(job.get("id", "")) == job_id), -1)
            if index < 0:
                raise RuntimeError("Queue job not found.")
            job = jobs[index]
            if job.get("status") not in {"queued", "retrying"}:
                raise RuntimeError("Only waiting jobs can be reordered.")
            # Waiting job positions, preserving completed history around them.
            waiting_indices = [i for i, row in enumerate(jobs) if isinstance(row, dict) and row.get("status") in {"queued", "retrying"}]
            pos = waiting_indices.index(index)
            if direction == "top": target_pos = 0
            elif direction == "bottom": target_pos = len(waiting_indices) - 1
            elif direction == "up": target_pos = max(0, pos - 1)
            elif direction == "down": target_pos = min(len(waiting_indices) - 1, pos + 1)
            else: raise RuntimeError("Unknown queue move.")
            if target_pos != pos:
                target_index = waiting_indices[target_pos]
                item = jobs.pop(index)
                if index < target_index:
                    target_index -= 1
                if direction in {"down", "bottom"}:
                    target_index += 1
                jobs.insert(max(0, min(len(jobs), target_index)), item)
                item["message"] = f"Reordered from phone ({direction})."
        self._save_action_queue()
        self.action_wakeup.set()
        return {"ok": True, "jobs": self.action_queue_jobs_payload()}

    def cancel_action_job(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = next((row for row in self.action_queue.get("jobs", []) if isinstance(row, dict) and str(row.get("id", "")) == job_id), None)
            if job is None:
                raise RuntimeError("Queue job not found.")
            status = str(job.get("status", ""))
            if status in {"queued", "retrying"}:
                job["status"] = "cancelled"
                job["message"] = "Cancelled from the phone before execution."
                job["cancel_requested"] = True
            elif status == "running":
                job["cancel_requested"] = True
                job["message"] = "Cancellation requested; stopping after the current file operation."
            else:
                raise RuntimeError("That job is no longer cancellable.")
        self._save_action_queue()
        self.action_wakeup.set()
        return {"ok": True, "jobs": self.action_queue_jobs_payload()}

    def _ready_key(self, mode: str, model_name: str) -> str:
        return f"{str(mode).casefold()}:{str(model_name).strip().casefold()}"

    def _save_ready_index(self) -> None:
        with self.lock:
            snapshots = self.ready_index.setdefault("snapshots", {})
            if len(snapshots) > 10000:
                ordered = sorted(
                    snapshots.items(),
                    key=lambda item: str(item[1].get("updated_at", "")) if isinstance(item[1], dict) else "",
                )
                for key, _value in ordered[:len(snapshots) - 9000]:
                    snapshots.pop(key, None)
            self.ready_index["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(READY_INDEX_PATH, self.ready_index)
            self.ready_count_cache.clear()
            self.catalog_view_cache.clear()

    def _serialize_chunk(self, mode: str, chunk: Any) -> Dict[str, Any]:
        if mode == "review":
            outputs = self.review.mosaic_outputs(chunk)
            if outputs:
                chunk.mosaics = list(outputs)
        else:
            outputs = [Path(value) for value in getattr(chunk, "mosaics", []) if Path(value).is_file()]
        files = []
        for video in chunk.files:
            files.append({
                "path": str(video.path),
                "start": video.start.isoformat(),
                "size": int(video.size),
                "mtime": float(video.mtime),
                "duration": float(video.duration),
                "duration_source": str(getattr(video, "duration_source", "snapshot")),
                "duration_warning": str(getattr(video, "duration_warning", "")),
            })
        row: Dict[str, Any] = {
            "signature": str(chunk.signature),
            "start": chunk.start.isoformat(),
            "end": chunk.end.isoformat(),
            "source_bytes": int(chunk.source_bytes),
            "folder": str(getattr(chunk, "folder", "")),
            "model_folder": str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))),
            "files": files,
            "source_identity": self._source_identity_from_descriptors(files),
            "mosaics": [str(path) for path in outputs],
            "ready": bool(outputs and self._embedded_layout_entry(mode, chunk, outputs)),
        }
        if mode == "original":
            row.update({
                "idx": int(getattr(chunk, "idx", 0)),
                "key": str(getattr(chunk, "key", "")),
                "durations_prepared": bool(getattr(chunk, "durations_prepared", False)),
                "preparation_warning": str(getattr(chunk, "preparation_warning", "")),
            })
        return row

    def _deserialize_chunk(self, mode: str, row: Dict[str, Any]) -> Any:
        if mode == "original":
            files = [
                self.mosaic.VideoInfo(
                    path=Path(str(item["path"])),
                    start=datetime.fromisoformat(str(item["start"])),
                    size=int(item["size"]),
                    mtime=float(item["mtime"]),
                    duration=max(1, int(round(float(item["duration"])))),
                    duration_source=str(item.get("duration_source", "snapshot")),
                    duration_warning=str(item.get("duration_warning", "")),
                )
                for item in row.get("files", []) if isinstance(item, dict)
            ]
            return self.mosaic.Chunk(
                idx=int(row.get("idx", 0)),
                folder=Path(str(row.get("folder", ""))),
                files=files,
                start=datetime.fromisoformat(str(row["start"])),
                end=datetime.fromisoformat(str(row["end"])),
                source_bytes=int(row.get("source_bytes", 0)),
                key=str(row.get("key", "")),
                signature=str(row.get("signature", "")),
                mosaics=[Path(str(value)) for value in row.get("mosaics", [])],
                durations_prepared=bool(row.get("durations_prepared", False)),
                preparation_warning=str(row.get("preparation_warning", "")),
            )
        files = [
            self.review.VideoInfo(
                path=Path(str(item["path"])),
                start=datetime.fromisoformat(str(item["start"])),
                size=int(item["size"]),
                mtime=float(item["mtime"]),
                duration=max(0.1, float(item["duration"])),
            )
            for item in row.get("files", []) if isinstance(item, dict)
        ]
        return self.review.Chunk(
            folder=Path(str(row.get("folder", ""))),
            model_folder=Path(str(row.get("model_folder", ""))),
            files=files,
            start=datetime.fromisoformat(str(row["start"])),
            end=datetime.fromisoformat(str(row["end"])),
            source_bytes=int(row.get("source_bytes", 0)),
            signature=str(row.get("signature", "")),
            mosaics=[Path(str(value)) for value in row.get("mosaics", [])],
        )

    def _persist_ready_snapshot(
        self,
        mode: str,
        model_name: str,
        chunks: Sequence[Any],
        model_bytes: int = 0,
        drives: Optional[Sequence[str]] = None,
    ) -> None:
        serialized = [self._serialize_chunk(mode, chunk) for chunk in chunks]
        key = self._ready_key(mode, model_name)
        payload = {
            "mode": mode,
            "model": model_name,
            "bytes": int(model_bytes or sum(int(row.get("source_bytes", 0)) for row in serialized)),
            "size": human_size(int(model_bytes or sum(int(row.get("source_bytes", 0)) for row in serialized))),
            "drives": sorted({str(value).upper() for value in (drives or []) if str(value)}),
            "chunks": serialized,
            "ready_chunks": sum(1 for row in serialized if row.get("ready")),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.lock:
            self.ready_index.setdefault("snapshots", {})[key] = payload
            for row in serialized:
                work_id = hashlib.sha256(f"{mode}\0{row.get('signature', '')}".encode("utf-8")).hexdigest()[:24]
                self.offline_media_lookup[work_id] = [str(value) for value in row.get("mosaics", [])]
        self._save_ready_index()

    def _hydrate_chunks_from_ready_snapshot(self, mode: str, model_name: str, chunks: Sequence[Any]) -> int:
        """Restore durable mosaic output references onto freshly rebuilt chunk metadata.

        Chunk rebuilding is allowed to be cheap and settings-independent.  In
        v2.13.0-v2.13.2, rebuilding an Originals model while
        use_existing_mosaics=False could immediately persist blank `mosaics`
        fields over a perfectly valid ready snapshot, making a model change from
        READY to not-ready merely because the user opened it.  Exact chunk
        signatures are the safety boundary: only identical chunks inherit output
        paths, and normal readiness validation still occurs before use.
        """
        key = self._ready_key(mode, model_name)
        with self.lock:
            snapshot = self.ready_index.get("snapshots", {}).get(key)
        if not isinstance(snapshot, dict):
            return 0
        by_signature = {
            str(row.get("signature", "")): row
            for row in snapshot.get("chunks", [])
            if isinstance(row, dict) and str(row.get("signature", ""))
        }
        restored = 0
        for chunk in chunks:
            signature = str(getattr(chunk, "signature", ""))
            row = by_signature.get(signature)
            if not isinstance(row, dict) or not row.get("ready"):
                continue
            outputs = row.get("mosaics", [])
            if not isinstance(outputs, list) or not outputs:
                continue
            current = [Path(value) for value in getattr(chunk, "mosaics", []) if str(value)]
            if current:
                continue
            try:
                chunk.mosaics = [Path(str(value)) for value in outputs if str(value)]
                if chunk.mosaics:
                    restored += 1
            except Exception:
                continue
        return restored

    def _pending_chunk_signatures(self) -> Set[str]:
        with self.lock:
            jobs = list(self.action_queue.get("jobs", []))
        terminal_reusable = {"cancelled", "undone", "failed", "retired", "blocked"}
        return {
            str(job.get("chunk_signature", ""))
            for job in jobs
            if isinstance(job, dict)
            and str(job.get("chunk_signature", ""))
            and str(job.get("status", "")) not in terminal_reusable
        }

    def _queued_delete_model_names(self) -> Set[str]:
        with self.lock:
            jobs = list(self.action_queue.get("jobs", []))
        names: Set[str] = set()
        for job in jobs:
            if not isinstance(job, dict) or str(job.get("kind", "")) != "permanent_delete_models":
                continue
            if str(job.get("status", "")) in {"cancelled", "undone"}:
                continue
            names.update(str(value).casefold() for value in job.get("model_names", []) if str(value))
        return names

    def ready_suggestions(
        self,
        mode: str,
        selected_drives: Set[str],
        exclude_model: str = "",
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        if mode == "cleanup":
            rows = self.cleanup_catalog_models(selected_drives)
            output = []
            for row in rows:
                if exclude_model and str(row.get("name", "")).casefold() == exclude_model.casefold():
                    continue
                output.append({
                    "name": row["name"], "mode": "cleanup", "ready_chunks": 1,
                    "ready_bytes": int(row.get("bytes", 0)), "ready_size": row.get("size", "0 B"),
                    "bytes": int(row.get("bytes", 0)), "size": row.get("size", "0 B"), "drives": row.get("drives", []),
                })
            return output[:max(1, min(10000, int(limit)))]
        mode = "review" if mode == "review" else "original"
        catalog_totals = {str(row.get("name", "")).casefold(): int(row.get("bytes", 0) or 0) for row in self.catalog_models(mode, selected_drives)}
        pending = self._pending_chunk_signatures()
        claimed_sources = self._claimed_source_identities()
        with self.lock:
            snapshots = [dict(value) for value in self.ready_index.get("snapshots", {}).values() if isinstance(value, dict)]
        output: List[Dict[str, Any]] = []
        for snapshot in snapshots:
            if str(snapshot.get("mode", "")) != mode:
                continue
            model = str(snapshot.get("model", ""))
            if exclude_model and model.casefold() == exclude_model.casefold():
                continue
            snapshot_drives = {str(value).upper() for value in snapshot.get("drives", [])}
            if selected_drives and snapshot_drives and not (snapshot_drives & selected_drives):
                continue
            ready = 0
            ready_bytes = 0
            # This endpoint is intentionally metadata-only so the phone never waits
            # on thousands of network/external-drive stat calls. quick_load_queue
            # validates the first actual mosaic before opening the suggestion.
            chunk_rows = [row for row in snapshot.get("chunks", []) if isinstance(row, dict)]
            chunk_rows.sort(key=lambda row: (-int(row.get("source_bytes", 0) or 0), str(row.get("start", ""))))
            for row in chunk_rows:
                if not isinstance(row, dict) or str(row.get("signature", "")) in pending:
                    continue
                source_identity = str(row.get("source_identity", "")) or self._source_identity_from_descriptors(row.get("files", []) if isinstance(row.get("files"), list) else [])
                if source_identity and source_identity in claimed_sources:
                    continue
                folder = Path(str(row.get("model_folder" if mode == "review" else "folder", "")))
                if selected_drives and drive_label(folder) not in selected_drives:
                    continue
                outputs = row.get("mosaics", [])
                if row.get("ready") and isinstance(outputs, list) and outputs:
                    ready += 1
                    ready_bytes += int(row.get("source_bytes", 0))
            if ready:
                output.append({
                    "name": model,
                    "mode": mode,
                    "ready_chunks": ready,
                    "ready_bytes": ready_bytes,
                    "ready_size": human_size(ready_bytes),
                    "bytes": int(catalog_totals.get(model.casefold(), int(snapshot.get("bytes", 0) or 0))),
                    "size": human_size(int(catalog_totals.get(model.casefold(), int(snapshot.get("bytes", 0) or 0)))),
                    "drives": sorted(snapshot_drives),
                })
        output.sort(key=lambda row: (-int(row.get("bytes", 0)), -int(row.get("ready_bytes", 0)), str(row.get("name", "")).casefold()))
        return output[:max(1, min(10000, int(limit)))]

    def _stable_direct_video_paths(self, folder: Path, extensions: Set[str]) -> Set[str]:
        """Return stable top-level video paths without probing duration metadata."""
        grace = max(0, int(self.mosaic_settings.get("recent_write_grace_seconds", 120) or 120))
        skip_recent = bool(self.mosaic_settings.get("skip_recent_writes", True))
        now = time.time()
        output: Set[str] = set()
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if Path(entry.name).suffix.casefold() not in extensions:
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if skip_recent and grace > 0 and now - float(stat.st_mtime) < grace:
                            continue
                        output.add(normalized(Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            pass
        return output

    def _existing_mosaic_sidecar_count(self, mode: str, model_name: str, selected_drives: Set[str]) -> int:
        """Cheap count of durable mosaic metadata files for one model.

        This is intentionally much cheaper than rebuilding chunk metadata.  It is
        used on phone-open to detect a stale ready-index snapshot such as "1 ready"
        while many valid sidecars are already sitting beside the recordings.
        """
        count = 0
        try:
            folders = self.model_folders(mode, model_name, selected_drives)
        except Exception:
            return 0
        for model_folder in folders:
            try:
                if mode == "review":
                    output_dir = model_folder / self.review.MOSAIC_DIRNAME
                    if output_dir.is_dir():
                        count += sum(1 for _ in output_dir.glob("review_*.json"))
                else:
                    output_dir = model_folder / self.mosaic.MOSAIC_DIRNAME
                    if output_dir.is_dir():
                        try:
                            state = self.mosaic.load_review_state(model_folder)
                            reviewed = state.get("reviewed", {}) if isinstance(state, dict) else {}
                        except Exception:
                            reviewed = {}
                        for sidecar in output_dir.glob("*.sources.json"):
                            try:
                                raw = load_json(sidecar, {})
                                key = str(raw.get("chunk_key", "")) if isinstance(raw, dict) else ""
                                if key and isinstance(reviewed, dict) and reviewed.get(key) in {"done", "skipped"}:
                                    continue
                            except Exception:
                                pass
                            count += 1
            except OSError:
                continue
        return count

    def _recover_original_snapshot_from_mosaics(
        self, model_name: str, selected_drives: Set[str], require_complete: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Reconstruct a COMPLETE Originals queue from durable mosaic sidecars.

        This is the rapid path that prevents a model with all mosaics already on
        disk from being needlessly "prepared" again. It performs no ffprobe and
        no frame generation. To avoid hiding new recordings, the snapshot is
        accepted only when every stable direct source video currently in scope is
        represented by one of the valid sidecars.
        """
        folders = self.model_folders("original", model_name, selected_drives)
        if not folders:
            return None
        extensions = parse_extensions(self.mosaic_settings.get("extensions", "mp4,ts"))
        recovered: List[Any] = []
        covered: Set[str] = set()
        stable_sources: Set[str] = set()
        manifest_chunks: Dict[str, Any] = {}
        try:
            with self.mosaic_manifest.lock:
                manifest_chunks = dict(self.mosaic_manifest.data.get("chunks", {}))
        except Exception:
            manifest_chunks = {}

        for folder in folders:
            stable_sources.update(self._stable_direct_video_paths(folder, extensions))
            output_dir = folder / self.mosaic.MOSAIC_DIRNAME
            if not output_dir.is_dir():
                continue
            try:
                sidecars = sorted(output_dir.glob("*.sources.json"))
            except OSError:
                continue
            try:
                review_state = self.mosaic.load_review_state(folder)
                reviewed_keys = review_state.get("reviewed", {}) if isinstance(review_state, dict) else {}
            except Exception:
                reviewed_keys = {}
            for sidecar in sidecars:
                raw = load_json(sidecar, {})
                if not isinstance(raw, dict):
                    continue
                # A sidecar can legitimately remain on disk after a chunk was
                # explicitly completed/skipped. Never resurrect that work merely
                # because we are repairing a stale mobile ready-index.
                chunk_key_value = str(raw.get("chunk_key", ""))
                if chunk_key_value and isinstance(reviewed_keys, dict) and reviewed_keys.get(chunk_key_value) in {"done", "skipped"}:
                    continue
                signature = str(raw.get("signature", "")).strip()
                raw_files = raw.get("files", [])
                raw_outputs = raw.get("outputs", [])
                if not signature or not isinstance(raw_files, list) or not raw_files or not isinstance(raw_outputs, list):
                    continue
                outputs = [Path(str(value)) for value in raw_outputs if str(value)]
                if not outputs or not all(path.is_file() for path in outputs):
                    continue
                videos: List[Any] = []
                invalid = False
                for item in raw_files:
                    if not isinstance(item, dict):
                        invalid = True; break
                    source = Path(str(item.get("path", "")))
                    if not source.is_file():
                        invalid = True; break
                    try:
                        stat = source.stat()
                    except OSError:
                        invalid = True; break
                    expected_size = int(item.get("size", stat.st_size) or stat.st_size)
                    expected_mtime = float(item.get("mtime", stat.st_mtime) or stat.st_mtime)
                    if int(stat.st_size) != expected_size or abs(float(stat.st_mtime) - expected_mtime) > 0.01:
                        invalid = True; break
                    start = self.mosaic.parse_start_from_name(source.name, stat.st_mtime)
                    cached = self.mosaic_duration_cache.get(source, int(stat.st_size), float(stat.st_mtime))
                    try:
                        sidecar_duration = float(item.get("duration", 0) or 0)
                    except Exception:
                        sidecar_duration = 0.0
                    tail = self.mosaic.parse_tail_duration_from_name(source.name)
                    # A validated v2 mosaic sidecar stores the exact duration that
                    # was used to generate its frame layout. Prefer that over a
                    # filename _tail_ tag: _tail_ describes remaining session/chunk
                    # time in some CTBRec naming schemes, not this segment's length.
                    duration = float(cached or sidecar_duration or tail or 0)
                    videos.append(self.mosaic.VideoInfo(
                        path=source, start=start, size=int(stat.st_size), mtime=float(stat.st_mtime),
                        duration=max(1, int(round(duration or 900))),
                        duration_source=(
                            "cached" if cached else
                            ("mosaic-sidecar" if sidecar_duration > 0 else
                             ("tail-tag provisional" if tail else "rapid snapshot"))
                        ),
                    ))
                if invalid or not videos:
                    continue
                # Improve uncached rapid durations using the next recording start,
                # without touching ffprobe. This is sufficient for UI/preview
                # metadata; exact duration remains lazily available later.
                for index, video in enumerate(videos):
                    if video.duration_source != "rapid snapshot":
                        continue
                    if index + 1 < len(videos):
                        delta = (videos[index + 1].start - video.start).total_seconds()
                        if 0 < delta <= 21600:
                            video.duration = max(1, int(round(delta)))
                manifest = manifest_chunks.get(signature, {}) if isinstance(manifest_chunks.get(signature, {}), dict) else {}
                start = datetime.fromisoformat(str(manifest.get("start"))) if manifest.get("start") else min(v.start for v in videos)
                end = datetime.fromisoformat(str(manifest.get("end"))) if manifest.get("end") else max(v.end for v in videos)
                chunk = self.mosaic.Chunk(
                    idx=int(manifest.get("idx", len(recovered) + 1) or len(recovered) + 1),
                    folder=folder,
                    files=videos,
                    start=start,
                    end=end,
                    source_bytes=int(manifest.get("source_bytes", sum(v.size for v in videos)) or sum(v.size for v in videos)),
                    key=str(raw.get("chunk_key", manifest.get("chunk_key", ""))),
                    signature=signature,
                    mosaics=outputs,
                    durations_prepared=False,
                    preparation_warning="Loaded from existing mosaic sidecar; exact durations remain lazy.",
                )
                if not self._embedded_layout_entry("original", chunk, outputs):
                    continue
                recovered.append(chunk)
                covered.update(normalized(video.path) for video in videos)

        if not recovered:
            return None
        if require_complete and not stable_sources.issubset(covered):
            return None
        pending = self._pending_chunk_signatures()
        recovered = [chunk for chunk in recovered if str(chunk.signature) not in pending and not self._chunk_is_claimed(chunk)]
        if not recovered:
            return None
        order = str(self.mosaic_settings.get("queue_order", "Largest chunks first"))
        if order == "Oldest first":
            recovered.sort(key=lambda chunk: (chunk.start, -int(chunk.source_bytes), normalized(chunk.folder)))
        elif order == "Newest first":
            recovered.sort(key=lambda chunk: (-chunk.start.timestamp(), -int(chunk.source_bytes), normalized(chunk.folder)))
        else:
            recovered.sort(key=lambda chunk: (-int(chunk.source_bytes), chunk.start, normalized(chunk.folder)))
        for index, chunk in enumerate(recovered, start=1):
            chunk.idx = index
        self._persist_ready_snapshot(
            "original", model_name, recovered,
            sum(int(chunk.source_bytes) for chunk in recovered),
            sorted(selected_drives),
        )
        with self.lock:
            return self.ready_index.get("snapshots", {}).get(self._ready_key("original", model_name))

    @staticmethod
    def _review_manifest_bounds(path: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
        """Recover start/end embedded in legacy Review mosaic filenames."""
        match = re.search(
            r"_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_",
            path.stem,
        )
        if not match:
            return None, None
        try:
            return (
                datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S"),
                datetime.strptime(match.group(2), "%Y-%m-%d_%H-%M-%S"),
            )
        except Exception:
            return None, None

    def _fast_review_videos_for_signature(
        self,
        review_folder: Path,
        signature: str,
        manifest_path: Path,
        extensions: Set[str],
    ) -> List[Any]:
        """Match a legacy v2.7 Review manifest to its exact source files.

        v2.7 Review sidecars had the chunk signature + hit-map but did not write
        the source-file list. The signature itself is SHA1 over each source's
        normalized path, byte size and mtime. We can therefore recover the exact
        contiguous file subset using cheap stat/timestamp work only -- no
        ffprobe, frame extraction, or Recu request.
        """
        try:
            rows = self.review.review_video_files(review_folder, extensions)
        except Exception:
            return []
        if not rows:
            return []
        records: List[Tuple[Path, os.stat_result, datetime]] = []
        for path, stat in rows:
            try:
                start = self.review.parse_start(path, stat.st_mtime)
            except Exception:
                start = datetime.fromtimestamp(float(stat.st_mtime))
            records.append((path, stat, start))
        records.sort(key=lambda item: (item[2], item[0].name.casefold()))
        expected_start, expected_end = self._review_manifest_bounds(manifest_path)
        starts = list(range(len(records)))
        if expected_start is not None:
            near = [
                index for index, (_path, _stat, stamp) in enumerate(records)
                if abs((stamp - expected_start).total_seconds()) <= 2.0
            ]
            if near:
                starts = near
        for first in starts:
            # A chunk is contiguous in the Review builder. Test progressively
            # larger subsets until the content-derived signature matches.
            for last in range(first + 1, len(records) + 1):
                subset = records[first:last]
                dummy = [
                    self.review.VideoInfo(
                        path=path,
                        start=stamp,
                        size=int(stat.st_size),
                        mtime=float(stat.st_mtime),
                        duration=1.0,
                    )
                    for path, stat, stamp in subset
                ]
                try:
                    candidate = self.review.chunk_signature(dummy)
                except Exception:
                    continue
                if candidate != signature:
                    continue
                # Restore useful durations without touching ffprobe. Prefer the
                # durable duration cache; otherwise use the same lightweight
                # 15-minute size estimate used elsewhere in the mobile sorter.
                largest = max((int(stat.st_size) for _path, stat, _stamp in subset), default=0)
                bytes_per_second = largest / 900.0 if largest > 0 else 0.0
                videos: List[Any] = []
                for path, stat, stamp in subset:
                    cached = self.review_duration_cache.get(path, int(stat.st_size), float(stat.st_mtime))
                    if cached and float(cached) > 0:
                        duration = float(cached)
                    elif bytes_per_second > 0:
                        duration = max(1.0, min(900.0, int(stat.st_size) / bytes_per_second))
                    else:
                        duration = 900.0
                    videos.append(
                        self.review.VideoInfo(
                            path=path,
                            start=stamp,
                            size=int(stat.st_size),
                            mtime=float(stat.st_mtime),
                            duration=float(duration),
                        )
                    )
                return videos
        return []

    def _recover_review_snapshot_from_mosaics(
        self, model_name: str, selected_drives: Set[str], require_complete: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Recover Review work directly from v2.8 OR legacy v2.7 mosaics."""
        folders = self.model_folders("review", model_name, selected_drives)
        if not folders:
            return None
        extensions = parse_extensions(self.review_settings.get("extensions", "mp4,ts"))
        recovered: List[Any] = []
        covered: Set[str] = set()
        stable_sources: Set[str] = set()
        for model_folder in folders:
            review_folder = model_folder / self.review.REVIEW_FOLDER_NAME
            stable_sources.update(self._stable_direct_video_paths(review_folder, extensions))
            output_dir = model_folder / self.review.MOSAIC_DIRNAME
            if not output_dir.is_dir():
                continue
            try:
                manifests = sorted(output_dir.glob("review_*.json"))
            except OSError:
                continue
            for manifest_path in manifests:
                raw = load_json(manifest_path, {})
                if not isinstance(raw, dict):
                    continue
                signature = str(raw.get("signature", "")).strip()
                outputs = [Path(str(value)) for value in raw.get("outputs", []) if str(value)]
                if not signature or not outputs or not all(path.is_file() for path in outputs):
                    continue
                videos: List[Any] = []
                invalid = False
                # v2.8+ source-rich manifest: zero reconstruction work.
                raw_files = raw.get("files")
                if isinstance(raw_files, list) and raw_files:
                    for item in raw_files:
                        if not isinstance(item, dict):
                            invalid = True; break
                        source = Path(str(item.get("path", "")))
                        if not source.is_file():
                            invalid = True; break
                        try:
                            stat = source.stat()
                        except OSError:
                            invalid = True; break
                        if int(item.get("size", stat.st_size)) != int(stat.st_size) or abs(float(item.get("mtime", stat.st_mtime)) - float(stat.st_mtime)) > 0.01:
                            invalid = True; break
                        start = datetime.fromisoformat(str(item.get("start"))) if item.get("start") else self.review.parse_start(source, stat.st_mtime)
                        duration = float(item.get("duration", 0) or 0)
                        if duration <= 0:
                            cached = self.review_duration_cache.get(source, int(stat.st_size), float(stat.st_mtime))
                            duration = float(cached or 900)
                        videos.append(self.review.VideoInfo(source, start, int(stat.st_size), float(stat.st_mtime), max(0.1, duration)))
                else:
                    # v2.7 sidecar: use its content-derived signature to recover
                    # the exact contiguous Review source list without probing.
                    videos = self._fast_review_videos_for_signature(
                        review_folder, signature, manifest_path, extensions
                    )
                    invalid = not bool(videos)
                if invalid or not videos:
                    continue
                filename_start, filename_end = self._review_manifest_bounds(manifest_path)
                try:
                    chunk_start = datetime.fromisoformat(str(raw.get("start"))) if raw.get("start") else (filename_start or min(v.start for v in videos))
                except Exception:
                    chunk_start = filename_start or min(v.start for v in videos)
                try:
                    chunk_end = datetime.fromisoformat(str(raw.get("end"))) if raw.get("end") else (filename_end or max(v.end for v in videos))
                except Exception:
                    chunk_end = filename_end or max(v.end for v in videos)
                chunk = self.review.Chunk(
                    folder=review_folder,
                    model_folder=model_folder,
                    files=videos,
                    start=chunk_start,
                    end=chunk_end,
                    source_bytes=int(raw.get("source_bytes", sum(v.size for v in videos)) or sum(v.size for v in videos)),
                    signature=signature,
                    mosaics=outputs,
                )
                if not self._embedded_layout_entry("review", chunk, outputs):
                    continue
                recovered.append(chunk)
                covered.update(normalized(video.path) for video in videos)
        # Complete background recovery still requires every current stable
        # Review source. The sort-first phone path may intentionally open the
        # valid subset that already has mosaics while missing chunks are created
        # behind the user.
        if not recovered:
            return None
        if require_complete and not stable_sources.issubset(covered):
            return None
        pending = self._pending_chunk_signatures()
        recovered = [chunk for chunk in recovered if str(chunk.signature) not in pending and not self._chunk_is_claimed(chunk)]
        if not recovered:
            return None
        recovered.sort(key=lambda chunk: (-int(chunk.source_bytes), chunk.start, normalized(chunk.folder)))
        self._persist_ready_snapshot(
            "review", model_name, recovered,
            sum(int(chunk.source_bytes) for chunk in recovered),
            sorted(selected_drives),
        )
        with self.lock:
            return self.ready_index.get("snapshots", {}).get(self._ready_key("review", model_name))

    def _recover_complete_snapshot_from_existing_mosaics(
        self, mode: str, model_name: str, selected_drives: Set[str]
    ) -> Optional[Dict[str, Any]]:
        if mode == "original":
            return self._recover_original_snapshot_from_mosaics(model_name, selected_drives, require_complete=True)
        if mode == "review":
            return self._recover_review_snapshot_from_mosaics(model_name, selected_drives, require_complete=True)
        return None

    def _recover_partial_snapshot_from_existing_mosaics(
        self, mode: str, model_name: str, selected_drives: Set[str]
    ) -> Optional[Dict[str, Any]]:
        """Recover any valid on-disk mosaics without requiring model completeness.

        This is deliberately the phone-opening path. It never creates frames or
        probes exact durations; it simply exposes already-sortable work now.
        """
        if mode == "original":
            return self._recover_original_snapshot_from_mosaics(model_name, selected_drives, require_complete=False)
        if mode == "review":
            return self._recover_review_snapshot_from_mosaics(model_name, selected_drives, require_complete=False)
        return None

    def _snapshot_covers_current_sources(
        self, mode: str, model_name: str, selected_drives: Set[str], snapshot: Dict[str, Any]
    ) -> bool:
        """Cheap completeness check: every current stable source appears in snapshot."""
        if not isinstance(snapshot, dict):
            return False
        folders = self.model_folders(mode, model_name, selected_drives)
        if not folders:
            return False
        extensions = parse_extensions(
            self.review_settings.get("extensions", "mp4,ts")
            if mode == "review" else self.mosaic_settings.get("extensions", "mp4,ts")
        )
        current: Set[str] = set()
        for folder in folders:
            source_folder = folder / self.review.REVIEW_FOLDER_NAME if mode == "review" else folder
            current.update(self._stable_direct_video_paths(source_folder, extensions))
        represented: Set[str] = set()
        for row in snapshot.get("chunks", []):
            if not isinstance(row, dict):
                continue
            physical = Path(str(row.get("model_folder" if mode == "review" else "folder", "")))
            if selected_drives and drive_label(physical) not in selected_drives:
                continue
            for item in row.get("files", []):
                if isinstance(item, dict) and item.get("path"):
                    represented.add(normalized(Path(str(item.get("path")))))
        return bool(current) and current.issubset(represented)

    def quick_load_queue(self, mode: str, model_name: str, selected_drives: Set[str], recover_sidecars: bool = True) -> Dict[str, Any]:
        """Open only mosaics that are already safely sortable, immediately.

        v2.9 intentionally does NOT require a complete model queue before opening.
        A model may have 3 ready chunks and 40 missing chunks: those 3 ready chunks
        are exposed at once, largest first. The clicked model is indexed in the
        background, but only the mosaic currently being viewed is generated
        automatically; future mosaics remain on-demand.
        """
        self.interactive_demand.set()
        self.mark_sort_session_active()
        if mode == "cleanup":
            return self.quick_load_cleanup_queue(model_name, selected_drives)
        mode = "review" if mode == "review" else "original"
        key = self._ready_key(mode, model_name)
        pending = self._pending_chunk_signatures()

        def chunks_from_snapshot(raw_snapshot: Dict[str, Any]) -> List[Any]:
            output: List[Any] = []
            max_initial = max(1, int(self.config.get("speed_mode", {}).get("open_fast_initial_chunks", 3) or 3))
            # v2.13.3: `open_fast_initial_chunks` is a synchronous VALIDATION
            # budget, not a cap on how many durable ready mosaics belong to the
            # queue.  If the catalog says 15 ready, opening the model must keep
            # all 15 in the queue while validating only the first few before
            # returning control to the phone.
            rows = [row for row in raw_snapshot.get("chunks", []) if isinstance(row, dict)]
            rows.sort(key=lambda row: (-int(row.get("source_bytes", 0) or 0), str(row.get("start", ""))))
            warm_checks = 0
            seen_signatures: Set[str] = set()
            seen_sources: Set[Tuple[str, ...]] = set()
            for row in rows:
                row_signature = str(row.get("signature", ""))
                source_key = tuple(sorted(normalized(Path(str(item.get("path", "")))) for item in row.get("files", []) if isinstance(item, dict) and item.get("path")))
                if row_signature in seen_signatures or (source_key and source_key in seen_sources):
                    continue
                if row_signature: seen_signatures.add(row_signature)
                if source_key: seen_sources.add(source_key)
                if row_signature in pending:
                    continue
                outputs_hint = row.get("mosaics", [])
                if not row.get("ready") or not isinstance(outputs_hint, list) or not outputs_hint:
                    continue
                try:
                    chunk = self._deserialize_chunk(mode, row)
                except Exception:
                    continue
                if self._chunk_is_claimed(chunk):
                    continue
                physical_folder = Path(str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))))
                if selected_drives and drive_label(physical_folder) not in selected_drives:
                    continue
                if warm_checks < max_initial:
                    warm_checks += 1
                    temp_queue = {"mode": mode, "model": model_name, "chunks": [chunk]}
                    if not self._chunk_mosaic_ready(temp_queue, chunk):
                        continue
                # Rows beyond the warm validation budget remain represented in
                # the queue from their durable ready snapshot.  current_payload
                # lazily verifies actual output files/layout when each reaches
                # the front and will regenerate only that stale chunk if needed.
                output.append(chunk)
            return output


        with self.lock:
            snapshot = self.ready_index.get("snapshots", {}).get(key)
        chunks = chunks_from_snapshot(snapshot) if isinstance(snapshot, dict) else []
        source = "ready-index"
        recovered_on_open = 0

        # v2.14.7: the ready-index is a derived cache, not the source of truth.
        # Older builds could leave it containing only one chunk even while many
        # perfectly valid sidecar-backed mosaics remained beside the recordings.
        # Count sidecars cheaply and, when disk metadata clearly outruns the
        # cached ready count (or the cache has nothing usable), rebuild the ready
        # subset directly from those sidecars.  This performs JSON/stat checks
        # only -- no ffprobe, ffmpeg or frame generation -- so an already-built
        # model opens with all trustworthy mosaics instead of pretending they
        # do not exist.
        cached_ready = len(chunks)
        sidecar_count = self._existing_mosaic_sidecar_count(mode, model_name, selected_drives) if recover_sidecars else 0
        if recover_sidecars and (not chunks or sidecar_count > cached_ready):
            repaired = self._recover_partial_snapshot_from_existing_mosaics(mode, model_name, selected_drives)
            if isinstance(repaired, dict):
                repaired_chunks = chunks_from_snapshot(repaired)
                if len(repaired_chunks) >= len(chunks):
                    recovered_on_open = max(0, len(repaired_chunks) - len(chunks))
                    snapshot = repaired
                    chunks = repaired_chunks
                    source = "sidecar-reindex"

        if not chunks:
            return {
                "ready": False,
                "reason": "No ready mosaics exist for this model yet.",
                "ready_chunks": 0,
            }

        queue_id = secrets.token_urlsafe(12)
        queue_data = {
            "id": queue_id,
            "mode": mode,
            "model": model_name,
            "drives": sorted(selected_drives),
            "chunks": chunks,
            "initial_count": len(chunks),
            "known_total_chunks": max(len(chunks), len(snapshot.get("chunks", [])) if isinstance(snapshot, dict) else len(chunks)),
            "drafts": {},
            "history": [],
            "notes": [
                "Sort-first mode: opened existing ready mosaics immediately. While this model stays open, the configured look-ahead window is refilled automatically."
            ],
            "created_at": time.time(),
            "busy": False,
            "preparing_model": False,
            "open_error": "",
            "phone_open": False,
            "prefetch_cancel_requested": False,
            "recu_status": "not_started" if mode in {"original", "review"} else "disabled",
            "recu_error": "",
            "recu_data": None,
            "prefetch_status": {
                "state": "idle", "ready": 0, "target": 0,
                "message": "Ready mosaics opened immediately. Automatic look-ahead will fill after this model's chunk inventory is attached.",
            },
            "priority_generation_task_id": "",
        }
        with self.lock:
            self.queues[queue_id] = queue_data
        # Recu is enrichment only. It is started asynchronously after the mosaic
        # is already visible and has no say in whether the queue can open.
        if mode in {"original", "review"} and bool(self.config.get("recu", {}).get("enabled", True)):
            self.start_recu_for_queue(queue_id, force=False)
        return {
            "ready": True,
            "queue_id": queue_id,
            "current": self.current_payload(queue_id),
            "source": source,
            "ready_chunks": len(chunks),
            "recovered_existing_mosaics": int(recovered_on_open),
            "sidecar_count": int(sidecar_count),
        }

    def _deactivate_queue_work_locked(self, queue_id: str) -> None:
        """Stop hidden open-model work when the phone leaves or switches queues."""
        queue_id = str(queue_id or "").strip()
        if not queue_id:
            return
        queue_data = self.queues.get(queue_id)
        if queue_data is not None:
            # Explicit leave/switch is authoritative. Poll-based lease renewal
            # is allowed only while this flag remains true, so a late stale
            # status response can never resurrect background work for a model
            # the user actually left.
            queue_data["phone_open"] = False
            queue_data["prefetch_cancel_requested"] = True
            status = queue_data.get("prefetch_status")
            if isinstance(status, dict) and status.get("state") in {"running", "waiting_idle", "paused_active", "paused_priority"}:
                queue_data["prefetch_status"] = {
                    **status, "state": "cancelled",
                    "message": "Stopped because this model is no longer open on the phone.",
                }
        for key, targets in list(self.priority_generation_targets.items()):
            if queue_id not in targets:
                continue
            targets.discard(queue_id)
            if targets:
                continue
            task_id = self.priority_generation_tasks.get(key, "")
            record = self.tasks.get(task_id) if task_id else None
            if record is not None and record.status in {"queued", "running"}:
                record.cancel_requested = True

    def mark_sort_session_active(self, lease_seconds: float = 45.0, queue_id: str = "") -> Dict[str, Any]:
        with self.lock:
            clean_queue_id = str(queue_id or "").strip()
            previous_queue_id = str(self.active_sort_queue_id or "")
            if clean_queue_id:
                # There is only one visible phone sorting queue. Close every
                # other queue that still carries the durable phone-open flag,
                # including one whose timer lease expired and therefore already
                # disappeared from active_sort_queue_id.
                stale_open = [
                    str(qid) for qid, data in self.queues.items()
                    if str(qid) != clean_queue_id and isinstance(data, dict) and bool(data.get("phone_open", False))
                ]
                if previous_queue_id and previous_queue_id != clean_queue_id and previous_queue_id not in stale_open:
                    stale_open.append(previous_queue_id)
                for stale_queue_id in stale_open:
                    self._deactivate_queue_work_locked(stale_queue_id)
            self.sort_session_active_until = max(self.sort_session_active_until, time.time() + max(10.0, float(lease_seconds)))
            if clean_queue_id:
                self.active_sort_queue_id = clean_queue_id
                queue_data = self.queues.get(clean_queue_id)
                if queue_data is not None:
                    queue_data["phone_open"] = True
                    queue_data["prefetch_cancel_requested"] = False
        return {
            "ok": True,
            "active_until": self.sort_session_active_until,
            "queue_id": self.active_sort_queue_id,
        }

    def mark_sort_session_inactive(self) -> Dict[str, Any]:
        with self.lock:
            previous_queue_id = str(self.active_sort_queue_id or "")
            open_queue_ids = [
                str(qid) for qid, data in self.queues.items()
                if isinstance(data, dict) and bool(data.get("phone_open", False))
            ]
            if previous_queue_id and previous_queue_id not in open_queue_ids:
                open_queue_ids.append(previous_queue_id)
            for queue_id in open_queue_ids:
                self._deactivate_queue_work_locked(queue_id)
            self.sort_session_active_until = 0.0
            self.active_sort_queue_id = ""
        self.action_wakeup.set()
        return {"ok": True}

    def _touch_sort_queue_from_poll(self, queue_id: str, lease_seconds: float = 45.0) -> bool:
        """Renew the active-phone lease from a queue-specific current/status poll.

        iOS may throttle JavaScript timers even while the review screen remains
        visibly open.  The client simultaneously polls this exact queue for
        current/status updates, so a successful queue-specific poll is stronger
        evidence that the model is still open than the timer heartbeat alone.

        Explicit leave/switch calls set ``phone_open`` false.  That durable flag
        prevents an in-flight stale poll from resurrecting an abandoned queue.
        A poll may reclaim an expired lease only for the queue that the phone had
        explicitly opened and only when no different queue is currently active.
        """
        queue_id = str(queue_id or "").strip()
        if not queue_id:
            return False
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if queue_data is None or not bool(queue_data.get("phone_open", False)):
                return False
            active_queue = str(self.active_sort_queue_id or "")
            if active_queue and active_queue != queue_id:
                return False
            self.active_sort_queue_id = queue_id
            self.sort_session_active_until = max(
                float(self.sort_session_active_until or 0.0),
                time.time() + max(10.0, float(lease_seconds)),
            )
            queue_data["prefetch_cancel_requested"] = False
            return True

    def sort_session_active(self) -> bool:
        with self.lock:
            active = time.time() < float(self.sort_session_active_until or 0.0)
            if not active and self.active_sort_queue_id:
                # Self-heal an abandoned browser/PWA session. The 45-second
                # heartbeat lease is the source of truth, not stale queue state.
                self.active_sort_queue_id = ""
            return active

    def _priority_model_task_key(self, mode: str, model_name: str, selected_drives: Set[str]) -> str:
        return f"{mode}:{model_name.casefold()}:{','.join(sorted(selected_drives))}"

    def _priority_model_wait_turn(self, key: str, progress: Optional[Callable[[str, int, int], None]] = None) -> None:
        """Yield older model-prep tasks whenever the user clicks another model."""
        while not self.stop_event.is_set():
            with self.lock:
                active = str(self.priority_model_key or "")
            if not active or active == key:
                return
            if progress is not None:
                try: progress("Yielding to the model currently selected on the phone…", 0, 0)
                except Exception: pass
            time.sleep(0.10)

    def _merge_ready_chunks_into_queue(self, queue_id: str, ready_chunks: Sequence[Any], known_total: int = 0) -> None:
        """Append newly generated work without ever displacing the mosaic being viewed."""
        if not queue_id:
            return
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if not queue_data:
                return
            mode = str(queue_data.get("mode", ""))
            history_signatures = {
                str(getattr(entry.get("chunk"), "signature", ""))
                for entry in queue_data.get("history", []) if isinstance(entry, dict) and entry.get("chunk") is not None
            }
            current_chunks = list(queue_data.get("chunks", []))
            existing_signatures = {str(getattr(chunk, "signature", "")) for chunk in current_chunks}
            additions = []
            for chunk in ready_chunks:
                signature = str(getattr(chunk, "signature", ""))
                if not signature or signature in existing_signatures or signature in history_signatures:
                    continue
                if self._chunk_is_claimed(chunk):
                    continue
                temp = {"mode": mode, "model": queue_data.get("model", ""), "chunks": [chunk]}
                if self._chunk_mosaic_ready(temp, chunk):
                    additions.append(chunk)
                    existing_signatures.add(signature)
            if additions:
                if current_chunks:
                    head, tail = current_chunks[0], current_chunks[1:] + additions
                    tail.sort(key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                    queue_data["chunks"] = [head] + tail
                else:
                    additions.sort(key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                    queue_data["chunks"] = additions
            if known_total:
                queue_data["known_total_chunks"] = max(int(queue_data.get("known_total_chunks", 0) or 0), int(known_total))
            completed = len(queue_data.get("history", []))
            queue_data["initial_count"] = max(int(queue_data.get("initial_count", 0) or 0), completed + len(queue_data.get("chunks", [])))
            if additions:
                queue_data["prefetch_status"] = {
                    "state": "idle",
                    "ready": max(0, len(queue_data.get("chunks", [])) - 1),
                    "target": max(0, int(self.background_settings_for_mode(mode).get("upcoming_count", 3))),
                    "message": f"Recovered {len(additions)} additional valid existing mosaic(s) from disk metadata. Automatic look-ahead will keep refilling while this model stays open.",
                }

    def _merge_model_chunks_into_queue(self, queue_id: str, chunks: Sequence[Any]) -> None:
        """Attach one stable chunk inventory; look-ahead generation is scheduled separately."""
        if not queue_id:
            return
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if not queue_data:
                return
            history_signatures = {
                str(getattr(entry.get("chunk"), "signature", ""))
                for entry in queue_data.get("history", []) if isinstance(entry, dict) and entry.get("chunk") is not None
            }
            current_chunks = list(queue_data.get("chunks", []))
            existing = {str(getattr(chunk, "signature", "")) for chunk in current_chunks}
            additions = []
            for chunk in chunks:
                signature = str(getattr(chunk, "signature", ""))
                if not signature or signature in existing or signature in history_signatures or self._chunk_is_claimed(chunk):
                    continue
                additions.append(chunk)
                existing.add(signature)
            if current_chunks:
                head = current_chunks[0]
                tail = current_chunks[1:] + additions
                tail.sort(key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                queue_data["chunks"] = [head] + tail
            else:
                additions.sort(key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                queue_data["chunks"] = additions
            queue_data["known_total_chunks"] = max(int(queue_data.get("known_total_chunks", 0) or 0), len(chunks))
            completed = len(queue_data.get("history", []))
            queue_data["initial_count"] = max(1, completed + len(queue_data.get("chunks", [])))
            queue_data["preparing_model"] = False
            queue_data["open_error"] = ""
            if queue_data.get("chunks"):
                head = queue_data["chunks"][0]
                ready = self._chunk_mosaic_ready(queue_data, head)
                queue_data["current_mosaic_status"] = {
                    "state": "ready" if ready else "queued",
                    "signature": str(getattr(head, "signature", "")),
                    "message": "Mosaic ready." if ready else "Preparing this mosaic because you opened this model…",
                }

    def _sync_pending_model_task(self, queue_id: str, queue_data: Optional[Dict[str, Any]] = None) -> None:
        """Keep a pending model-open queue attached to a real indexing task.

        v2.15.2 introduced a deliberate pending placeholder so a fast sorter could
        not exhaust the initial READY subset and be sent back to Models before the
        full chunk inventory attached. A narrow same-model task handoff race could
        leave that placeholder orphaned forever: the old shared indexing task could
        finish between the new queue joining its target set and the worker's final
        target snapshot.

        The queue/status endpoints call this cheap synchronizer while the placeholder
        is visible. It mirrors live task progress into the mosaic card, surfaces real
        task errors, and restarts an orphaned/done/cancelled preparation task when the
        model is still the active phone queue. It never generates media itself.
        """
        queue_id = str(queue_id or "").strip()
        if not queue_id:
            return
        with self.lock:
            current = queue_data if queue_data is not None else self.queues.get(queue_id)
            if current is None or self.current_chunk(current) is not None or not bool(current.get("preparing_model")):
                return
            task_id = str(current.get("priority_generation_task_id", "") or "")
            record = self.tasks.get(task_id) if task_id else None
            active = str(self.active_sort_queue_id or "") == queue_id and time.time() < float(self.sort_session_active_until or 0.0)
            attempts = int(current.get("prepare_attempts", 0) or 0)
            mode = str(current.get("mode", "original"))
            model_name = str(current.get("model", ""))
            selected_drives = {str(value).upper() for value in current.get("drives", []) if str(value)}

            if record is not None and record.status in {"queued", "running"}:
                message = str(record.progress or "").strip()
                if record.status == "queued" or not message:
                    message = "Waiting for the model-index worker…"
                current["current_mosaic_status"] = {
                    "state": "running", "signature": "__pending__",
                    "message": message,
                }
                current["prepare_last_progress_at"] = float(record.updated_at or time.time())
                return

            if record is not None and record.status == "error":
                message = str(record.error or record.progress or "Model indexing failed.")
                current["preparing_model"] = False
                current["open_error"] = message
                current["current_mosaic_status"] = {"state": "error", "signature": "__pending__", "message": message}
                return

            if record is not None and record.status == "done":
                indexed = 0
                if isinstance(record.result, dict):
                    try: indexed = int(record.result.get("indexed", 0) or 0)
                    except Exception: indexed = 0
                if indexed <= 0:
                    message = "No sortable chunks were found for this model on the selected drives."
                    current["preparing_model"] = False
                    current["open_error"] = message
                    current["current_mosaic_status"] = {"state": "error", "signature": "__pending__", "message": message}
                    return

            # Missing/cancelled/done-with-indexed-work but still no attached chunk:
            # this is the orphaned-placeholder failure class. Re-arm it only while
            # the phone is still actively looking at this exact queue.
            if not active:
                return
            if attempts >= 3:
                message = "Model indexing did not attach a chunk after automatic recovery attempts. Return to Models and open it again, or collect diagnostics."
                current["preparing_model"] = False
                current["open_error"] = message
                current["current_mosaic_status"] = {"state": "error", "signature": "__pending__", "message": message}
                return
            current["prepare_attempts"] = attempts + 1
            current["current_mosaic_status"] = {
                "state": "running", "signature": "__pending__",
                "message": f"Recovering model-open indexing (attempt {attempts + 1}/3)…",
            }

        task_id = self.prioritize_model_mosaics(mode, model_name, selected_drives, queue_id=queue_id)
        with self.lock:
            latest = self.queues.get(queue_id)
            if latest is not None and task_id:
                latest["priority_generation_task_id"] = task_id

    def prioritize_model_mosaics(self, mode: str, model_name: str, selected_drives: Set[str], queue_id: str = "") -> str:
        """Index the clicked model, prioritize its current mosaic, then refill bounded look-ahead."""
        if mode not in {"original", "review"}:
            return ""
        key = self._priority_model_task_key(mode, model_name, selected_drives)
        self.interactive_demand.set()
        if queue_id:
            self.mark_sort_session_active(queue_id=queue_id)
        else:
            self.mark_sort_session_active()
        with self.lock:
            self.priority_model_key = key
            self.priority_model_epoch += 1
            if queue_id:
                self.priority_generation_targets.setdefault(key, set()).add(queue_id)
            existing_id = self.priority_generation_tasks.get(key, "")
            existing = self.tasks.get(existing_id) if existing_id else None
            if existing is not None and existing.status in {"queued", "running"} and not bool(existing.cancel_requested):
                return existing_id

        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            processed_targets: Set[str] = set()

            def queue_progress(message: str, current: int = 0, total: int = 0) -> None:
                progress(message, current, total)
                with self.lock:
                    for target in list(self.priority_generation_targets.get(key, set())):
                        queue_data = self.queues.get(target)
                        if queue_data is None or self.current_chunk(queue_data) is not None:
                            continue
                        queue_data["prepare_last_progress_at"] = time.time()
                        queue_data["current_mosaic_status"] = {
                            "state": "running", "signature": "__pending__", "message": str(message),
                        }

            try:
                # v2.14.9: sidecar recovery is a repair path, not routine work.
                # v2.14.8 invoked it for every clicked model and then displayed
                # "reindexed" even when the READY cache already represented all
                # valid sidecar-backed mosaics. Compare the queue we actually
                # opened with the cheap sidecar inventory first, and only scan
                # sidecars when disk metadata can add something useful.
                with self.lock:
                    targets = list(self.priority_generation_targets.get(key, set()))
                    loaded_ready = max(
                        (len(self.queues.get(target, {}).get("chunks", [])) for target in targets),
                        default=0,
                    )
                sidecar_count = self._existing_mosaic_sidecar_count(mode, model_name, selected_drives)
                if sidecar_count > loaded_ready:
                    queue_progress(f"Recovering {sidecar_count - loaded_ready} additional existing mosaic(s) for {model_name}…", 0, 0)
                    repaired = self._recover_partial_snapshot_from_existing_mosaics(mode, model_name, selected_drives)
                    if isinstance(repaired, dict):
                        recovered_ready = []
                        for row in repaired.get("chunks", []):
                            if not isinstance(row, dict) or not row.get("ready"):
                                continue
                            try:
                                recovered_ready.append(self._deserialize_chunk(mode, row))
                            except Exception:
                                continue
                        with self.lock:
                            targets = list(self.priority_generation_targets.get(key, set()))
                        for target in targets:
                            self._merge_ready_chunks_into_queue(target, recovered_ready, len(repaired.get("chunks", [])))
                else:
                    queue_progress(f"Existing READY cache already covers {loaded_ready} compatible mosaic(s) for {model_name}; no sidecar reindex needed.", 0, 0)

                queue_progress(f"Indexing {model_name}…", 0, 0)
                folders = self.model_folders(mode, model_name, selected_drives)
                if not folders:
                    raise RuntimeError("Model is no longer available on the selected drives.")
                chunks = self._build_chunks_for_library_model(
                    mode, model_name, folders,
                    lambda message: queue_progress(str(message), 0, 0),
                )
                chunks = [chunk for chunk in chunks if str(getattr(chunk, "signature", "")) not in self._pending_chunk_signatures() and not self._chunk_is_claimed(chunk)]
                chunks.sort(key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                model_bytes = sum(int(getattr(chunk, "source_bytes", 0)) for chunk in chunks)
                self._persist_ready_snapshot(mode, model_name, chunks, model_bytes, sorted(selected_drives))
                queue_progress(f"Attached {len(chunks)} chunk(s) for {model_name}.", len(chunks), len(chunks))
                with self.lock:
                    targets = list(self.priority_generation_targets.get(key, set()))
                for target in targets:
                    self._merge_model_chunks_into_queue(target, chunks)
                    processed_targets.add(str(target))

                # No all-model loop here. The currently open queue gets immediate
                # current-mosaic priority plus a bounded automatic look-ahead refill.
                # Leaving/switching models cancels that queue's prefetch demand.
                for target in targets:
                    with self.lock:
                        active = str(self.active_sort_queue_id or "") == str(target) and self.sort_session_active()
                    if active:
                        self.request_interactive_mosaic(target, False)
                        self.schedule_prefetch(target)
                        if bool(self.config.get("recu", {}).get("enabled", True)):
                            self.start_recu_for_queue(target, force=False)
                return {"model": model_name, "indexed": len(chunks), "generated": 0, "policy": "current-plus-bounded-lookahead"}
            except TaskCancelled:
                raise
            except Exception as exc:
                with self.lock:
                    targets = list(self.priority_generation_targets.get(key, set()))
                    for target in targets:
                        queue_data = self.queues.get(target)
                        if queue_data is not None:
                            queue_data["preparing_model"] = False
                            queue_data["open_error"] = str(exc)
                            queue_data["current_mosaic_status"] = {
                                "state": "error", "signature": "__pending__", "message": str(exc),
                            }
                        processed_targets.add(str(target))
                raise
            finally:
                with self.lock:
                    if self.priority_model_key == key:
                        self.priority_model_key = ""
                    targets = self.priority_generation_targets.get(key)
                    if targets is not None:
                        targets.difference_update(processed_targets)
                        if not targets:
                            self.priority_generation_targets.pop(key, None)
                self.action_wakeup.set()

        task_id = self.create_task(f"Open-model index: {model_name}", worker, pool=self.priority_executor)
        with self.lock:
            self.priority_generation_tasks[key] = task_id
        return task_id

    def open_model_sort_first(self, mode: str, model_name: str, selected_drives: Set[str]) -> Dict[str, Any]:
        """Open exactly the model the user tapped; never auto-hop to another model."""
        mode = "review" if mode == "review" else ("cleanup" if mode == "cleanup" else "original")
        result = self.quick_load_queue(mode, model_name, selected_drives, recover_sidecars=False)
        queue_id = str(result.get("queue_id", "")) if result.get("ready") else ""
        if not queue_id and mode in {"original", "review"}:
            queue_id = secrets.token_urlsafe(12)
            queue_data = {
                "id": queue_id, "mode": mode, "model": model_name, "drives": sorted(selected_drives),
                "chunks": [], "initial_count": 1, "known_total_chunks": 0,
                "drafts": {}, "history": [],
                "notes": ["Opened on demand. This model stays selected while its first sortable mosaic is prepared."],
                "created_at": time.time(), "busy": False,
                "preparing_model": True, "open_error": "",
                "phone_open": False,
                "prepare_started_at": time.time(), "prepare_last_progress_at": time.time(), "prepare_attempts": 1,
                "recu_status": "not_started", "recu_error": "", "recu_data": None,
                "prefetch_cancel_requested": False,
                "prefetch_status": {
                    "state": "idle", "ready": 0, "target": 0,
                    "message": "Automatic look-ahead will fill as soon as this model's chunk inventory is ready.",
                },
                "current_mosaic_status": {
                    "state": "running", "signature": "__pending__",
                    "message": "Opening this model and locating its first chunk…",
                },
                "priority_generation_task_id": "",
            }
            with self.lock:
                self.queues[queue_id] = queue_data
            self.mark_sort_session_active(queue_id=queue_id)
            result = {
                "ready": False, "pending": True, "queue_id": queue_id,
                "ready_chunks": 0, "recovered_existing_mosaics": 0, "sidecar_count": 0,
            }
        elif queue_id:
            # Even when ready mosaics already exist, keep the model logically open
            # until its complete current chunk inventory is attached. Otherwise a
            # fast sorter can consume the ready subset before indexing finishes and
            # be incorrectly sent to Done while additional chunks still exist.
            with self.lock:
                queue_data = self.queues.get(queue_id)
                if queue_data is not None and mode in {"original", "review"}:
                    queue_data["preparing_model"] = True
                    queue_data["open_error"] = ""
                    queue_data["prepare_started_at"] = time.time()
                    queue_data["prepare_last_progress_at"] = time.time()
                    queue_data["prepare_attempts"] = 1
                    queue_data["prefetch_cancel_requested"] = False
            self.mark_sort_session_active(queue_id=queue_id)
        task_id = self.prioritize_model_mosaics(mode, model_name, selected_drives, queue_id=queue_id) if mode in {"original", "review"} else ""
        result["priority_task_id"] = task_id
        if queue_id:
            with self.lock:
                queue_data = self.queues.get(queue_id)
                if queue_data is not None:
                    queue_data["priority_generation_task_id"] = task_id
            if not result.get("current"):
                result["current"] = self.current_payload(queue_id)
        return result

    def _work_token(self, descriptor: Dict[str, Any]) -> str:
        raw = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        secret = str(self.config.get("secret_key", "")).encode("utf-8")
        sig = hmac.new(secret, ("ctbrec-offline-work:" + body).encode("utf-8"), hashlib.sha256).hexdigest()
        return body + "." + sig

    def _decode_work_token(self, token: str) -> Dict[str, Any]:
        body, sep, supplied = str(token).partition(".")
        if not sep or not body or not supplied:
            raise RuntimeError("Offline work token is malformed.")
        secret = str(self.config.get("secret_key", "")).encode("utf-8")
        expected = hmac.new(secret, ("ctbrec-offline-work:" + body).encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise RuntimeError("Offline work token failed verification.")
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode((body + padding).encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict) or int(payload.get("version", 0) or 0) != 1:
            raise RuntimeError("Offline work token version is unsupported.")
        return payload

    def _descriptor_for_chunk(self, mode: str, model_name: str, chunk: Any) -> Dict[str, Any]:
        files = [{
            "index": index,
            "source": str(video.path),
            "bytes": int(video.size),
            "mtime": float(video.mtime),
        } for index, video in enumerate(chunk.files)]
        return {
            "version": 1,
            "work_id": hashlib.sha256(f"{mode}\0{chunk.signature}".encode("utf-8")).hexdigest()[:24],
            "mode": mode,
            "model": model_name,
            "chunk_signature": str(chunk.signature),
            "chunk_key": str(getattr(chunk, "key", "")),
            "folder": str(getattr(chunk, "folder", "")),
            "model_folder": str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))),
            "files": files,
            "issued_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _offline_display_bundle(self, mode: str, model_name: str, chunk: Any) -> Optional[Dict[str, Any]]:
        outputs = [Path(value) for value in getattr(chunk, "mosaics", []) if Path(value).is_file()]
        if mode == "review":
            outputs = self.review.mosaic_outputs(chunk)
            if outputs:
                chunk.mosaics = list(outputs)
        if not outputs:
            return None
        layout = self._embedded_layout_entry(mode, chunk, outputs)
        if layout is None:
            return None
        descriptor = self._descriptor_for_chunk(mode, model_name, chunk)
        token = self._work_token(descriptor)
        parts: List[Dict[str, Any]] = []
        for part_index, (output, raw_part) in enumerate(zip(outputs, layout.get("parts", []))):
            width = int(raw_part.get("width", 0) or 0); height = int(raw_part.get("height", 0) or 0)
            tiles = []
            for raw_tile in raw_part.get("tiles", []):
                file_index = int(raw_tile.get("file_index", -1))
                tiles.append({
                    "file_index": file_index,
                    "number": file_index + 1,
                    "left": 100.0 * int(raw_tile.get("left_px", 0)) / max(1, width),
                    "top": 100.0 * int(raw_tile.get("top_px", 0)) / max(1, height),
                    "width": 100.0 * int(raw_tile.get("width_px", 0)) / max(1, width),
                    "height": 100.0 * int(raw_tile.get("height_px", 0)) / max(1, height),
                })
            parts.append({
                "index": part_index,
                "url": f"/api/offline-media/{descriptor['work_id']}/{part_index}",
                "width": width,
                "height": height,
                "tiles": tiles,
            })
        files = []
        cumulative = 0.0
        for index, video in enumerate(chunk.files):
            files.append({
                "index": index, "number": index + 1, "name": video.path.name,
                "size": human_size(video.size), "bytes": int(video.size),
                "duration_seconds": float(video.duration),
                "duration": self.review.format_clock(video.duration) if hasattr(self.review, "format_clock") else str(video.duration),
                "chunk_start_seconds": cumulative, "kinks": [],
            })
            cumulative += float(video.duration)
        recu_payload: Dict[str, Any] = {"status": "offline-cache", "error": "", "segments": {}, "unmatched": []}
        if mode == "original":
            try:
                cached = self.mosaic.recu_data_from_payload(self.recu_cache.get_raw(model_name), source="offline cache")
                if cached is not None:
                    temp_queue = {"recu_data": cached, "recu_status": "ready", "recu_error": ""}
                    recu_payload = self.recu_markers_for_chunk(temp_queue, chunk)
                    recu_payload["status"] = "offline-cache"
            except Exception:
                pass
        return {
            "work_id": descriptor["work_id"],
            "work_token": token,
            "mode": mode,
            "model": model_name,
            "chunk": {
                "signature": str(chunk.signature),
                "size": human_size(chunk.source_bytes),
                "bytes": int(chunk.source_bytes),
                "file_count": len(chunk.files),
                "files": files,
                "parts": parts,
                "mosaic_ready": True,
            },
            "recu": recu_payload,
            "downloaded_at": "",
        }

    def offline_pack(self, mode: str, selected_drives: Set[str], limit: int, max_mb: float, model_names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        mode = "review" if mode == "review" else "original"
        requested = {str(value).casefold().strip() for value in (model_names or []) if str(value).strip()}
        suggestions = self.ready_suggestions(mode, selected_drives, "", 500 if requested else 50)
        if requested:
            suggestions = [row for row in suggestions if str(row.get("name", "")).casefold() in requested]
            # Disk-space-first: selected models remain largest -> smallest.
            suggestions.sort(key=lambda row: (-int(row.get("bytes", 0)), -int(row.get("ready_chunks", 0)), str(row.get("name", "")).casefold()))
        bundles: List[Dict[str, Any]] = []
        total_estimate = 0
        max_bytes = max(25 * 1024 * 1024, int(float(max_mb) * 1024 * 1024))
        pending = self._pending_chunk_signatures()
        for suggestion in suggestions:
            key = self._ready_key(mode, str(suggestion["name"]))
            with self.lock:
                snapshot = self.ready_index.get("snapshots", {}).get(key)
            if not isinstance(snapshot, dict):
                continue
            chunk_rows = [row for row in snapshot.get("chunks", []) if isinstance(row, dict)]
            chunk_rows.sort(key=lambda row: (-int(row.get("source_bytes", 0) or 0), str(row.get("start", ""))))
            for row in chunk_rows:
                if len(bundles) >= max(1, int(limit)):
                    break
                if not isinstance(row, dict) or str(row.get("signature", "")) in pending:
                    continue
                try:
                    chunk = self._deserialize_chunk(mode, row)
                except Exception:
                    continue
                if self._chunk_is_claimed(chunk):
                    continue
                physical = Path(str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))))
                if selected_drives and drive_label(physical) not in selected_drives:
                    continue
                bundle = self._offline_display_bundle(mode, str(suggestion["name"]), chunk)
                if bundle is None:
                    continue
                image_bytes = 0
                for raw_path in row.get("mosaics", []):
                    try: image_bytes += Path(str(raw_path)).stat().st_size
                    except OSError: pass
                if bundles and total_estimate + image_bytes > max_bytes:
                    break
                bundle["estimated_download_bytes"] = image_bytes
                bundles.append(bundle)
                total_estimate += image_bytes
            if len(bundles) >= max(1, int(limit)) or total_estimate >= max_bytes:
                break
        return {
            "mode": mode,
            "bundles": bundles,
            "count": len(bundles),
            "estimated_download_bytes": total_estimate,
            "estimated_download_size": human_size(total_estimate),
            "note": "Offline decisions persist on the iPhone and sync the next time this app is open with a connection to the PC.",
        }

    def prepare_offline_pack_task(
        self,
        mode: str,
        selected_drives: Set[str],
        limit: int,
        max_mb: float,
        model_names: Sequence[str],
    ) -> str:
        """Promote selected offline models to highest priority, then build a pack.

        Unlike the old builder, callers may select ANY catalog model. Existing
        mosaics are reused immediately; missing mosaics are generated through the
        interactive-priority executor before background library/NSFW work resumes.
        """
        mode = "review" if mode == "review" else "original"
        requested = [str(value).strip() for value in model_names if str(value).strip()]
        if not requested:
            raise RuntimeError("Choose at least one model for the offline pack.")
        catalog_rows = self.catalog_models(mode, selected_drives)
        by_name = {str(row.get("name", "")).casefold(): row for row in catalog_rows}
        missing = [name for name in requested if name.casefold() not in by_name]
        if missing:
            raise RuntimeError(
                "These selected models are no longer in the current drive scope: " + ", ".join(missing[:8])
            )
        # Preserve the model-list order (normally largest-first), not arbitrary
        # Set ordering from the browser.
        requested_keys = {name.casefold() for name in requested}
        ordered_models = [str(row["name"]) for row in catalog_rows if str(row["name"]).casefold() in requested_keys]
        target_chunks = max(1, int(limit))

        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            self.interactive_demand.set()
            queues: Dict[str, List[Any]] = {}
            notes: Dict[str, List[str]] = {}
            total_models = len(ordered_models)
            # First resolve/build metadata for every requested model. A complete
            # sidecar-backed model avoids the expensive build path entirely.
            for model_index, model in enumerate(ordered_models, start=1):
                progress(
                    f"Offline pack priority {model_index}/{total_models}: indexing {model}…",
                    model_index - 1,
                    total_models,
                )
                key = self._ready_key(mode, model)
                with self.lock:
                    snapshot = self.ready_index.get("snapshots", {}).get(key)
                if not isinstance(snapshot, dict) or not self._snapshot_covers_current_sources(
                    mode, model, selected_drives, snapshot
                ):
                    snapshot = self._recover_complete_snapshot_from_existing_mosaics(
                        mode, model, selected_drives
                    )
                chunks: List[Any] = []
                if isinstance(snapshot, dict) and self._snapshot_covers_current_sources(
                    mode, model, selected_drives, snapshot
                ):
                    pending = self._pending_chunk_signatures()
                    for row in snapshot.get("chunks", []):
                        if not isinstance(row, dict) or str(row.get("signature", "")) in pending:
                            continue
                        try:
                            chunk = self._deserialize_chunk(mode, row)
                        except Exception:
                            continue
                        physical = Path(str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))))
                        if selected_drives and drive_label(physical) not in selected_drives:
                            continue
                        chunks.append(chunk)
                    notes[model] = ["Reused durable/existing mosaic metadata."]
                if not chunks:
                    chunks, build_notes = self._build_priority_chunks(
                        mode,
                        model,
                        selected_drives,
                        lambda text, _c=0, _t=0, m=model: progress(
                            f"Offline pack • {m}: {text}", model_index - 1, total_models
                        ),
                    )
                    notes[model] = list(build_notes)
                chunks = sorted(list(chunks), key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                queues[model] = list(chunks)
                if chunks:
                    self._persist_ready_snapshot(
                        mode,
                        model,
                        chunks,
                        sum(int(getattr(chunk, "source_bytes", 0)) for chunk in chunks),
                        sorted(selected_drives),
                    )

            # Give every selected model a chance to contribute a ready chunk
            # before filling the remaining pack slots. This makes custom model
            # selection meaningful even when the overall limit is small.
            prepared = 0
            made_progress = True
            round_index = 0
            while prepared < target_chunks and made_progress:
                made_progress = False
                for model in ordered_models:
                    chunks = queues.get(model, [])
                    if round_index >= len(chunks):
                        continue
                    chunk = chunks[round_index]
                    temp_queue = {
                        "mode": mode, "model": model, "chunks": chunks,
                        "drives": sorted(selected_drives), "notes": notes.get(model, []),
                    }
                    if not self._chunk_mosaic_ready(temp_queue, chunk):
                        progress(
                            f"Highest priority: preparing {model} offline mosaic {round_index + 1}/{len(chunks)}…",
                            prepared,
                            target_chunks,
                        )
                        self.ensure_chunk_mosaic(
                            temp_queue,
                            chunk,
                            False,
                            lambda message, current=0, total=0, m=model: progress(
                                f"Highest priority • {m}: {message}", prepared, target_chunks
                            ),
                        )
                    if self._chunk_mosaic_ready(temp_queue, chunk):
                        prepared += 1
                        made_progress = True
                        self._persist_ready_snapshot(
                            mode,
                            model,
                            chunks,
                            sum(int(getattr(value, "source_bytes", 0)) for value in chunks),
                            sorted(selected_drives),
                        )
                        if prepared >= target_chunks:
                            break
                round_index += 1

            pack = self.offline_pack(mode, selected_drives, target_chunks, max_mb, ordered_models)
            pack["selected_models"] = ordered_models
            pack["priority_prepared"] = prepared
            progress(
                f"Offline pack ready: {pack.get('count', 0)} chunk(s).",
                int(pack.get("count", 0)),
                max(1, int(pack.get("count", 0))),
            )
            return pack

        self.interactive_demand.set()
        return self.create_task("Prepare offline pack", worker, pool=self.interactive_executor)

    def _enqueue_descriptor_action(self, descriptor: Dict[str, Any], draft: Dict[str, Any], client_action_id: str) -> Dict[str, Any]:
        mode = str(descriptor.get("mode", ""))
        if mode not in {"original", "review"}:
            raise RuntimeError("Offline work can sync only Originals or Review decisions.")
        files: List[Dict[str, Any]] = []
        raw_files = descriptor.get("files", [])
        if mode == "original":
            keep = {int(value) for value in draft.get("keep_indices", []) if str(value).lstrip("-").isdigit()}
            for item in raw_files:
                if not isinstance(item, dict): continue
                index = int(item.get("index", 0))
                files.append({**item, "decision": "KEEP" if index in keep else "DELETE"})
        else:
            allowed = {"Leave for review", "DELETE", "Cumshots", "Misc Hot Scenes"}
            decisions = draft.get("decisions", {}) if isinstance(draft.get("decisions", {}), dict) else {}
            for item in raw_files:
                if not isinstance(item, dict): continue
                index = int(item.get("index", 0))
                decision = str(decisions.get(str(index), decisions.get(index, "Leave for review")))
                files.append({**item, "decision": decision if decision in allowed else "Leave for review"})
        # Validate the signed source snapshot before accepting it into the durable queue.
        for item in files:
            source = Path(str(item.get("source", "")))
            if not source.exists():
                raise RuntimeError(f"Offline item is stale because a source file no longer exists: {source.name}")
            try:
                stat = source.stat()
                if int(item.get("bytes", -1)) != int(stat.st_size) or abs(float(item.get("mtime", -1)) - float(stat.st_mtime)) > 0.01:
                    raise RuntimeError(f"Offline item is stale because a source file changed: {source.name}")
            except OSError as exc:
                raise RuntimeError(f"Could not validate offline source {source.name}: {exc}") from exc
        source_identity = self._source_identity_from_descriptors(files)
        existing = self._existing_job_for_source_identity(source_identity)
        if existing is not None:
            with self.lock:
                self.offline_sync_history.setdefault("client_actions", {})[client_action_id] = {
                    "job_id": existing.get("id", ""), "accepted_at": datetime.now().isoformat(timespec="seconds"),
                    "duplicate_source_set": True,
                }
                atomic_write_json(OFFLINE_SYNC_PATH, self.offline_sync_history)
            return existing
        job = {
            "id": secrets.token_urlsafe(12), "kind": "chunk_moves", "status": "queued",
            "mode": mode, "model": str(descriptor.get("model", "")),
            "chunk_signature": str(descriptor.get("chunk_signature", "")),
            "source_identity": source_identity,
            "chunk_key": str(descriptor.get("chunk_key", "")),
            "folder": str(descriptor.get("folder", "")),
            "model_folder": str(descriptor.get("model_folder", "")),
            "files": files, "mosaic_artifacts": [], "attempts": 0,
            "client_action_id": client_action_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": "Queued from iPhone offline/speed mode.", "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
            self.offline_sync_history.setdefault("client_actions", {})[client_action_id] = {
                "job_id": job["id"], "accepted_at": datetime.now().isoformat(timespec="seconds")
            }
            atomic_write_json(OFFLINE_SYNC_PATH, self.offline_sync_history)
        self._save_action_queue(); self.action_wakeup.set()
        return job

    def sync_offline_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        client_action_id = str(payload.get("client_action_id", "")).strip()
        if not client_action_id:
            raise RuntimeError("Offline sync requires a client_action_id.")
        with self.lock:
            previous = self.offline_sync_history.get("client_actions", {}).get(client_action_id)
        if isinstance(previous, dict):
            return {"accepted": True, "duplicate": True, "job_id": str(previous.get("job_id", ""))}
        descriptor = self._decode_work_token(str(payload.get("work_token", "")))
        draft = payload.get("draft", {}) if isinstance(payload.get("draft", {}), dict) else {}
        job = self._enqueue_descriptor_action(descriptor, draft, client_action_id)
        return {"accepted": True, "duplicate": False, "job_id": job["id"], "message": "Offline decision safely entered the durable PC queue."}

    def offline_media_path(self, work_id: str, part_index: int) -> Optional[Path]:
        with self.lock:
            outputs = list(self.offline_media_lookup.get(str(work_id), []))
        if 0 <= int(part_index) < len(outputs):
            path = Path(str(outputs[int(part_index)]))
            if path.is_file():
                return path
        return None

    def _queued_destination(self, job: Dict[str, Any], item: Dict[str, Any]) -> Optional[Path]:
        decision = str(item.get("decision", ""))
        source = Path(str(item.get("source", "")))
        if decision in {"Leave for review", "KEEP_CLEANUP"}:
            return None
        raw = str(item.get("destination", "")).strip()
        if raw:
            return Path(raw)
        if job.get("mode") == "original":
            folder = Path(str(job.get("folder", source.parent)))
            if decision == "KEEP":
                tag = str(item.get("position_from_end_tag", "")).strip()
                kept_name = source.name
                if tag and "_endminus_" not in source.stem.casefold():
                    kept_name = f"{source.stem}{tag}{source.suffix}"
                destination = folder / "Review" / kept_name
            else:
                destination = self.mosaic.deletion_folder(folder) / source.name
        elif job.get("mode") == "cleanup":
            destination = self.mosaic.deletion_folder(source.parent) / source.name
        elif job.get("mode") == "deletion":
            # Deletion-recovery jobs always carry an explicit validated target.
            return None
        else:
            model_folder = Path(str(job.get("model_folder", source.parent.parent)))
            tag = str(item.get("position_from_end_tag", "")).strip()
            kept_name = source.name
            if tag and decision in {"Cumshots", "Misc Hot Scenes"} and "_endminus_" not in source.stem.casefold():
                kept_name = f"{source.stem}{tag}{source.suffix}"
            if decision == "Cumshots":
                destination = model_folder / self.review.CUMSHOTS_FOLDER_NAME / kept_name
            elif decision == "Misc Hot Scenes":
                destination = model_folder / self.review.MISC_FOLDER_NAME / kept_name
            else:
                destination = self.review.deletion_folder(model_folder) / source.name
        item["destination"] = str(destination)
        return destination

    def _process_queued_move(self, job: Dict[str, Any], item: Dict[str, Any]) -> Tuple[bool, str]:
        destination = self._queued_destination(job, item)
        if destination is None:
            if job.get("mode") == "cleanup" and str(item.get("decision", "")) == "KEEP_CLEANUP":
                self._set_cleanup_reviewed(item, True)
                return True, "Kept in place and marked reviewed for non-NSFW cleanup."
            return True, "Left in Review."
        source = Path(str(item.get("source", "")))
        expected_size = int(item.get("bytes", 0) or 0)
        if not source.exists():
            if destination.exists() and (expected_size <= 0 or destination.stat().st_size == expected_size):
                return True, "Already moved."
            # A missing source cannot be repaired by retrying. In practice this
            # is most commonly a stale/duplicate mobile mosaic whose source set
            # was already handled by an earlier durable instruction. Retire it
            # idempotently so it can never poison the queue/background worker.
            return True, f"Source already absent; stale/duplicate instruction retired as already handled: {source.name}"
        if destination.exists():
            # Never assume an existing same-size file is the same recording while
            # the source still exists. Preserve both by choosing a unique target.
            chooser = self.mosaic.unique_destination if job.get("mode") in {"original", "cleanup"} else self.review.unique_destination
            destination = chooser(destination)
            item["destination"] = str(destination)
            self._save_action_queue()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if job.get("mode") in {"original", "cleanup"}:
            actual = self.mosaic.move_file_fast(source, destination)
        else:
            actual = self.review.move_fast(source, destination)
        item["destination"] = str(actual)
        return True, f"Moved to {actual}"

    def _process_permanent_delete_model_job(self, job: Dict[str, Any]) -> str:
        extensions = parse_extensions(self.review_settings.get("extensions", "mp4,ts"))
        folders = [Path(str(value)) for value in job.get("folders", [])]
        deleted = int(job.get("deleted", 0) or 0)
        deleted_bytes = int(job.get("deleted_bytes", 0) or 0)
        errors: List[str] = []
        queue_settings = self.config.get("action_queue", {})
        batch_size = max(1, min(512, int(queue_settings.get("batch_size", 64) or 64)))

        paths: List[Path] = []
        for folder in folders:
            if bool(job.get("cancel_requested")):
                raise TaskCancelled("Permanent deletion cancelled from the phone.")
            if not folder.is_dir() or not self._is_inside_deletion_root(folder):
                continue
            try:
                paths.extend(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in extensions)
            except OSError as exc:
                errors.append(f"{folder}: {exc}")

        # Re-enumerate the remaining on-disk files on every retry. Already
        # deleted paths are absent, so restarting at zero is idempotent and
        # cannot skip survivors after a crash.
        cursor = 0
        while cursor < len(paths):
            if bool(job.get("cancel_requested")):
                raise TaskCancelled("Permanent deletion cancelled from the phone.")
            self._yield_durable_job_to_phone(job)
            affected: Set[Path] = set()
            processed = 0
            with self.heavy_io_lock:
                while cursor < len(paths) and processed < batch_size:
                    path = paths[cursor]
                    cursor += 1
                    processed += 1
                    if not self._is_inside_deletion_root(path):
                        errors.append(f"Safety block: {path} is outside a configured MARKED_FOR_DELETION root.")
                        continue
                    model_folder = self._deletion_model_folder_for_path(path)
                    if model_folder is not None:
                        affected.add(model_folder)
                    try:
                        size = path.stat().st_size if path.exists() else 0
                        if path.exists():
                            path.unlink()
                            deleted += 1
                            deleted_bytes += int(size)
                            self._cleanup_empty_deletion_parents(path)
                    except OSError as exc:
                        errors.append(f"{path.name}: {exc}")
                    if bool(job.get("cancel_requested")) or self._interactive_phone_work_pending():
                        break
            job["processed_files"] = cursor
            job["deleted"] = deleted
            job["deleted_bytes"] = deleted_bytes
            job["message"] = (
                f"Permanent deletion: {cursor}/{len(paths)} inspected • "
                f"{deleted} file(s), {human_size(deleted_bytes)} deleted."
            )
            self._save_action_queue()
            if affected:
                try:
                    self._refresh_deletion_catalog_folders(affected)
                except Exception as exc:
                    append_log(f"Could not live-refresh deletion catalog during permanent deletion: {exc}")

        job["deleted"] = deleted
        job["deleted_bytes"] = deleted_bytes
        job["delete_errors"] = errors[:50]
        if errors and deleted == 0:
            raise RuntimeError("; ".join(errors[:8]))
        try:
            self._refresh_deletion_catalog_folders(folders)
        except Exception as exc:
            append_log(f"Could not finalize deletion catalog refresh: {exc}")
        message = f"Permanently deleted {deleted} file(s), {human_size(deleted_bytes)} from {len(job.get('model_names', []))} selected model(s)."
        if errors:
            message += f" {len(errors)} file(s) could not be deleted."
        return message

    def _frame_cut_idle_minutes(self, job: Optional[Dict[str, Any]] = None) -> float:
        if isinstance(job, dict) and "idle_minutes" in job:
            try:
                return max(0.0, float(job.get("idle_minutes", 0) or 0))
            except Exception:
                pass
        queue_settings = self.config.get("action_queue", {})
        if isinstance(queue_settings, dict) and "frame_cut_idle_minutes" in queue_settings:
            try:
                return max(0.0, float(queue_settings.get("frame_cut_idle_minutes", 10) or 0))
            except Exception:
                pass
        try:
            return max(0.0, float(self._library_settings().get("idle_minutes", 10) or 0))
        except Exception:
            return 10.0

    def _frame_cut_idle_ready(self, job: Dict[str, Any]) -> Tuple[bool, str]:
        required = self._frame_cut_idle_minutes(job) * 60.0
        if required <= 0:
            return True, ""
        idle = computer_idle_seconds()
        # Windows is the supported runtime. If the OS idle API is unavailable,
        # fail open rather than parking a destructive-but-safe queued operation
        # forever with no recovery path.
        if idle is None:
            return True, "Windows idle timer unavailable; proceeding without an idle measurement."
        if idle >= required:
            return True, ""
        return False, f"Waiting for PC idle: {max(0, int(required - idle))} more second(s)."

    def _action_job_ready(self, job: Dict[str, Any], now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else float(now)
        if str(job.get("status", "")) not in {"queued", "retrying"}:
            return False, ""
        if float(job.get("next_attempt_at", 0) or 0) > now:
            return False, ""
        if str(job.get("kind", "")) == "review_frame_cuts":
            return self._frame_cut_idle_ready(job)
        return True, ""

    def _review_chunk_from_frame_job(self, job: Dict[str, Any]) -> Any:
        rows = job.get("files", [])
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Frame-cut job no longer contains its source recording snapshot.")
        videos = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = Path(str(row.get("source", "")))
            start_raw = str(row.get("start", "")).strip()
            try:
                start = datetime.fromisoformat(start_raw) if start_raw else datetime.fromtimestamp(float(row.get("mtime", 0) or 0))
            except Exception:
                start = datetime.fromtimestamp(float(row.get("mtime", 0) or 0))
            videos.append(self.review.VideoInfo(
                path=path,
                start=start,
                size=int(row.get("bytes", 0) or 0),
                mtime=float(row.get("mtime", 0) or 0),
                duration=max(0.1, float(row.get("duration_seconds", 0.1) or 0.1)),
            ))
        if not videos:
            raise RuntimeError("Frame-cut job has no usable source recordings.")
        try:
            chunk_start = datetime.fromisoformat(str(job.get("chunk_start", "")))
        except Exception:
            chunk_start = min(video.start for video in videos)
        try:
            chunk_end = datetime.fromisoformat(str(job.get("chunk_end", "")))
        except Exception:
            chunk_end = max(video.end for video in videos)
        return self.review.Chunk(
            folder=Path(str(job.get("folder", videos[0].path.parent))),
            model_folder=Path(str(job.get("model_folder", videos[0].path.parent.parent))),
            files=videos,
            start=chunk_start,
            end=chunk_end,
            source_bytes=sum(max(0, int(video.size)) for video in videos),
            signature=str(job.get("chunk_signature", "")),
            mosaics=[Path(str(value)) for value in job.get("mosaic_artifacts", []) if str(value).lower().endswith(".jpg")],
        )

    def _probe_cut_duration(self, path: Path, ffmpeg: Path, ffprobe: Optional[Path]) -> Optional[float]:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if ffprobe is not None:
            try:
                result = subprocess.run(
                    [
                        str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path),
                    ],
                    capture_output=True, text=True, timeout=25,
                    creationflags=creationflags,
                )
                if result.returncode == 0:
                    value = float((result.stdout or "").strip())
                    if math.isfinite(value) and value > 0:
                        return value
            except Exception:
                pass
        try:
            result = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=25, creationflags=creationflags,
            )
            stderr = (result.stderr or b"").decode("utf-8", "replace")
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
            if match:
                return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        except Exception:
            pass
        return None

    def _verify_review_cut_output(
        self,
        path: Path,
        expected_duration: float,
        ffmpeg: Path,
        ffprobe: Optional[Path],
    ) -> Tuple[bool, str]:
        try:
            if not path.is_file():
                return False, "output file is missing"
            size = int(path.stat().st_size)
            if size < 16 * 1024:
                return False, f"output is unexpectedly small ({size} bytes)"
        except OSError as exc:
            return False, str(exc)
        actual = self._probe_cut_duration(path, ffmpeg, ffprobe)
        if actual is None:
            return False, "could not verify output duration"
        tolerance = max(2.5, float(expected_duration) * 0.03)
        if abs(float(actual) - float(expected_duration)) > tolerance:
            return False, (
                f"duration verification failed: expected about {expected_duration:.2f}s, "
                f"got {actual:.2f}s"
            )
        return True, f"verified {human_size(size)} • {actual:.2f}s"

    def _terminate_process(self, process: subprocess.Popen[Any]) -> None:
        try:
            process.terminate()
            process.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass

    def _frame_cut_stop_requested(self, job: Dict[str, Any]) -> bool:
        return bool(job.get("cancel_requested")) or str(job.get("status", "")) == "undo_requested"

    def _run_idle_frame_cut_ffmpeg(
        self,
        job: Dict[str, Any],
        command: Sequence[str],
        temp_output: Path,
        timeout_seconds: float,
    ) -> Tuple[int, bytes]:
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # Scheduler priority is above background mosaics/NSFW, while OS CPU
            # priority stays below-normal so the HTTP/UI control plane remains
            # responsive if a phone session is active on an otherwise-idle PC.
            creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        started = time.monotonic()
        with tempfile.TemporaryFile() as error_handle:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=error_handle,
                creationflags=creationflags,
            )
            while True:
                code = process.poll()
                if code is not None:
                    error_handle.seek(0)
                    return int(code), error_handle.read()
                if self._frame_cut_stop_requested(job):
                    self._terminate_process(process)
                    try:
                        temp_output.unlink()
                    except OSError:
                        pass
                    raise TaskCancelled("Frame-cut job stopped because Cancel/Back was requested from the phone.")
                ready, _reason = self._frame_cut_idle_ready(job)
                if not ready or self._interactive_phone_work_pending():
                    self._terminate_process(process)
                    try:
                        temp_output.unlink()
                    except OSError:
                        pass
                    raise ActionDeferred(
                        "Frame cut yielded because the PC became active or the phone requested mosaic work. "
                        "It will resume automatically after the PC is idle again."
                    )
                if time.monotonic() - started > timeout_seconds:
                    self._terminate_process(process)
                    try:
                        temp_output.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(f"ffmpeg frame-cut operation exceeded its {int(timeout_seconds)} second timeout.")
                time.sleep(0.5)

    def _process_review_frame_cut_job(self, job: Dict[str, Any]) -> str:
        """Create/verify precise clips first, then move every original to deletion.

        The job is idempotent across retries and process restarts. Planned output
        paths contain the durable job id; already-created clips are reverified
        and reused, while source moves use the normal non-overwriting queue move
        semantics.
        """
        chunk = self._review_chunk_from_frame_job(job)
        changed_sources: List[str] = []
        for row, video in zip([item for item in job.get("files", []) if isinstance(item, dict)], chunk.files):
            try:
                if not video.path.is_file():
                    continue
                stat = video.path.stat()
                expected_size = int(row.get("bytes", 0) or 0)
                expected_mtime = float(row.get("mtime", 0) or 0)
                if expected_size > 0 and int(stat.st_size) != expected_size:
                    changed_sources.append(f"{video.path.name} size changed")
                elif expected_mtime > 0 and abs(float(stat.st_mtime) - expected_mtime) > 0.01:
                    changed_sources.append(f"{video.path.name} modified")
            except OSError as exc:
                changed_sources.append(f"{video.path.name}: {exc}")
        if changed_sources:
            raise RuntimeError(
                "Frame-cut safety block: source recording changed after you made the mosaic decision: "
                + "; ".join(changed_sources[:8])
            )
        ffmpeg, ffprobe = self.review.resolve_ffmpeg(self.review_settings)
        if ffmpeg is None:
            raise RuntimeError("ffmpeg was not found for the queued Review frame-cut job.")

        clips = [row for row in job.get("clips", []) if isinstance(row, dict)]
        total_clips = len(clips)
        if not bool(job.get("clips_verified")):
            # All originals must still be available until replacement creation
            # is complete. A prior partial source move can only be resumed when
            # every planned replacement already verifies.
            missing_sources = [
                video.path.name for video in chunk.files
                if not video.path.is_file()
            ]
            all_existing_outputs_valid = True
            for clip in clips:
                output = Path(str(clip.get("output", "")))
                expected = max(0.1, float(clip.get("end_seconds", 0)) - float(clip.get("start_seconds", 0)))
                ok, _message = self._verify_review_cut_output(output, expected, ffmpeg, ffprobe)
                if not ok:
                    all_existing_outputs_valid = False
                    break
            if missing_sources and not all_existing_outputs_valid:
                raise RuntimeError(
                    "Cannot safely resume frame cutting because source recording(s) are missing before every "
                    "replacement clip was verified: " + ", ".join(missing_sources[:8])
                )

            with tempfile.TemporaryDirectory(prefix="mobile_review_frame_cut_") as temp:
                concat_file = Path(temp) / "sources.txt"
                if clips and not all_existing_outputs_valid:
                    self.review.write_concat_list(chunk.files, concat_file)
                for index, clip in enumerate(clips, start=1):
                    if self._frame_cut_stop_requested(job):
                        raise TaskCancelled("Frame-cut job stopped because Cancel/Back was requested from the phone.")
                    start_seconds = max(0.0, float(clip.get("start_seconds", 0) or 0))
                    end_seconds = max(start_seconds, float(clip.get("end_seconds", start_seconds) or start_seconds))
                    duration = max(0.1, end_seconds - start_seconds)
                    output = Path(str(clip.get("output", "")))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    ok, verification = self._verify_review_cut_output(output, duration, ffmpeg, ffprobe)
                    if ok:
                        clip["status"] = "verified"
                        clip["verification"] = verification
                        job["message"] = f"Frame cuts: reused verified clip {index}/{total_clips}."
                        self._save_action_queue()
                        continue

                    # This path is job-specific. Remove only an invalid output
                    # belonging to this durable job, never a generic user file.
                    token = str(job.get("id", ""))[:8]
                    if output.exists() and token and token in output.name:
                        try:
                            output.unlink()
                        except OSError:
                            pass
                    temp_output = output.with_name(f".{output.stem}.{token}.part.mp4")
                    try:
                        if temp_output.exists():
                            temp_output.unlink()
                    except OSError:
                        pass
                    command = [
                        str(ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", str(concat_file),
                        "-ss", f"{start_seconds:.3f}", "-t", f"{duration:.3f}",
                        "-map", "0:v:0", "-map", "0:a:0?",
                        "-threads", "1",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", "-y", str(temp_output),
                    ]
                    job["message"] = (
                        f"Idle frame cut {index}/{total_clips}: "
                        f"{self.review.format_clock(start_seconds)}–{self.review.format_clock(end_seconds)} "
                        f"→ {clip.get('destination', 'Review')}"
                    )
                    self._save_action_queue()
                    timeout_seconds = min(21600.0, max(180.0, duration * 3.0 + 180.0))
                    with self.heavy_io_lock:
                        code, stderr = self._run_idle_frame_cut_ffmpeg(job, command, temp_output, timeout_seconds)
                    if code != 0 or not temp_output.exists():
                        error = (stderr or b"").decode("utf-8", "replace")[-2500:]
                        raise RuntimeError(f"Frame cut {index}/{total_clips} failed: {error or 'ffmpeg returned no output.'}")
                    ok, verification = self._verify_review_cut_output(temp_output, duration, ffmpeg, ffprobe)
                    if not ok:
                        try:
                            temp_output.unlink()
                        except OSError:
                            pass
                        raise RuntimeError(f"Frame cut {index}/{total_clips} failed verification: {verification}")
                    os.replace(temp_output, output)
                    ok, verification = self._verify_review_cut_output(output, duration, ffmpeg, ffprobe)
                    if not ok:
                        raise RuntimeError(f"Frame cut {index}/{total_clips} failed final verification: {verification}")
                    clip["status"] = "verified"
                    clip["verification"] = verification
                    clip["bytes"] = int(output.stat().st_size)
                    self._save_action_queue()

            job["clips_verified"] = True
            job["clips_verified_at"] = datetime.now().isoformat(timespec="seconds")
            job["message"] = (
                f"Verified {len(clips)} replacement clip(s). Moving original Review recording(s) to deletion…"
            )
            self._save_action_queue()

        if self._frame_cut_stop_requested(job):
            raise TaskCancelled("Frame-cut job stopped before original recordings were moved because Cancel/Back was requested.")

        # Only now may originals leave Review. This loop is safe to replay:
        # destination paths are persisted and missing source + matching
        # destination is treated as already moved.
        items = [row for row in job.get("files", []) if isinstance(row, dict)]
        moved = 0
        affected_deletion: Set[Path] = set()
        for index, item in enumerate(items, start=1):
            if self._frame_cut_stop_requested(job):
                raise TaskCancelled("Frame-cut job stopped because Cancel/Back was requested from the phone.")
            self._yield_durable_job_to_phone(job)
            with self.heavy_io_lock:
                ok, message = self._process_queued_move(job, item)
            item["result"] = message
            if not ok:
                raise RuntimeError(message)
            moved += 1
            destination_raw = str(item.get("destination", "")).strip()
            if destination_raw:
                folder = self._deletion_model_folder_for_path(Path(destination_raw))
                if folder is not None:
                    affected_deletion.add(folder)
            job["processed_files"] = index
            job["message"] = f"Frame cuts verified; moving originals to deletion {index}/{len(items)}."
            self._save_action_queue()

        if affected_deletion:
            try:
                self._refresh_deletion_catalog_folders(affected_deletion)
            except Exception as exc:
                append_log(f"Could not live-refresh deletion catalog after frame cuts: {exc}")

        with self.heavy_io_lock:
            for raw in job.get("mosaic_artifacts", []):
                artifact = Path(str(raw))
                try:
                    if artifact.is_file():
                        artifact.unlink()
                except OSError:
                    pass

        kept_seconds = sum(
            max(0.0, float(clip.get("end_seconds", 0)) - float(clip.get("start_seconds", 0)))
            for clip in clips
        )
        return (
            f"Created/verified {len(clips)} precise kept clip(s) ({self.review.format_clock(kept_seconds)} total) "
            f"and moved {moved} original Review recording(s) to MARKED_FOR_DELETION."
        )

    def _process_action_job(self, job: Dict[str, Any]) -> str:
        kind = str(job.get("kind", "chunk_moves"))
        if kind == "permanent_delete_models":
            return self._process_permanent_delete_model_job(job)
        if kind in {"keep_last_all", "keep_last_model"}:
            return self._process_keep_last_job(job)
        if kind == "ignore_model_folders":
            return self._process_ignore_model_folders_job(job)
        if kind == "review_frame_cuts":
            return self._process_review_frame_cut_job(job)
        if kind == "undo_chunk":
            target = self._find_action_job(str(job.get("target_job_id", "")))
            if target is None:
                return "Undo target no longer exists; nothing to restore."
            return self._undo_action_job(target)

        moved = 0
        left = 0
        errors: List[str] = []
        items = [item for item in job.get("files", []) if isinstance(item, dict)]
        queue_settings = self.config.get("action_queue", {})
        batch_size = max(1, min(512, int(queue_settings.get("batch_size", 64) or 64)))
        affected_deletion: Set[Path] = set()
        cursor = 0

        # Same-drive moves are metadata renames and are dramatically faster when
        # we avoid re-locking and atomically rewriting the queue JSON after every
        # single file. We still check for requested model-mosaic work after each
        # individual operation so a phone request can cut a batch short.
        while cursor < len(items):
            if bool(job.get("cancel_requested")):
                raise TaskCancelled("Queued action cancelled from the phone.")
            if str(job.get("status", "")) == "undo_requested":
                break
            self._yield_durable_job_to_phone(job)
            processed = 0
            with self.heavy_io_lock:
                while cursor < len(items) and processed < batch_size:
                    if bool(job.get("cancel_requested")):
                        break
                    if str(job.get("status", "")) == "undo_requested":
                        break
                    item = items[cursor]
                    source = Path(str(item.get("source", "")))
                    ok, message = self._process_queued_move(job, item)
                    item["result"] = message
                    destination_raw = str(item.get("destination", "")).strip()
                    for candidate in (source, Path(destination_raw) if destination_raw else None):
                        if candidate is None:
                            continue
                        folder = self._deletion_model_folder_for_path(candidate)
                        if folder is not None:
                            affected_deletion.add(folder)
                    if ok:
                        if str(item.get("decision")) in {"Leave for review", "KEEP_CLEANUP"}:
                            left += 1
                        else:
                            moved += 1
                    else:
                        errors.append(message)
                    cursor += 1
                    processed += 1
                    # Do not finish the rest of a large batch once a newly
                    # clicked model/offline preparation needs FFmpeg/disk access.
                    if self._interactive_phone_work_pending():
                        break
            job["processed_files"] = cursor
            job["message"] = (
                f"Applying queued {job.get('mode')} decisions: {cursor}/{len(items)} processed • "
                f"{moved} moved • {left} unchanged."
            )
            self._save_action_queue()
            if affected_deletion:
                try:
                    self._refresh_deletion_catalog_folders(affected_deletion)
                except Exception as exc:
                    append_log(f"Could not live-refresh deletion catalog: {exc}")
                affected_deletion.clear()

        if bool(job.get("cancel_requested")):
            raise TaskCancelled("Queued action cancelled from the phone.")
        if errors:
            raise RuntimeError("; ".join(errors[:8]))

        undo_requested = str(job.get("status", "")) == "undo_requested"
        self._yield_durable_job_to_phone(job)
        with self.heavy_io_lock:
            if job.get("mode") == "original" and not undo_requested:
                folder = Path(str(job.get("folder", "")))
                chunk_key = str(job.get("chunk_key", ""))
                if folder.is_dir() and chunk_key:
                    state = self.mosaic.load_review_state(folder)
                    state.setdefault("reviewed", {})[chunk_key] = "done"
                    self.mosaic.save_review_state(folder, state)
                signature = str(job.get("chunk_signature", ""))
                if signature:
                    self.mosaic_manifest.forget(signature)
            if not undo_requested:
                for raw in job.get("mosaic_artifacts", []):
                    artifact = Path(str(raw))
                    try:
                        if artifact.is_file():
                            artifact.unlink()
                    except OSError:
                        pass

        if undo_requested:
            return f"Stopped queued {job.get('mode')} decisions early for Back; moved {moved}; left unchanged {left}."
        return f"Applied queued {job.get('mode')} decisions: moved {moved}; left unchanged {left}."

    def _background_heavy_work_active(self) -> bool:
        """True while a whole-library/Non-NSFW heavy background pass owns the background lane.

        Due *retry* jobs must not repeatedly interrupt useful background generation.
        Fresh phone decisions still outrank these passes through the normal scheduler.
        """
        with self.lock:
            library_future = getattr(self, "library_future", None)
            nsfw_future = getattr(self, "nsfw_future", None)
        return bool(
            (library_future is not None and not library_future.done())
            or (nsfw_future is not None and not nsfw_future.done())
        )

    def _action_loop(self) -> None:
        while not self.stop_event.is_set():
            selected: Optional[Dict[str, Any]] = None
            now = time.time()
            with self.lock:
                candidates = [
                    job for job in self.action_queue.get("jobs", [])
                    if isinstance(job, dict)
                    and job.get("status") in {"queued", "retrying"}
                    and float(job.get("next_attempt_at", 0) or 0) <= now
                ]
                candidates.sort(key=lambda job: (
                    0 if str(job.get("kind", "")) == "undo_chunk" else
                    1 if str(job.get("status", "")) == "queued" else 2,
                    str(job.get("created_at", "")),
                ))
            eligible: List[Dict[str, Any]] = []
            for job in candidates:
                ready, reason = self._action_job_ready(job, now)
                if ready:
                    eligible.append(job)
                elif str(job.get("kind", "")) == "review_frame_cuts" and reason:
                    job["message"] = reason
            candidates = eligible
            undo_waiting = next((job for job in candidates if str(job.get("kind", "")) == "undo_chunk"), None)
            # A due retry is cleanup/recovery work, not something that should
            # repeatedly steal the heavy-I/O lane from a useful background pass.
            # Fresh queued instructions still outrank the whole-library passes.
            if (
                undo_waiting is None
                and candidates
                and str(candidates[0].get("status", "")) == "retrying"
                and str(candidates[0].get("kind", "")) != "review_frame_cuts"
                and self._background_heavy_work_active()
            ):
                self.action_wakeup.wait(0.5)
                self.action_wakeup.clear()
                continue
            # Back/undo remains a correctness operation. Ordinary sorting
            # moves, Keep Last, restores, and permanent deletion wait only behind
            # requested/current-model mosaic work. They outrank whole-library
            # mosaic and Non-NSFW passes and may catch up while the user reviews
            # an already-ready JPEG.
            if undo_waiting is None and self._mosaic_work_preempts_actions():
                self.action_wakeup.wait(0.5)
                self.action_wakeup.clear()
                continue
            with self.lock:
                # Re-read because status may have changed while we waited.
                candidates = [
                    job for job in self.action_queue.get("jobs", [])
                    if isinstance(job, dict)
                    and job.get("status") in {"queued", "retrying"}
                    and float(job.get("next_attempt_at", 0) or 0) <= time.time()
                ]
                candidates.sort(key=lambda job: (
                    0 if str(job.get("kind", "")) == "undo_chunk" else
                    1 if str(job.get("status", "")) == "queued" else 2,
                    str(job.get("created_at", "")),
                ))
            candidates = [
                job for job in candidates
                if self._action_job_ready(job, time.time())[0]
            ]
            with self.lock:
                if candidates:
                    selected = candidates[0]
                    selected["status"] = "running"
                    selected["started_at"] = datetime.now().isoformat(timespec="seconds")
                    selected["message"] = "Applying deferred PC instruction…"
                    self.action_running_id = str(selected.get("id", ""))
            if selected is None:
                self.action_wakeup.wait(1.0)
                self.action_wakeup.clear()
                continue
            self._save_action_queue()
            try:
                message = self._process_action_job(selected)
                with self.lock:
                    selected["status"] = "done"
                    selected["message"] = message
                    selected["error"] = ""
                    selected["completed_at"] = datetime.now().isoformat(timespec="seconds")
                    self.action_running_id = ""
                append_log(f"Queued mobile action completed: {message}")
            except TaskCancelled as exc:
                with self.lock:
                    selected["status"] = "cancelled"
                    selected["message"] = str(exc) or "Cancelled from the phone."
                    selected["error"] = ""
                    selected["completed_at"] = datetime.now().isoformat(timespec="seconds")
                    self.action_running_id = ""
                append_log(f"Queued mobile action cancelled: {exc}")
            except ActionDeferred as exc:
                with self.lock:
                    selected["status"] = "queued"
                    selected["message"] = str(exc)
                    selected["error"] = ""
                    selected["next_attempt_at"] = 0
                    self.action_running_id = ""
                append_log(f"Queued frame-cut action yielded without consuming a retry: {exc}")
            except Exception as exc:
                attempts = int(selected.get("attempts", 0) or 0) + 1
                queue_settings = self.config.get("action_queue", {})
                initial = max(2, int(queue_settings.get("initial_retry_seconds", 20)))
                maximum = max(initial, int(queue_settings.get("maximum_retry_seconds", 300)))
                maximum_attempts = max(1, int(queue_settings.get("maximum_attempts", 3)))
                delay = min(maximum, initial * (2 ** min(attempts - 1, 8)))
                with self.lock:
                    selected["status"] = "failed" if attempts >= maximum_attempts else "retrying"
                    selected["attempts"] = attempts
                    selected["error"] = str(exc)
                    selected["message"] = (
                        "Automatic retries stopped; inspect mobile_reviewer.log and restart/resubmit after fixing the file issue."
                        if attempts >= maximum_attempts else f"Will retry in {delay} seconds."
                    )
                    selected["next_attempt_at"] = 0 if attempts >= maximum_attempts else time.time() + delay
                    self.action_running_id = ""
                append_log(f"Queued mobile action failed attempt {attempts}: {exc}\n{traceback.format_exc()}")
            finally:
                self._save_action_queue()

    def _enqueue_review_frame_cut_action(
        self,
        queue_data: Dict[str, Any],
        chunk: Any,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        timeline = self._review_frame_timeline(chunk)
        if not timeline:
            raise RuntimeError(
                "Frame Cut requires an exact timestamped Review mosaic. Regenerate this mosaic once and try again."
            )
        raw = payload.get("frame_decisions", {})
        raw = raw if isinstance(raw, dict) else {}
        allowed_kept = {"Leave for review", "Cumshots", "Misc Hot Scenes"}
        decisions: Dict[int, str] = {}
        for key, value in raw.items():
            try:
                frame_index = int(key)
            except Exception:
                continue
            decision = str(value)
            if decision in allowed_kept:
                decisions[frame_index] = decision

        # Each tile represents [its timestamp, next tile timestamp). Consecutive
        # selected tiles of the same destination are merged into one output
        # range; all unselected time is discarded when originals reach deletion.
        ranges: List[Dict[str, Any]] = []
        selected_frames = 0
        for row in timeline:
            frame_index = int(row["frame_index"])
            decision = decisions.get(frame_index)
            if decision not in allowed_kept:
                continue
            selected_frames += 1
            start = max(0.0, float(row["chunk_seconds"]))
            end = max(start, float(row["interval_end_seconds"]))
            if end <= start + 0.001:
                continue
            if (
                ranges
                and str(ranges[-1]["destination"]) == decision
                and abs(float(ranges[-1]["end_seconds"]) - start) <= 0.05
            ):
                ranges[-1]["end_seconds"] = end
                ranges[-1]["frame_end"] = frame_index
            else:
                ranges.append({
                    "start_seconds": start,
                    "end_seconds": end,
                    "destination": decision,
                    "frame_start": frame_index,
                    "frame_end": frame_index,
                })

        files: List[Dict[str, Any]] = []
        for index, video in enumerate(chunk.files):
            files.append({
                "index": index,
                "source": str(video.path),
                "bytes": int(video.size),
                "mtime": float(video.mtime),
                "start": video.start.isoformat(),
                "duration_seconds": float(video.duration),
                # Once replacement clips verify, every original source recording
                # moves to deletion regardless of which sampled intervals were kept.
                "decision": "DELETE",
            })
        source_identity = self._source_identity_from_descriptors(files)
        existing = self._existing_job_for_source_identity(source_identity)
        if existing is not None:
            return existing

        job_id = secrets.token_urlsafe(12)
        token = job_id[:8]
        clips: List[Dict[str, Any]] = []
        for spec in ranges:
            destination = str(spec["destination"])
            if destination == "Cumshots":
                destination_dir = chunk.model_folder / self.review.CUMSHOTS_FOLDER_NAME
            elif destination == "Misc Hot Scenes":
                destination_dir = chunk.model_folder / self.review.MISC_FOLDER_NAME
            else:
                destination_dir = chunk.model_folder / self.review.REVIEW_FOLDER_NAME
            base_name = self.review.build_trim_output_name(
                chunk,
                float(spec["start_seconds"]),
                float(spec["end_seconds"]),
            )
            base_path = Path(base_name)
            output_name = f"{base_path.stem}_vf_{token}{base_path.suffix}"
            clips.append({
                **spec,
                "output": str(destination_dir / output_name),
                "status": "queued",
                "verification": "",
            })

        output_dir = chunk.model_folder / self.review.MOSAIC_DIRNAME
        base = self.review.mosaic_base(chunk)
        artifacts = [str(path) for path in output_dir.glob(base + "*.jpg")]
        artifacts.append(str(output_dir / f"{base}.json"))
        idle_minutes = self._frame_cut_idle_minutes()
        kept_seconds = sum(
            max(0.0, float(row["end_seconds"]) - float(row["start_seconds"]))
            for row in ranges
        )
        job = {
            "id": job_id,
            "kind": "review_frame_cuts",
            "status": "queued",
            "mode": "review",
            "model": str(queue_data.get("model", "")),
            "chunk_signature": str(getattr(chunk, "signature", "")),
            "source_identity": source_identity,
            "folder": str(getattr(chunk, "folder", "")),
            "model_folder": str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))),
            "chunk_start": chunk.start.isoformat(),
            "chunk_end": chunk.end.isoformat(),
            "files": files,
            "clips": clips,
            "mosaic_artifacts": sorted(set(artifacts)),
            "selected_frames": selected_frames,
            "frame_count": len(timeline),
            "kept_seconds": kept_seconds,
            "clips_verified": False,
            "idle_only": True,
            "idle_minutes": idle_minutes,
            "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": (
                f"Queued precise frame cut: {selected_frames}/{len(timeline)} sampled frame interval(s) selected, "
                f"{len(clips)} merged output clip(s), {self.review.format_clock(kept_seconds)} kept. "
                f"Waiting for {idle_minutes:g} minute(s) of PC idle time."
            ),
            "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue()
        self.action_wakeup.set()
        return job

    def _enqueue_action_for_chunk(self, queue_data: Dict[str, Any], chunk: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(queue_data.get("mode", "original"))
        if mode not in {"original", "review"}:
            raise RuntimeError("Only Originals and Review decisions can be queued here.")
        if mode == "review" and bool(payload.get("frame_cut_mode")):
            return self._enqueue_review_frame_cut_action(queue_data, chunk, payload)
        files: List[Dict[str, Any]] = []
        if mode == "original":
            raw = payload.get("keep_indices", [])
            keep = {int(value) for value in raw if str(value).lstrip("-").isdigit()}
            keep = {value for value in keep if 0 <= value < len(chunk.files)}
            durations = [max(0.1, float(getattr(video, "duration", 0.1))) for video in chunk.files]
            total_duration = sum(durations)
            cursor_seconds = 0.0
            for index, video in enumerate(chunk.files):
                start_seconds = cursor_seconds
                end_seconds = min(total_duration, cursor_seconds + durations[index])
                cursor_seconds = end_seconds
                start_from_end = max(0, int(round(total_duration - start_seconds)))
                end_from_end = max(0, int(round(total_duration - end_seconds)))
                sm, ss = divmod(start_from_end, 60); em, es = divmod(end_from_end, 60)
                files.append({
                    "index": index,
                    "source": str(video.path),
                    "bytes": int(video.size),
                    "mtime": float(video.mtime),
                    "decision": "KEEP" if index in keep else "DELETE",
                    "chunk_start_seconds": float(start_seconds),
                    "chunk_end_seconds": float(end_seconds),
                    "position_from_end_tag": f"_endminus_{sm}m{ss:02d}s_to_{em}m{es:02d}s",
                })
        else:
            raw = payload.get("decisions", {})
            allowed = {"Leave for review", "DELETE", "Cumshots", "Misc Hot Scenes"}
            durations = [max(0.1, float(getattr(video, "duration", 0.1))) for video in chunk.files]
            total_duration = sum(durations)
            cursor_seconds = 0.0
            for index, video in enumerate(chunk.files):
                decision = str(raw.get(str(index), raw.get(index, "Leave for review")))
                start_seconds = cursor_seconds
                end_seconds = min(total_duration, cursor_seconds + durations[index])
                cursor_seconds = end_seconds
                start_from_end = max(0, int(round(total_duration - start_seconds)))
                end_from_end = max(0, int(round(total_duration - end_seconds)))
                sm, ss = divmod(start_from_end, 60); em, es = divmod(end_from_end, 60)
                files.append({
                    "index": index,
                    "source": str(video.path),
                    "bytes": int(video.size),
                    "mtime": float(video.mtime),
                    "decision": decision if decision in allowed else "Leave for review",
                    "chunk_start_seconds": float(start_seconds),
                    "chunk_end_seconds": float(end_seconds),
                    "position_from_end_tag": f"_endminus_{sm}m{ss:02d}s_to_{em}m{es:02d}s",
                })
        source_identity = self._source_identity_from_descriptors(files)
        existing = self._existing_job_for_source_identity(source_identity)
        if existing is not None:
            return existing
        artifacts = [str(Path(path)) for path in getattr(chunk, "mosaics", []) if Path(path).exists()]
        if mode == "original":
            output_dir = chunk.folder / self.mosaic.MOSAIC_DIRNAME
            base = self.mosaic.mosaic_base_stem(chunk)
            artifacts.extend(str(path) for path in output_dir.glob(base + "*.jpg"))
            artifacts.append(str(output_dir / f"{base}.sources.json"))
        else:
            output_dir = chunk.model_folder / self.review.MOSAIC_DIRNAME
            base = self.review.mosaic_base(chunk)
            artifacts.extend(str(path) for path in output_dir.glob(base + "*.jpg"))
            artifacts.append(str(output_dir / f"{base}.json"))
        job = {
            "id": secrets.token_urlsafe(12),
            "kind": "chunk_moves",
            "status": "queued",
            "mode": mode,
            "model": str(queue_data.get("model", "")),
            "chunk_signature": str(getattr(chunk, "signature", "")),
            "source_identity": source_identity,
            "chunk_key": str(getattr(chunk, "key", "")),
            "folder": str(getattr(chunk, "folder", "")),
            "model_folder": str(getattr(chunk, "model_folder", getattr(chunk, "folder", ""))),
            "files": files,
            "mosaic_artifacts": sorted(set(artifacts)),
            "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": "Queued from the phone.",
            "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue()
        self.action_wakeup.set()
        return job

    def _restore_target_for_deleted(self, source: Path) -> Tuple[Optional[Path], str]:
        """Choose a safe restore destination for a file in MARKED_FOR_DELETION.

        Durable move history is authoritative. Older deletion files may predate
        that history and can belong to a logical model that exists in more than
        one configured recording root. In that case, do not block the user with
        an ambiguity error: restore the explicitly rescued recording into the
        canonical same-drive Review folder for that physical model.

        The canonical fallback is deterministic (configured root order), keeps
        rescued material out of the deletion bucket, and ensures the Review
        sorter will see it regardless of which duplicate physical model folder
        originally contained it.
        """
        source_key = normalized(source)
        with self.lock:
            history = list(self.action_queue.get("jobs", []))
        for job in reversed(history):
            if not isinstance(job, dict):
                continue
            for item in job.get("files", []):
                if not isinstance(item, dict):
                    continue
                destination = str(item.get("destination", ""))
                if destination and normalized(Path(destination)) == source_key:
                    original_raw = str(item.get("source", "")).strip()
                    if original_raw:
                        original = Path(original_raw)
                        return original, "Recorded mobile move history"

        deletion_roots = configured_deletion_roots(self.roots)
        drive = drive_label(source)
        deletion_root = deletion_roots.get(drive)
        if deletion_root is None:
            return None, "No configured deletion root for this drive"
        try:
            relative = source.resolve(strict=False).relative_to(deletion_root.resolve(strict=False))
        except Exception:
            return None, "File is outside the configured deletion root"
        parts = relative.parts
        if len(parts) < 2:
            return None, "Deletion path does not identify a model"

        physical_model = parts[0]
        # Prefer already-existing same-drive physical model folders in configured
        # root order. This deliberately treats same-name folders as one logical
        # model instead of raising an ambiguity error.
        candidates: List[Path] = []
        seen: Set[str] = set()
        for root in self.roots:
            if drive_label(root).upper() != drive.upper():
                continue
            candidate = root / physical_model
            key = normalized(candidate)
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_dir():
                    candidates.append(candidate)
            except OSError:
                continue

        if not candidates:
            # The original folder can disappear after every direct recording was
            # moved. If the configured same-drive recording root still exists,
            # recreating the physical model folder is safer than leaving a user-
            # selected rescue trapped in MARKED_FOR_DELETION.
            for root in self.roots:
                if drive_label(root).upper() != drive.upper():
                    continue
                try:
                    if root.is_dir():
                        candidates.append(root / physical_model)
                        break
                except OSError:
                    continue

        if not candidates:
            return None, "No usable same-drive recording root exists for this model"

        model_folder = candidates[0]
        target_dir = model_folder / self.review.REVIEW_FOLDER_NAME
        return target_dir / source.name, (
            "Canonical same-drive Review folder"
            if len(candidates) == 1
            else f"Canonical same-drive Review folder (selected first of {len(candidates)} same-name model folders)"
        )

    def _enqueue_deletion_restore_action(self, queue_data: Dict[str, Any], chunk: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("restore_indices", [])
        selected = {int(value) for value in raw if str(value).lstrip("-").isdigit()}
        selected = {value for value in selected if 0 <= value < len(chunk.files)}
        if not selected:
            # A no-op still advances rapidly and is represented as a durable job
            # only when actual restores are requested.
            return {"id": "no-op", "status": "done", "message": "No files selected to restore."}
        files: List[Dict[str, Any]] = []
        unavailable: List[str] = []
        for index in sorted(selected):
            video = chunk.files[index]
            target, reason = self._restore_target_for_deleted(video.path)
            if target is None:
                unavailable.append(f"#{index + 1} {video.path.name}: {reason}")
                continue
            files.append({
                "index": index, "source": str(video.path), "bytes": int(video.size),
                "mtime": float(video.mtime), "decision": "RESTORE",
                "destination": str(target), "restore_reason": reason,
            })
        if unavailable:
            raise RuntimeError("Cannot safely restore selected file(s): " + "; ".join(unavailable[:6]))
        source_identity = self._source_identity_from_descriptors(files)
        existing = self._existing_job_for_source_identity(source_identity)
        if existing is not None:
            return existing
        artifacts = [str(Path(path)) for path in getattr(chunk, "mosaics", []) if Path(path).exists()]
        job = {
            "id": secrets.token_urlsafe(12), "kind": "deletion_restore", "status": "queued",
            "mode": "deletion", "model": str(queue_data.get("model", "")),
            "chunk_signature": str(getattr(chunk, "signature", "")),
            "source_identity": source_identity,
            "folder": str(getattr(chunk, "folder", "")),
            "model_folder": str(getattr(chunk, "model_folder", "")),
            "files": files, "mosaic_artifacts": artifacts, "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"Queued {len(files)} deletion-recovery restore(s) from the phone.", "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue(); self.action_wakeup.set()
        return job

    def retry_blocked_actions(self) -> Dict[str, Any]:
        count = 0
        with self.lock:
            for job in self.action_queue.get("jobs", []):
                if isinstance(job, dict) and job.get("status") in {"failed", "retired", "blocked"}:
                    job["status"] = "queued"
                    job["attempts"] = 0
                    job["next_attempt_at"] = 0
                    job["message"] = "Manually requeued from the phone."
                    job["error"] = ""
                    count += 1
        self._save_action_queue()
        if count:
            self.action_wakeup.set()
        return {"requeued": count, "message": f"Requeued {count} failed/stopped sorting job(s)."}

    def _keep_last_settings(self) -> Dict[str, Any]:
        raw = self.config.get("keep_last", {})
        merged = dict(DEFAULT_CONFIG["keep_last"])
        if isinstance(raw, dict):
            merged.update(raw)
        return merged

    def _keep_last_rules_path(self) -> Path:
        raw = str(self._keep_last_settings().get("file_path", "keeplasts.txt")).strip() or "keeplasts.txt"
        path = Path(os.path.expandvars(os.path.expanduser(raw.strip('"'))))
        if path.is_absolute():
            return path
        local = APP_DIR / path
        if local.is_file():
            return local
        # The established model sorter commonly lives above Mobile Reviewer in
        # the same project tree. For the default filename only, find that file
        # in a parent folder without doing an expensive recursive search.
        if path.name.casefold() == "keeplasts.txt":
            for parent in list(APP_DIR.parents)[:5]:
                candidate = parent / path.name
                if candidate.is_file():
                    return candidate
        return local

    def keep_last_status_payload(self) -> Dict[str, Any]:
        path = self._keep_last_rules_path()
        rules = self.keep_last.load_rules(path)
        return {
            "path": str(path), "exists": path.is_file(), "rule_count": len(rules),
            "message": (f"{len(rules):,} Keep Last model rule(s) ready." if rules else f"No Keep Last rules found at {path}."),
        }

    def queue_keep_last_all(self) -> Dict[str, Any]:
        path = self._keep_last_rules_path()
        rules = self.keep_last.load_rules(path)
        if not rules:
            raise RuntimeError(f"No Keep Last rules were found in {path}.")
        settings = self._keep_last_settings()
        job = {
            "id": secrets.token_urlsafe(12), "kind": "keep_last_all", "status": "queued",
            "mode": "keep_last", "model": f"{len(rules)} designated models",
            "rules": rules, "rules_path": str(path),
            "gap_minutes": float(settings.get("gap_minutes", 30)),
            "recent_write_grace_seconds": int(settings.get("recent_write_grace_seconds", 120)),
            "attempts": 0, "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"Keep Last all queued for {len(rules)} designated model(s).", "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue(); self.action_wakeup.set()
        return {"queued": True, "job_id": job["id"], "rule_count": len(rules), "message": job["message"]}

    def keep_last_minutes_for_model(self, model_name: str) -> float:
        rules = self.keep_last.load_rules(self._keep_last_rules_path())
        target = str(model_name).casefold().strip()
        for name, minutes in rules.items():
            if str(name).casefold().strip() == target:
                return float(minutes)
        return 0.0

    def queue_keep_last_model(self, model_name: str, minutes: float) -> Dict[str, Any]:
        model_name = str(model_name or "").strip()
        minutes = float(minutes or 0)
        if not model_name:
            raise RuntimeError("Keep Last requires a model name.")
        path = self._keep_last_rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rules = self.keep_last.load_rules(path)
        # Preserve original spellings/order where possible.
        target_key = model_name.casefold()
        updated: Dict[str, float] = {}
        replaced = False
        for name, value in rules.items():
            if str(name).casefold() == target_key:
                if minutes > 0:
                    updated[model_name] = minutes
                replaced = True
            else:
                updated[str(name)] = float(value)
        if not replaced and minutes > 0:
            updated[model_name] = minutes
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        lines = [f"{name}:{value:g}" for name, value in updated.items()]
        tmp.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")
        os.replace(tmp, path)
        if minutes <= 0:
            return {"queued": False, "model": model_name, "minutes": 0, "message": f"Removed Keep Last rule for {model_name}."}
        settings = self._keep_last_settings()
        job = {
            "id": secrets.token_urlsafe(12), "kind": "keep_last_model", "status": "queued",
            "mode": "keep_last", "model": model_name, "rules": {model_name: minutes}, "rules_path": str(path),
            "gap_minutes": float(settings.get("gap_minutes", 30)),
            "recent_write_grace_seconds": int(settings.get("recent_write_grace_seconds", 120)),
            "attempts": 0, "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"Keep Last {minutes:g} minutes queued for {model_name}.", "error": "",
        }
        with self.lock:
            # Replace an older still-waiting single-model Keep Last job for the
            # same model rather than stacking obsolete rules.
            for old in self.action_queue.get("jobs", []):
                if not isinstance(old, dict): continue
                if str(old.get("kind", "")) == "keep_last_model" and str(old.get("model", "")).casefold() == target_key and str(old.get("status", "")) in {"queued", "retrying"}:
                    old["status"] = "cancelled"; old["message"] = "Superseded by a newer Keep Last rule."
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue(); self.action_wakeup.set()
        return {"queued": True, "job_id": job["id"], "model": model_name, "minutes": minutes, "message": job["message"]}

    def _queue_ignore_model_folders(self, logical_model: str, model_names: Sequence[str]) -> Dict[str, Any]:
        wanted = {str(name).casefold().strip() for name in model_names if str(name).strip()}
        wanted.add(str(logical_model).casefold().strip())
        folders: Dict[str, str] = {}
        with self.lock:
            rows = list(self.catalog.get("original", [])) + list(self.catalog.get("review", []))
        for row in rows:
            if not isinstance(row, dict): continue
            name = str(row.get("name", "")).casefold().strip()
            physical = Path(str(row.get("folder", "")))
            physical_base = re.sub(r"_dup\d+$", "", physical.name, flags=re.I).casefold()
            if name not in wanted and physical_base not in wanted:
                continue
            if physical.is_dir(): folders[normalized(physical)] = str(physical)
        # Direct root scan is intentionally shallow and catches folders that may
        # have fallen out of a stale catalog.
        for root in self.roots:
            try:
                for child in root.iterdir():
                    if not child.is_dir(): continue
                    physical_base = re.sub(r"_dup\d+$", "", child.name, flags=re.I).casefold()
                    if physical_base in wanted:
                        folders[normalized(child)] = str(child)
            except OSError:
                continue
        job = {
            "id": secrets.token_urlsafe(12), "kind": "ignore_model_folders", "status": "queued",
            "mode": "ignore", "model": logical_model, "model_names": sorted(wanted),
            "folders": list(folders.values()), "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": f"Queued {len(folders)} recording folder(s) for Ignore → MARKED_FOR_DELETION.", "error": "",
        }
        with self.lock: self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue(); self.action_wakeup.set()
        return job

    def _merge_folder_non_overwriting(self, source: Path, destination: Path, job: Dict[str, Any]) -> Tuple[int, int]:
        moved = 0; moved_bytes = 0
        destination.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        try: files = [p for p in source.rglob("*") if p.is_file()]
        except OSError: files = []
        for path in files:
            if bool(job.get("cancel_requested")): raise TaskCancelled("Ignore-folder move cancelled from the phone.")
            self._yield_durable_job_to_phone(job)
            try: rel = path.relative_to(source)
            except Exception: rel = Path(path.name)
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists(): target = self.mosaic.unique_destination(target)
            try: size = path.stat().st_size
            except OSError: size = 0
            with self.heavy_io_lock:
                actual = self.mosaic.move_file_fast(path, target)
            moved += 1; moved_bytes += size
        # Remove only directories that became empty; never delete residual files.
        try:
            for directory in sorted([p for p in source.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
                try: directory.rmdir()
                except OSError: pass
            source.rmdir()
        except OSError:
            pass
        return moved, moved_bytes

    def _process_ignore_model_folders_job(self, job: Dict[str, Any]) -> str:
        folders = [Path(str(v)) for v in job.get("folders", []) if str(v)]
        deletion_roots = configured_deletion_roots(self.roots)
        moved_folders = 0; merged_files = 0; moved_bytes = 0
        for index, source in enumerate(folders, start=1):
            if bool(job.get("cancel_requested")): raise TaskCancelled("Ignore-folder move cancelled from the phone.")
            if not source.exists():
                continue
            drive = drive_label(source)
            deletion_root = deletion_roots.get(drive)
            if deletion_root is None:
                raise RuntimeError(f"No MARKED_FOR_DELETION root is configured for {source}.")
            destination = deletion_root / source.name
            self._yield_durable_job_to_phone(job)
            # Fast path: same-volume rename of the whole tree.
            if not destination.exists():
                deletion_root.mkdir(parents=True, exist_ok=True)
                try:
                    with self.heavy_io_lock:
                        os.replace(source, destination)
                    moved_folders += 1
                    job["message"] = f"Ignore folders: {index}/{len(folders)} moved • {moved_folders} whole-folder fast move(s)."
                    self._save_action_queue()
                    try: self._refresh_deletion_catalog_folders({destination})
                    except Exception: pass
                    continue
                except OSError:
                    pass
            count, size = self._merge_folder_non_overwriting(source, destination, job)
            merged_files += count; moved_bytes += size
            job["message"] = f"Ignore folders: {index}/{len(folders)} processed • {moved_folders} fast folder(s) • {merged_files} merged file(s)."
            self._save_action_queue()
            try: self._refresh_deletion_catalog_folders({destination})
            except Exception: pass
        try: self.start_catalog_scan()
        except Exception: pass
        return f"Ignore folder routing complete: {moved_folders} whole folder(s) moved instantly; {merged_files} file(s) merged into existing deletion folders."

    def model_admin_payload(self, model_name: str) -> Dict[str, Any]:
        minutes = self.keep_last_minutes_for_model(model_name)
        payload = self.model_admin.model_payload(model_name, minutes)
        payload["admin_status"] = self.model_admin.discovery_payload()
        return payload

    def update_model_admin(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        model = str(payload.get("model", "")).strip()
        action = str(payload.get("action", "")).strip().casefold()
        if not model: raise RuntimeError("Model control requires a model name.")
        if action == "priority": result = self.model_admin.set_priority(model, str(payload.get("tier", "unsorted")))
        elif action == "easy_sort": result = self.model_admin.set_easy_sort(model, bool(payload.get("enabled", False)))
        elif action == "hidden": result = self.model_admin.set_hidden(model, bool(payload.get("enabled", False)))
        elif action == "suspended": result = self.model_admin.set_suspended(model, bool(payload.get("enabled", False)))
        elif action == "mark_later": result = self.model_admin.set_mark_later(model, bool(payload.get("enabled", False)))
        elif action == "force_priority": result = self.model_admin.set_force_priority(model, bool(payload.get("enabled", False)))
        elif action == "keep_last": result = self.queue_keep_last_model(model, float(payload.get("minutes", 0) or 0))
        elif action == "alias": result = self.model_admin.add_alias(model, str(payload.get("spec", "")))
        elif action == "ignore":
            if not bool(payload.get("confirm", False)):
                raise RuntimeError("Ignore requires explicit confirmation.")
            admin_result = self.model_admin.ignore_model(model)
            folder_job = self._queue_ignore_model_folders(model, admin_result.get("model_names", []))
            result = {**admin_result, "folder_job_id": folder_job.get("id"), "message": admin_result.get("message") or f"Removed {model} from live CTBRec and queued its recording folders for MARKED_FOR_DELETION."}
        else: raise RuntimeError("Unknown model control action.")
        return {"ok": True, "result": result, "model": self.model_admin_payload(model)}

    def _process_keep_last_job(self, job: Dict[str, Any]) -> str:
        """Apply KeepLast through the durable queue without blocking phone work."""
        rules = {str(k): float(v) for k, v in (job.get("rules", {}) or {}).items() if float(v) > 0}
        if not rules:
            return "Keep Last had no designated models to process."

        def duration_lookup(path: Path, size: int, mtime: float) -> Optional[float]:
            try:
                value = self.mosaic_duration_cache.get(path, size, mtime)
                return float(value) if value else None
            except Exception:
                return None

        # Planning is stat/timestamp/cache work only; it deliberately does not
        # hold the shared heavy-I/O lock. This keeps video preview/current-model
        # generation responsive while a large Keep Last pass is being planned.
        plan = self.keep_last.build_plan(
            self.roots,
            rules,
            gap_minutes=float(job.get("gap_minutes", 30) or 30),
            recent_write_grace_seconds=float(job.get("recent_write_grace_seconds", 120) or 120),
            duration_lookup=duration_lookup,
        )
        review_moves = list(plan.get("review_moves", []))
        delete_moves = list(plan.get("delete_moves", plan.get("moves", [])))
        operations: List[Dict[str, Any]] = (
            [{**item, "decision": "REVIEW"} for item in review_moves]
            + [{**item, "decision": "DELETE"} for item in delete_moves]
        )
        operations.sort(key=lambda item: (
            str(item.get("model", "")).casefold(),
            str(item.get("start", "")),
            0 if str(item.get("decision")) == "REVIEW" else 1,
            str(item.get("source", "")).casefold(),
        ))
        job["planned_files"] = len(operations)
        job["planned_review_files"] = len(review_moves)
        job["planned_delete_files"] = len(delete_moves)
        job["planned_bytes"] = sum(int(x.get("bytes", 0) or 0) for x in operations)
        job["files"] = operations
        job["plan_stats"] = {
            "rules": int(plan.get("rules", len(rules))),
            "models": int(plan.get("models", 0)),
            "folders": int(plan.get("folders", 0)),
            "protected_recent": int(plan.get("protected_recent", 0)),
            "skipped_no_timestamp": int(plan.get("skipped_no_timestamp", 0)),
            "scan_errors": list(plan.get("errors", []))[:50],
        }
        self._save_action_queue()

        deletion_roots = configured_deletion_roots(self.roots)
        moved_review = 0
        moved_delete = 0
        moved_bytes = 0
        total = len(operations)
        queue_settings = self.config.get("action_queue", {})
        batch_size = max(1, min(512, int(queue_settings.get("batch_size", 64) or 64)))
        cursor = 0

        while cursor < total:
            if bool(job.get("cancel_requested")):
                raise TaskCancelled(
                    f"Keep Last cancelled after moving {moved_review + moved_delete} file(s)."
                )
            self._yield_durable_job_to_phone(job)
            affected_deletion: Set[Path] = set()
            processed = 0
            with self.heavy_io_lock:
                while cursor < total and processed < batch_size:
                    item = operations[cursor]
                    cursor += 1
                    processed += 1
                    source = Path(str(item.get("source", "")))
                    if not source.is_file():
                        item["result"] = "Source no longer exists; skipped."
                        if self._interactive_phone_work_pending():
                            break
                        continue

                    decision = str(item.get("decision", "DELETE"))
                    if decision == "REVIEW":
                        model_folder = Path(str(item.get("folder", source.parent)))
                        destination = model_folder / "Review" / source.name
                    else:
                        drive = drive_label(source)
                        bucket = deletion_roots.get(drive)
                        if bucket is None:
                            raise RuntimeError(f"No MARKED_FOR_DELETION root is configured for {source}.")
                        destination = bucket / safe_model_folder_name(str(item.get("model", source.parent.name))) / source.name

                    destination = self.mosaic.unique_destination(destination)
                    actual = self.mosaic.move_file_fast(source, destination)
                    item["destination"] = str(actual)
                    item["result"] = f"Moved to {actual}"
                    if decision == "REVIEW":
                        moved_review += 1
                    else:
                        moved_delete += 1
                        folder = self._deletion_model_folder_for_path(actual)
                        if folder is not None:
                            affected_deletion.add(folder)
                    moved_bytes += int(item.get("bytes", 0) or 0)

                    # A newly clicked model gets the lane after the current move;
                    # otherwise keep consuming same-drive metadata moves rapidly.
                    if bool(job.get("cancel_requested")) or self._interactive_phone_work_pending():
                        break

            job["processed_files"] = cursor
            job["moved"] = moved_review + moved_delete
            job["moved_review"] = moved_review
            job["moved_delete"] = moved_delete
            job["moved_bytes"] = moved_bytes
            job["message"] = (
                f"Keep Last: {cursor}/{total} processed • "
                f"{moved_review} to Review • {moved_delete} to MARKED_FOR_DELETION."
            )
            self._save_action_queue()
            if affected_deletion:
                try:
                    self._refresh_deletion_catalog_folders(affected_deletion)
                except Exception as exc:
                    append_log(f"Could not live-refresh deletion catalog during Keep Last: {exc}")

        stats = job.get("plan_stats", {})
        protected = int(stats.get("protected_recent", 0)) + int(stats.get("skipped_no_timestamp", 0))
        message = (
            f"Keep Last complete: {moved_review} retained file(s) moved to Review; "
            f"{moved_delete} older file(s) moved to MARKED_FOR_DELETION; "
            f"{human_size(moved_bytes)} processed."
        )
        if protected:
            message += f" {protected} recent/unparseable file(s) were safely left untouched."
        if stats.get("scan_errors"):
            message += f" {len(stats['scan_errors'])} scan warning(s)."
        return message

    def _nsfw_settings(self) -> Dict[str, Any]:
        raw = self.config.get("non_nsfw_cleanup", {})
        merged = dict(DEFAULT_CONFIG["non_nsfw_cleanup"])
        if isinstance(raw, dict):
            merged.update(raw)
        extensions = parse_extensions(self.mosaic_settings.get("extensions", "mp4,ts"))
        merged["extensions"] = sorted(extensions)
        return merged

    def _scheduler_settings(self) -> Dict[str, Any]:
        raw = self.config.get("background_scheduler", {})
        merged = dict(DEFAULT_CONFIG["background_scheduler"])
        if isinstance(raw, dict):
            merged.update(raw)
        priority = str(merged.get("priority", "mosaics_first"))
        merged["priority"] = priority if priority in {"mosaics_first", "cleanup_first"} else "mosaics_first"
        return merged

    def _persist_nsfw_status(self, **updates: Any) -> None:
        with self.lock:
            self.nsfw_status.update(updates)
            self.nsfw_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(NSFW_STATE_PATH, self.nsfw_status)

    def nsfw_status_payload(self) -> Dict[str, Any]:
        with self.lock:
            payload = dict(self.nsfw_status)
            future = self.nsfw_future
        payload["running"] = bool(future is not None and not future.done())
        payload["detector_installed"] = importlib.util.find_spec("nudenet") is not None
        payload["candidate_size"] = human_size(int(payload.get("candidate_bytes", 0) or 0))
        return payload

    def _save_nsfw_index(self) -> None:
        with self.lock:
            self.nsfw_index["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(NSFW_INDEX_PATH, self.nsfw_index)

    def cleanup_catalog_models(self, selected_drives: Set[str]) -> List[Dict[str, Any]]:
        with self.lock:
            rows = [dict(value) for value in self.nsfw_index.get("models", {}).values() if isinstance(value, dict)]
        result: List[Dict[str, Any]] = []
        for row in rows:
            candidate_bytes = int(row.get("candidate_bytes", 0) or 0)
            candidate_files = int(row.get("candidate_files", 0) or 0)
            # Listing is metadata-only. The manifest and every candidate source
            # are validated on tap by quick_load_cleanup_queue; doing thousands
            # of network-drive stats on each tab switch made Cleanup feel frozen.
            if candidate_bytes <= 0 or candidate_files <= 0:
                continue
            drives = {str(value).upper() for value in row.get("drives", []) if str(value)}
            # Cleanup mosaics are prebuilt across all physical folders for the
            # logical model. Never expose a mosaic that contains a drive the
            # phone did not select. Multi-drive models therefore appear only
            # when every contributing drive is included ("All" naturally works).
            if selected_drives and drives and not drives.issubset(selected_drives):
                continue
            result.append({
                "name": str(row.get("model", "")),
                "bytes": candidate_bytes,
                "size": human_size(candidate_bytes),
                "folder_count": len(row.get("folders", [])),
                "drives": sorted(drives),
                "candidate_files": candidate_files,
                "sample_count": int(row.get("sample_count", 0) or 0),
                "explicit_files": int(row.get("explicit_files", 0) or 0),
                "uncertain_files": int(row.get("uncertain_files", 0) or 0),
                "ready_chunks": 1,
            })
        result.sort(key=lambda row: (-int(row.get("bytes", 0)), str(row.get("name", "")).casefold()))
        return result

    def _cleanup_index_entry(self, model_name: str) -> Optional[Dict[str, Any]]:
        key = str(model_name).casefold().strip()
        with self.lock:
            row = self.nsfw_index.get("models", {}).get(key)
            return dict(row) if isinstance(row, dict) else None

    def quick_load_cleanup_queue(self, model_name: str, selected_drives: Set[str]) -> Dict[str, Any]:
        row = self._cleanup_index_entry(model_name)
        if row is None:
            return {"ready": False, "reason": "This model has not finished non-NSFW analysis yet."}
        drives = {str(value).upper() for value in row.get("drives", []) if str(value)}
        if selected_drives and drives and not drives.issubset(selected_drives):
            return {"ready": False, "reason": "This prepared cleanup mosaic spans a drive that is not currently selected. Select all of the model's listed drives or use All."}
        manifest_path = Path(str(row.get("manifest", "")))
        chunk = self.nsfw.manifest_to_chunk(manifest_path)
        if chunk is None:
            return {"ready": False, "reason": "The prepared cleanup mosaic is missing or stale; the PC will rebuild it on the next idle pass."}
        # Validate every source signature before exposing destructive choices.
        for video in chunk.files:
            try:
                stat = video.path.stat()
            except OSError:
                return {"ready": False, "reason": "A candidate recording changed or disappeared; the PC must rescan this model."}
            if int(stat.st_size) != int(video.size) or abs(float(stat.st_mtime) - float(video.mtime)) > 0.001:
                return {"ready": False, "reason": "A candidate recording changed; the PC must rescan this model before cleanup."}
        outputs = [Path(value) for value in chunk.mosaics]
        if not outputs or not all(path.is_file() for path in outputs):
            return {"ready": False, "reason": "The cleanup mosaic images are missing; the PC will regenerate them while idle."}
        queue_id = secrets.token_urlsafe(12)
        queue_data = {
            "id": queue_id,
            "mode": "cleanup",
            "model": model_name,
            "drives": sorted(selected_drives),
            "chunks": [chunk],
            "initial_count": 1,
            "drafts": {},
            "history": [],
            "notes": ["Loaded from the completed non-NSFW background analysis."],
            "created_at": time.time(),
            "busy": False,
            "recu_status": "disabled",
            "recu_error": "",
            "recu_data": None,
            "prefetch_status": {"state": "ready", "ready": 1, "target": 1, "message": "Continuous model cleanup mosaic is ready."},
        }
        with self.lock:
            self.queues[queue_id] = queue_data
        return {"ready": True, "queue_id": queue_id, "current": self.current_payload(queue_id)}

    def _cleanup_reviewed_path(self, source: Path) -> Path:
        return source.parent / "._non_nsfw_cleanup_reviewed.json"

    def _set_cleanup_reviewed(self, item: Dict[str, Any], enabled: bool) -> None:
        source = Path(str(item.get("source", "")))
        if not source.parent.is_dir():
            return
        path = self._cleanup_reviewed_path(source)
        raw = load_json(path, {"version": 1, "files": {}})
        if not isinstance(raw, dict):
            raw = {"version": 1, "files": {}}
        files = raw.setdefault("files", {})
        key = normalized(source)
        if enabled:
            files[key] = {
                "size": int(item.get("bytes", 0) or 0),
                "mtime": float(item.get("mtime", 0.0) or 0.0),
                "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            }
        else:
            files.pop(key, None)
        if files:
            atomic_write_json(path, raw)
        else:
            try:
                path.unlink()
            except OSError:
                pass

    def _enqueue_cleanup_action(self, queue_data: Dict[str, Any], chunk: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("delete_indices", [])
        delete_indices = {int(value) for value in raw if str(value).lstrip("-").isdigit()}
        delete_indices = {value for value in delete_indices if 0 <= value < len(chunk.files)}
        files: List[Dict[str, Any]] = []
        for index, video in enumerate(chunk.files):
            files.append({
                "index": index, "source": str(video.path), "bytes": int(video.size), "mtime": float(video.mtime),
                "decision": "DELETE" if index in delete_indices else "KEEP_CLEANUP",
            })
        source_identity = self._source_identity_from_descriptors(files)
        existing = self._existing_job_for_source_identity(source_identity)
        if existing is not None:
            return existing
        job = {
            "id": secrets.token_urlsafe(12), "kind": "cleanup_moves", "status": "queued",
            "mode": "cleanup", "model": str(queue_data.get("model", "")),
            "chunk_signature": str(chunk.signature), "source_identity": source_identity, "folder": str(chunk.folder), "model_folder": str(chunk.model_folder),
            "files": files, "mosaic_artifacts": [], "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": "Queued non-NSFW cleanup decisions from the phone.", "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
            # Hide the now-reviewed result immediately. The next idle analysis
            # will rediscover only changed/unreviewed candidate files.
            self.nsfw_index.setdefault("models", {}).pop(str(queue_data.get("model", "")).casefold(), None)
        self._save_action_queue(); self._save_nsfw_index(); self.action_wakeup.set()
        return job

    def _nsfw_can_continue(self, ignore_idle: bool = False) -> Tuple[bool, str]:
        if self.nsfw_cancel_requested.is_set():
            return False, "Cancelled from the phone."
        blocker = self._whole_library_blocker_reason()
        if blocker:
            return False, f"Paused briefly for {blocker}; blocker will be rechecked automatically."
        settings = self._nsfw_settings()
        if not ignore_idle and bool(settings.get("pause_when_active", True)):
            idle = computer_idle_seconds()
            required = max(0.0, float(settings.get("idle_minutes", 10)) * 60.0)
            if idle is not None and idle < required:
                return False, f"Waiting for {max(0, int(required - idle))} more idle seconds."
        return True, ""

    def _run_nsfw_model_worker(self, model: str, folders: Sequence[Path], source_bytes: int, drives: Sequence[str], ignore_idle: bool) -> Optional[Dict[str, Any]]:
        ffmpeg, ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
        if ffmpeg is None:
            raise RuntimeError("ffmpeg was not found for non-NSFW analysis.")
        with tempfile.TemporaryDirectory(prefix="ctbrec_nsfw_parent_") as temp_raw:
            temp = Path(temp_raw)
            request_path = temp / "request.json"; result_path = temp / "result.json"; status_path = temp / "status.json"
            request = {
                "model": model, "folders": [str(path) for path in folders],
                "settings": self._nsfw_settings(), "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe or ""),
            }
            atomic_write_json(request_path, request)
            command = [sys.executable, str(NSFW_CLEANUP_SCRIPT), "--request", str(request_path), "--result", str(result_path), "--status", str(status_path)]
            creationflags = 0
            if os.name == "nt":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            env = dict(os.environ)
            env.update({"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_WAIT_POLICY": "PASSIVE"})
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creationflags, env=env)
            with self.lock:
                self.nsfw_process = process
            stderr_tail = b""
            try:
                while process.poll() is None:
                    allowed, reason = self._nsfw_can_continue(ignore_idle=ignore_idle)
                    if not allowed:
                        try:
                            process.terminate()
                            process.wait(timeout=6)
                        except Exception:
                            try: process.kill()
                            except Exception: pass
                        self._persist_nsfw_status(
                            state=("cancelled" if str(reason).casefold().startswith("cancel") else "paused"),
                            message=reason,
                            current_model=model,
                            **({"completed_at": datetime.now().isoformat(timespec="seconds")} if str(reason).casefold().startswith("cancel") else {}),
                        )
                        return None
                    status = load_json(status_path, {})
                    if isinstance(status, dict) and status.get("message"):
                        self._persist_nsfw_status(
                            state="running", current_model=model, message=str(status.get("message", "")),
                            current=int(status.get("current", 0) or 0), total=int(status.get("total", 0) or 0),
                            phase=str(status.get("phase", "scan")),
                        )
                    time.sleep(0.45)
                try:
                    _out, stderr_tail = process.communicate(timeout=3)
                except Exception:
                    pass
            finally:
                with self.lock:
                    if self.nsfw_process is process:
                        self.nsfw_process = None
            raw = load_json(result_path, {})
            if process.returncode != 0 or not isinstance(raw, dict) or not raw.get("ok"):
                detail = str(raw.get("error", "")) if isinstance(raw, dict) else ""
                if not detail:
                    detail = stderr_tail.decode("utf-8", "replace")[-1800:] or f"worker exited {process.returncode}"
                raise RuntimeError(detail)
            result = dict(raw.get("result", {}))
            result["source_bytes"] = int(source_bytes)
            result["source_size"] = human_size(source_bytes)
            result["drives"] = list(drives)
            return result

    def _run_nsfw_generation(self, ignore_idle: bool = False) -> None:
        """Run one largest-first cleanup pass in a single low-priority child.

        NudeNet is loaded once per full pass, not once per model. The child
        atomically emits rolling results after every model, so completed model
        mosaics become selectable immediately and survive a pause/termination.
        """
        catalog = self.catalog_models("original", set())
        catalog.sort(key=lambda row: (-int(row.get("bytes", 0)), str(row.get("name", "")).casefold()))
        ffmpeg, ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
        if ffmpeg is None:
            raise RuntimeError("ffmpeg was not found for non-NSFW analysis.")
        plan: List[Dict[str, Any]] = []
        for row in catalog:
            model = str(row.get("name", ""))
            folders = self.model_folders("original", model, set())
            if not folders:
                continue
            plan.append({
                "model": model, "folders": [str(path) for path in folders],
                "source_bytes": int(row.get("bytes", 0) or 0), "drives": list(row.get("drives", [])),
            })
        self._persist_nsfw_status(
            state="running", message=f"Scanning {len(plan):,} models for non-explicit recordings, largest first.",
            total_models=len(plan), model_index=0, models_scanned=0, errors=0, candidate_bytes=0,
        )
        if not plan:
            self._persist_nsfw_status(
                state="complete", message="Non-NSFW pass complete: no recording models were found.",
                total_models=0, models_scanned=0, errors=0, candidate_bytes=0,
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return
        with tempfile.TemporaryDirectory(prefix="ctbrec_nsfw_pass_") as temp_raw:
            temp = Path(temp_raw)
            request_path = temp / "request.json"; result_path = temp / "result.json"
            status_path = temp / "status.json"; rolling_path = temp / "rolling.json"
            atomic_write_json(request_path, {
                "models": plan, "settings": self._nsfw_settings(),
                "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe or ""),
            })
            command = [
                sys.executable, str(NSFW_CLEANUP_SCRIPT), "--request", str(request_path),
                "--result", str(result_path), "--status", str(status_path), "--rolling", str(rolling_path),
            ]
            creationflags = 0
            if os.name == "nt":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            env = dict(os.environ)
            env.update({
                "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1", "OMP_WAIT_POLICY": "PASSIVE",
            })
            # Serialize all FFmpeg/disk-heavy background work behind the same
            # lock used by mobile actions and mosaic generation. Interactive
            # demand sets an event; this monitor then terminates the child and
            # releases the lock within roughly half a second.
            self.heavy_io_lock.acquire()
            try:
                process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creationflags, env=env,
                )
            except Exception:
                self.heavy_io_lock.release()
                raise
            with self.lock:
                self.nsfw_process = process
            consumed_models: Set[str] = set()
            seen_errors: Set[str] = set()
            stderr_tail = b""

            def consume_rolling() -> Tuple[int, int, int]:
                rolling = load_json(rolling_path, {})
                if not isinstance(rolling, dict):
                    return len(consumed_models), len(seen_errors), 0
                changed = False
                for result in rolling.get("results", []):
                    if not isinstance(result, dict):
                        continue
                    model = str(result.get("model", "")).strip()
                    if not model or model.casefold() in consumed_models:
                        continue
                    consumed_models.add(model.casefold())
                    with self.lock:
                        self.nsfw_index.setdefault("models", {})[model.casefold()] = dict(result)
                    changed = True
                for error in rolling.get("errors", []):
                    if not isinstance(error, dict):
                        continue
                    key = f"{error.get('model','')}|{error.get('error','')}"
                    if key not in seen_errors:
                        seen_errors.add(key)
                        append_log(f"Non-NSFW cleanup worker skipped {error.get('model','')}: {error.get('error','')}")
                if changed:
                    self._save_nsfw_index()
                with self.lock:
                    candidate = sum(
                        int(value.get("candidate_bytes", 0) or 0)
                        for value in self.nsfw_index.get("models", {}).values() if isinstance(value, dict)
                    )
                return len(consumed_models), len(seen_errors), candidate

            try:
                while process.poll() is None:
                    scanned, errors, candidate_bytes = consume_rolling()
                    allowed, reason = self._nsfw_can_continue(ignore_idle=ignore_idle)
                    if not allowed:
                        try:
                            process.terminate(); process.wait(timeout=8)
                        except Exception:
                            try: process.kill()
                            except Exception: pass
                        consume_rolling()
                        self._persist_nsfw_status(
                            state=("cancelled" if str(reason).casefold().startswith("cancel") else "paused"),
                            message=reason, models_scanned=scanned, errors=errors,
                            candidate_bytes=candidate_bytes,
                            **({"completed_at": datetime.now().isoformat(timespec="seconds")} if str(reason).casefold().startswith("cancel") else {}),
                        )
                        return
                    status = load_json(status_path, {})
                    if isinstance(status, dict) and status.get("message"):
                        self._persist_nsfw_status(
                            state="running", current_model=str(status.get("model", self.nsfw_status.get("current_model", ""))),
                            model_index=int(status.get("model_index", self.nsfw_status.get("model_index", 0)) or 0),
                            total_models=len(plan), message=str(status.get("message", "")),
                            current=int(status.get("current", 0) or 0), total=int(status.get("total", 0) or 0),
                            phase=str(status.get("phase", "scan")), models_scanned=scanned, errors=errors,
                            candidate_bytes=candidate_bytes,
                        )
                    time.sleep(0.45)
                try:
                    _out, stderr_tail = process.communicate(timeout=4)
                except Exception:
                    pass
            finally:
                with self.lock:
                    if self.nsfw_process is process:
                        self.nsfw_process = None
                self.heavy_io_lock.release()
            scanned, errors, candidate_bytes = consume_rolling()
            raw = load_json(result_path, {})
            if process.returncode != 0 or not isinstance(raw, dict) or not raw.get("ok"):
                detail = str(raw.get("error", "")) if isinstance(raw, dict) else ""
                if not detail:
                    detail = stderr_tail.decode("utf-8", "replace")[-1800:] or f"worker exited {process.returncode}"
                raise RuntimeError(detail)
            # A successful complete pass is authoritative for which logical
            # models still exist. Remove stale index rows for models no longer
            # in the Originals catalog; paused passes deliberately keep them.
            plan_keys = {str(row.get("model", "")).casefold() for row in plan if str(row.get("model", ""))}
            with self.lock:
                models_index = self.nsfw_index.setdefault("models", {})
                for stale_key in [key for key in models_index if key not in plan_keys]:
                    models_index.pop(stale_key, None)
            self._save_nsfw_index()
            self._persist_nsfw_status(
                state="complete",
                message=f"Non-NSFW pass complete: scanned {scanned:,} models; candidates {human_size(candidate_bytes)}; errors {errors:,}.",
                models_scanned=scanned, candidate_bytes=candidate_bytes, errors=errors, total_models=len(plan),
                model_index=len(plan), completed_at=datetime.now().isoformat(timespec="seconds"),
            )

    def _catalog_scan_actively_running(self) -> bool:
        """Return True only when catalog_status=scanning is backed by a live task.

        Older builds could leave the status flag at "scanning" after a task had
        already died/completed. This self-heals that state so heavy workers do
        not wait forever behind a phantom disk scan.
        """
        with self.lock:
            active = any(
                record.label == "Scan disks" and record.status in {"queued", "running"}
                for record in self.tasks.values()
            )
            status = str(self.catalog_status.get("status", ""))
            has_catalog = bool(self.catalog.get("original") or self.catalog.get("review") or self.catalog.get("deletion"))
            if status == "scanning" and not active:
                self.catalog_status = {
                    **self.catalog_status,
                    "status": "ready" if has_catalog else "unknown",
                    "progress": (
                        "Recovered a stale scan status; using the last completed disk catalog."
                        if has_catalog else "No active disk scan is running."
                    ),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                return False
            return status == "scanning" and active

    def schedule_nsfw_generation(self, force: bool = False) -> bool:
        settings = self._nsfw_settings()
        if not bool(settings.get("enabled", True)) and not force:
            return False
        if importlib.util.find_spec("nudenet") is None:
            self._persist_nsfw_status(state="needs_install", message="NudeNet is not installed. Run install_mobile_reviewer.bat and restart the server.")
            return False
        with self.lock:
            if self.nsfw_future is not None and not self.nsfw_future.done():
                return False
            if self.library_future is not None and not self.library_future.done():
                return False
            status_snapshot = dict(self.nsfw_status); catalog_snapshot = dict(self.catalog_status)
        if catalog_snapshot.get("status") == "scanning" and self._catalog_scan_actively_running():
            with self.lock:
                progress_text = str(self.catalog_status.get("progress", "Disk-size scan is running…"))
            self._persist_nsfw_status(state="waiting", message=f"Waiting for active disk-size scan: {progress_text}")
            return False
        if not force and status_snapshot.get("state") == "complete":
            try:
                completed = datetime.fromisoformat(str(status_snapshot.get("completed_at", "")))
                interval = max(0.0, float(settings.get("rescan_minutes", 240))) * 60.0
                if (datetime.now() - completed).total_seconds() < interval:
                    return False
            except Exception:
                pass
        if not force:
            allowed, reason = self._nsfw_can_continue(ignore_idle=False)
            if not allowed:
                self._persist_nsfw_status(state="waiting", message=reason)
                return False
        self.nsfw_cancel_requested.clear()
        def run() -> None:
            try:
                self._run_nsfw_generation(ignore_idle=force)
            except Exception as exc:
                append_log(f"Non-NSFW cleanup pass crashed: {exc}\n{traceback.format_exc()}")
                self._persist_nsfw_status(state="error", message=str(exc))
        future = self.library_executor.submit(run)
        with self.lock:
            self.nsfw_future = future
        return True

    def force_nsfw_generation(self) -> Dict[str, Any]:
        self.nsfw_force_requested.set()
        started = self.schedule_nsfw_generation(force=True)
        if started:
            message = "Non-NSFW analysis started."
        else:
            with self.lock:
                catalog = dict(self.catalog_status)
                library_running = bool(self.library_future is not None and not self.library_future.done())
            if catalog.get("status") == "scanning":
                message = f"Non-NSFW pass queued behind disk-size scan: {catalog.get('progress', 'scanning…')}"
                self._persist_nsfw_status(state="queued", message=message)
            elif library_running:
                message = "Non-NSFW pass queued behind the current whole-library mosaic pass."
                self._persist_nsfw_status(state="queued", message=message)
            else:
                message = "Non-NSFW pass queued and will start as soon as the heavy-work slot is available."
                self._persist_nsfw_status(state="queued", message=message)
        return {"started": started, "queued": not started, "message": message}

    def _background_pass_due(self, kind: str) -> bool:
        if kind == "mosaics":
            settings = self._library_settings(); status = self.library_status
            if not bool(settings.get("enabled", True)): return False
            minutes = float(settings.get("rescan_minutes", 120))
        else:
            settings = self._nsfw_settings(); status = self.nsfw_status
            if not bool(settings.get("enabled", True)): return False
            minutes = float(settings.get("rescan_minutes", 240))
        state = str(status.get("state", "")).casefold()
        # Whole-library mosaics are designed to run unattended. If the worker
        # itself crashes outside the per-model guards, retry the persisted cursor
        # after a bounded backoff instead of remaining dead forever. Non-NSFW
        # errors remain manual because detector/config failures are usually
        # systemic rather than one bad recording.
        if state == "error":
            if kind != "mosaics":
                return False
            retry_minutes = max(1.0, float(settings.get("error_retry_minutes", 5) or 5))
            try:
                updated = datetime.fromisoformat(str(status.get("updated_at", "")))
                return (datetime.now() - updated).total_seconds() >= retry_minutes * 60.0
            except Exception:
                return True
        if state not in {"complete", "cancelled"}: return True
        try:
            completed = datetime.fromisoformat(str(status.get("completed_at", "")))
            return (datetime.now() - completed).total_seconds() >= max(0.0, minutes) * 60.0
        except Exception:
            # A deliberate cancellation without a timestamp should stay stopped
            # until the user requests another pass or the server is restarted.
            return state != "cancelled"

    def _schedule_prioritized_background(self) -> None:
        with self.lock:
            heavy_running = (
                (self.library_future is not None and not self.library_future.done())
                or (self.nsfw_future is not None and not self.nsfw_future.done())
            )
        if heavy_running:
            return
        priority = self._scheduler_settings().get("priority", "mosaics_first")
        first, second = ("mosaics", "cleanup") if priority == "mosaics_first" else ("cleanup", "mosaics")
        if self._background_pass_due(first):
            if first == "mosaics": self.schedule_library_generation(force=False)
            else: self.schedule_nsfw_generation(force=False)
            return
        if self._background_pass_due(second):
            if second == "mosaics": self.schedule_library_generation(force=False)
            else: self.schedule_nsfw_generation(force=False)

    def _library_settings(self) -> Dict[str, Any]:
        raw = self.config.get("library_background", {})
        return dict(raw) if isinstance(raw, dict) else {}

    def _persist_library_status(self, **updates: Any) -> None:
        with self.lock:
            self.library_status.update(updates)
            self.library_status["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(LIBRARY_STATE_PATH, self.library_status)

    def library_status_payload(self) -> Dict[str, Any]:
        with self.lock:
            payload = dict(self.library_status)
            future = self.library_future
        payload["running"] = bool(future is not None and not future.done())
        return payload

    def _prune_finished_scheduler_futures(self) -> None:
        """Drop completed futures so stale queue bookkeeping cannot become a blocker."""
        with self.lock:
            for mapping in (self.interactive_futures, self.prefetch_futures):
                for key, future in list(mapping.items()):
                    if future is None or future.done():
                        mapping.pop(key, None)

    def _interactive_phone_work_pending(self) -> bool:
        """True only for *live* phone-requested mosaic work.

        v2.11.2 treated prefetch futures for every queue ever opened in this
        server process as interactive forever. Maintenance also kept rescheduling
        those old queues, which could starve the 4,000+ model library pass even
        when the phone had been untouched for hours. The sorting heartbeat lease
        and currently visible queue are now authoritative.
        """
        self._prune_finished_scheduler_futures()
        active_session = self.sort_session_active()
        with self.lock:
            active_queue = str(self.active_sort_queue_id or "") if active_session else ""
            if active_queue:
                prefix = active_queue + ":"
                if any(
                    future is not None and not future.done() and str(key).startswith(prefix)
                    for key, future in self.interactive_futures.items()
                ):
                    return True
                future = self.prefetch_futures.get(active_queue)
                if future is not None and not future.done():
                    return True

            # Priority tasks may exist for older clicked models. Only treat one
            # as a scheduler blocker while the phone still has a live sorting
            # lease; otherwise whole-library generation must keep moving.
            if active_session:
                return any(
                    bool(getattr(record, "interactive_priority", False))
                    and record.status in {"queued", "running"}
                    and (time.time() - float(record.updated_at or record.created_at or 0.0)) < 900.0
                    for record in self.tasks.values()
                )
            return False

    def _durable_action_work_pending(self) -> bool:
        """True when a real user instruction is eligible/running on the PC queue.

        Idle-only frame cuts do not pause background work merely because they
        exist. The moment the PC satisfies their idle requirement they become a
        real blocker and therefore outrank whole-library mosaics/NSFW scanning.
        """
        now = time.time()
        with self.lock:
            jobs = [job for job in self.action_queue.get("jobs", []) if isinstance(job, dict)]
        for job in jobs:
            status = str(job.get("status", ""))
            if status == "running":
                if str(job.get("kind", "")) == "review_frame_cuts":
                    return True
                if int(job.get("attempts", 0) or 0) == 0 or str(job.get("kind", "")) == "undo_chunk":
                    return True
            if status == "queued":
                ready, _reason = self._action_job_ready(job, now)
                if ready:
                    return True
            if status == "retrying" and str(job.get("kind", "")) == "review_frame_cuts":
                ready, _reason = self._action_job_ready(job, now)
                if ready:
                    return True
        return False

    def _yield_durable_job_to_phone(self, job: Dict[str, Any]) -> None:
        """Let requested model-mosaic work take the heavy-I/O lane first.

        Merely having a sorting screen open is no longer enough to park the
        durable queue. The queue may catch up while the user is reviewing an
        already-ready JPEG; it yields as soon as a requested/missing mosaic needs
        the lane.
        """
        while self._interactive_phone_work_pending():
            if bool(job.get("cancel_requested")):
                raise TaskCancelled("Queued action cancelled from the phone.")
            time.sleep(0.05)

    def _priority_work_waiting(self) -> bool:
        """Immediate phone mosaic demand that may pause open-model prefetch work."""
        return bool(self.interactive_demand.is_set())

    def _whole_library_blocker_reason(self) -> str:
        """Return a precise, freshly revalidated blocker or an empty string.

        This intentionally re-reads both scheduler futures and the durable PC
        queue each time. A stale Event/status string can therefore never park the
        library pass by itself.
        """
        if self._interactive_phone_work_pending():
            with self.lock:
                queue_id = str(self.active_sort_queue_id or "")
                queue_data = self.queues.get(queue_id) if queue_id else None
                model = str(queue_data.get("model", "")).strip() if isinstance(queue_data, dict) else ""
            return f"requested phone mosaic work{f' for {model}' if model else ''}"
        if self._durable_action_work_pending():
            now = time.time()
            with self.lock:
                for job in self.action_queue.get("jobs", []):
                    if not isinstance(job, dict):
                        continue
                    status = str(job.get("status", ""))
                    eligible = status == "running"
                    if status in {"queued", "retrying"}:
                        eligible = self._action_job_ready(job, now)[0]
                    if eligible:
                        model = str(job.get("model", "")).strip()
                        detail = " idle frame-cut" if str(job.get("kind", "")) == "review_frame_cuts" else ""
                        return f"PC queue {status}{detail} instruction{f' for {model}' if model else ''}"
            return "PC queue instruction"
        return ""

    def _whole_library_work_should_yield(self) -> bool:
        """Whole-library mosaic/NSFW passes sit below *real* live user work."""
        return bool(self._whole_library_blocker_reason())

    def _mosaic_work_preempts_actions(self) -> bool:
        """Only model/offline mosaic preparation outranks durable file instructions.

        Whole-library mosaic and Non-NSFW passes are below queued moves/deletions.
        """
        return self._interactive_phone_work_pending()

    def _library_can_continue(self, ignore_idle: bool = False) -> Tuple[bool, str]:
        if self.library_cancel_requested.is_set():
            return False, "Cancelled from the phone."
        blocker = self._whole_library_blocker_reason()
        if blocker:
            return False, f"Paused briefly for {blocker}; blocker will be rechecked automatically."
        settings = self._library_settings()
        if not ignore_idle and bool(settings.get("pause_when_active", True)):
            idle = computer_idle_seconds()
            required = max(0.0, float(settings.get("idle_minutes", 10)) * 60.0)
            if idle is not None and idle < required:
                return False, f"Waiting for {max(0, int(required - idle))} more idle seconds."
        return True, ""

    def _mosaic_lock_for(self, mode: str, signature: str) -> threading.RLock:
        key = f"{mode}:{signature}"
        with self.lock:
            return self.mosaic_locks.setdefault(key, threading.RLock())

    def _build_chunks_for_library_model(
        self,
        mode: str,
        model_name: str,
        folders: Sequence[Path],
        progress: Callable[[str], None],
    ) -> List[Any]:
        if mode == "original":
            chunks, _notes = self.mosaic.build_model_queue(
                folders,
                dict(self.mosaic_settings),
                self.mosaic_duration_cache,
                self.mosaic_manifest,
                include_skipped=False,
                progress=progress,
                session_generated_signatures=self.session_generated_signatures,
                cancel=threading.Event(),
                probe_actual_durations=False,
            )
            chunks = list(chunks)
            self._hydrate_chunks_from_ready_snapshot(mode, model_name, chunks)
            return chunks
        chunks = list(self.review.build_chunks(
            folders,
            dict(self.review_settings),
            self.review_duration_cache,
            progress,
            probe_actual_durations=False,
        ))
        self._hydrate_chunks_from_ready_snapshot(mode, model_name, chunks)
        return chunks

    def _load_persisted_library_plan(self) -> Optional[Dict[str, Any]]:
        raw = load_json(LIBRARY_PLAN_PATH, {})
        if not isinstance(raw, dict) or int(raw.get("version", 0) or 0) != 1:
            return None
        rows = raw.get("plan")
        if not isinstance(rows, list) or not rows:
            return None
        clean = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mode = str(row.get("mode", ""))
            name = str(row.get("name", "")).strip()
            if mode not in {"original", "review"} or not name:
                continue
            clean.append({
                "mode": mode, "name": name,
                "bytes": int(row.get("bytes", 0) or 0),
                "drives": list(row.get("drives", [])) if isinstance(row.get("drives", []), list) else [],
            })
        if not clean:
            return None
        raw["plan"] = clean
        return raw

    def _persist_library_plan(self, plan: Sequence[Dict[str, Any]], fingerprint: str) -> None:
        atomic_write_json(LIBRARY_PLAN_PATH, {
            "version": 1,
            "fingerprint": str(fingerprint),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "plan": [
                {"mode": str(row.get("mode", "")), "name": str(row.get("name", "")),
                 "bytes": int(row.get("bytes", 0) or 0),
                 "drives": list(row.get("drives", [])) if isinstance(row.get("drives", []), (list, tuple, set)) else []}
                for row in plan
            ],
        })

    def _clear_library_plan(self) -> None:
        try:
            LIBRARY_PLAN_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    def _library_plan_fingerprint(self, plan: Sequence[Dict[str, Any]]) -> str:
        """Stable identity for one largest-first traversal.

        The resume cursor is keyed only by the durable ordered model identity.
        Mutable byte totals and drive-size metadata must never invalidate an
        in-progress traversal; new sizes/models are incorporated next pass.
        """
        payload = [
            {"mode": str(row.get("mode", "")), "name": str(row.get("name", ""))}
            for row in plan
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()

    def _library_resume_cursor(self, plan_fingerprint: str, plan_length: int) -> Tuple[int, int, bool]:
        with self.lock:
            state = dict(self.library_status)
        same_plan = str(state.get("pass_fingerprint", "")) == str(plan_fingerprint)
        state_name = str(state.get("state", "")).casefold()
        resumable = same_plan and state_name in {"paused", "waiting", "running", "queued", "error"}
        if not resumable:
            return 0, 0, False
        model_index = max(0, min(plan_length, int(state.get("resume_model_index", 0) or 0)))
        chunk_index = max(0, int(state.get("resume_chunk_index", 0) or 0))
        return model_index, chunk_index, True

    def _run_library_generation(self, ignore_idle: bool = False) -> None:
        settings = self._library_settings()
        # A pass is a durable traversal snapshot.  Do NOT rebuild/reorder it just
        # because a later disk scan notices that active recordings changed size.
        # v2.14.3 fingerprinted mutable byte totals, so any catalog refresh after
        # a pause/restart could reset a 4,000-model pass back to model 1.
        persisted = self._load_persisted_library_plan()
        with self.lock:
            prior_state_name = str(self.library_status.get("state", "")).casefold()
        if persisted is not None and prior_state_name in {"paused", "waiting", "running", "queued", "error"}:
            plan = list(persisted["plan"])
            plan_fingerprint = str(persisted.get("fingerprint", "")) or self._library_plan_fingerprint(plan)
        else:
            plan: List[Dict[str, Any]] = []
            if bool(settings.get("original_enabled", True)):
                plan.extend({"mode": "original", **row} for row in self.catalog_models("original", set()))
            if bool(settings.get("review_enabled", True)):
                plan.extend({"mode": "review", **row} for row in self.catalog_models("review", set()))
            plan.sort(key=lambda row: (-int(row.get("bytes", 0)), str(row.get("name", "")).casefold(), str(row.get("mode", ""))))
            plan_fingerprint = self._library_plan_fingerprint(plan)
            if plan:
                self._persist_library_plan(plan, plan_fingerprint)
        start_model, start_chunk, resumed = self._library_resume_cursor(plan_fingerprint, len(plan))
        with self.lock:
            prior = dict(self.library_status)
        generated = int(prior.get("generated", 0) or 0) if resumed else 0
        reused = int(prior.get("reused", 0) or 0) if resumed else 0
        errors = int(prior.get("errors", 0) or 0) if resumed else 0
        skipped_models = int(prior.get("skipped_models", 0) or 0) if resumed else 0
        if start_model >= len(plan) and plan:
            start_model, start_chunk, resumed = 0, 0, False
            generated = reused = errors = skipped_models = 0
        self._persist_library_status(
            state="running",
            message=(
                f"Resuming largest-first mosaic pass at model {start_model + 1:,}/{len(plan):,}."
                if resumed and plan else
                f"Preparing mosaics for {len(plan):,} model queues, largest first."
            ),
            total_models=len(plan), model_index=start_model,
            generated=generated, reused=reused, errors=errors, skipped_models=skipped_models,
            **({"last_skipped_model": "", "last_skipped_error": ""} if not resumed else {}),
            pass_fingerprint=plan_fingerprint,
            resume_model_index=start_model,
            resume_chunk_index=start_chunk,
            resumed=bool(resumed),
        )
        for zero_model_index in range(start_model, len(plan)):
            row = plan[zero_model_index]
            model_index = zero_model_index + 1
            allowed, reason = self._library_can_continue(ignore_idle=ignore_idle)
            if not allowed:
                self._persist_library_status(
                    state=("cancelled" if str(reason).casefold().startswith("cancel") else "paused"),
                    message=reason,
                    model_index=zero_model_index,
                    resume_model_index=zero_model_index,
                    resume_chunk_index=(start_chunk if zero_model_index == start_model else 0),
                    **({"completed_at": datetime.now().isoformat(timespec="seconds")} if str(reason).casefold().startswith("cancel") else {}),
                )
                return
            mode = str(row["mode"])
            model = str(row["name"])
            append_log(f"Whole-library model {model_index}/{len(plan)}: {model} ({mode}) {row.get('size', '')}")
            folders = self.model_folders(mode, model, set())
            if not folders:
                self._persist_library_status(resume_model_index=model_index, resume_chunk_index=0)
                continue
            model_resume_chunk = start_chunk if zero_model_index == start_model else 0
            self._persist_library_status(
                state="running",
                current_mode=mode,
                current_model=model,
                model_index=model_index,
                total_models=len(plan),
                resume_model_index=zero_model_index,
                resume_chunk_index=model_resume_chunk,
                message=f"{model_index}/{len(plan)}: checking existing mosaics for {model} ({mode})…",
            )
            recovered_snapshot = self._recover_complete_snapshot_from_existing_mosaics(mode, model, set())
            if isinstance(recovered_snapshot, dict):
                recovered_chunks: List[Any] = []
                pending = self._pending_chunk_signatures()
                for saved in recovered_snapshot.get("chunks", []):
                    if not isinstance(saved, dict) or str(saved.get("signature", "")) in pending:
                        continue
                    try:
                        recovered_chunk = self._deserialize_chunk(mode, saved)
                    except Exception:
                        continue
                    if self._chunk_is_claimed(recovered_chunk):
                        continue
                    recovered_chunks.append(recovered_chunk)
                if recovered_chunks:
                    # On a resumed pass this model may already have been partly
                    # counted before the pause. Do not double-count the skipped
                    # prefix; count only from the persisted cursor onward.
                    newly_reused = max(0, len(recovered_chunks) - model_resume_chunk)
                    reused += newly_reused
                    self._persist_ready_snapshot(
                        mode, model, recovered_chunks,
                        int(row.get("bytes", 0) or 0), row.get("drives", []),
                    )
                    self._persist_library_status(
                        reused=reused,
                        chunk_index=len(recovered_chunks),
                        total_chunks=len(recovered_chunks),
                        resume_model_index=model_index,
                        resume_chunk_index=0,
                        message=f"{model}: all {len(recovered_chunks)} existing mosaic chunk(s) recovered instantly; no preparation needed.",
                    )
                    continue
            self._persist_library_status(
                state="running",
                current_mode=mode,
                current_model=model,
                model_index=model_index,
                total_models=len(plan),
                resume_model_index=zero_model_index,
                resume_chunk_index=model_resume_chunk,
                message=f"{model_index}/{len(plan)}: building missing/current queue metadata for {model}…",
            )
            max_attempts = max(1, min(8, int(settings.get("mosaic_retry_attempts", 3) or 3)))
            retry_delay = max(0.0, min(60.0, float(settings.get("mosaic_retry_delay_seconds", 2) or 0)))
            chunks: Optional[List[Any]] = None
            build_error = ""
            for build_attempt in range(1, max_attempts + 1):
                allowed_build, build_reason = self._library_can_continue(ignore_idle=ignore_idle)
                if not allowed_build:
                    self._persist_library_status(
                        state=("cancelled" if str(build_reason).casefold().startswith("cancel") else "paused"),
                        message=build_reason,
                        current_model=model, current_mode=mode, model_index=model_index,
                        resume_model_index=zero_model_index, resume_chunk_index=model_resume_chunk,
                    )
                    return
                try:
                    built = self._build_chunks_for_library_model(
                        mode,
                        model,
                        folders,
                        lambda message, mi=model_index, ba=build_attempt: self._persist_library_status(
                            state="running",
                            current_mode=mode,
                            current_model=model,
                            model_index=mi,
                            total_models=len(plan),
                            model_attempt=ba,
                            model_attempt_limit=max_attempts,
                            message=str(message),
                        ),
                    )
                    chunks = sorted(list(built), key=lambda chunk: (-int(getattr(chunk, "source_bytes", 0)), getattr(chunk, "start", datetime.min)))
                    break
                except Exception as exc:
                    build_error = str(exc)
                    append_log(
                        f"Whole-library queue build attempt {build_attempt}/{max_attempts} failed for {mode} {model}: {exc}\n"
                        + traceback.format_exc()
                    )
                    if build_attempt < max_attempts and retry_delay > 0:
                        self._persist_library_status(
                            state="running",
                            message=f"{model}: metadata build attempt {build_attempt}/{max_attempts} failed; retrying in {retry_delay:g}s. {exc}",
                        )
                        deadline = time.time() + retry_delay
                        while time.time() < deadline:
                            allowed_retry, _ = self._library_can_continue(ignore_idle=ignore_idle)
                            if not allowed_retry:
                                break
                            time.sleep(min(0.25, max(0.0, deadline - time.time())))
            if chunks is None:
                errors += 1
                skipped_models += 1
                append_log(f"Whole-library skipped model after metadata-build retries: {mode} {model}: {build_error}")
                self._persist_library_status(
                    errors=errors, skipped_models=skipped_models,
                    last_skipped_model=model, last_skipped_error=build_error,
                    message=f"Skipped {model} after {max_attempts} failed metadata-build attempts; continuing with the next model. {build_error}",
                    resume_model_index=model_index, resume_chunk_index=0, model_attempt=0,
                )
                continue
            queue_data = {
                "id": f"library-{mode}-{model.casefold()}",
                "mode": mode,
                "model": model,
                "chunks": chunks,
            }
            self._persist_ready_snapshot(mode, model, chunks, int(row.get("bytes", 0) or 0), row.get("drives", []))
            first_chunk_index = max(0, min(len(chunks), model_resume_chunk))
            for zero_chunk_index in range(first_chunk_index, len(chunks)):
                chunk = chunks[zero_chunk_index]
                chunk_index = zero_chunk_index + 1
                allowed, reason = self._library_can_continue(ignore_idle=ignore_idle)
                if not allowed:
                    self._persist_library_status(
                        state=("cancelled" if str(reason).casefold().startswith("cancel") else "paused"),
                        message=reason,
                        current_model=model,
                        current_mode=mode,
                        model_index=model_index,
                        chunk_index=zero_chunk_index,
                        total_chunks=len(chunks),
                        resume_model_index=zero_model_index,
                        resume_chunk_index=zero_chunk_index,
                        **({"completed_at": datetime.now().isoformat(timespec="seconds")} if str(reason).casefold().startswith("cancel") else {}),
                    )
                    return
                if self._chunk_mosaic_ready(queue_data, chunk):
                    reused += 1
                    self._persist_ready_snapshot(mode, model, chunks, int(row.get("bytes", 0) or 0), row.get("drives", []))
                    self._persist_library_status(
                        reused=reused, chunk_index=chunk_index, total_chunks=len(chunks),
                        resume_model_index=zero_model_index,
                        resume_chunk_index=chunk_index,
                    )
                    continue
                max_attempts = max(1, min(8, int(settings.get("mosaic_retry_attempts", 3) or 3)))
                retry_delay = max(0.0, min(60.0, float(settings.get("mosaic_retry_delay_seconds", 2) or 0)))
                mosaic_ok = False
                last_error = ""
                for attempt in range(1, max_attempts + 1):
                    allowed_attempt, attempt_reason = self._library_can_continue(ignore_idle=ignore_idle)
                    if not allowed_attempt:
                        self._persist_library_status(
                            state=("cancelled" if str(attempt_reason).casefold().startswith("cancel") else "paused"),
                            message=attempt_reason,
                            current_model=model,
                            current_mode=mode,
                            model_index=model_index,
                            chunk_index=zero_chunk_index,
                            total_chunks=len(chunks),
                            resume_model_index=zero_model_index,
                            resume_chunk_index=zero_chunk_index,
                        )
                        return
                    try:
                        self._persist_library_status(
                            state="running",
                            message=(
                                f"{model} ({mode}) chunk {chunk_index}/{len(chunks)}…"
                                if attempt == 1 else
                                f"{model}: retry {attempt}/{max_attempts} for chunk {chunk_index}/{len(chunks)}…"
                            ),
                            chunk_index=chunk_index,
                            total_chunks=len(chunks),
                            model_attempt=attempt,
                            model_attempt_limit=max_attempts,
                        )
                        self.ensure_chunk_mosaic(
                            queue_data,
                            chunk,
                            False,
                            lambda message, current=0, total=0: self._persist_library_status(
                                state="running",
                                current_model=model,
                                current_mode=mode,
                                chunk_index=chunk_index,
                                total_chunks=len(chunks),
                                model_attempt=attempt,
                                model_attempt_limit=max_attempts,
                                message=f"{model}: {message}",
                                current=int(current),
                                total=int(total),
                            ),
                        )
                        mosaic_ok = True
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        append_log(
                            f"Whole-library mosaic attempt {attempt}/{max_attempts} failed for {mode} {model}: {exc}\n"
                            + traceback.format_exc()
                        )
                        if attempt < max_attempts and retry_delay > 0:
                            self._persist_library_status(
                                state="running",
                                message=f"{model}: attempt {attempt}/{max_attempts} failed; retrying in {retry_delay:g}s. {exc}",
                            )
                            # Sleep in short pieces so real user work can preempt
                            # the retry immediately rather than waiting out a long delay.
                            deadline = time.time() + retry_delay
                            while time.time() < deadline:
                                allowed_retry, _retry_reason = self._library_can_continue(ignore_idle=ignore_idle)
                                if not allowed_retry:
                                    break
                                time.sleep(min(0.25, max(0.0, deadline - time.time())))
                if mosaic_ok:
                    generated += 1
                    self._persist_ready_snapshot(mode, model, chunks, int(row.get("bytes", 0) or 0), row.get("drives", []))
                    self._persist_library_status(
                        generated=generated,
                        resume_model_index=zero_model_index,
                        resume_chunk_index=chunk_index,
                        model_attempt=0,
                    )
                    continue

                # One pathological model must never pin a 4,000+ model pass.
                # After bounded retries, quarantine it for this traversal and
                # advance the persisted cursor to the next model.
                errors += 1
                skipped_models += 1
                append_log(
                    f"Whole-library skipped model after {max_attempts} failed attempt(s): {mode} {model}: {last_error}"
                )
                self._persist_library_status(
                    errors=errors,
                    skipped_models=skipped_models,
                    last_skipped_model=model,
                    last_skipped_error=last_error,
                    message=f"Skipped {model} after {max_attempts} failed mosaic attempts; continuing with the next model. {last_error}",
                    resume_model_index=model_index,
                    resume_chunk_index=0,
                    model_attempt=0,
                )
                break
            self._persist_library_status(resume_model_index=model_index, resume_chunk_index=0)
        self._clear_library_plan()
        self._persist_library_status(
            state="complete",
            message=f"Whole-library pass complete: generated {generated:,}; reused {reused:,}; skipped models {skipped_models:,}; errors {errors:,}.",
            generated=generated,
            reused=reused,
            errors=errors,
            skipped_models=skipped_models,
            model_index=len(plan),
            resume_model_index=len(plan),
            resume_chunk_index=0,
            pass_fingerprint=plan_fingerprint,
            resumed=False,
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )

    def schedule_library_generation(self, force: bool = False) -> bool:
        settings = self._library_settings()
        if not bool(settings.get("enabled", True)) and not force:
            return False
        with self.lock:
            if self.library_future is not None and not self.library_future.done():
                return False
            if self.nsfw_future is not None and not self.nsfw_future.done():
                return False
            status_snapshot = dict(self.library_status)
            catalog_snapshot = dict(self.catalog_status)
        if catalog_snapshot.get("status") == "scanning" and self._catalog_scan_actively_running():
            with self.lock:
                progress_text = str(self.catalog_status.get("progress", "Disk-size scan is running…"))
            self._persist_library_status(state="waiting", message=f"Waiting for active disk-size scan: {progress_text}")
            return False
        if not force and status_snapshot.get("state") == "complete":
            try:
                completed = datetime.fromisoformat(str(status_snapshot.get("completed_at", "")))
                interval = max(0.0, float(settings.get("rescan_minutes", 120))) * 60.0
                if (datetime.now() - completed).total_seconds() < interval:
                    return False
                catalog_updated = datetime.fromisoformat(str(catalog_snapshot.get("updated_at", "")))
                if catalog_updated <= completed:
                    self._persist_library_status(state="waiting", message="Refreshing model sizes before the next largest-first pass.")
                    self.start_catalog_scan()
                    return False
            except Exception:
                pass
        if not force:
            allowed, reason = self._library_can_continue(ignore_idle=False)
            if not allowed:
                self._persist_library_status(state="waiting", message=reason)
                return False
        self.library_cancel_requested.clear()
        def run() -> None:
            try:
                self._run_library_generation(ignore_idle=force)
            except Exception as exc:
                append_log(f"Whole-library generation crashed: {exc}\n{traceback.format_exc()}")
                self._persist_library_status(state="error", message=str(exc))
        future = self.library_executor.submit(run)
        with self.lock:
            self.library_future = future
        return True

    def force_library_generation(self) -> Dict[str, Any]:
        self.library_force_requested.set()
        started = self.schedule_library_generation(force=True)
        if started:
            message = "Whole-library generation started."
        else:
            with self.lock:
                catalog = dict(self.catalog_status)
                nsfw_running = bool(self.nsfw_future is not None and not self.nsfw_future.done())
            if catalog.get("status") == "scanning":
                message = f"Whole-library mosaic pass queued behind disk-size scan: {catalog.get('progress', 'scanning…')}"
                self._persist_library_status(state="queued", message=message)
            elif nsfw_running:
                message = "Whole-library mosaic pass queued behind the current Non-NSFW pass."
                self._persist_library_status(state="queued", message=message)
            else:
                message = "Whole-library mosaic pass queued for the next heavy-work slot."
                self._persist_library_status(state="queued", message=message)
        return {"started": started, "queued": not started, "message": message}

    def request_interactive_mosaic(self, queue_id: str, force: bool = False) -> None:
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if queue_data is None:
                return
            chunk = self.current_chunk(queue_data)
            if chunk is None:
                return
            signature = str(getattr(chunk, "signature", ""))
            existing = self.interactive_futures.get(f"{queue_id}:{signature}")
            if existing is not None and not existing.done():
                return
            queue_data["current_mosaic_status"] = {
                "state": "queued",
                "signature": signature,
                "message": "Queued for immediate mosaic preparation.",
            }
        def set_status(payload: Dict[str, Any]) -> None:
            current = self.current_chunk(queue_data)
            if current is not None and str(getattr(current, "signature", "")) == signature:
                queue_data["current_mosaic_status"] = {"signature": signature, **payload}

        def run() -> None:
            self.interactive_demand.set()
            def guarded_progress(message: str, current: int = 0, total: int = 0) -> None:
                with self.lock:
                    active = self.sort_session_active() and str(self.active_sort_queue_id or "") == str(queue_id)
                if not active:
                    raise TaskCancelled("Stopped mosaic generation because this model is no longer open on the phone.")
                set_status({"state": "running", "message": str(message), "current": int(current), "total": int(total)})
            try:
                guarded_progress("Preparing mosaic on the PC…", 0, 0)
                self.ensure_chunk_mosaic(queue_data, chunk, force, guarded_progress)
                set_status({"state": "ready", "message": "Mosaic ready."})
            except TaskCancelled as exc:
                set_status({"state": "cancelled", "message": str(exc)})
            except Exception as exc:
                set_status({"state": "error", "message": str(exc)})
                append_log(f"Interactive mosaic failed for {queue_data.get('model')}: {exc}\n{traceback.format_exc()}")

        future = self.interactive_executor.submit(run)
        with self.lock:
            self.interactive_futures[f"{queue_id}:{signature}"] = future

    def cancel_background_work(self, kind: str) -> Dict[str, Any]:
        kind = str(kind or "").casefold()
        if kind in {"nsfw", "cleanup", "non-nsfw"}:
            self.nsfw_force_requested.clear(); self.nsfw_cancel_requested.set()
            with self.lock:
                process = self.nsfw_process
            if process is not None and process.poll() is None:
                try: process.terminate()
                except Exception: pass
            self._persist_nsfw_status(
                state="cancelling",
                message="Cancellation requested from phone; stopping the current detector batch.",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return {"ok": True, "message": "Non-NSFW cancellation requested."}
        if kind in {"library", "mosaics", "whole-library"}:
            self.library_force_requested.clear(); self.library_cancel_requested.set()
            self._persist_library_status(
                state="cancelling",
                message="Cancellation requested from phone; stopping after the current mosaic operation.",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            return {"ok": True, "message": "Whole-library mosaic cancellation requested."}
        if kind in {"catalog", "scan"}:
            with self.lock:
                candidates = [record for record in self.tasks.values() if record.label == "Scan disks" and record.status in {"queued", "running"}]
            for record in candidates: record.cancel_requested = True
            return {"ok": True, "message": "Disk-size scan cancellation requested."}
        raise RuntimeError("Unknown background job.")

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        with self.lock:
            record = self.tasks.get(str(task_id))
            if record is None:
                raise RuntimeError("Task not found.")
            if record.status not in {"queued", "running"}:
                raise RuntimeError("That task is no longer cancellable.")
            record.cancel_requested = True
            record.progress = "Cancellation requested…"
            record.updated_at = time.time()
        return record.payload()

    def work_status_payload(self) -> Dict[str, Any]:
        with self.lock:
            task_rows = [record.payload() for record in self.tasks.values() if record.status in {"queued", "running", "error", "cancelled"}]
            catalog = dict(self.catalog_status)
        task_rows.sort(key=lambda row: float(row.get("created_at", 0)), reverse=True)
        return {
            "catalog": catalog,
            "tasks": task_rows[:20],
            "library": self.library_status_payload(),
            "nsfw": self.nsfw_status_payload(),
            "action_queue": self.action_queue_summary(),
            "action_jobs": self.action_queue_jobs_payload(),
            "scheduler": self._scheduler_settings(),
        }

    def _maintenance_loop(self) -> None:
        while not self.stop_event.wait(8.0):
            try:
                self._prune_finished_scheduler_futures()
                active_sorting = self.sort_session_active()
                with self.lock:
                    active_queue_id = str(self.active_sort_queue_id or "") if active_sorting else ""
                    interactive_active = any(
                        future is not None and not future.done()
                        for key, future in self.interactive_futures.items()
                        if active_queue_id and str(key).startswith(active_queue_id + ":")
                    )
                    interactive_task_active = active_sorting and any(
                        bool(getattr(record, "interactive_priority", False))
                        and record.status in {"queued", "running"}
                        and (time.time() - float(record.updated_at or record.created_at or 0.0)) < 900.0
                        for record in self.tasks.values()
                    )
                if not interactive_active and not interactive_task_active:
                    self.interactive_demand.clear()
                # v2.15.2: automatic look-ahead is a bounded property of the
                # currently visible model. This retry point self-heals cases where
                # a refill attempt paused for the current mosaic, PC activity, or
                # model indexing. Hidden/abandoned queues are never scheduled.
                if active_queue_id:
                    self.schedule_prefetch(active_queue_id)
                if self.library_force_requested.is_set():
                    if self.schedule_library_generation(force=True):
                        self.library_force_requested.clear()
                elif self.nsfw_force_requested.is_set():
                    if self.schedule_nsfw_generation(force=True):
                        self.nsfw_force_requested.clear()
                else:
                    self._schedule_prioritized_background()
            except Exception:
                append_log("Background maintenance error:\n" + traceback.format_exc())

    def create_task(
        self,
        label: str,
        worker: Callable[[Callable[[str, int, int], None]], Any],
        pool: Optional[ThreadPoolExecutor] = None,
    ) -> str:
        task_id = secrets.token_urlsafe(10)
        record = TaskRecord(task_id, label)
        record.interactive_priority = bool(pool in {self.interactive_executor, self.priority_executor})
        with self.lock:
            self.tasks[task_id] = record

        def progress(text: str, current: int = 0, total: int = 0) -> None:
            if record.cancel_requested:
                raise TaskCancelled("Cancelled from the phone.")
            with self.lock:
                record.progress = str(text)
                record.current = int(current)
                record.total = int(total)
                record.updated_at = time.time()

        def run() -> None:
            with self.lock:
                record.status = "running"
                record.updated_at = time.time()
            try:
                result = worker(progress)
                with self.lock:
                    record.result = result
                    record.status = "done"
                    record.progress = "Complete."
                    record.updated_at = time.time()
            except TaskCancelled as exc:
                with self.lock:
                    record.status = "cancelled"
                    record.error = ""
                    record.progress = str(exc) or "Cancelled."
                    record.updated_at = time.time()
                    if label == "Scan disks":
                        self.catalog_status = {
                            "status": "cancelled",
                            "progress": "Disk-size scan cancelled; the last completed catalog remains usable.",
                            "current": int(self.catalog_status.get("current", 0) or 0),
                            "total": int(self.catalog_status.get("total", 0) or 0),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
            except Exception as exc:
                trace = traceback.format_exc()
                append_log(f"Task {label} failed: {exc}\n{trace}")
                with self.lock:
                    record.status = "error"
                    record.error = str(exc)
                    record.progress = "Failed."
                    record.updated_at = time.time()
                    if label == "Scan disks":
                        self.catalog_status = {
                            "status": "error",
                            "progress": f"Disk-size scan failed: {exc}. The last completed catalog remains usable.",
                            "current": int(self.catalog_status.get("current", 0) or 0),
                            "total": int(self.catalog_status.get("total", 0) or 0),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }

        (pool or self.executor).submit(run)
        return task_id

    def start_catalog_scan(self) -> str:
        with self.lock:
            for record in self.tasks.values():
                if record.label == "Scan disks" and record.status in {"queued", "running"}:
                    return record.id

        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                self.catalog_status = {"status": "scanning", "progress": "Starting scan…", "current": 0, "total": len(self.roots), "updated_at": ""}
            original_exts = parse_extensions(self.mosaic_settings.get("extensions", "mp4,ts"))
            review_exts = parse_extensions(self.review_settings.get("extensions", "mp4,ts"))
            original_rows: List[Dict[str, Any]] = []
            review_rows: List[Dict[str, Any]] = []
            deletion_rows: List[Dict[str, Any]] = []
            total = len(self.roots)
            for root_index, root in enumerate(self.roots, start=1):
                message = f"Scanning {root_index}/{total}: {root}"
                with self.lock:
                    self.catalog_status.update({"status": "scanning", "progress": message, "current": root_index - 1, "total": total + len(configured_deletion_roots(self.roots))})
                progress(message, root_index - 1, total + len(configured_deletion_roots(self.roots)))
                try:
                    with os.scandir(root) as iterator:
                        entries = list(iterator)
                except OSError as exc:
                    append_log(f"Catalog root unavailable: {root}: {exc}")
                    continue
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                            continue
                        model_folder = Path(entry.path)
                        logical = self.mosaic.canonical_model_name(entry.name)
                        original_bytes, original_ts_bytes = direct_video_byte_breakdown(model_folder, original_exts)
                        if original_bytes > 0:
                            original_rows.append({
                                "name": logical,
                                "physical_name": entry.name,
                                "folder": str(model_folder),
                                "root": str(root),
                                "drive": drive_label(root),
                                "bytes": original_bytes,
                                "ts_bytes": original_ts_bytes,
                            })
                        review_folder = model_folder / self.review.REVIEW_FOLDER_NAME
                        review_bytes, review_ts_bytes = direct_video_byte_breakdown(review_folder, review_exts) if review_folder.is_dir() else (0, 0)
                        if review_bytes > 0:
                            review_rows.append({
                                "name": logical,
                                "physical_name": entry.name,
                                "folder": str(model_folder),
                                "root": str(root),
                                "drive": drive_label(root),
                                "bytes": review_bytes,
                                "ts_bytes": review_ts_bytes,
                            })
                    except OSError:
                        continue
            deletion_roots = configured_deletion_roots(self.roots)
            for drive, deletion_root in deletion_roots.items():
                message = f"Scanning permanent-deletion queue on {drive}: {deletion_root}"
                deletion_index = list(deletion_roots).index(drive) + 1
                with self.lock:
                    self.catalog_status.update({"status": "scanning", "progress": message, "current": total + deletion_index - 1, "total": total + len(deletion_roots)})
                progress(message, total + deletion_index - 1, total + len(deletion_roots))
                if not deletion_root.is_dir():
                    continue
                try:
                    entries = list(os.scandir(deletion_root))
                except OSError as exc:
                    append_log(f"Deletion root unavailable: {deletion_root}: {exc}")
                    continue
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        model_folder = Path(entry.path)
                        logical = self.mosaic.canonical_model_name(entry.name)
                        marked_bytes = recursive_video_bytes(model_folder, review_exts)
                        if marked_bytes > 0:
                            deletion_rows.append({
                                "name": logical,
                                "physical_name": entry.name,
                                "folder": str(model_folder),
                                "root": str(deletion_root),
                                "drive": drive,
                                "bytes": marked_bytes,
                            })
                    except OSError:
                        continue
            updated_at = datetime.now().isoformat(timespec="seconds")
            with self.lock:
                self.catalog = {"original": original_rows, "review": review_rows, "deletion": deletion_rows}
                self.catalog_status = {
                    "status": "ready",
                    "progress": (
                        f"Sized {len(original_rows):,} original, {len(review_rows):,} Review, "
                        f"and {len(deletion_rows):,} marked-for-deletion folders."
                    ),
                    "updated_at": updated_at,
                    "current": total + len(deletion_roots),
                    "total": total + len(deletion_roots),
                }
                atomic_write_json(CATALOG_PATH, {
                    "updated_at": updated_at,
                    "roots_file": str(self.roots_path),
                    "catalog": self.catalog,
                })
                self.catalog_view_cache.clear()
                self.ready_count_cache.clear()
            final_total = total + len(deletion_roots)
            progress("Disk-size scan complete.", final_total, final_total)
            return {
                "original_folders": len(original_rows),
                "review_folders": len(review_rows),
                "deletion_folders": len(deletion_rows),
            }

        return self.create_task("Scan disks", worker)

    def drives(self) -> List[str]:
        values = {drive_label(root) for root in self.roots}
        return sorted(values)

    def invalidate_catalog_views(self) -> None:
        """Drop only derived phone-list caches; never mutate source state."""
        with self.lock:
            self.catalog_view_cache.clear()
            self.ready_count_cache.clear()

    def _ready_counts_for_mode(self, mode: str, selected_drives: Set[str]) -> Dict[str, int]:
        """Cheap cached ready-chunk counts for the model list.

        Readiness shown in the list is intentionally a hint.  The exact JPEG
        and hit-map are still validated by quick_load_queue when the user taps a
        model.  Keeping this path metadata-only avoids thousands of image/stat
        checks on every tab switch.
        """
        mode = "review" if mode == "review" else "original"
        drive_key = tuple(sorted(str(value).upper() for value in selected_drives))
        cache_key = (mode, drive_key)
        with self.lock:
            updated_at = str(self.ready_index.get("updated_at", ""))
            cached = self.ready_count_cache.get(cache_key)
            if cached and time.time() - cached[0] < 5.0:
                return dict(cached[2])
            snapshots = [dict(value) for value in self.ready_index.get("snapshots", {}).values() if isinstance(value, dict)]

        counts: Dict[str, int] = {}
        for snapshot in snapshots:
            if str(snapshot.get("mode", "")) != mode:
                continue
            model = str(snapshot.get("model", "")).casefold()
            if not model:
                continue
            count = 0
            for row in snapshot.get("chunks", []):
                if not isinstance(row, dict) or not row.get("ready"):
                    continue
                outputs = row.get("mosaics", [])
                if not isinstance(outputs, list) or not outputs:
                    continue
                physical = Path(str(row.get("model_folder" if mode == "review" else "folder", "")))
                if selected_drives and drive_label(physical) not in selected_drives:
                    continue
                count += 1
            if count:
                counts[model] = counts.get(model, 0) + count
        with self.lock:
            self.ready_count_cache[cache_key] = (time.time(), updated_at, dict(counts))
        return counts

    def catalog_models(self, mode: str, selected_drives: Set[str], model_filter: str = "") -> List[Dict[str, Any]]:
        if mode == "cleanup":
            return self.cleanup_catalog_models(selected_drives)
        if mode not in {"original", "review", "deletion"}:
            mode = "original"
        drive_key = tuple(sorted(str(value).upper() for value in selected_drives))
        filter_key = str(model_filter or "").strip().casefold()
        cache_key = (mode, drive_key, filter_key)
        with self.lock:
            catalog_stamp = str(self.catalog_status.get("updated_at", ""))
        ready_stamp = ""
        if mode in {"original", "review"}:
            with self.lock:
                ready_stamp = str(self.ready_index.get("updated_at", ""))
        try:
            admin_token = self.model_admin.filter_cache_token() if mode in {"original", "review"} and self.model_admin is not None else tuple()
        except Exception:
            admin_token = tuple()
        with self.lock:
            cached = self.catalog_view_cache.get(cache_key)
        if cached:
            built_at, cached_catalog_stamp, cached_ready_stamp, cached_admin_token, cached_rows = cached
            # Short TTL keeps readiness fresh while making rapid tab switches
            # effectively memory-speed even during background generation.
            if (
                time.time() - built_at < 5.0
                and cached_catalog_stamp == catalog_stamp
                and cached_ready_stamp == ready_stamp
                and cached_admin_token == admin_token
            ):
                return [dict(row) for row in cached_rows]
        with self.lock:
            rows = list(self.catalog.get(mode, []))
        queued_delete_names = self._queued_delete_model_names() if mode == "deletion" else set()
        aggregate: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            drive = str(row.get("drive", "")).upper()
            if selected_drives and drive not in selected_drives:
                continue
            name = str(row.get("name", "")).strip()
            if not name or (mode == "deletion" and name.casefold() in queued_delete_names):
                continue
            key = name.casefold()
            item = aggregate.setdefault(key, {
                "name": name, "bytes": 0, "ts_bytes": 0, "ts_stats_known": True, "folders": [], "drives": set(),
            })
            item["bytes"] += int(row.get("bytes", 0) or 0)
            if "ts_bytes" in row:
                item["ts_bytes"] += int(row.get("ts_bytes", 0) or 0)
            else:
                # Old cached catalogs predate the per-extension subtotal.  Do
                # not pretend zero TS bytes are known; one ordinary Rescan fills
                # this field without touching mosaics/queues.
                item["ts_stats_known"] = False
            item["folders"].append(str(row.get("folder", "")))
            item["drives"].add(drive)
        ready_by_model: Dict[str, int] = self._ready_counts_for_mode(mode, selected_drives) if mode in {"original", "review"} else {}
        with self.lock:
            ready_snapshots = dict(self.ready_index.get("snapshots", {})) if mode in {"original", "review"} else {}
        result = []
        for item in aggregate.values():
            avg_chunk_bytes = 0
            chunk_count = 0
            chunk_stats_known = False
            chunk_stats_updated_at = ""
            if mode in {"original", "review"}:
                snapshot = ready_snapshots.get(self._ready_key(mode, str(item["name"])))
                if isinstance(snapshot, dict):
                    snapshot_drives = {str(value).upper() for value in snapshot.get("drives", []) if str(value)}
                    current_drives = {str(value).upper() for value in item["drives"] if str(value)}
                    # READY snapshots are authoritative chunk metadata only for the
                    # same current drive scope and model byte total. This avoids
                    # ranking a filtered/stale model with statistics from a different
                    # physical copy while requiring no new disk scan or FFmpeg work.
                    if snapshot_drives == current_drives and int(snapshot.get("bytes", -1) or -1) == int(item["bytes"]):
                        chunk_bytes = [
                            max(0, int(row.get("source_bytes", 0) or 0))
                            for row in snapshot.get("chunks", [])
                            if isinstance(row, dict) and int(row.get("source_bytes", 0) or 0) > 0
                        ]
                        if chunk_bytes:
                            chunk_count = len(chunk_bytes)
                            avg_chunk_bytes = int(round(sum(chunk_bytes) / chunk_count))
                            chunk_stats_known = True
                            chunk_stats_updated_at = str(snapshot.get("updated_at", ""))
            result.append({
                "name": item["name"],
                "bytes": item["bytes"],
                "size": human_size(item["bytes"]),
                "ts_bytes": int(item.get("ts_bytes", 0) or 0),
                "ts_size": human_size(int(item.get("ts_bytes", 0) or 0)) if item.get("ts_stats_known") else "",
                "ts_stats_known": bool(item.get("ts_stats_known")),
                "folder_count": len(item["folders"]),
                "drives": sorted(item["drives"]),
                "ready_chunks": ready_by_model.get(str(item["name"]).casefold(), 0),
                "avg_chunk_bytes": avg_chunk_bytes,
                "avg_chunk_size": human_size(avg_chunk_bytes) if chunk_stats_known else "",
                "chunk_count": chunk_count,
                "chunk_stats_known": chunk_stats_known,
                "chunk_stats_updated_at": chunk_stats_updated_at,
            })
        if mode in {"original", "review"}:
            hidden = self.model_admin.hidden_names() if self.model_admin is not None else set()
            ignored = self.model_admin.ignored_names() if self.model_admin is not None else set()
            if filter_key == "hidden":
                result = [row for row in result if str(row.get("name", "")).casefold() in hidden and str(row.get("name", "")).casefold() not in ignored]
            elif filter_key in {"easy", "easy_sort", "easysort"}:
                easy = self.model_admin.easy_sort_names() if self.model_admin is not None else set()
                result = [row for row in result if str(row.get("name", "")).casefold() in easy and str(row.get("name", "")).casefold() not in hidden and str(row.get("name", "")).casefold() not in ignored]
            else:
                result = [row for row in result if str(row.get("name", "")).casefold() not in hidden and str(row.get("name", "")).casefold() not in ignored]
        result.sort(key=lambda row: (-int(row["bytes"]), str(row["name"]).casefold()))
        with self.lock:
            self.catalog_view_cache[cache_key] = (time.time(), catalog_stamp, ready_stamp, admin_token, [dict(row) for row in result])
        return result

    def model_folders(self, mode: str, model_name: str, selected_drives: Set[str]) -> List[Path]:
        key = model_name.casefold().strip()
        found: Dict[str, Path] = {}
        with self.lock:
            rows = list(self.catalog.get(mode, []))
        for row in rows:
            if str(row.get("name", "")).casefold() != key:
                continue
            drive = str(row.get("drive", "")).upper()
            if selected_drives and drive not in selected_drives:
                continue
            path = Path(str(row.get("folder", "")))
            if path.is_dir():
                found[normalized(path)] = path
        return sorted(found.values(), key=normalized)

    def _build_priority_chunks(
        self,
        mode: str,
        model_name: str,
        selected_drives: Set[str],
        progress: Callable[[str, int, int], None],
    ) -> Tuple[List[Any], List[str]]:
        """Build chunk metadata only; never probe exact durations or generate frames."""
        folders = self.model_folders(mode, model_name, selected_drives)
        if not folders:
            raise RuntimeError(
                "No matching model folders were found on the selected drives. Rescan disks if files changed."
            )
        notes: List[str] = []
        if mode == "original":
            chunks, notes = self.mosaic.build_model_queue(
                folders,
                dict(self.mosaic_settings),
                self.mosaic_duration_cache,
                self.mosaic_manifest,
                include_skipped=False,
                progress=lambda text: progress(str(text), 0, 0),
                session_generated_signatures=self.session_generated_signatures,
                cancel=threading.Event(),
                probe_actual_durations=False,
            )
            chunks = list(chunks)
            self._hydrate_chunks_from_ready_snapshot(mode, model_name, chunks)
            return chunks, list(notes)
        if mode == "review":
            chunks = list(self.review.build_chunks(
                folders,
                dict(self.review_settings),
                self.review_duration_cache,
                lambda text: progress(str(text), 0, 0),
                probe_actual_durations=False,
            ))
            self._hydrate_chunks_from_ready_snapshot(mode, model_name, chunks)
            return chunks, notes
        raise RuntimeError("Priority chunk building supports Originals and Review only.")

    def load_queue(self, mode: str, model_name: str, selected_drives: Set[str]) -> str:
        if mode == "cleanup":
            def cleanup_worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
                progress("Loading prepared non-NSFW cleanup mosaic…", 0, 1)
                result = self.quick_load_cleanup_queue(model_name, selected_drives)
                if not result.get("ready"):
                    raise RuntimeError(str(result.get("reason", "Cleanup analysis is not ready yet.")))
                progress("Ready.", 1, 1)
                return {"queue_id": result["queue_id"], "chunk_count": 1, "notes": ["Loaded prepared cleanup result."]}
            return self.create_task(f"Load cleanup {model_name}", cleanup_worker)
        mode = mode if mode in {"original", "review", "deletion"} else "original"

        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            folders = self.model_folders(mode, model_name, selected_drives)
            if not folders:
                raise RuntimeError("No matching model folders were found on the selected drives. Rescan disks if files changed.")
            notes: List[str] = []
            progress(f"Building {model_name} chunk queue…", 0, 0)
            if mode in {"original", "review"}:
                chunks, notes = self._build_priority_chunks(mode, model_name, selected_drives, progress)
            else:
                chunks = build_deletion_chunks(
                    model_name,
                    folders,
                    dict(self.review_settings),
                    self.review,
                    self.review_duration_cache,
                    lambda text: progress(str(text), 0, 0),
                )
            queue_id = secrets.token_urlsafe(12)
            queue_data = {
                "id": queue_id,
                "mode": mode,
                "model": model_name,
                "drives": sorted(selected_drives),
                "chunks": list(chunks),
                "initial_count": len(chunks),
                "drafts": {},
                "history": [],
                "notes": notes,
                "created_at": time.time(),
                "busy": False,
                "recu_status": "not_started" if mode in {"original", "review"} else "disabled",
                "recu_error": "",
                "recu_data": None,
                "prefetch_status": {
                    "state": "idle",
                    "ready": 0,
                    "target": 0,
                    "message": "Not started.",
                },
            }
            with self.lock:
                self.queues[queue_id] = queue_data
            if mode in {"original", "review"} and chunks:
                try:
                    self._persist_ready_snapshot(
                        mode, model_name, chunks,
                        sum(int(getattr(chunk, "source_bytes", 0)) for chunk in chunks),
                        sorted(selected_drives),
                    )
                except Exception as exc:
                    append_log(f"Could not persist prepared queue snapshot for {model_name}: {exc}")
            if chunks:
                self.request_interactive_mosaic(queue_id, False)
                if mode in {"original", "review"} and bool(self.config.get("recu", {}).get("enabled", True)):
                    self.start_recu_for_queue(queue_id, force=False)
            return {"queue_id": queue_id, "chunk_count": len(chunks), "notes": notes[:20]}

        self.interactive_demand.set()
        return self.create_task(f"Load {model_name}", worker, pool=self.interactive_executor)

    def current_chunk(self, queue_data: Dict[str, Any]) -> Any:
        chunks = queue_data.get("chunks", [])
        return chunks[0] if chunks else None

    def _mosaic_sidecar_payload(self, mode: str, chunk: Any) -> Dict[str, Any]:
        try:
            if mode == "original":
                sidecar = chunk.folder / self.mosaic.MOSAIC_DIRNAME / f"{self.mosaic.mosaic_base_stem(chunk)}.sources.json"
            elif mode == "cleanup":
                sidecar = Path(str(getattr(chunk, "manifest_path", "") or (chunk.model_folder / self.nsfw.MOSAIC_DIRNAME / self.nsfw.MANIFEST_FILENAME)))
            else:
                sidecar = chunk.model_folder / self.review.MOSAIC_DIRNAME / f"{self.review.mosaic_base(chunk)}.json"
            raw = load_json(sidecar, {})
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _align_chunk_files_to_mosaic_sources(self, mode: str, chunk: Any) -> bool:
        """Make the JPEG's recorded source order authoritative for mobile numbering.

        Chunk signatures intentionally ignore source ordering, so a valid mosaic
        can survive a later queue rebuild whose files happen to be ordered
        differently. The old positional tap-map then produces an off-by-one (or
        worse) overlay even though the JPEG itself is correct. v2.9.1 reorders
        the live VideoInfo objects to the sidecar's generation-time source order
        before exposing numbers/taps.
        """
        raw = self._mosaic_sidecar_payload(mode, chunk)
        recorded = raw.get("files", []) if isinstance(raw, dict) else []
        current = list(getattr(chunk, "files", []) or [])
        if not isinstance(recorded, list) or not recorded or len(recorded) != len(current):
            return False

        def exact_key(path: Path, size: int, mtime: float) -> Tuple[str, int, float]:
            return (normalized(path), int(size), round(float(mtime), 3))

        exact: Dict[Tuple[str, int, float], List[Any]] = {}
        loose: Dict[Tuple[str, int, float], List[Any]] = {}
        for video in current:
            ekey = exact_key(video.path, int(video.size), float(video.mtime))
            exact.setdefault(ekey, []).append(video)
            lkey = (video.path.name.casefold(), int(video.size), round(float(video.mtime), 3))
            loose.setdefault(lkey, []).append(video)

        ordered: List[Any] = []
        used: Set[int] = set()
        for row in recorded:
            if not isinstance(row, dict):
                return False
            raw_path = Path(str(row.get("path", "")))
            size = int(row.get("size", 0) or 0)
            mtime = float(row.get("mtime", 0) or 0)
            candidates = [item for item in exact.get(exact_key(raw_path, size, mtime), []) if id(item) not in used]
            if not candidates:
                # Path can legitimately change when a Review/deletion recovery
                # mosaic is reused after a move. Basename+size+mtime is a safe
                # fallback only when it identifies one current recording.
                lkey = (raw_path.name.casefold(), size, round(mtime, 3))
                candidates = [item for item in loose.get(lkey, []) if id(item) not in used]
            if len(candidates) != 1:
                return False
            video = candidates[0]
            used.add(id(video))
            ordered.append(video)

        if len(ordered) != len(current):
            return False
        try:
            chunk.files[:] = ordered
        except Exception:
            chunk.files = ordered
        return True

    def _embedded_layout_entry(self, mode: str, chunk: Any, outputs: Sequence[Path]) -> Optional[Dict[str, Any]]:
        """Return the exact hit-map written beside a v2.4+ mosaic.

        This metadata lives with the JPEGs on the recording drive, so updating or
        moving the mobile-reviewer package cannot orphan the tap geometry.
        """
        try:
            raw = self._mosaic_sidecar_payload(mode, chunk)
            if raw.get("files") and not self._align_chunk_files_to_mosaic_sources(mode, chunk):
                # Never display a positional overlay when the sidecar's source
                # identities cannot be reconciled with the current chunk. The
                # caller will regenerate a trustworthy mosaic/hit-map instead.
                return None
            layout = raw.get("mobile_layout_v2") if isinstance(raw, dict) else None
            if not isinstance(layout, dict) or int(layout.get("version", 0) or 0) < 2:
                return None
            layout_mode = str(layout.get("mode", ""))
            acceptable_modes = {mode}
            if mode == "deletion": acceptable_modes.add("review")
            if layout_mode not in acceptable_modes or str(layout.get("signature", "")) != str(chunk.signature):
                return None
            parts = layout.get("parts", [])
            if not isinstance(parts, list) or len(parts) != len(outputs):
                return None
            actual = [normalized(Path(value)) for value in outputs]
            recorded = [normalized(Path(str(part.get("output", "")))) for part in parts if isinstance(part, dict)]
            if recorded != actual:
                return None
            output_signature: List[Tuple[str, int, int]] = []
            for output in outputs:
                try:
                    stat = Path(output).stat()
                    output_signature.append((normalized(Path(output)), int(stat.st_mtime_ns), int(stat.st_size)))
                except OSError:
                    return None
            layout_hash = hashlib.sha256(json.dumps(layout, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:20]
            cache_key = (str(mode), str(chunk.signature), tuple(output_signature), layout_hash)
            with self.lock:
                cached_layout = self.validated_layout_cache.get(cache_key)
            if cached_layout is not None:
                return cached_layout
            for part, output in zip(parts, outputs):
                if not isinstance(part, dict) or not Path(output).is_file():
                    return None
                with Image.open(output) as opened:
                    width, height = opened.size
                if int(part.get("width", -1)) != int(width) or int(part.get("height", -1)) != int(height):
                    return None
                tiles = part.get("tiles", [])
                if not isinstance(tiles, list) or not tiles:
                    return None
                for tile in tiles:
                    if not isinstance(tile, dict):
                        return None
                    file_index = int(tile.get("file_index", -1))
                    if file_index < 0 or file_index >= len(chunk.files):
                        return None
                    left = int(tile.get("left_px", -1)); top = int(tile.get("top_px", -1))
                    tw = int(tile.get("width_px", 0)); th = int(tile.get("height_px", 0))
                    if left < 0 or top < 0 or tw <= 0 or th <= 0 or left + tw > width + 2 or top + th > height + 2:
                        return None
            # Validate the number/order of tap targets against the generation
            # plan whenever enough source metadata exists.  This catches old
            # mosaics whose JPEG and sidecar came from different generations,
            # producing duplicate/missing/shifted tap boxes.
            flat_tiles = [tile for part in parts if isinstance(part, dict) for tile in part.get("tiles", []) if isinstance(tile, dict)]
            if layout_mode == "original":
                side_files = raw.get("files", []) if isinstance(raw.get("files", []), list) else []
                durations = []
                complete_durations = len(side_files) == len(chunk.files)
                if complete_durations:
                    for idx, video in enumerate(chunk.files):
                        value = side_files[idx].get("duration") if isinstance(side_files[idx], dict) else None
                        if value is None:
                            value = self.mosaic_duration_cache.get(video.path, video.size, video.mtime)
                        if value is None:
                            complete_durations = False; break
                        durations.append(float(value))
                if complete_durations:
                    settings = layout.get("settings", {}) if isinstance(layout.get("settings", {}), dict) else {}
                    expected = self.mosaic.build_frame_plan(
                        chunk, durations,
                        int(settings.get("sample_every_seconds", self.mosaic_settings.get("sample_every_seconds", 300))),
                        int(settings.get("max_total_frames", self.mosaic_settings.get("max_total_frames", 240))),
                    )
                    expected_files = [int(row[0]) - 1 for row in expected]
                    actual_files = [int(tile.get("file_index", -1)) for tile in flat_tiles]
                    if actual_files != expected_files:
                        return None
                    if int(layout.get("version", 0) or 0) >= 3:
                        expected_seconds = [float(row[2]) for row in expected]
                        actual_seconds = [float(tile.get("local_seconds", -1)) for tile in flat_tiles]
                        if actual_seconds != expected_seconds:
                            return None
            elif layout_mode == "review" and int(layout.get("version", 0) or 0) >= 3:
                frame_indices = [int(tile.get("frame_index", -1)) for tile in flat_tiles]
                if frame_indices != list(range(len(frame_indices))):
                    return None

            # Boxes within one image should never substantially overlap or repeat.
            for part in parts:
                if not isinstance(part, dict):
                    continue
                boxes = []
                for tile in part.get("tiles", []):
                    if not isinstance(tile, dict): continue
                    box = (int(tile.get("left_px", 0)), int(tile.get("top_px", 0)), int(tile.get("width_px", 0)), int(tile.get("height_px", 0)))
                    if box in boxes:
                        return None
                    boxes.append(box)

            with self.lock:
                if len(self.validated_layout_cache) >= 4096:
                    # This is only a derived performance cache.  A full clear is
                    # cheaper and safer than bookkeeping an LRU for thousands of mosaics.
                    self.validated_layout_cache.clear()
                self.validated_layout_cache[cache_key] = layout
            return layout
        except Exception:
            return None

    def _review_frame_timeline(self, chunk: Any, outputs: Optional[Sequence[Path]] = None) -> List[Dict[str, Any]]:
        """Return authoritative Review mosaic frame boundaries in visual order.

        v2.12 mosaics persist timestamps directly in the sidecar. Older embedded
        v2 layouts can be upgraded losslessly because their generation settings
        and source durations are also persisted beside the JPEG. Legacy central
        hit maps intentionally do not qualify: exact timestamps are required
        before destructive frame-level trimming is allowed.
        """
        if outputs is None:
            outputs = self.review.mosaic_outputs(chunk)
        outputs = [Path(value) for value in (outputs or []) if Path(value).is_file()]
        if not outputs:
            return []
        embedded = self._embedded_layout_entry("review", chunk, outputs)
        if embedded is None:
            return []

        raw_tiles: List[Dict[str, Any]] = []
        for part in embedded.get("parts", []):
            if not isinstance(part, dict):
                return []
            tiles = part.get("tiles", [])
            if not isinstance(tiles, list):
                return []
            raw_tiles.extend(tile for tile in tiles if isinstance(tile, dict))
        if not raw_tiles:
            return []

        # Prefer exact generation-time timestamps from v3 sidecars.
        exact = all(
            "frame_index" in tile and "chunk_seconds" in tile and "interval_end_seconds" in tile
            for tile in raw_tiles
        )
        if exact:
            timeline: List[Dict[str, Any]] = []
            for visual_index, tile in enumerate(raw_tiles):
                try:
                    start = max(0.0, float(tile.get("chunk_seconds", 0.0)))
                    end = max(start, float(tile.get("interval_end_seconds", start)))
                    timeline.append({
                        "frame_index": int(tile.get("frame_index", visual_index)),
                        "file_index": int(tile.get("file_index", -1)),
                        "chunk_seconds": start,
                        "local_seconds": max(0.0, float(tile.get("local_seconds", 0.0))),
                        "interval_end_seconds": min(float(chunk.total_duration), end),
                    })
                except Exception:
                    return []
            timeline.sort(key=lambda row: int(row["frame_index"]))
            if [int(row["frame_index"]) for row in timeline] != list(range(len(timeline))):
                return []
            return timeline

        # Embedded v2 sidecars did not persist timestamps per tile, but did
        # persist the exact sample settings and source durations. Reconstruct
        # the same continuous sample plan rather than guessing from today's
        # settings.
        settings = embedded.get("settings", {}) if isinstance(embedded.get("settings", {}), dict) else {}
        try:
            spacing = max(1, int(settings.get("sample_every_seconds", self.review_settings.get("sample_every_seconds", 180))))
            max_frames = max(1, int(settings.get("max_total_frames", self.review_settings.get("max_total_frames", 300))))
            plan = self.review.build_continuous_sample_plan(
                chunk.files,
                spacing_seconds=spacing,
                max_frames=max_frames,
            )
        except Exception:
            return []
        if len(plan) != len(raw_tiles):
            return []
        identity_to_index = {id(video): index for index, video in enumerate(chunk.files)}
        timeline = []
        for frame_index, (video, local_seconds, chunk_seconds) in enumerate(plan):
            end = float(plan[frame_index + 1][2]) if frame_index + 1 < len(plan) else float(chunk.total_duration)
            timeline.append({
                "frame_index": frame_index,
                "file_index": int(identity_to_index.get(id(video), -1)),
                "chunk_seconds": float(chunk_seconds),
                "local_seconds": float(local_seconds),
                "interval_end_seconds": max(float(chunk_seconds), min(float(chunk.total_duration), end)),
            })
        return timeline

    def _layout_cache_entry(self, mode: str, chunk: Any, outputs: Sequence[Path]) -> Optional[Dict[str, Any]]:
        """Read the old central v2.3 layout cache only when its geometry is plausible.

        v2.3 could combine a JPEG made with one sample spacing with a hit-map
        reconstructed using another. A simple cell-aspect sanity check rejects
        exactly that failure instead of drawing misleading tap targets.
        """
        entry = self.layout_cache.get("layouts", {}).get(str(chunk.signature))
        if not isinstance(entry, dict) or entry.get("mode") != mode:
            return None
        cached_outputs = [Path(str(value)) for value in entry.get("outputs", [])]
        actual_outputs = [Path(value) for value in outputs]
        if [normalized(path) for path in cached_outputs] != [normalized(path) for path in actual_outputs]:
            return None
        if not actual_outputs or not all(path.is_file() for path in actual_outputs):
            return None
        try:
            file_numbers = [int(value) for value in entry.get("file_numbers", [])]
            columns = max(1, int(entry.get("columns", 3)))
            max_tiles = max(columns, int(entry.get("max_tiles", 120)))
            header_height = max(0, int(entry.get("header_height", 46 if mode == "original" else 44)))
            if not file_numbers:
                return None
            cursor = 0
            for output in actual_outputs:
                count = min(max_tiles, max(0, len(file_numbers) - cursor))
                if count <= 0:
                    return None
                rows = max(1, int(math.ceil(count / columns)))
                with Image.open(output) as opened:
                    width, height = opened.size
                cell_width = float(width) / float(columns)
                cell_height = float(max(1, height - header_height)) / float(rows)
                ratio = cell_height / max(1.0, cell_width)
                # Normal webcam mosaics span landscape through portrait frames.
                # A ratio below 0.30 is the signature of the v2.3 stacked-number bug.
                if ratio < 0.30 or ratio > 2.25:
                    return None
                cursor += count
            if cursor != len(file_numbers):
                return None
        except Exception:
            return None
        return entry

    def _record_layout_cache(self, mode: str, chunk: Any, outputs: Sequence[Path]) -> None:
        if mode == "original":
            durations = []
            for video in chunk.files:
                cached = self.mosaic_duration_cache.get(video.path, video.size, video.mtime)
                durations.append(float(cached if cached is not None else video.duration))
            plan = self.mosaic.build_frame_plan(
                chunk, durations,
                int(self.mosaic_settings.get("sample_every_seconds", 300)),
                int(self.mosaic_settings.get("max_total_frames", 240)),
            )
            file_numbers = [int(item[0]) for item in plan]
            columns = max(1, int(self.mosaic_settings.get("columns", 3)))
            max_tiles = max(columns, int(self.mosaic_settings.get("max_tiles_per_image", 120)))
            header_height = 46
        else:
            plan = self.review.build_continuous_sample_plan(
                chunk.files,
                spacing_seconds=int(self.review_settings.get("sample_every_seconds", 180)),
                max_frames=int(self.review_settings.get("max_total_frames", 300)),
            )
            by_identity = {id(video): index for index, video in enumerate(chunk.files, start=1)}
            file_numbers = [by_identity.get(id(item[0]), 1) for item in plan]
            columns = max(1, int(self.review_settings.get("columns", 3)))
            max_tiles = max(columns, int(self.review_settings.get("max_tiles_per_image", 120)))
            header_height = 44
        self.layout_cache.setdefault("layouts", {})[str(chunk.signature)] = {
            "mode": mode,
            "outputs": [str(Path(path)) for path in outputs],
            "file_numbers": file_numbers,
            "columns": columns,
            "max_tiles": max_tiles,
            "header_height": header_height,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        # Keep a generous but bounded history.
        layouts = self.layout_cache["layouts"]
        if len(layouts) > 20000:
            ordered = sorted(layouts.items(), key=lambda item: str(item[1].get("generated_at", "")))
            for key, _entry in ordered[:len(layouts) - 18000]:
                layouts.pop(key, None)
        atomic_write_json(LAYOUT_CACHE_PATH, self.layout_cache)

    def ensure_chunk_mosaic(
        self,
        queue_data: Dict[str, Any],
        chunk: Any,
        force: bool,
        progress: Callable[[str, int, int], None],
    ) -> None:
        if chunk is None:
            return
        mode = str(queue_data["mode"])
        if mode == "cleanup":
            outputs = [Path(path) for path in getattr(chunk, "mosaics", []) if Path(path).is_file()]
            if outputs and self._embedded_layout_entry(mode, chunk, outputs):
                progress("Existing non-NSFW cleanup mosaic reused.", 1, 1)
                return
            raise RuntimeError("Non-NSFW cleanup mosaics are produced only by the idle detector worker; use another ready model while it rebuilds.")
        signature = str(getattr(chunk, "signature", ""))
        lock = self._mosaic_lock_for(mode, signature)
        with lock:
            if mode == "original":
                existing = [Path(path) for path in getattr(chunk, "mosaics", []) if Path(path).is_file()]
            else:
                existing = self.review.mosaic_outputs(chunk)
                if existing:
                    chunk.mosaics = list(existing)
            layout_valid = bool(self._embedded_layout_entry(mode, chunk, existing) or self._layout_cache_entry(mode, chunk, existing)) if existing else False
            if existing and layout_valid and not force:
                progress("Existing mobile mosaic reused.", 1, 1)
                return
            effective_force = bool(force or (existing and not layout_valid))
            with self.heavy_io_lock:
                # Another worker may have completed the mosaic while this worker waited.
                if mode == "original":
                    existing = [Path(path) for path in getattr(chunk, "mosaics", []) if Path(path).is_file()]
                else:
                    existing = self.review.mosaic_outputs(chunk)
                    if existing:
                        chunk.mosaics = list(existing)
                layout_valid = bool(self._embedded_layout_entry(mode, chunk, existing) or self._layout_cache_entry(mode, chunk, existing)) if existing else False
                if existing and layout_valid and not force:
                    progress("Existing mobile mosaic reused.", 1, 1)
                    return
                if force or (existing and not layout_valid):
                    removed, cleanup_errors = self._purge_mosaic_artifacts_for_chunk_sources(mode, chunk)
                    if removed:
                        append_log(f"Purged {removed} stale/duplicate mosaic artifact(s) before regenerating {mode} chunk {signature}.")
                    if cleanup_errors:
                        append_log("Mosaic artifact cleanup warnings: " + " | ".join(cleanup_errors[:20]))
                    chunk.mosaics = []
                if mode == "original":
                    progress("Preparing mosaic…", 0, max(1, len(chunk.files)))
                    ok, message, outputs = self.mosaic.generate_mosaic(
                        chunk,
                        dict(self.mosaic_settings),
                        self.mosaic_duration_cache,
                        self.mosaic_manifest,
                        threading.Event(),
                        [None],
                        lambda current, total, text: progress(str(text), int(current), int(total)),
                        self.session_generated_signatures,
                        force_regenerate=effective_force,
                    )
                else:
                    if effective_force:
                        self.review.delete_chunk_mosaics(chunk)
                    progress("Preparing exact metadata for this Review chunk only…", 0, max(1, len(chunk.files)))
                    self.review.prepare_chunk_durations(
                        chunk,
                        dict(self.review_settings),
                        self.review_duration_cache,
                        lambda current, total, text: progress(str(text), int(current), int(total)),
                    )
                    progress("Preparing Review mosaic…", 0, max(1, len(chunk.files)))
                    ok, message, outputs = self.review.generate_mosaic(
                        chunk,
                        dict(self.review_settings),
                        self.review_duration_cache,
                        lambda current, total, text: progress(str(text), int(current), int(total)),
                    )
                if not ok:
                    raise RuntimeError(message)
                chunk.mosaics = list(outputs)
                self._record_layout_cache(mode, chunk, outputs)

    def ensure_current_mosaic(
        self,
        queue_data: Dict[str, Any],
        force: bool,
        progress: Callable[[str, int, int], None],
    ) -> None:
        self.ensure_chunk_mosaic(queue_data, self.current_chunk(queue_data), force, progress)

    def _chunk_mosaic_ready(self, queue_data: Dict[str, Any], chunk: Any) -> bool:
        mode = queue_data["mode"]
        if mode in {"original", "cleanup"}:
            outputs = [Path(path) for path in getattr(chunk, "mosaics", []) if Path(path).is_file()]
        else:
            outputs = self.review.mosaic_outputs(chunk)
            if outputs:
                chunk.mosaics = list(outputs)
        return bool(outputs and (self._embedded_layout_entry(mode, chunk, outputs) or self._layout_cache_entry(mode, chunk, outputs)))

    def prefetch_status(self, queue_data: Dict[str, Any]) -> Dict[str, Any]:
        status = queue_data.get("prefetch_status", {})
        return dict(status) if isinstance(status, dict) else {}

    def background_settings_for_mode(self, mode: str) -> Dict[str, Any]:
        raw = self.config.get("background_mosaics", {})
        if isinstance(raw, dict) and ("original" in raw or "review" in raw):
            selected = raw.get("review" if mode in {"review", "deletion"} else "original", {})
            return dict(selected) if isinstance(selected, dict) else {}
        return dict(raw) if isinstance(raw, dict) else {}

    def schedule_prefetch(self, queue_id: str, ignore_idle: bool = False) -> None:
        """Keep the active model's configured future-mosaic window full.

        `upcoming_count` counts mosaics *after* the current chunk. The worker
        repeatedly re-snapshots the queue, so if the user submits the current
        chunk while a refill is already running, the newly exposed tail chunk is
        pulled into the same refill pass instead of waiting for another manual
        request. Only the currently visible queue may run automatically.
        """
        if not ignore_idle:
            if not self.sort_session_active():
                return
            with self.lock:
                if str(self.active_sort_queue_id or "") != str(queue_id):
                    return
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            return
        settings = self.background_settings_for_mode(str(queue_data.get("mode", "original")))
        if not bool(settings.get("enabled", True)) and not ignore_idle:
            queue_data["prefetch_status"] = {
                "state": "idle", "ready": 0, "target": 0,
                "message": "Automatic look-ahead is disabled for this mode.",
            }
            return
        target = max(0, int(settings.get("upcoming_count", 3)))
        if target <= 0:
            queue_data["prefetch_status"] = {
                "state": "idle", "ready": 0, "target": 0,
                "message": "Automatic look-ahead is set to 0 upcoming mosaics.",
            }
            return
        with self.lock:
            current_future = self.prefetch_futures.get(queue_id)
            if current_future is not None and not current_future.done():
                return

        def active_queue_interactive_running() -> bool:
            self._prune_finished_scheduler_futures()
            with self.lock:
                prefix = str(queue_id) + ":"
                return any(
                    future is not None and not future.done() and str(key).startswith(prefix)
                    for key, future in self.interactive_futures.items()
                )

        def set_prefetch(state: str, ready: int, window_target: int, message: str) -> None:
            with self.lock:
                current = self.queues.get(queue_id)
                if current is not None:
                    current["prefetch_status"] = {
                        "state": state, "ready": int(ready), "target": int(window_target), "message": str(message),
                    }

        def run() -> None:
            while not self.stop_event.is_set():
                with self.lock:
                    current = self.queues.get(queue_id)
                    if current is None:
                        return
                    cancelled = bool(current.get("prefetch_cancel_requested"))
                    chunks = list(current.get("chunks", []))
                    preparing_model = bool(current.get("preparing_model"))
                    active_queue = str(self.active_sort_queue_id or "")
                if cancelled:
                    set_prefetch("cancelled", 0, target, "Stopped because this model is no longer open.")
                    return
                if not ignore_idle:
                    if not self.sort_session_active() or active_queue != str(queue_id):
                        set_prefetch("cancelled", 0, target, "Stopped because this model is no longer open.")
                        return

                if len(chunks) <= 1:
                    if preparing_model:
                        set_prefetch(
                            "running", 0, target,
                            f"Waiting for this model's chunk inventory, then keeping up to {target} mosaic(s) ready ahead.",
                        )
                        time.sleep(0.10)
                        continue
                    set_prefetch("ready", 0, 0, "No additional chunks remain to prepare.")
                    return

                upcoming = chunks[1:target + 1]
                ready_flags = [self._chunk_mosaic_ready(current, chunk) for chunk in upcoming]
                ready = sum(1 for value in ready_flags if value)
                window_target = len(upcoming)

                # A ready subset may have opened before full model indexing
                # finished. Do not declare the smaller temporary window full;
                # stay attached until indexing reveals the real remaining chunks.
                if preparing_model and window_target < target and ready >= window_target:
                    set_prefetch(
                        "running", ready, target,
                        f"Look-ahead {ready}/{target} ready; waiting for remaining chunk metadata.",
                    )
                    time.sleep(0.10)
                    continue

                if ready >= window_target:
                    set_prefetch(
                        "ready", ready, window_target,
                        f"Look-ahead full: {ready}/{window_target} upcoming mosaics ready.",
                    )
                    return

                # The current mosaic always wins. Rather than abandoning the
                # refill attempt, wait briefly and continue as soon as that one
                # interactive mosaic is ready.
                if not ignore_idle and active_queue_interactive_running():
                    set_prefetch(
                        "paused_priority", ready, window_target,
                        f"Look-ahead {ready}/{window_target} ready; waiting for the current mosaic.",
                    )
                    time.sleep(0.10)
                    continue

                if not ignore_idle and bool(settings.get("idle_only", True)):
                    idle = computer_idle_seconds()
                    required = max(0.0, float(settings.get("idle_minutes", 10)) * 60.0)
                    if idle is not None and idle < required:
                        set_prefetch(
                            "waiting_idle", ready, window_target,
                            f"Look-ahead {ready}/{window_target} ready; waiting about {max(0, int(required - idle))} more idle seconds to refill.",
                        )
                        return

                missing_index = next((i for i, value in enumerate(ready_flags) if not value), None)
                if missing_index is None:
                    continue
                chunk = upcoming[missing_index]
                display_index = missing_index + 1
                try:
                    self.ensure_chunk_mosaic(
                        current, chunk, False,
                        lambda text, current_step=0, total_steps=0, i=display_index: set_prefetch(
                            "running", ready, window_target, f"Upcoming {i}/{window_target}: {text}"
                        ),
                    )
                except Exception as exc:
                    append_log(f"Background mosaic failed for {current.get('model')}: {exc}")
                    set_prefetch("error", ready, window_target, f"Future mosaic error: {exc}")
                    return
                # Loop and re-snapshot. If the current chunk was submitted while
                # generation ran, this immediately exposes and fills the new tail.

        future = self.prefetch_executor.submit(run)
        with self.lock:
            self.prefetch_futures[queue_id] = future

    def prefetch_task(self, queue_id: str) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            mode = str(queue_data.get("mode", "original"))
            model_name = str(queue_data.get("model", ""))
            selected_drives = {str(value).upper() for value in queue_data.get("drives", []) if str(value)}
            settings = self.background_settings_for_mode(mode)

            # Reconcile already-existing on-disk mosaics first.  Previously this
            # button only looked at the queue object created when the model was
            # opened. If that queue happened to contain one stale ready-index row,
            # it incorrectly reported 0 upcoming even when many sidecar-backed
            # mosaics were physically present.
            repaired = self._recover_partial_snapshot_from_existing_mosaics(mode, model_name, selected_drives)
            if isinstance(repaired, dict):
                ready_chunks = []
                for row in repaired.get("chunks", []):
                    if not isinstance(row, dict) or not row.get("ready"):
                        continue
                    try:
                        ready_chunks.append(self._deserialize_chunk(mode, row))
                    except Exception:
                        continue
                self._merge_ready_chunks_into_queue(queue_id, ready_chunks, len(repaired.get("chunks", [])))
                with self.lock:
                    queue_data = self.queues.get(queue_id) or queue_data

            target = max(0, int(settings.get("upcoming_count", 3)))
            upcoming = list(queue_data.get("chunks", []))[1:target + 1]
            ready = 0
            if not upcoming:
                known_total = int(queue_data.get("known_total_chunks", len(queue_data.get("chunks", []))) or 0)
                if known_total > len(queue_data.get("chunks", [])):
                    queue_data["prefetch_status"] = {
                        "state": "running", "ready": len(queue_data.get("chunks", [])), "target": known_total,
                        "message": "Existing mosaics were reindexed; remaining chunk metadata is still being reconciled by the sort-first worker.",
                    }
                    return {"ready": 0, "target": max(0, known_total - 1), "message": queue_data["prefetch_status"]["message"]}
            for index, chunk in enumerate(upcoming, start=1):
                if bool(queue_data.get("prefetch_cancel_requested")):
                    raise TaskCancelled("Generate Ahead stopped because this model is no longer open.")
                progress(f"Preparing future mosaic {index}/{len(upcoming)}…", index - 1, len(upcoming))
                self.ensure_chunk_mosaic(queue_data, chunk, False, progress)
                ready += 1
            queue_data["prefetch_status"] = {
                "state": "ready", "ready": ready, "target": len(upcoming),
                "message": f"{ready}/{len(upcoming)} upcoming mosaics are ready.",
            }
            return {"ready": ready, "target": len(upcoming), "message": queue_data["prefetch_status"]["message"]}
        return self.create_task("Generate future mosaics", worker)

    def start_recu_for_queue(self, queue_id: str, force: bool = False) -> Optional[str]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if queue_data is None or queue_data.get("mode") not in {"original", "review"}:
                return None
            access = self.recu_status()
            if not access.get("enabled"):
                queue_data["recu_status"] = "disabled"
                return None
            if not access.get("session_captured") and not access.get("browser_running"):
                queue_data["recu_status"] = "needs_verification"
                queue_data["recu_error"] = "Verify Recu in Mobile Settings to load kink markers."
                return None
            if queue_data.get("recu_status") in {"loading", "ready"} and not force:
                return queue_data.get("recu_task_id")
            queue_data["recu_status"] = "loading"
            queue_data["recu_error"] = ""

        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            settings = dict(self.mosaic_settings)
            recu_cfg = dict(self.config.get("recu", {}) if isinstance(self.config.get("recu"), dict) else {})
            settings["recu_enabled"] = bool(recu_cfg.get("enabled", True))
            settings["recu_scrape_comments"] = False
            settings["recu_concurrent_requests"] = max(1, min(24, int(recu_cfg.get("concurrent_requests", 12) or 12)))
            settings["recu_request_timeout_seconds"] = max(5, int(recu_cfg.get("request_timeout_seconds", settings.get("recu_request_timeout_seconds", 20)) or 20))
            progress(f"Loading Recu kinks for {queue_data['model']}…", 0, 0)

            # Prefer the verified Chrome network stack when that dedicated
            # browser is running. Network.loadNetworkResource keeps the real
            # browser credentials without visibly navigating each listing page;
            # the bridge itself falls back to lightweight verified navigation
            # if Chrome refuses background resource loading. If Chrome is not
            # currently running, retain the established captured-cookie HTTP
            # path rather than forcing a browser window open.
            browser_running = self.recu_browser.is_running()
            fetch_one = self.recu_browser.fetch_html if browser_running else None
            fetch_many = self.recu_browser.fetch_many if browser_running else None
            data = self.mosaic.scrape_recu_model(
                queue_data["model"],
                settings,
                self.recu_cache,
                threading.Event(),
                force=force,
                progress=lambda text: progress(str(text), 0, 0),
                fetch_html_fn=fetch_one,
                fetch_many_fn=fetch_many,
            )
            auth_like_errors = [err for err in data.errors if self._recu_error_looks_auth_related(err)]
            if not browser_running and auth_like_errors:
                # Do not turn raw/captured-cookie rejection into a re-auth loop.
                # Confirm against the exact persisted Chrome profile first. If
                # that browser navigation succeeds, authentication is still good
                # and the fast transport alone was the problem.
                try:
                    self._ensure_recu_browser_navigation(
                        str(settings.get("recu_base_url", "https://recu.me")),
                        max(20, int(settings.get("recu_request_timeout_seconds", 20)) + 15),
                    )
                    append_log(
                        "Recu raw-HTTP scan reported auth-looking errors, but verified browser navigation remained valid; "
                        "not requesting re-authentication."
                    )
                except RecuReauthRequired:
                    raise
                except Exception as exc:
                    raise RuntimeError(
                        f"Recu background transport failed and the verification-browser fallback could not be confirmed: {exc}"
                    ) from exc
            chunks = list(queue_data.get("chunks", []))
            background = self.background_settings_for_mode(str(queue_data.get("mode", "original")))
            window_count = max(1, int(background.get("upcoming_count", 3)) + 1)
            window = chunks[:window_count]
            tolerance = int(settings.get("recu_match_tolerance_seconds", 120))
            max_hours = float(settings.get("recu_lazy_max_duration_hours", 24))
            ids = self.mosaic.candidate_recu_video_ids_for_chunks(
                window, data, tolerance, max_hours, allowed_kinds={"kink"}
            )
            if ids:
                self.mosaic.resolve_recu_moment_timings(
                    data.moments, data.video_meta, settings, threading.Event(), data.errors,
                    progress=lambda text: progress(str(text), 0, 0),
                    only_video_ids=ids,
                    fetch_html_fn=fetch_one,
                )
                try:
                    payload = self.mosaic.recu_data_to_payload(data)
                    payload["local_timezone"] = str(settings.get("recu_local_timezone", "America/New_York"))
                    self.recu_cache.set_raw(queue_data["model"], payload)
                except Exception as exc:
                    append_log(f"Could not persist mobile Recu timing: {exc}")
            with self.lock:
                queue_data["recu_data"] = data
                queue_data["recu_status"] = "ready" if data.moments else "empty"
                queue_data["recu_error"] = "; ".join(data.errors[:3])
            memory = data.scan_memory if isinstance(getattr(data, "scan_memory", None), dict) else {}
            pages = int(memory.get("last_pages_fetched", 0) or 0)
            new_sessions = int(memory.get("last_new_sessions", 0) or 0)
            boundaries = int(memory.get("last_stop_boundaries", 0) or 0)
            return {
                "moments": len(data.moments),
                "new_sessions": new_sessions,
                "pages_fetched": pages,
                "stop_boundaries": boundaries,
                "errors": data.errors[:5],
                "message": (
                    f"Loaded {len(data.moments)} Recu marker(s); "
                    f"{new_sessions} new session(s) from {pages} page request(s)."
                ),
            }

        def guarded_worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            try:
                return worker(progress)
            except RecuReauthRequired as exc:
                with self.lock:
                    queue_data["recu_status"] = "needs_verification"
                    queue_data["recu_error"] = str(exc)
                raise
            except Exception as exc:
                with self.lock:
                    queue_data["recu_status"] = "error"
                    queue_data["recu_error"] = str(exc)
                raise

        task_id = self.create_task("Load Recu kinks", guarded_worker)
        with self.lock:
            queue_data["recu_task_id"] = task_id
        return task_id

    def recu_markers_for_chunk(self, queue_data: Dict[str, Any], chunk: Any) -> Dict[str, Any]:
        data = queue_data.get("recu_data")
        if data is None:
            return {
                "status": queue_data.get("recu_status", "not_started"),
                "error": queue_data.get("recu_error", ""),
                "segments": {},
            }
        tolerance = int(self.mosaic_settings.get("recu_match_tolerance_seconds", 120))
        moments = [
            item for item in self.mosaic.match_recu_moments(chunk, data, tolerance)
            if item.kind == "kink"
        ]
        segments: Dict[str, List[Dict[str, Any]]] = {}
        unmatched: List[Dict[str, Any]] = []
        for item in moments:
            segment = self.mosaic.chunk_event_segment(chunk, item.event_local)
            location = self.mosaic.chunk_event_location(chunk, item.event_local)
            marker = {
                "name": item.kink_name or item.kink_slug or "Kink",
                "slug": item.kink_slug,
                "emoji": item.emoji or "🔥",
                "event_time": item.event_local.isoformat(),
                "location": self.mosaic.locate_chunk_event(chunk, item.event_local),
                "offset_seconds": float(location[2]) if location is not None else 0.0,
                "url": item.url,
            }
            if segment is None:
                unmatched.append(marker)
            else:
                segments.setdefault(str(segment - 1), []).append(marker)
        return {
            "status": queue_data.get("recu_status", "ready"),
            "error": queue_data.get("recu_error", ""),
            "segments": segments,
            "unmatched": unmatched,
        }

    def _chunk_source_paths(self, chunk: Any) -> Set[str]:
        return {normalized(Path(str(video.path))) for video in getattr(chunk, "files", []) if getattr(video, "path", None)}

    def _purge_mosaic_artifacts_for_chunk_sources(self, mode: str, chunk: Any) -> Tuple[int, List[str]]:
        """Delete stale mosaic sets that represent this exact source-file set.

        Chunk indices can shift after new recordings arrive, so filename/base alone
        is not a safe identity.  Sidecar source lists are authoritative.
        """
        expected = self._chunk_source_paths(chunk)
        removed = 0; errors: List[str] = []
        folders: Set[Path] = set()
        if mode == "original":
            folders.add(Path(str(getattr(chunk, "folder", ""))) / self.mosaic.MOSAIC_DIRNAME)
            patterns = ("*.sources.json",)
        else:
            folders.add(Path(str(getattr(chunk, "model_folder", getattr(chunk, "folder", "")))) / self.review.MOSAIC_DIRNAME)
            patterns = ("*.json",)
        for output_dir in folders:
            if not output_dir.is_dir():
                continue
            for pattern in patterns:
                for sidecar in output_dir.glob(pattern):
                    try:
                        raw = load_json(sidecar, {})
                        if not isinstance(raw, dict): continue
                        rows = raw.get("files", []) if isinstance(raw.get("files", []), list) else []
                        represented = {normalized(Path(str(row.get("path", "")))) for row in rows if isinstance(row, dict) and row.get("path")}
                        sig_match = str(raw.get("signature", "")) == str(getattr(chunk, "signature", ""))
                        sources_match = bool(expected) and represented == expected
                        if not (sig_match or sources_match): continue
                        candidates = [Path(str(value)) for value in raw.get("outputs", []) if str(value)]
                        candidates.append(sidecar)
                        for candidate in candidates:
                            try:
                                if candidate.exists(): candidate.unlink(); removed += 1
                            except Exception as exc:
                                errors.append(f"{candidate.name}: {exc}")
                    except Exception as exc:
                        errors.append(f"{sidecar.name}: {exc}")
        if mode == "original":
            try: self.mosaic_manifest.forget(str(getattr(chunk, "signature", "")))
            except Exception: pass
        else:
            try:
                extra_removed, extra_errors = self.review.delete_chunk_mosaics(chunk)
                removed += int(extra_removed); errors.extend(extra_errors)
            except Exception as exc:
                errors.append(str(exc))
        with self.lock:
            self.validated_layout_cache.clear()
            self.layout_cache.get("layouts", {}).pop(str(getattr(chunk, "signature", "")), None)
        try: atomic_write_json(LAYOUT_CACHE_PATH, self.layout_cache)
        except Exception: pass
        return removed, errors

    def rebuild_current_chunk_task(self, queue_id: str) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            mode = str(queue_data.get("mode", ""))
            if mode not in {"original", "review"}:
                raise RuntimeError("Chunk reconciliation is available for Originals and Review queues.")
            old_chunk = self.current_chunk(queue_data)
            if old_chunk is None:
                return {"queue_id": queue_id, "done": True}
            model = str(queue_data.get("model", "")); drives = set(queue_data.get("drives", []))
            old_paths = self._chunk_source_paths(old_chunk)
            progress("Re-scanning this model from disk and rebuilding chunk membership…", 0, 4)
            folders = self.model_folders(mode, model, drives)
            if not folders:
                raise RuntimeError("The model folders are no longer present on the selected drives.")
            fresh_chunks = self._build_chunks_for_library_model(mode, model, folders, lambda msg: progress(str(msg), 1, 4))
            if not fresh_chunks:
                raise RuntimeError("No current video files were found for this model after the disk re-check.")

            def score(candidate: Any) -> Tuple[int, float, int]:
                current = self._chunk_source_paths(candidate)
                overlap = len(old_paths & current)
                try:
                    temporal = max(0.0, min(old_chunk.end.timestamp(), candidate.end.timestamp()) - max(old_chunk.start.timestamp(), candidate.start.timestamp()))
                except Exception:
                    temporal = 0.0
                return overlap, temporal, int(getattr(candidate, "source_bytes", 0))
            fresh = max(fresh_chunks, key=score)
            if score(fresh)[0] == 0 and score(fresh)[1] <= 0:
                raise RuntimeError("The previous chunk no longer overlaps any current disk chunk; return to Models and reopen the model instead.")
            fresh_paths = self._chunk_source_paths(fresh)
            missing = sorted(old_paths - fresh_paths)
            added = sorted(fresh_paths - old_paths)

            progress("Removing stale/duplicate mosaics tied to this source set…", 2, 4)
            removed_old, purge_errors = self._purge_mosaic_artifacts_for_chunk_sources(mode, old_chunk)
            if str(getattr(fresh, "signature", "")) != str(getattr(old_chunk, "signature", "")):
                removed_new, errors_new = self._purge_mosaic_artifacts_for_chunk_sources(mode, fresh)
                removed_old += removed_new; purge_errors.extend(errors_new)
            fresh.mosaics = []

            with self.lock:
                current_queue = self.queues.get(queue_id)
                if current_queue is None:
                    raise RuntimeError("That review queue closed during reconciliation.")
                tail = []
                fresh_set = self._chunk_source_paths(fresh)
                for candidate in list(current_queue.get("chunks", []))[1:]:
                    candidate_set = self._chunk_source_paths(candidate)
                    # Never leave a second queued chunk representing the same files.
                    if candidate_set == fresh_set or (candidate_set and candidate_set.issubset(fresh_set)):
                        continue
                    tail.append(candidate)
                current_queue["chunks"] = [fresh] + tail
                current_queue["drafts"].pop(str(getattr(old_chunk, "signature", "")), None)
                current_queue["initial_count"] = max(1, len(current_queue["history"]) + len(current_queue["chunks"]))
                current_queue["known_total_chunks"] = max(len(fresh_chunks), len(current_queue["chunks"]))

            progress("Generating one fresh authoritative mosaic + hit map…", 3, 4)
            self.ensure_chunk_mosaic(queue_data, fresh, True, lambda msg, cur=0, total=0: progress(str(msg), 3, 4))
            # Persist the fully rebuilt model snapshot so future opens use the
            # reconciled chunks instead of resurrecting old duplicates.
            self._hydrate_chunks_from_ready_snapshot(mode, model, fresh_chunks)
            for index, candidate in enumerate(fresh_chunks):
                if str(getattr(candidate, "signature", "")) == str(getattr(fresh, "signature", "")):
                    fresh_chunks[index] = fresh
            model_bytes = sum(int(getattr(item, "source_bytes", 0)) for item in fresh_chunks)
            self._persist_ready_snapshot(mode, model, fresh_chunks, model_bytes, sorted(drives))
            self.invalidate_catalog_views()
            progress("Chunk reconciliation complete.", 4, 4)
            detail = f"Rebuilt from {len(fresh.files)} current file(s); added {len(added)} newly discovered file(s); {len(missing)} prior file(s) were no longer present; removed {removed_old} stale mosaic artifact(s)."
            if purge_errors:
                detail += f" {len(purge_errors)} stale-artifact cleanup warning(s) were logged."
                append_log("Chunk rebuild mosaic cleanup warnings: " + " | ".join(purge_errors[:20]))
            return {"queue_id": queue_id, "message": detail, "added": len(added), "missing": len(missing), "removed_artifacts": removed_old}
        return self.create_task("Rebuild chunk + mosaic", worker)

    def mosaic_task(self, queue_id: str, force: bool = False) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            chunk = self.current_chunk(queue_data)
            if chunk is None:
                return {"queue_id": queue_id, "done": True}
            signature = str(getattr(chunk, "signature", ""))
            self.request_interactive_mosaic(queue_id, force)
            while True:
                current = self.current_chunk(queue_data)
                if current is None or str(getattr(current, "signature", "")) != signature:
                    return {"queue_id": queue_id, "advanced": True}
                status = dict(queue_data.get("current_mosaic_status", {}))
                state = str(status.get("state", "queued"))
                progress(str(status.get("message", "Preparing mosaic…")), int(status.get("current", 0) or 0), int(status.get("total", 0) or 0))
                if state == "ready":
                    return {"queue_id": queue_id}
                if state == "error":
                    raise RuntimeError(str(status.get("message", "Mosaic generation failed.")))
                time.sleep(0.25)
        return self.create_task("Regenerate mosaic" if force else "Generate mosaic", worker)

    def submit_current(self, queue_id: str, payload: Dict[str, Any]) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            chunk = self.current_chunk(queue_data)
            if chunk is None:
                return {"done": True}
            expected_signature = str(payload.get("_chunk_signature", "") or "")
            if expected_signature and expected_signature != str(chunk.signature):
                raise RuntimeError("Those choices belong to the previous mosaic; the current chunk has already changed.")
            action_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
            progress("Recording decisions in the durable PC queue…", 0, 1)
            mode = str(queue_data.get("mode", ""))
            if mode == "cleanup": job = self._enqueue_cleanup_action(queue_data, chunk, action_payload)
            elif mode == "deletion": job = self._enqueue_deletion_restore_action(queue_data, chunk, action_payload)
            else: job = self._enqueue_action_for_chunk(queue_data, chunk, action_payload)
            saved_draft = json.loads(json.dumps(action_payload))
            with self.lock:
                queue_data.setdefault("history", []).append({
                    "kind": "submit",
                    "chunk": chunk,
                    "job_id": job["id"],
                    "draft": saved_draft,
                })
                if queue_data.get("chunks") and queue_data["chunks"][0] is chunk:
                    queue_data["chunks"].pop(0)
                queue_data.get("drafts", {}).pop(getattr(chunk, "signature", ""), None)
            remaining = len(queue_data.get("chunks", []))
            if self.current_chunk(queue_data) is not None:
                self.request_interactive_mosaic(queue_id, False)
                if queue_data.get("mode") in {"original", "review"}:
                    self.schedule_prefetch(queue_id)
                    self.start_recu_for_queue(queue_id, force=False)
            progress("Decisions safely queued. You can continue sorting.", 1, 1)
            return {
                "message": f"Queued this chunk on the PC as job {job['id'][:8]}. You can continue immediately.",
                "remaining": remaining,
                "action_job_id": job["id"],
            }
        return self.create_task("Queue chunk decisions", worker)

    def submit_current_fast(self, queue_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically save a phone decision and advance without task polling."""
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            raise RuntimeError("That review queue no longer exists.")
        chunk = self.current_chunk(queue_data)
        if chunk is None:
            return {"done": True, "current": self.current_payload(queue_id)}
        expected_signature = str(payload.get("_chunk_signature", "") or "")
        if expected_signature and expected_signature != str(chunk.signature):
            raise RuntimeError("Those choices belong to the previous mosaic; the current chunk has already changed.")
        action_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
        mode = str(queue_data.get("mode", ""))
        if mode == "cleanup": job = self._enqueue_cleanup_action(queue_data, chunk, action_payload)
        elif mode == "deletion": job = self._enqueue_deletion_restore_action(queue_data, chunk, action_payload)
        else: job = self._enqueue_action_for_chunk(queue_data, chunk, action_payload)
        saved_draft = json.loads(json.dumps(action_payload))
        with self.lock:
            queue_data.setdefault("history", []).append({
                "kind": "submit", "chunk": chunk, "job_id": job["id"], "draft": saved_draft,
            })
            if queue_data.get("chunks") and queue_data["chunks"][0] is chunk:
                queue_data["chunks"].pop(0)
            queue_data.get("drafts", {}).pop(getattr(chunk, "signature", ""), None)
        if self.current_chunk(queue_data) is not None:
            self.request_interactive_mosaic(queue_id, False)
            if queue_data.get("mode") in {"original", "review"}:
                self.schedule_prefetch(queue_id)
                self.start_recu_for_queue(queue_id, force=False)
        return {
            "queued": True,
            "message": f"Queued on the PC as job {job['id'][:8]}. You can keep sorting.",
            "action_job_id": job["id"],
            "current": self.current_payload(queue_id),
        }

    def skip_current_fast(self, queue_id: str) -> Dict[str, Any]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            chunks = queue_data.get("chunks", [])
            if len(chunks) > 1:
                skipped = chunks.pop(0)
                chunks.append(skipped)
                queue_data.setdefault("history", []).append({"kind": "skip", "chunk": skipped})
            elif len(chunks) == 1:
                return {"message": "This is the only remaining chunk.", "current": self.current_payload(queue_id)}
        self.request_interactive_mosaic(queue_id, False)
        if queue_data.get("mode") in {"original", "review"}:
            self.schedule_prefetch(queue_id)
        return {"message": "Skipped for now.", "current": self.current_payload(queue_id)}

    def skip_current(self, queue_id: str) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
                if queue_data is None:
                    raise RuntimeError("That review queue no longer exists.")
                chunks = queue_data.get("chunks", [])
                if len(chunks) > 1:
                    skipped = chunks.pop(0)
                    chunks.append(skipped)
                    queue_data.setdefault("history", []).append({"kind": "skip", "chunk": skipped})
                elif len(chunks) == 1:
                    return {"remaining": 1, "message": "This is the only remaining chunk."}
            self.request_interactive_mosaic(queue_id, False)
            if queue_data.get("mode") in {"original", "review"}:
                self.schedule_prefetch(queue_id)
            return {"remaining": len(queue_data.get("chunks", [])), "message": "Skipped for now."}
        return self.create_task("Skip chunk", worker)

    def _find_action_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for job in self.action_queue.get("jobs", []):
                if isinstance(job, dict) and str(job.get("id", "")) == str(job_id):
                    return job
        return None

    def _undo_action_job(self, job: Dict[str, Any]) -> str:
        """Restore files moved by one mobile submit without overwriting anything.

        Queued/retrying work is cancelled first. If the worker is already in a
        file operation, Back waits for that current operation, then reverses any
        destinations that were actually created.
        """
        with self.lock:
            status = str(job.get("status", ""))
            if status in {"queued", "retrying", "blocked"}:
                job["status"] = "cancelled"
                job["message"] = "Cancelled by Back before/after partial processing."
            elif status == "running":
                job["status"] = "undo_requested"
                job["message"] = "Back requested; finishing the current file operation before restoring files."
        self._save_action_queue()

        restored = 0
        already = 0
        errors: List[str] = []
        affected_deletion: Set[Path] = set()
        with self.heavy_io_lock:
            for item in reversed(job.get("files", [])):
                if not isinstance(item, dict):
                    continue
                decision = str(item.get("decision", ""))
                if decision == "Leave for review":
                    continue
                if job.get("mode") == "cleanup" and decision == "KEEP_CLEANUP":
                    self._set_cleanup_reviewed(item, False)
                    already += 1
                    continue
                source = Path(str(item.get("source", "")))
                destination = self._queued_destination(job, item)
                if destination is None:
                    continue
                for candidate in (source, destination):
                    deletion_folder = self._deletion_model_folder_for_path(candidate)
                    if deletion_folder is not None:
                        affected_deletion.add(deletion_folder)
                try:
                    if source.exists():
                        if destination.exists():
                            errors.append(f"Both original and destination exist; refused to overwrite either: {source.name}")
                        else:
                            already += 1
                        continue
                    if not destination.exists():
                        errors.append(f"Neither original nor moved destination exists: {source.name}")
                        continue
                    source.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(destination, source)
                    except OSError:
                        shutil.move(str(destination), str(source))
                    restored += 1
                except Exception as exc:
                    errors.append(f"{source.name}: {exc}")

            if str(job.get("kind", "")) == "review_frame_cuts":
                # Frame-cut outputs are uniquely named with this durable job id.
                # Back means restore the exact pre-submit state, so remove only
                # outputs that this job itself created; never touch a generic
                # user clip or a filename without the job token.
                token = str(job.get("id", ""))[:8]
                for clip in job.get("clips", []):
                    if not isinstance(clip, dict):
                        continue
                    output = Path(str(clip.get("output", "")))
                    if not token or token not in output.name:
                        continue
                    try:
                        if output.is_file():
                            output.unlink()
                    except Exception as exc:
                        errors.append(f"Could not remove generated frame-cut output {output.name}: {exc}")
                    # Also clear only this job's known temporary output name.
                    temp_output = output.with_name(f".{output.stem}.{token}.part.mp4")
                    try:
                        if temp_output.is_file():
                            temp_output.unlink()
                    except Exception as exc:
                        errors.append(f"Could not remove temporary frame-cut output {temp_output.name}: {exc}")

            if job.get("mode") == "original":
                folder = Path(str(job.get("folder", "")))
                chunk_key = str(job.get("chunk_key", ""))
                if folder.is_dir() and chunk_key:
                    try:
                        state = self.mosaic.load_review_state(folder)
                        reviewed = state.setdefault("reviewed", {})
                        reviewed.pop(chunk_key, None)
                        self.mosaic.save_review_state(folder, state)
                    except Exception as exc:
                        errors.append(f"Could not restore original review-state marker: {exc}")

        if affected_deletion:
            try:
                self._refresh_deletion_catalog_folders(affected_deletion)
            except Exception as exc:
                append_log(f"Could not live-refresh deletion catalog after undo: {exc}")

        if errors:
            with self.lock:
                job["status"] = "blocked"
                job["error"] = "; ".join(errors[:8])
                job["message"] = "Back could not safely restore every file."
            self._save_action_queue()
            raise RuntimeError(job["error"] )
        with self.lock:
            job["status"] = "undone"
            job["error"] = ""
            job["message"] = f"Undone by Back: restored {restored}; already at original location {already}."
            job["completed_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_action_queue()
        return str(job["message"])

    def back_current_fast(self, queue_id: str) -> Dict[str, Any]:
        """Restore the prior UI chunk immediately; queue any heavy undo on the PC."""
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if queue_data is None:
                raise RuntimeError("That review queue no longer exists.")
            history = queue_data.setdefault("history", [])
            if not history:
                return {"message": "There is no previous chunk in this session.", "current": self.current_payload(queue_id)}
            entry = history.pop()
            chunk = entry.get("chunk")
        if chunk is None:
            raise RuntimeError("Previous-chunk history was incomplete.")
        signature = str(getattr(chunk, "signature", ""))
        message = "Returned to the previous chunk."
        if str(entry.get("kind", "")) == "submit":
            job = self._find_action_job(str(entry.get("job_id", "")))
            if job is not None:
                status = str(job.get("status", ""))
                if status in {"queued", "retrying", "blocked"}:
                    with self.lock:
                        job["status"] = "cancelled"
                        job["message"] = "Cancelled instantly by Back before filesystem work."
                    message = "Previous queued decision cancelled before filesystem work."
                    self._save_action_queue()
                elif status != "undone":
                    with self.lock:
                        if status == "running":
                            job["status"] = "undo_requested"
                            job["message"] = "Back requested; undo is queued immediately after the current file operation."
                        undo = {
                            "id": secrets.token_urlsafe(12), "kind": "undo_chunk", "status": "queued",
                            "mode": str(job.get("mode", "")), "model": str(job.get("model", "")),
                            "target_job_id": str(job.get("id", "")), "chunk_signature": signature,
                            "attempts": 0, "created_at": datetime.now().isoformat(timespec="seconds"),
                            "message": "Undo queued by Back.", "error": "",
                        }
                        self.action_queue.setdefault("jobs", []).append(undo)
                    self._save_action_queue(); self.action_wakeup.set()
                    message = "Back restored the chunk now; any completed file moves are being undone on the PC queue."
            draft = entry.get("draft", {})
            with self.lock:
                queue_data.setdefault("drafts", {})[signature] = draft if isinstance(draft, dict) else {}
        with self.lock:
            chunks = queue_data.setdefault("chunks", [])
            chunks[:] = [candidate for candidate in chunks if str(getattr(candidate, "signature", "")) != signature]
            chunks.insert(0, chunk)
        self.request_interactive_mosaic(queue_id, False)
        if queue_data.get("mode") in {"original", "review"}:
            self.schedule_prefetch(queue_id)
            self.start_recu_for_queue(queue_id, force=False)
        return {"message": message, "current": self.current_payload(queue_id)}

    def back_current(self, queue_id: str) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            with self.lock:
                queue_data = self.queues.get(queue_id)
                if queue_data is None:
                    raise RuntimeError("That review queue no longer exists.")
                history = queue_data.setdefault("history", [])
                if not history:
                    return {"message": "There is no previous chunk in this session.", "remaining": len(queue_data.get("chunks", []))}
                entry = history.pop()
                chunk = entry.get("chunk")
            if chunk is None:
                raise RuntimeError("Previous-chunk history was incomplete.")
            signature = str(getattr(chunk, "signature", ""))
            kind = str(entry.get("kind", ""))
            message = "Returned to the previous chunk."
            if kind == "submit":
                job = self._find_action_job(str(entry.get("job_id", "")))
                if job is not None:
                    progress("Cancelling/restoring the previous sorting instruction…", 0, 1)
                    message = self._undo_action_job(job)
                draft = entry.get("draft", {})
                with self.lock:
                    queue_data.setdefault("drafts", {})[signature] = draft if isinstance(draft, dict) else {}
            with self.lock:
                chunks = queue_data.setdefault("chunks", [])
                chunks[:] = [candidate for candidate in chunks if str(getattr(candidate, "signature", "")) != signature]
                chunks.insert(0, chunk)
            self.request_interactive_mosaic(queue_id, False)
            if queue_data.get("mode") in {"original", "review"}:
                self.schedule_prefetch(queue_id)
                self.start_recu_for_queue(queue_id, force=False)
            progress("Previous chunk restored.", 1, 1)
            return {"message": message, "remaining": len(queue_data.get("chunks", []))}
        return self.create_task("Back to previous chunk", worker)

    def prepare_model_permanent_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a fast model-level confirmation without recursively scanning files.

        Exact enumeration and path validation happen later inside the durable PC
        action worker. This keeps the iPhone confirmation path effectively instant.
        """
        raw_models = payload.get("models", [])
        requested = {str(value).strip().casefold() for value in raw_models if str(value).strip()}
        drives = {str(value).upper() for value in payload.get("drives", []) if str(value).strip()}
        if not requested:
            raise RuntimeError("Select at least one model to permanently delete.")
        selected_rows: List[Dict[str, Any]] = []
        with self.lock:
            rows = list(self.catalog.get("deletion", []))
        for row in rows:
            if str(row.get("name", "")).strip().casefold() not in requested:
                continue
            drive = str(row.get("drive", "")).upper()
            if drives and drive not in drives:
                continue
            folder = Path(str(row.get("folder", "")))
            if folder.is_dir() and self._is_inside_deletion_root(folder):
                selected_rows.append(dict(row))
        if not selected_rows:
            raise RuntimeError("The selected deletion models are no longer available. Rescan the disks.")
        model_names = sorted({str(row.get("name", "")) for row in selected_rows}, key=str.casefold)
        total_bytes = sum(int(row.get("bytes", 0) or 0) for row in selected_rows)
        token = secrets.token_urlsafe(20)
        self.delete_confirmations[token] = {
            "kind": "models_fast",
            "model_names": model_names,
            "folders": [str(row.get("folder", "")) for row in selected_rows],
            "catalog_bytes": total_bytes,
            "expires_at": time.time() + 180,
        }
        return {
            "confirmation_token": token,
            "kind": "models",
            "model_count": len(model_names),
            "models": model_names,
            "file_count": None,
            "bytes": total_bytes,
            "size": human_size(total_bytes),
            "expires_seconds": 180,
            "required_phrase": "DELETE",
            "warning": "Deletion will be queued instantly. The PC will enumerate and permanently delete the selected model folders in the background after earlier queued sorting work.",
        }

    def confirm_model_permanent_delete_fast(self, token: str, phrase: str) -> Dict[str, Any]:
        confirmation = self.delete_confirmations.pop(token, None)
        if confirmation is None or float(confirmation.get("expires_at", 0)) < time.time():
            raise RuntimeError("The deletion confirmation expired. Review the selection again.")
        if str(confirmation.get("kind", "")) != "models_fast":
            # Put it back for the legacy file-level path.
            self.delete_confirmations[token] = confirmation
            raise RuntimeError("That confirmation is not a model-level deletion request.")
        if str(phrase).strip() != "DELETE":
            raise RuntimeError("Enter DELETE exactly to confirm permanent deletion.")
        job = {
            "id": secrets.token_urlsafe(12),
            "kind": "permanent_delete_models",
            "status": "queued",
            "mode": "deletion",
            "model": ", ".join(str(value) for value in confirmation.get("model_names", [])),
            "model_names": list(confirmation.get("model_names", [])),
            "folders": list(confirmation.get("folders", [])),
            "catalog_bytes": int(confirmation.get("catalog_bytes", 0) or 0),
            "attempts": 0,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message": "Permanent deletion queued on the PC.",
            "error": "",
        }
        with self.lock:
            self.action_queue.setdefault("jobs", []).append(job)
        self._save_action_queue()
        self.action_wakeup.set()
        return {
            "queued": True,
            "job_id": job["id"],
            "kind": "models",
            "message": f"Queued permanent deletion of {len(job['model_names'])} model(s). You can continue immediately.",
        }

    def prepare_permanent_delete(self, queue_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None or queue_data.get("mode") != "deletion":
            raise RuntimeError("That is not a permanent-deletion queue.")
        chunk = self.current_chunk(queue_data)
        if chunk is None:
            raise RuntimeError("No marked-for-deletion chunk remains.")
        raw = payload.get("delete_indices", [])
        selected = {int(value) for value in raw if str(value).lstrip("-").isdigit()}
        selected = {value for value in selected if 0 <= value < len(chunk.files)}
        if not selected:
            raise RuntimeError("Select at least one file to permanently delete.")
        selected_files = [chunk.files[index] for index in sorted(selected)]
        token = secrets.token_urlsafe(20)
        expires_at = time.time() + 180
        self.delete_confirmations[token] = {
            "kind": "files",
            "queue_id": queue_id,
            "signature": chunk.signature,
            "indices": sorted(selected),
            "paths": [str(item.path) for item in selected_files],
            "expires_at": expires_at,
        }
        return {
            "confirmation_token": token,
            "file_count": len(selected_files),
            "bytes": sum(int(item.size) for item in selected_files),
            "size": human_size(sum(int(item.size) for item in selected_files)),
            "expires_seconds": 180,
            "required_phrase": "DELETE",
            "warning": "This permanently unlinks the selected files. They will not go to another recovery folder.",
        }

    def _deletion_model_folder_for_path(self, path: Path) -> Optional[Path]:
        """Return the top-level MARKED_FOR_DELETION/<physical_model> folder for path."""
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = Path(os.path.abspath(str(path)))
        for _drive, root in configured_deletion_roots(self.roots).items():
            try:
                root_resolved = root.resolve(strict=False)
                relative = resolved.relative_to(root_resolved)
            except Exception:
                continue
            if relative.parts:
                return root / relative.parts[0]
        return None

    def _refresh_deletion_catalog_folders(self, folders: Iterable[Path]) -> None:
        """Incrementally refresh only affected deletion-model rows.

        This keeps the Delete tab live after queued moves/restores/permanent
        deletions without launching a full multi-drive catalog scan.
        """
        normalized_folders: Dict[str, Path] = {}
        for raw in folders:
            folder = self._deletion_model_folder_for_path(Path(raw)) or Path(raw)
            normalized_folders[normalized(folder)] = folder
        if not normalized_folders:
            return

        extensions = parse_extensions(self.review_settings.get("extensions", "mp4,ts"))
        deletion_roots = configured_deletion_roots(self.roots)
        replacements: Dict[str, Optional[Dict[str, Any]]] = {}
        for key, folder in normalized_folders.items():
            drive = drive_label(folder)
            deletion_root = deletion_roots.get(drive)
            if deletion_root is None:
                replacements[key] = None
                continue
            try:
                marked_bytes = recursive_video_bytes(folder, extensions) if folder.is_dir() else 0
            except OSError:
                marked_bytes = 0
            if marked_bytes <= 0:
                replacements[key] = None
            else:
                replacements[key] = {
                    "name": self.mosaic.canonical_model_name(folder.name),
                    "physical_name": folder.name,
                    "folder": str(folder),
                    "root": str(deletion_root),
                    "drive": drive,
                    "bytes": int(marked_bytes),
                }

        with self.lock:
            rows = [
                dict(row) for row in self.catalog.get("deletion", [])
                if isinstance(row, dict) and normalized(Path(str(row.get("folder", "")))) not in replacements
            ]
            rows.extend(row for row in replacements.values() if isinstance(row, dict))
            self.catalog["deletion"] = rows
            updated_at = datetime.now().isoformat(timespec="seconds")
            if self.catalog_status.get("status") == "ready":
                self.catalog_status["progress"] = (
                    f"Catalog live • {len(self.catalog.get('original', [])):,} original, "
                    f"{len(self.catalog.get('review', [])):,} Review, "
                    f"{len(rows):,} marked-for-deletion folders."
                )
                self.catalog_status["updated_at"] = updated_at
            atomic_write_json(CATALOG_PATH, {
                "updated_at": updated_at,
                "roots_file": str(self.roots_path),
                "catalog": self.catalog,
            })

    def _is_inside_deletion_root(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = Path(os.path.abspath(str(path)))
        for root in configured_deletion_roots(self.roots).values():
            try:
                root_resolved = root.resolve(strict=False)
                if os.path.commonpath([str(resolved), str(root_resolved)]) == str(root_resolved):
                    return True
            except Exception:
                continue
        return False

    def _cleanup_empty_deletion_parents(self, path: Path) -> None:
        roots = [root.resolve(strict=False) for root in configured_deletion_roots(self.roots).values()]
        current = path.parent
        while current.exists():
            if any(normalized(current) == normalized(root) for root in roots):
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def confirm_permanent_delete(self, token: str, phrase: str) -> str:
        def worker(progress: Callable[[str, int, int], None]) -> Dict[str, Any]:
            confirmation = self.delete_confirmations.pop(token, None)
            if confirmation is None or float(confirmation.get("expires_at", 0)) < time.time():
                raise RuntimeError("The deletion confirmation expired. Review the selection again.")
            if str(phrase).strip() != "DELETE":
                raise RuntimeError("Enter DELETE exactly to confirm permanent deletion.")

            kind = str(confirmation.get("kind", "files"))
            queue_data: Optional[Dict[str, Any]] = None
            chunk: Any = None
            if kind == "files":
                queue_id = str(confirmation.get("queue_id", ""))
                with self.lock:
                    queue_data = self.queues.get(queue_id)
                if queue_data is None or queue_data.get("mode") != "deletion":
                    raise RuntimeError("The deletion queue no longer exists.")
                chunk = self.current_chunk(queue_data)
                if chunk is None or str(chunk.signature) != str(confirmation.get("signature", "")):
                    raise RuntimeError("The current chunk changed. Review the deletion selection again.")

            paths = [Path(value) for value in confirmation.get("paths", [])]
            deleted = 0
            deleted_bytes = 0
            errors: List[str] = []
            for index, path in enumerate(paths, start=1):
                progress(f"Permanently deleting {index}/{len(paths)}: {path.name}", index - 1, len(paths))
                if not self._is_inside_deletion_root(path):
                    errors.append(f"Safety block: {path} is outside a configured MARKED_FOR_DELETION root.")
                    continue
                try:
                    size = path.stat().st_size if path.exists() else 0
                    path.unlink()
                    deleted += 1
                    deleted_bytes += int(size)
                    self._cleanup_empty_deletion_parents(path)
                except OSError as exc:
                    errors.append(f"{path.name}: {exc}")

            if kind == "files" and queue_data is not None and chunk is not None:
                self.review.delete_chunk_mosaics(chunk)
                with self.lock:
                    if queue_data.get("chunks") and queue_data["chunks"][0] is chunk:
                        queue_data["chunks"].pop(0)
                    queue_data.get("drafts", {}).pop(getattr(chunk, "signature", ""), None)
                remaining = len(queue_data.get("chunks", []))
            else:
                selected_folders = {normalized(Path(value)) for value in confirmation.get("folders", [])}
                with self.lock:
                    refreshed_rows = []
                    for row in self.catalog.get("deletion", []):
                        folder = Path(str(row.get("folder", "")))
                        if normalized(folder) in selected_folders and recursive_video_bytes(
                            folder, parse_extensions(self.review_settings.get("extensions", "mp4,ts"))
                        ) <= 0:
                            continue
                        refreshed_rows.append(row)
                    self.catalog["deletion"] = refreshed_rows
                    cached = load_json(CATALOG_PATH, {})
                    if isinstance(cached, dict):
                        cached["catalog"] = self.catalog
                        cached["updated_at"] = datetime.now().isoformat(timespec="seconds")
                        atomic_write_json(CATALOG_PATH, cached)
                remaining = len(self.catalog_models("deletion", set()))

            message = f"Permanently deleted {deleted} file(s), {human_size(deleted_bytes)}."
            if kind == "models":
                message = f"Deleted {len(confirmation.get('model_names', []))} selected model(s): {deleted} file(s), {human_size(deleted_bytes)}."
            if errors:
                message += f" {len(errors)} file(s) were not deleted."
                append_log("Permanent deletion warnings: " + "; ".join(errors[:20]))
            return {
                "message": message,
                "deleted": deleted,
                "deleted_bytes": deleted_bytes,
                "errors": errors[:20],
                "remaining": remaining,
                "kind": kind,
            }
        return self.create_task("Permanently delete files", worker)

    def _preview_settings(self) -> Dict[str, Any]:
        raw = self.config.get("video_preview", {})
        merged = dict(DEFAULT_CONFIG.get("video_preview", {}))
        if isinstance(raw, dict):
            merged.update(raw)
        return merged

    def preview_exact_duration(self, path: Path, fallback: float = 0.0, allow_fallback: bool = False) -> float:
        """Probe the media itself for preview duration; never trust filename tail tags.

        CTBRec filenames may contain ``_tail_1h35m...`` meaning remaining time
        in the larger recording/session, not the duration of this 15-minute
        segment. Preview scrub limits therefore come only from ffprobe/ffmpeg
        (or this exact-probe cache). A caller may explicitly opt into a fallback
        only for legacy non-interactive uses.
        """
        try:
            stat = path.stat()
        except OSError:
            return max(0.0, float(fallback or 0.0)) if allow_fallback else 0.0
        key = normalized(path)
        signature = (int(stat.st_size), float(stat.st_mtime))
        with self.lock:
            cached = self.preview_duration_cache.get(key)
        if cached and cached[0] == signature[0] and abs(cached[1] - signature[1]) < 0.001:
            return max(0.0, float(cached[2]))

        ffmpeg, ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        duration = 0.0

        if ffprobe is not None:
            try:
                result = subprocess.run(
                    [
                        str(ffprobe), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "format=duration:stream=duration",
                        "-of", "json", str(path),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=8, creationflags=creationflags,
                )
                if result.returncode == 0:
                    payload = json.loads(result.stdout or "{}")
                    candidates: List[float] = []
                    try:
                        candidates.append(float(payload.get("format", {}).get("duration", 0)))
                    except Exception:
                        pass
                    for stream in payload.get("streams", []) or []:
                        try:
                            candidates.append(float(stream.get("duration", 0)))
                        except Exception:
                            pass
                    duration = max((value for value in candidates if math.isfinite(value) and value > 0), default=0.0)
            except Exception:
                pass

        if duration <= 0 and ffmpeg is not None:
            try:
                result = subprocess.run(
                    [str(ffmpeg), "-hide_banner", "-i", str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    timeout=8, creationflags=creationflags,
                )
                match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
                if match:
                    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
            except Exception:
                pass

        if duration > 0 and math.isfinite(duration):
            with self.lock:
                self.preview_duration_cache[key] = (signature[0], signature[1], float(duration))
            return float(duration)
        if allow_fallback and float(fallback or 0.0) > 0:
            return float(fallback)
        return 0.0

    def _preview_hls_key(self, path: Path) -> str:
        try:
            stat = path.stat()
            raw = f"{normalized(path)}\0{int(stat.st_size)}\0{float(stat.st_mtime):.6f}"
        except OSError:
            raw = normalized(path)
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:28]


    def _prune_preview_seekable_cache(self, keep: Optional[Path] = None) -> None:
        """Bound the zero-reencode seekable preview cache by age and total bytes.

        Cached files are derivative MP4 wrappers only; deleting them never touches
        recordings.  Access updates mtime, so the size pass is effectively LRU.
        """
        settings = self._preview_settings()
        max_bytes = max(512 * 1024 * 1024, int(float(settings.get("seekable_cache_max_gb", 8) or 8) * (1024 ** 3)))
        cutoff = time.time() - max(1.0, float(settings.get("cache_hours", 18) or 18)) * 3600.0
        try:
            PREVIEW_SEEKABLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            entries: List[Tuple[float, int, Path]] = []
            for child in PREVIEW_SEEKABLE_CACHE_DIR.glob("*.mp4"):
                try:
                    stat = child.stat()
                except OSError:
                    continue
                if keep is not None and child.resolve() == keep.resolve():
                    entries.append((stat.st_mtime, stat.st_size, child)); continue
                if stat.st_mtime < cutoff:
                    try: child.unlink()
                    except OSError: pass
                    continue
                entries.append((stat.st_mtime, stat.st_size, child))
            total = sum(size for _mtime, size, _child in entries)
            if total <= max_bytes:
                return
            for _mtime, size, child in sorted(entries, key=lambda row: row[0]):
                if total <= max_bytes:
                    break
                if keep is not None:
                    try:
                        if child.resolve() == keep.resolve():
                            continue
                    except OSError:
                        pass
                try:
                    child.unlink(); total -= size
                except OSError:
                    pass
        except OSError:
            pass

    def preview_seekable_copy(self, path: Path, diag_id: str = "") -> Path:
        """Return a cached browser-seekable MP4 made with stream copy when possible.

        Many CTBRec .ts recordings already contain browser-friendly H.264/AAC, but
        Chromium does not reliably open raw MPEG-TS through a normal <video> URL.
        A one-time `-c copy` remux changes only the container, not the encoded media.
        Once materialized, normal HTTP Range reads make scrubbing behave like an
        ordinary MP4 instead of restarting an FFmpeg compatibility pipe on every seek.
        """
        if not path.is_file():
            raise RuntimeError("Preview source file no longer exists.")
        key = self._preview_hls_key(path)
        PREVIEW_SEEKABLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = PREVIEW_SEEKABLE_CACHE_DIR / f"{key}.mp4"
        lock = self.preview_seekable_locks.setdefault(key, threading.Lock())
        with lock:
            if target.is_file():
                try:
                    if target.stat().st_size > 64 * 1024:
                        os.utime(target, None)
                        self.preview_diagnostic_event(diag_id, "seekable_cache_hit", {"bytes": target.stat().st_size})
                        self._prune_preview_seekable_cache(keep=target)
                        return target
                except OSError:
                    pass
            ffmpeg, _ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
            if ffmpeg is None:
                raise RuntimeError("ffmpeg was not found for seekable preview remux.")
            source_duration = self.preview_exact_duration(path)
            temp_target = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp.mp4")
            try:
                temp_target.unlink(missing_ok=True)
            except Exception:
                pass
            command = [
                str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
                "-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?",
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", "-y", str(temp_target),
            ]
            self.preview_diagnostic_event(diag_id, "seekable_cache_build_start", {
                "source_suffix": path.suffix.casefold(), "source_bytes": path.stat().st_size,
                "source_duration": source_duration,
            })
            started = time.perf_counter()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                result = subprocess.run(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    timeout=180, creationflags=creationflags,
                )
            except subprocess.TimeoutExpired as exc:
                try: temp_target.unlink(missing_ok=True)
                except Exception: pass
                self.preview_diagnostic_event(diag_id, "seekable_cache_build_failed", {
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "error": "timeout",
                })
                raise RuntimeError("Seekable zero-reencode preview remux timed out.") from exc
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            stderr_tail = (result.stderr or "")[-8000:]
            if result.returncode != 0 or not temp_target.is_file() or temp_target.stat().st_size <= 64 * 1024:
                try: temp_target.unlink(missing_ok=True)
                except Exception: pass
                self.preview_diagnostic_event(diag_id, "seekable_cache_build_failed", {
                    "elapsed_ms": elapsed_ms, "returncode": result.returncode, "stderr": stderr_tail,
                })
                raise RuntimeError("Zero-reencode seekable remux failed. " + (stderr_tail.strip().splitlines()[-1] if stderr_tail.strip() else "ffmpeg returned no usable MP4."))
            output_duration = self.preview_exact_duration(temp_target)
            if source_duration > 2 and (output_duration <= 0 or output_duration < source_duration * 0.90):
                try: temp_target.unlink(missing_ok=True)
                except Exception: pass
                self.preview_diagnostic_event(diag_id, "seekable_cache_build_failed", {
                    "elapsed_ms": elapsed_ms, "returncode": result.returncode,
                    "source_duration": source_duration, "output_duration": output_duration,
                    "error": "truncated_output", "stderr": stderr_tail,
                })
                raise RuntimeError(f"Zero-reencode remux produced a truncated preview ({output_duration:.1f}s of {source_duration:.1f}s).")
            os.replace(temp_target, target)
            try: os.utime(target, None)
            except OSError: pass
            self.preview_diagnostic_event(diag_id, "seekable_cache_build_complete", {
                "elapsed_ms": elapsed_ms, "bytes": target.stat().st_size,
                "source_duration": source_duration, "output_duration": output_duration,
            })
            self._prune_preview_seekable_cache(keep=target)
            return target

    def preview_hls_playlist(self, path: Path, fallback_duration: float = 0.0, start_hint: float = 0.0) -> Tuple[str, float, int]:
        settings = self._preview_settings()
        segment_seconds = max(4, min(30, int(settings.get("hls_segment_seconds", 8) or 8)))
        duration = self.preview_exact_duration(path)
        if duration <= 0:
            raise RuntimeError("Could not determine the recording duration from media metadata for seekable preview.")
        segment_count = max(1, int(math.ceil(duration / float(segment_seconds))))
        target_duration = max(1, segment_seconds)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        requested_start = max(0.0, min(float(start_hint or 0.0), max(0.0, duration - 0.25)))
        if requested_start > 0:
            lines.append(f"#EXT-X-START:TIME-OFFSET={requested_start:.3f},PRECISE=YES")
        for index in range(segment_count):
            start = index * segment_seconds
            length = min(float(segment_seconds), max(0.05, duration - start))
            lines.append(f"#EXTINF:{length:.3f},")
            lines.append(f"{index}.ts")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n", duration, segment_seconds

    def _prune_preview_hls_cache(self) -> None:
        now = time.time()
        with self.lock:
            if now - float(self.preview_cache_last_prune or 0.0) < 600:
                return
            self.preview_cache_last_prune = now
        settings = self._preview_settings()
        cutoff = now - max(1.0, float(settings.get("cache_hours", 18) or 18)) * 3600.0
        try:
            PREVIEW_HLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for child in PREVIEW_HLS_CACHE_DIR.iterdir():
                if not child.is_dir():
                    continue
                try:
                    newest = max((p.stat().st_mtime for p in child.iterdir() if p.is_file()), default=child.stat().st_mtime)
                    if newest < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    def preview_hls_segment(self, path: Path, segment_index: int, fallback_duration: float = 0.0) -> Path:
        """Create/cache one independently-requestable H.264/AAC MPEG-TS HLS segment.

        The output timestamps are offset to their real position in the source so
        Safari receives one continuous VOD timeline even though segments are
        transcoded only when the phone asks for them.
        """
        settings = self._preview_settings()
        segment_seconds = max(4, min(30, int(settings.get("hls_segment_seconds", 8) or 8)))
        duration = self.preview_exact_duration(path)
        if duration <= 0:
            raise RuntimeError("Could not determine the recording duration from media metadata for seekable preview.")
        start = max(0, int(segment_index)) * segment_seconds
        if start >= duration + 0.001:
            raise RuntimeError("That preview segment is beyond the end of the recording.")
        length = min(float(segment_seconds), max(0.05, duration - start))
        key = self._preview_hls_key(path)
        cache_dir = PREVIEW_HLS_CACHE_DIR / key
        cache_dir.mkdir(parents=True, exist_ok=True)
        output = cache_dir / f"{int(segment_index):07d}.ts"
        if output.is_file() and output.stat().st_size > 4096:
            try: os.utime(output, None)
            except OSError: pass
            return output
        lock_key = f"{key}:{int(segment_index)}"
        with self.lock:
            segment_lock = self.preview_hls_locks.setdefault(lock_key, threading.Lock())
        with segment_lock:
            if output.is_file() and output.stat().st_size > 4096:
                return output
            ffmpeg, _ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
            if ffmpeg is None:
                raise RuntimeError("ffmpeg was not found for iPhone HLS preview.")
            max_width = max(320, min(1920, int(settings.get("max_width", 960) or 960)))
            crf = max(18, min(36, int(settings.get("video_crf", 28) or 28)))
            audio_kbps = max(48, min(192, int(settings.get("audio_bitrate_kbps", 96) or 96)))
            temp_output = output.with_suffix(".tmp.ts")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            vf = f"scale='min({max_width},iw)':-2:force_original_aspect_ratio=decrease"
            command = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
                "-ss", f"{float(start):.3f}", "-i", str(path), "-t", f"{length:.3f}",
                "-map", "0:v:0", "-map", "0:a:0?", "-threads", "1",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2", "-ar", "44100",
                "-output_ts_offset", f"{float(start):.3f}", "-muxdelay", "0", "-muxpreload", "0",
                "-f", "mpegts", "-y", str(temp_output),
            ]
            # Only one expensive compatibility segment is encoded at a time on
            # the older PC. Normal phone API requests remain on other threads.
            with self.preview_transcode_semaphore:
                timeout_seconds = max(30, int(length * 8 + 20))
                try:
                    result = subprocess.run(
                        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        creationflags=creationflags, timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    try: temp_output.unlink(missing_ok=True)
                    except Exception: pass
                    raise RuntimeError(f"iPhone preview segment timed out after {timeout_seconds} seconds.") from exc
            if result.returncode != 0 or not temp_output.is_file() or temp_output.stat().st_size < 1024:
                try: temp_output.unlink(missing_ok=True)
                except Exception: pass
                detail = result.stderr.decode("utf-8", "replace")[-1800:]
                raise RuntimeError("iPhone preview segment could not be transcoded. " + detail)
            os.replace(temp_output, output)
            self._prune_preview_hls_cache()
            return output

    def _create_preview_diagnostic(self, queue_id: str, queue_data: Dict[str, Any], chunk: Any, index: int, path: Path) -> str:
        diag_id = uuid.uuid4().hex
        try:
            stat = path.stat()
            size = int(stat.st_size)
            mtime = float(stat.st_mtime)
        except OSError:
            size = 0
            mtime = 0.0
        entry = {
            "diagnostic_id": diag_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "queue_id": str(queue_id),
            "model": str(queue_data.get("model", "")),
            "mode": str(queue_data.get("mode", "")),
            "chunk_signature": str(getattr(chunk, "signature", "")),
            "file_index": int(index),
            "path": str(path),
            "name": path.name,
            "suffix": path.suffix.casefold(),
            "size_bytes": size,
            "mtime": mtime,
            "content_type": video_content_type(path),
            "events": [],
        }
        with self.lock:
            self.preview_diagnostics[diag_id] = entry
            self.preview_diagnostics_order.append(diag_id)
            while len(self.preview_diagnostics_order) > 80:
                stale = self.preview_diagnostics_order.pop(0)
                self.preview_diagnostics.pop(stale, None)
        return diag_id

    def preview_diagnostic_event(self, diag_id: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        diag_id = str(diag_id or "").strip()
        if not diag_id:
            return
        row = {
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "event": str(event or "event")[:120],
            "data": dict(data or {}),
        }
        # Keep event payload JSON-friendly and bounded.
        try:
            row["data"] = json.loads(json.dumps(row["data"], ensure_ascii=False, default=str))
        except Exception:
            row["data"] = {"detail": str(data)[:2000]}
        with self.lock:
            entry = self.preview_diagnostics.get(diag_id)
            if not entry:
                return
            events = entry.setdefault("events", [])
            events.append(row)
            if len(events) > 250:
                del events[:-250]

    def preview_probe(self, path: Path) -> Dict[str, Any]:
        _ffmpeg, ffprobe = self.review.resolve_ffmpeg(dict(self.review_settings))
        if ffprobe is None:
            return {"ok": False, "error": "ffprobe was not found."}
        command = [
            str(ffprobe), "-v", "error", "-show_format", "-show_streams",
            "-show_error", "-of", "json", str(path),
        ]
        started = time.perf_counter()
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            payload: Dict[str, Any]
            try:
                payload = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
                if not isinstance(payload, dict): payload = {"raw": payload}
            except Exception:
                payload = {"stdout": result.stdout.decode("utf-8", "replace")[-12000:]}
            payload.update({
                "ok": result.returncode == 0, "returncode": result.returncode,
                "elapsed_ms": elapsed_ms, "stderr": result.stderr.decode("utf-8", "replace")[-12000:],
            })
            return payload
        except Exception as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)}

    def preview_diagnostic_zip(self, diag_id: str, user_agent: str = "") -> bytes:
        with self.lock:
            entry = dict(self.preview_diagnostics.get(str(diag_id), {}))
            if entry:
                entry["events"] = list(entry.get("events", []))
        if not entry:
            raise RuntimeError("Preview diagnostic session was not found. Re-open the file preview and reproduce the failure first.")
        path = Path(str(entry.get("path", "")))
        probe = self.preview_probe(path) if path.is_file() else {"ok": False, "error": "Source file no longer exists."}
        summary = {
            **entry,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "server_version": "CTBRecMobile/2.15.10",
            "user_agent": str(user_agent or ""),
            "video_preview_settings": self._preview_settings(),
            "source_exists": path.is_file(),
        }
        # Log tail is useful for contemporaneous server exceptions but remains
        # bounded. It may contain model names/local paths; the UI warns the user.
        log_tail = ""
        try:
            with LOG_PATH.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 160_000), os.SEEK_SET)
                log_tail = handle.read().decode("utf-8", "replace")
        except OSError:
            pass
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("preview_summary.json", json.dumps(summary, indent=2, ensure_ascii=False, default=str))
            archive.writestr("ffprobe.json", json.dumps(probe, indent=2, ensure_ascii=False, default=str))
            archive.writestr("mobile_reviewer_log_tail.txt", log_tail)
            archive.writestr("README.txt", "CTBRec Mobile Reviewer preview diagnostics. No video/media bytes are included. Local file paths and model/file names may be present.\n")
        return out.getvalue()

    def preview_items(self, queue_id: str, indices: Sequence[int]) -> Dict[str, Any]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            raise RuntimeError("That review queue no longer exists.")
        chunk = self.current_chunk(queue_data)
        if chunk is None:
            raise RuntimeError("No current chunk is available for preview.")
        selected = []
        seen: Set[int] = set()
        for raw in indices:
            try: index = int(raw)
            except Exception: continue
            if index in seen or index < 0 or index >= len(chunk.files): continue
            seen.add(index); selected.append(index)
        if not selected:
            raise RuntimeError("Select at least one segment/file to preview.")
        recu_by_index: Dict[int, float] = {}
        # Recu can be useful even after a recording has moved to Review/deletion.
        if len(selected) == 1:
            try:
                data = queue_data.get("recu_data")
                if data is None:
                    data = self.mosaic.recu_data_from_payload(self.recu_cache.get_raw(str(queue_data.get("model", ""))), source="preview cache")
                if data is not None:
                    temp = {"recu_data": data, "recu_status": "ready", "recu_error": ""}
                    markers = self.recu_markers_for_chunk(temp, chunk).get("segments", {}).get(str(selected[0]), [])
                    if markers:
                        recu_by_index[selected[0]] = max(0.0, float(markers[0].get("offset_seconds", 0.0) or 0.0))
            except Exception:
                pass
        items = []
        for index in selected:
            video = chunk.files[index]
            actual_duration = self.preview_exact_duration(Path(video.path))
            start = recu_by_index.get(index, 0.0)
            if actual_duration > 0:
                start = min(start, max(0.0, actual_duration - 0.05))
            suffix = video.path.suffix.casefold()
            direct = suffix in {".mp4", ".m4v", ".mov"}
            base = f"/api/video/{urllib.parse.quote(queue_id)}/{urllib.parse.quote(str(chunk.signature))}/{index}"
            diag_id = self._create_preview_diagnostic(queue_id, queue_data, chunk, index, Path(video.path))
            self.preview_diagnostic_event(diag_id, "duration_resolved", {
                "actual_duration_seconds": actual_duration,
                "queue_duration_seconds": float(video.duration),
                "queue_duration_source": str(getattr(video, "duration_source", "")),
            })
            diag_q = urllib.parse.quote(diag_id)
            items.append({
                "index": index, "number": index + 1, "name": video.path.name,
                "size": human_size(video.size), "duration": float(actual_duration),
                "duration_known": bool(actual_duration > 0),
                "start_seconds": start, "recu_start": index in recu_by_index,
                "direct": direct, "diagnostic_id": diag_id, "stream_url_base": base,
                # Desktop browsers on the same PC should bypass ffmpeg entirely
                # and read the original file through the loopback Range endpoint.
                # Browsers cannot safely open file:// paths from an HTTP/PWA
                # origin, so this is the browser-compatible zero-transcode path.
                "local_disk_url": base + f"?direct=1&local=1&diag={diag_q}",
                "seekable_url": base + f"?seekable=1&diag={diag_q}",
                "remux_url": base + f"?start={urllib.parse.quote(str(start))}&diag={diag_q}",
                "url": base + ((f"?direct=1&diag={diag_q}") if direct else f"?start={urllib.parse.quote(str(start))}&diag={diag_q}"),
                "compatibility_url": base + f"?start={urllib.parse.quote(str(start))}&transcode=1&diag={diag_q}",
                "hls_url": f"/api/video-hls/{urllib.parse.quote(queue_id)}/{urllib.parse.quote(str(chunk.signature))}/{index}/index.m3u8?start={urllib.parse.quote(str(start))}&diag={diag_q}",
            })
        return {"items": items, "count": len(items)}

    def preview_source(self, queue_id: str, signature: str, index: int) -> Optional[Path]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            return None
        chunk = self.current_chunk(queue_data)
        if chunk is None or str(getattr(chunk, "signature", "")) != str(signature):
            return None
        if index < 0 or index >= len(chunk.files):
            return None
        path = Path(str(chunk.files[index].path))
        return path if path.is_file() else None

    def save_draft(self, queue_id: str, payload: Dict[str, Any]) -> bool:
        with self.lock:
            queue_data = self.queues.get(queue_id)
            if not queue_data:
                return False
            chunk = self.current_chunk(queue_data)
            if chunk is None:
                return False
            expected_signature = str(payload.get("_chunk_signature", "") or "")
            if expected_signature and expected_signature != str(chunk.signature):
                # A delayed autosave from the previous mosaic must never poison
                # the draft state of the newly advanced chunk.
                return False
            clean = {key: value for key, value in payload.items() if not str(key).startswith("_")}
            queue_data.setdefault("drafts", {})[chunk.signature] = clean
            return True

    def queue_status_payload(self, queue_id: str) -> Dict[str, Any]:
        """Cheap polling payload that never opens mosaic JPEGs or rebuilds layouts."""
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            raise KeyError(queue_id)
        poll_active = self._touch_sort_queue_from_poll(queue_id)
        self._sync_pending_model_task(queue_id, queue_data)
        chunk = self.current_chunk(queue_data)
        if poll_active and chunk is not None:
            status = dict(queue_data.get("current_mosaic_status", {}))
            if (str(status.get("signature", "")) == str(getattr(chunk, "signature", ""))
                    and str(status.get("state", "")) == "cancelled"
                    and "no longer open" in str(status.get("message", "")).casefold()):
                self.request_interactive_mosaic(queue_id, False)
            prefetch = self.prefetch_status(queue_data)
            if (str(prefetch.get("state", "")) == "cancelled"
                    and "no longer open" in str(prefetch.get("message", "")).casefold()):
                self.schedule_prefetch(queue_id)
        if chunk is None and (queue_data.get("preparing_model") or queue_data.get("open_error")):
            status = dict(queue_data.get("current_mosaic_status", {}))
            return {
                "done": False, "queue_id": queue_id, "mode": queue_data["mode"],
                "model": queue_data["model"], "remaining": 1,
                "initial_count": max(1, int(queue_data.get("initial_count", 1) or 1)),
                "can_back": bool(queue_data.get("history", [])),
                "chunk_signature": "__pending__",
                "recu": {"status": "not_started", "error": "", "segments": {}, "unmatched": []},
                "prefetch": self.prefetch_status(queue_data),
                "mosaic_status": status or {"state": "running", "signature": "__pending__", "message": "Opening this model…"},
                "action_queue": self.action_queue_summary(),
                "library_background": self.library_status_payload(),
                "non_nsfw_background": self.nsfw_status_payload(),
                "background_scheduler": self._scheduler_settings(),
            }
        if chunk is None:
            return {
                "done": True, "queue_id": queue_id, "mode": queue_data["mode"],
                "model": queue_data["model"], "remaining": 0,
                "initial_count": queue_data["initial_count"],
                "can_back": bool(queue_data.get("history", [])),
            }
        mode = str(queue_data.get("mode", ""))
        recu_payload = self.recu_markers_for_chunk(queue_data, chunk) if mode in {"original", "review"} else {
            "status": "disabled", "error": "", "segments": {}, "unmatched": []
        }
        current_status = dict(queue_data.get("current_mosaic_status", {}))
        if str(current_status.get("signature", "")) != str(chunk.signature):
            # Queues returned by open-fast contain already-validated mosaics.
            has_outputs = bool(getattr(chunk, "mosaics", []) or (mode == "review" and self.review.mosaic_outputs(chunk)))
            current_status = {
                "state": "ready" if has_outputs else ("waiting_idle" if mode == "cleanup" else "queued"),
                "signature": str(chunk.signature),
                "message": "Mosaic ready." if has_outputs else "Preparing mosaic…",
            }
        return {
            "done": False,
            "queue_id": queue_id,
            "mode": mode,
            "model": queue_data["model"],
            "remaining": len(queue_data.get("chunks", [])),
            "initial_count": queue_data.get("initial_count", len(queue_data.get("chunks", []))),
            "can_back": bool(queue_data.get("history", [])),
            "chunk_signature": str(chunk.signature),
            "recu": recu_payload,
            "prefetch": self.prefetch_status(queue_data),
            "mosaic_status": current_status,
            "action_queue": self.action_queue_summary(),
            "library_background": self.library_status_payload(),
            "non_nsfw_background": self.nsfw_status_payload(),
            "background_scheduler": self._scheduler_settings(),
        }

    def current_payload(self, queue_id: str) -> Dict[str, Any]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if queue_data is None:
            raise KeyError(queue_id)
        poll_active = self._touch_sort_queue_from_poll(queue_id)
        self._sync_pending_model_task(queue_id, queue_data)
        chunk = self.current_chunk(queue_data)
        if poll_active and chunk is not None:
            status = dict(queue_data.get("current_mosaic_status", {}))
            if (str(status.get("signature", "")) == str(getattr(chunk, "signature", ""))
                    and str(status.get("state", "")) == "cancelled"
                    and "no longer open" in str(status.get("message", "")).casefold()):
                self.request_interactive_mosaic(queue_id, False)
            prefetch = self.prefetch_status(queue_data)
            if (str(prefetch.get("state", "")) == "cancelled"
                    and "no longer open" in str(prefetch.get("message", "")).casefold()):
                self.schedule_prefetch(queue_id)
        if chunk is None and (queue_data.get("preparing_model") or queue_data.get("open_error")):
            status = dict(queue_data.get("current_mosaic_status", {}))
            return {
                "done": False,
                "queue_id": queue_id,
                "mode": queue_data["mode"],
                "model": queue_data["model"],
                "remaining": 1,
                "initial_count": max(1, int(queue_data.get("initial_count", 1) or 1)),
                "can_back": bool(queue_data.get("history", [])),
                "chunk": {
                    "signature": "__pending__", "size": "Preparing…", "bytes": 0,
                    "file_count": 0, "files": [], "parts": [],
                    "frame_cut_available": False,
                    "frame_cut_unavailable_reason": "Chunk metadata is still being prepared.",
                },
                "draft": {},
                "recu": {"status": "not_started", "error": "", "segments": {}, "unmatched": []},
                "prefetch": self.prefetch_status(queue_data),
                "mosaic_status": status or {"state": "running", "signature": "__pending__", "message": "Opening this model…"},
                "action_queue": self.action_queue_summary(),
                "library_background": self.library_status_payload(),
                "non_nsfw_background": self.nsfw_status_payload(),
                "background_scheduler": self._scheduler_settings(),
            }
        if chunk is None:
            return {
                "done": True,
                "queue_id": queue_id,
                "mode": queue_data["mode"],
                "model": queue_data["model"],
                "remaining": 0,
                "initial_count": queue_data["initial_count"],
                "can_back": bool(queue_data.get("history", [])),
            }
        outputs = [Path(path) for path in getattr(chunk, "mosaics", []) if Path(path).exists()]
        parts = self.layout_parts(queue_id, queue_data, chunk, outputs) if outputs else []
        layout_ready = bool(parts)
        if (not outputs or not layout_ready) and queue_data.get("mode") != "cleanup":
            # Existing legacy JPEGs are never overlaid with a guessed hit-map.
            # The interactive worker migrates them once to an exact v2 layout.
            self.request_interactive_mosaic(queue_id, False)
        draft = queue_data.get("drafts", {}).get(chunk.signature, {})
        if queue_data.get("mode") == "deletion" and not draft:
            draft = {"restore_indices": []}
        if queue_data.get("mode") == "cleanup" and not draft:
            # Cleanup is deliberately opt-out: every AI-screened candidate file
            # starts selected for MARKED_FOR_DELETION, and tapping any tile keeps
            # that entire recording in place instead.
            draft = {"delete_indices": list(range(len(chunk.files)))}
        recu_payload = self.recu_markers_for_chunk(queue_data, chunk) if queue_data.get("mode") in {"original", "review"} else {
            "status": "disabled", "error": "", "segments": {}, "unmatched": []
        }
        files = []
        cumulative = 0.0
        for index, video in enumerate(chunk.files):
            file_payload = {
                "index": index,
                "number": index + 1,
                "name": video.path.name,
                "size": human_size(video.size),
                "bytes": int(video.size),
                "duration_seconds": float(video.duration),
                "duration": self.review.format_clock(video.duration) if hasattr(self.review, "format_clock") else str(video.duration),
                "chunk_start_seconds": cumulative,
                "kinks": list(recu_payload.get("segments", {}).get(str(index), [])),
            }
            if queue_data.get("mode") == "deletion":
                target, reason = self._restore_target_for_deleted(video.path)
                file_payload["restore_available"] = target is not None
                file_payload["restore_target"] = str(target or "")
                file_payload["restore_reason"] = reason
            files.append(file_payload)
            cumulative += float(video.duration)
        frame_tiles = [
            tile
            for part in parts
            if isinstance(part, dict)
            for tile in part.get("tiles", [])
            if isinstance(tile, dict)
        ]
        frame_cut_available = bool(
            queue_data.get("mode") == "review"
            and frame_tiles
            and all("frame_index" in tile and "chunk_seconds" in tile and "interval_end_seconds" in tile for tile in frame_tiles)
        )
        payload = {
            "done": False,
            "queue_id": queue_id,
            "mode": queue_data["mode"],
            "model": queue_data["model"],
            "remaining": len(queue_data["chunks"]),
            "initial_count": queue_data["initial_count"],
            "can_back": bool(queue_data.get("history", [])),
            "notes": queue_data.get("notes", [])[:10],
            "draft": draft,
            "recu": recu_payload,
            "prefetch": self.prefetch_status(queue_data),
            "mosaic_status": (
                dict(queue_data.get("current_mosaic_status", {}))
                if str(queue_data.get("current_mosaic_status", {}).get("signature", "")) == str(chunk.signature)
                else {"state": "ready" if layout_ready else ("waiting_idle" if queue_data.get("mode") == "cleanup" else "queued"), "signature": str(chunk.signature), "message": "Mosaic ready." if layout_ready else ("Cleanup mosaic is being rebuilt by the idle PC scanner." if queue_data.get("mode") == "cleanup" else "Preparing exact tap map…")}
            ),
            "action_queue": self.action_queue_summary(),
            "library_background": self.library_status_payload(),
            "non_nsfw_background": self.nsfw_status_payload(),
            "background_scheduler": self._scheduler_settings(),
            "chunk": {
                "signature": chunk.signature,
                "folder": str(getattr(chunk, "folder", "")),
                "size": human_size(chunk.source_bytes),
                "bytes": int(chunk.source_bytes),
                "start": chunk.start.isoformat(sep=" ", timespec="seconds"),
                "end": chunk.end.isoformat(sep=" ", timespec="seconds"),
                "file_count": len(chunk.files),
                "files": files,
                "mosaic_ready": layout_ready,
                "frame_cut_available": frame_cut_available,
                "frame_count": len(frame_tiles) if frame_cut_available else 0,
                "frame_cut_unavailable_reason": (
                    "" if frame_cut_available or queue_data.get("mode") != "review"
                    else "Exact frame timestamps are not embedded in this mosaic. Regenerate it once before using Frame Cut."
                ),
                "parts": parts,
            },
        }
        return payload

    def layout_parts(
        self,
        queue_id: str,
        queue_data: Dict[str, Any],
        chunk: Any,
        outputs: Sequence[Path],
    ) -> List[Dict[str, Any]]:
        if Image is None or not outputs:
            return []
        mode = queue_data["mode"]
        # Align the live file list to the order baked into the JPEG before any
        # overlay numbers, file rows, previews, or decisions are exposed.
        self._align_chunk_files_to_mosaic_sources(mode, chunk)

        # v2.4+ mosaics carry their exact pixel hit-map beside the JPEG. This is
        # the authoritative mapping and remains correct even if mobile settings
        # are later changed or the reviewer package is replaced.
        embedded = self._embedded_layout_entry(mode, chunk, outputs)
        if embedded is not None:
            parts: List[Dict[str, Any]] = []
            review_timeline = self._review_frame_timeline(chunk, outputs) if mode == "review" else []
            frame_cursor = 0
            for part_index, (output, raw_part) in enumerate(zip(outputs, embedded.get("parts", []))):
                width = int(raw_part.get("width", 0) or 0)
                height = int(raw_part.get("height", 0) or 0)
                if width <= 0 or height <= 0:
                    continue
                tiles: List[Dict[str, Any]] = []
                for raw_tile in raw_part.get("tiles", []):
                    file_index = int(raw_tile.get("file_index", -1))
                    left = int(raw_tile.get("left_px", 0)); top = int(raw_tile.get("top_px", 0))
                    tw = int(raw_tile.get("width_px", 0)); th = int(raw_tile.get("height_px", 0))
                    tile_payload = {
                        "file_index": file_index,
                        "number": file_index + 1,
                        "left": 100.0 * left / width,
                        "top": 100.0 * top / height,
                        "width": 100.0 * tw / width,
                        "height": 100.0 * th / height,
                    }
                    if mode == "review" and frame_cursor < len(review_timeline):
                        frame_row = review_timeline[frame_cursor]
                        tile_payload.update({
                            "frame_index": int(frame_row["frame_index"]),
                            "chunk_seconds": float(frame_row["chunk_seconds"]),
                            "local_seconds": float(frame_row["local_seconds"]),
                            "interval_end_seconds": float(frame_row["interval_end_seconds"]),
                        })
                    tiles.append(tile_payload)
                    frame_cursor += 1
                parts.append({
                    "index": part_index,
                    "url": f"/media/{urllib.parse.quote(queue_id)}/{urllib.parse.quote(chunk.signature)}/{part_index}",
                    "width": width,
                    "height": height,
                    "tiles": tiles,
                    "layout_source": "embedded-v2",
                })
            return parts

        # A v2.3 central cache can still be used when its geometry passes the
        # sanity check in _layout_cache_entry. Never rebuild a hit-map from the
        # *current* settings: that is what caused the stacked/mismatched numbers.
        cached_layout = self._layout_cache_entry(mode, chunk, outputs)
        if cached_layout is None:
            return []
        file_numbers = [int(value) for value in cached_layout.get("file_numbers", [])]
        columns = max(1, int(cached_layout.get("columns", 3)))
        max_tiles = max(columns, int(cached_layout.get("max_tiles", 120)))
        header_height = max(0, int(cached_layout.get("header_height", 46 if mode == "original" else 44)))

        parts: List[Dict[str, Any]] = []
        for part_index, output in enumerate(outputs):
            start = part_index * max_tiles
            part_numbers = file_numbers[start:start + max_tiles]
            if not part_numbers:
                continue
            try:
                with Image.open(output) as opened:
                    width, height = opened.size
            except Exception:
                continue
            rows = max(1, int(math.ceil(len(part_numbers) / columns)))
            cell_width = width / columns
            body_height = max(1.0, height - header_height)
            cell_height = body_height / rows
            tiles = []
            for tile_index, file_number in enumerate(part_numbers):
                row, column = divmod(tile_index, columns)
                tiles.append({
                    "file_index": file_number - 1,
                    "number": file_number,
                    "left": 100.0 * (column * cell_width) / width,
                    "top": 100.0 * (header_height + row * cell_height) / height,
                    "width": 100.0 * cell_width / width,
                    "height": 100.0 * cell_height / height,
                })
            parts.append({
                "index": part_index,
                "url": f"/media/{urllib.parse.quote(queue_id)}/{urllib.parse.quote(chunk.signature)}/{part_index}",
                "width": width,
                "height": height,
                "tiles": tiles,
                "layout_source": "legacy-cache",
            })
        return parts

    def media_path(self, queue_id: str, signature: str, part_index: int) -> Optional[Path]:
        with self.lock:
            queue_data = self.queues.get(queue_id)
        if not queue_data:
            return None
        for chunk in queue_data.get("chunks", []):
            if str(getattr(chunk, "signature", "")) != signature:
                continue
            outputs = [Path(path) for path in getattr(chunk, "mosaics", [])]
            if 0 <= part_index < len(outputs) and outputs[part_index].is_file():
                return outputs[part_index]
        return None

    def settings_revision_token(self) -> str:
        """Stable optimistic-lock token for the editable mobile configuration.

        Excludes secrets while still changing whenever a Settings field changes.
        Old/stale/uninitialized PWAs therefore cannot overwrite a newer config.
        """
        editable = {
            key: value for key, value in self.config.items()
            if key not in {"access_pin", "secret_key"}
        }
        raw = json.dumps(editable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def mobile_settings_payload(self) -> Dict[str, Any]:
        original_background = self.background_settings_for_mode("original")
        review_background = self.background_settings_for_mode("review")
        recu = dict(self.config.get("recu", {}))
        library = self._library_settings()
        return {
            "settings_revision": self.settings_revision_token(),
            "config_repair_notice": str(getattr(self, "config_repair_notice", "") or ""),
            "library_background": {
                "enabled": bool(library.get("enabled", True)),
                "original_enabled": bool(library.get("original_enabled", True)),
                "review_enabled": bool(library.get("review_enabled", True)),
                "idle_minutes": float(library.get("idle_minutes", 10)),
                "rescan_minutes": float(library.get("rescan_minutes", 120)),
                "pause_when_active": bool(library.get("pause_when_active", True)),
            },
            "library_status": self.library_status_payload(),
            "keep_last": {
                "file_path": str(self._keep_last_settings().get("file_path", "keeplasts.txt")),
                "gap_minutes": float(self._keep_last_settings().get("gap_minutes", 30)),
                "recent_write_grace_seconds": int(self._keep_last_settings().get("recent_write_grace_seconds", 120)),
            },
            "keep_last_status": self.keep_last_status_payload(),
            "non_nsfw_status": self.nsfw_status_payload(),
            "background_scheduler": self._scheduler_settings(),
            "frame_cut": {
                "idle_minutes": float(self._frame_cut_idle_minutes()),
            },
            "action_queue": self.action_queue_summary(),
            "non_nsfw_cleanup": {
                "enabled": bool(self._nsfw_settings().get("enabled", True)),
                "idle_minutes": float(self._nsfw_settings().get("idle_minutes", 10)),
                "rescan_minutes": float(self._nsfw_settings().get("rescan_minutes", 240)),
                "pause_when_active": bool(self._nsfw_settings().get("pause_when_active", True)),
                "sample_every_seconds": int(self._nsfw_settings().get("sample_every_seconds", 180)),
                "detector_model": str(self._nsfw_settings().get("detector_model", "640m")),
                "confidence_threshold": float(self._nsfw_settings().get("confidence_threshold", 0.45)),
                "borderline_confidence": float(self._nsfw_settings().get("borderline_confidence", 0.20)),
                "batch_size": int(self._nsfw_settings().get("batch_size", 3)),
                "columns": int(self._nsfw_settings().get("columns", 4)),
                "tile_width": int(self._nsfw_settings().get("tile_width", 260)),
                "max_tiles_per_image": int(self._nsfw_settings().get("max_tiles_per_image", 120)),
                "jpeg_quality": int(self._nsfw_settings().get("jpeg_quality", 84)),
                "recent_write_grace_seconds": int(self._nsfw_settings().get("recent_write_grace_seconds", 120)),
                "explicit_classes": list(self._nsfw_settings().get("explicit_classes", self.nsfw.DEFAULT_EXPLICIT_CLASSES)),
            },
            "background_mosaics": {
                "original": {
                    "enabled": bool(original_background.get("enabled", True)),
                    "upcoming_count": int(original_background.get("upcoming_count", 3)),
                    "idle_only": bool(original_background.get("idle_only", True)),
                    "idle_minutes": float(original_background.get("idle_minutes", 10)),
                },
                "review": {
                    "enabled": bool(review_background.get("enabled", True)),
                    "upcoming_count": int(review_background.get("upcoming_count", 3)),
                    "idle_only": bool(review_background.get("idle_only", True)),
                    "idle_minutes": float(review_background.get("idle_minutes", 10)),
                },
            },
            "model_admin": {
                "enabled": bool(self.config.get("model_admin", {}).get("enabled", True)),
                "auto_discover": bool(self.config.get("model_admin", {}).get("auto_discover", True)),
                "models_json_paths": list(self.config.get("model_admin", {}).get("models_json_paths", [])),
            },
            "model_admin_status": self.model_admin.discovery_payload(),
            "live_bridge": dict(self.config.get("live_bridge", {})),
            "live_bridge_status": self.live_bridge.discovery_payload(),
            "mosaic": {
                "sample_every_seconds": int(self.mosaic_settings.get("sample_every_seconds", 300)),
                "columns": int(self.mosaic_settings.get("columns", 3)),
                "tile_width": int(self.mosaic_settings.get("tile_width", 480)),
                "max_total_frames": int(self.mosaic_settings.get("max_total_frames", 240)),
                "use_existing_mosaics": bool(self.mosaic_settings.get("use_existing_mosaics", True)),
            },
            "review": {
                "sample_every_seconds": int(self.review_settings.get("sample_every_seconds", 180)),
                "columns": int(self.review_settings.get("columns", 3)),
                "tile_width": int(self.review_settings.get("tile_width", 480)),
                "max_total_frames": int(self.review_settings.get("max_total_frames", 300)),
                "use_existing_mosaics": bool(self.review_settings.get("use_existing_mosaics", True)),
            },
            "recu": {
                "enabled": bool(recu.get("enabled", True)),
                "chrome_path": str(recu.get("chrome_path", "")),
                "base_url": str(recu.get("base_url", "https://recu.me")),
                "browser_debug_port": int(recu.get("browser_debug_port", 9223)),
                "auto_refresh_current": bool(recu.get("auto_refresh_current", True)),
            },
            "speed_mode": {
                "ready_suggestions": int(self.config.get("speed_mode", {}).get("ready_suggestions", 8)),
                "offline_pack_chunks": int(self.config.get("speed_mode", {}).get("offline_pack_chunks", 12)),
                "offline_pack_max_mb": float(self.config.get("speed_mode", {}).get("offline_pack_max_mb", 750)),
            },
            "recu_status": self.recu_status(),
        }

    def update_mobile_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        supplied_revision = str(payload.get("settings_revision", "") or "").strip()
        current_revision = self.settings_revision_token()
        if not supplied_revision:
            raise RuntimeError("Settings were not loaded before Save. Reopen Mobile Settings and try again; no configuration was changed.")
        if not hmac.compare_digest(supplied_revision, current_revision):
            raise RuntimeError("Settings changed since this screen loaded. Reopen Mobile Settings before saving; no configuration was changed.")
        library_raw = payload.get("library_background", {})
        if isinstance(library_raw, dict):
            current_library = self._library_settings()
            self.config["library_background"] = {
                "enabled": bool(library_raw.get("enabled", current_library.get("enabled", True))),
                "original_enabled": bool(library_raw.get("original_enabled", current_library.get("original_enabled", True))),
                "review_enabled": bool(library_raw.get("review_enabled", current_library.get("review_enabled", True))),
                "idle_minutes": max(0.0, float(library_raw.get("idle_minutes", current_library.get("idle_minutes", 10)))),
                "rescan_minutes": max(0.0, float(library_raw.get("rescan_minutes", current_library.get("rescan_minutes", 120)))),
                "pause_when_active": bool(library_raw.get("pause_when_active", current_library.get("pause_when_active", True))),
            }
        frame_cut_raw = payload.get("frame_cut", {})
        if isinstance(frame_cut_raw, dict):
            current_action = dict(self.config.get("action_queue", {}))
            current_action["frame_cut_idle_minutes"] = max(
                0.0, float(frame_cut_raw.get("idle_minutes", self._frame_cut_idle_minutes()))
            )
            self.config["action_queue"] = current_action

        keep_raw = payload.get("keep_last", {})
        if isinstance(keep_raw, dict):
            current_keep = self._keep_last_settings()
            self.config["keep_last"] = {
                "file_path": str(keep_raw.get("file_path", current_keep.get("file_path", "keeplasts.txt"))).strip() or "keeplasts.txt",
                "gap_minutes": max(0.0, float(keep_raw.get("gap_minutes", current_keep.get("gap_minutes", 30)))),
                "recent_write_grace_seconds": max(0, int(keep_raw.get("recent_write_grace_seconds", current_keep.get("recent_write_grace_seconds", 120)))),
            }
        nsfw_raw = payload.get("non_nsfw_cleanup", {})
        if isinstance(nsfw_raw, dict):
            current_nsfw = self._nsfw_settings()
            self.config["non_nsfw_cleanup"] = {
                **current_nsfw,
                "enabled": bool(nsfw_raw.get("enabled", current_nsfw.get("enabled", True))),
                "idle_minutes": max(0.0, float(nsfw_raw.get("idle_minutes", current_nsfw.get("idle_minutes", 10)))),
                "rescan_minutes": max(0.0, float(nsfw_raw.get("rescan_minutes", current_nsfw.get("rescan_minutes", 240)))),
                "pause_when_active": bool(nsfw_raw.get("pause_when_active", current_nsfw.get("pause_when_active", True))),
                "sample_every_seconds": max(1, int(nsfw_raw.get("sample_every_seconds", current_nsfw.get("sample_every_seconds", 180)))),
                "detector_model": "320n" if str(nsfw_raw.get("detector_model", current_nsfw.get("detector_model", "640m"))).casefold() in {"320n", "fast"} else "640m",
                "confidence_threshold": max(0.10, min(0.95, float(nsfw_raw.get("confidence_threshold", current_nsfw.get("confidence_threshold", 0.45))))),
                "borderline_confidence": max(0.05, min(0.90, float(nsfw_raw.get("borderline_confidence", current_nsfw.get("borderline_confidence", 0.20))))),
                "batch_size": max(1, int(nsfw_raw.get("batch_size", current_nsfw.get("batch_size", 3)))),
                "frame_extract_timeout_seconds": max(5, min(180, int(nsfw_raw.get("frame_extract_timeout_seconds", current_nsfw.get("frame_extract_timeout_seconds", 45))))),
                "frame_extract_batch_size": max(1, min(12, int(nsfw_raw.get("frame_extract_batch_size", current_nsfw.get("frame_extract_batch_size", 6))))),
                "columns": max(1, int(nsfw_raw.get("columns", current_nsfw.get("columns", 4)))),
                "tile_width": max(1, int(nsfw_raw.get("tile_width", current_nsfw.get("tile_width", 260)))),
                "max_tiles_per_image": max(1, int(nsfw_raw.get("max_tiles_per_image", current_nsfw.get("max_tiles_per_image", 120)))),
                "jpeg_quality": max(55, min(95, int(nsfw_raw.get("jpeg_quality", current_nsfw.get("jpeg_quality", 84))))),
                "recent_write_grace_seconds": max(0, int(nsfw_raw.get("recent_write_grace_seconds", current_nsfw.get("recent_write_grace_seconds", 120)))),
                "explicit_classes": list(current_nsfw.get("explicit_classes", self.nsfw.DEFAULT_EXPLICIT_CLASSES)),
            }
        scheduler_raw = payload.get("background_scheduler", {})
        if isinstance(scheduler_raw, dict):
            priority = str(scheduler_raw.get("priority", self._scheduler_settings().get("priority", "mosaics_first")))
            self.config["background_scheduler"] = {"priority": priority if priority in {"mosaics_first", "cleanup_first"} else "mosaics_first"}

        background_raw = payload.get("background_mosaics", {})
        if isinstance(background_raw, dict):
            migrated: Dict[str, Dict[str, Any]] = {}
            for mode in ("original", "review"):
                current = self.background_settings_for_mode(mode)
                raw = background_raw.get(mode, background_raw if "enabled" in background_raw else {})
                if not isinstance(raw, dict):
                    raw = {}
                migrated[mode] = {
                    "enabled": bool(raw.get("enabled", current.get("enabled", True))),
                    "upcoming_count": max(0, int(raw.get("upcoming_count", current.get("upcoming_count", 3)))),
                    "idle_only": bool(raw.get("idle_only", current.get("idle_only", True))),
                    "idle_minutes": max(0.0, float(raw.get("idle_minutes", current.get("idle_minutes", 10)))),
                }
            self.config["background_mosaics"] = migrated

        for key, settings, defaults in (
            ("mosaic", self.mosaic_settings, {"sample_every_seconds": 300, "columns": 3, "tile_width": 480, "max_total_frames": 240}),
            ("review", self.review_settings, {"sample_every_seconds": 180, "columns": 3, "tile_width": 480, "max_total_frames": 300}),
        ):
            raw = payload.get(key, {})
            if not isinstance(raw, dict):
                continue
            settings["sample_every_seconds"] = max(1, int(raw.get("sample_every_seconds", settings.get("sample_every_seconds", defaults["sample_every_seconds"]))))
            settings["columns"] = max(1, int(raw.get("columns", settings.get("columns", defaults["columns"]))))
            settings["tile_width"] = max(1, int(raw.get("tile_width", settings.get("tile_width", defaults["tile_width"]))))
            settings["max_total_frames"] = max(1, int(raw.get("max_total_frames", settings.get("max_total_frames", defaults["max_total_frames"]))))
            settings["use_existing_mosaics"] = bool(raw.get("use_existing_mosaics", settings.get("use_existing_mosaics", True)))

        recu_raw = payload.get("recu", {})
        if isinstance(recu_raw, dict):
            recu = dict(self.config.get("recu", {}))
            recu["enabled"] = bool(recu_raw.get("enabled", recu.get("enabled", True)))
            recu["chrome_path"] = str(recu_raw.get("chrome_path", recu.get("chrome_path", ""))).strip()
            recu["base_url"] = str(recu_raw.get("base_url", recu.get("base_url", "https://recu.me"))).strip().rstrip("/") or "https://recu.me"
            recu["browser_debug_port"] = max(1024, min(65535, int(recu_raw.get("browser_debug_port", recu.get("browser_debug_port", 9223)))))
            recu["auto_refresh_current"] = bool(recu_raw.get("auto_refresh_current", recu.get("auto_refresh_current", True)))
            self.config["recu"] = recu
            self.mosaic_settings["recu_enabled"] = recu["enabled"]
            self.mosaic_settings["recu_base_url"] = recu["base_url"]

        self.config["mosaic_settings"] = {
            **(self.config.get("mosaic_settings", {}) if isinstance(self.config.get("mosaic_settings"), dict) else {}),
            "sample_every_seconds": self.mosaic_settings["sample_every_seconds"],
            "columns": self.mosaic_settings["columns"],
            "tile_width": self.mosaic_settings["tile_width"],
            "max_total_frames": self.mosaic_settings["max_total_frames"],
            "use_existing_mosaics": bool(self.mosaic_settings.get("use_existing_mosaics", True)),
        }
        self.config["review_settings"] = {
            **(self.config.get("review_settings", {}) if isinstance(self.config.get("review_settings"), dict) else {}),
            "sample_every_seconds": self.review_settings["sample_every_seconds"],
            "columns": self.review_settings["columns"],
            "tile_width": self.review_settings["tile_width"],
            "max_total_frames": self.review_settings["max_total_frames"],
            "use_existing_mosaics": bool(self.review_settings.get("use_existing_mosaics", True)),
        }
        model_admin_raw = payload.get("model_admin", {})
        if isinstance(model_admin_raw, dict):
            current_admin = dict(self.config.get("model_admin", {}))
            current_admin["enabled"] = bool(model_admin_raw.get("enabled", current_admin.get("enabled", True)))
            current_admin["auto_discover"] = bool(model_admin_raw.get("auto_discover", current_admin.get("auto_discover", True)))
            paths_raw = model_admin_raw.get("models_json_paths", current_admin.get("models_json_paths", []))
            if isinstance(paths_raw, str):
                paths = [line.strip() for line in paths_raw.replace(";", "\n").splitlines() if line.strip()]
            elif isinstance(paths_raw, list):
                paths = [str(value).strip() for value in paths_raw if str(value).strip()]
            else:
                paths = []
            current_admin["models_json_paths"] = paths
            self.config["model_admin"] = current_admin
            self.model_admin.configure(current_admin)

        bridge_raw = payload.get("live_bridge", {})
        if isinstance(bridge_raw, dict):
            current_bridge = dict(self.config.get("live_bridge", {}))
            current_bridge["enabled"] = bool(bridge_raw.get("enabled", current_bridge.get("enabled", True)))
            current_bridge["auto_discover"] = bool(bridge_raw.get("auto_discover", current_bridge.get("auto_discover", True)))
            current_bridge["controller_config_path"] = str(bridge_raw.get("controller_config_path", current_bridge.get("controller_config_path", ""))).strip()
            current_bridge["ctbrec_dir"] = str(bridge_raw.get("ctbrec_dir", current_bridge.get("ctbrec_dir", ""))).strip()
            current_bridge["bridge_port"] = max(1024, min(65535, int(bridge_raw.get("bridge_port", current_bridge.get("bridge_port", 8791)))))
            current_bridge["heartbeat_seconds"] = max(1.0, min(30.0, float(bridge_raw.get("heartbeat_seconds", current_bridge.get("heartbeat_seconds", 2.0)))))
            current_bridge["snapshot_poll_seconds"] = max(2.0, min(60.0, float(bridge_raw.get("snapshot_poll_seconds", current_bridge.get("snapshot_poll_seconds", 5.0)))))
            current_bridge["offline_grace_seconds"] = max(3.0, min(120.0, float(bridge_raw.get("offline_grace_seconds", current_bridge.get("offline_grace_seconds", 10.0)))))
            self.config["live_bridge"] = current_bridge
            self.live_bridge.configure(current_bridge)

        speed_raw = payload.get("speed_mode", {})
        current_speed = dict(self.config.get("speed_mode", {}))
        if isinstance(speed_raw, dict):
            current_speed["ready_suggestions"] = max(1, int(speed_raw.get("ready_suggestions", current_speed.get("ready_suggestions", 8))))
            current_speed["offline_pack_chunks"] = max(1, int(speed_raw.get("offline_pack_chunks", current_speed.get("offline_pack_chunks", 12))))
            current_speed["offline_pack_max_mb"] = max(1.0, float(speed_raw.get("offline_pack_max_mb", current_speed.get("offline_pack_max_mb", 750))))
            self.config["speed_mode"] = current_speed
        self.save_config()
        return self.mobile_settings_payload()


STATE = MobileReviewerState()


_CLIENT_DISCONNECT_WINERRORS = {64, 10053, 10054, 10057, 10058}
_CLIENT_DISCONNECT_ERRNOS = {
    value for value in (
        getattr(errno, "EPIPE", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ENOTCONN", None),
    ) if value is not None
}


def _parse_http_byte_range(range_header: str, size: int) -> Optional[Tuple[int, int]]:
    """Parse one HTTP byte range, including RFC suffix ranges used by video players.

    Returns (start, end) inclusive, or None for an invalid/unsatisfiable range.
    Browsers commonly request the tail of MP4/MOV files with ``bytes=-N`` to
    find metadata; v2.14.0 misread that as bytes 0-N, which could make direct
    desktop playback fail even though the source file itself was valid.
    """
    size = max(0, int(size))
    if size <= 0:
        return None
    raw = str(range_header or "").strip()
    if not raw:
        return (0, size - 1)
    if not raw.casefold().startswith("bytes="):
        return None
    # A video element normally asks for one range. If a client supplies a
    # multipart range, serving the first range is sufficient for our simple
    # streaming endpoint and avoids constructing multipart/byteranges bodies.
    spec = raw.split("=", 1)[1].split(",", 1)[0].strip()
    match = re.fullmatch(r"(\d*)-(\d*)", spec)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, second = match.group(1), match.group(2)
    if first:
        start = int(first)
        if start >= size:
            return None
        end = int(second) if second else size - 1
        end = min(size - 1, end)
        if end < start:
            return None
        return (start, end)
    suffix = int(second)
    if suffix <= 0:
        return None
    suffix = min(size, suffix)
    return (size - suffix, size - 1)


def _is_client_disconnect(exc: BaseException) -> bool:
    """Return True for normal browser/socket disconnects that should not crash a request thread."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in _CLIENT_DISCONNECT_WINERRORS:
        return True
    return getattr(exc, "errno", None) in _CLIENT_DISCONNECT_ERRNOS


class ResilientThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP server that treats browser cancellations as normal disconnects, not server crashes."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if exc is not None and _is_client_disconnect(exc):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "CTBRecMobile/2.15.10"
    # HTTP/1.0 is intentionally retained for compatibility with Tailscale Serve
    # and iOS standalone PWAs. v2.13.0 HTTP/1.1/gzip caused transport-level
    # `Failed to fetch` regressions on the real phone path.
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        append_log("HTTP " + (format % args))

    def is_local(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def has_auth_cookie(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("ctbrec_auth")
        return bool(value and hmac.compare_digest(value.value, STATE.auth_cookie_value))

    def is_authenticated(self) -> bool:
        # Tailscale Serve inserts this header and removes spoofed incoming copies.
        if self.headers.get("Tailscale-User-Login"):
            return True
        if self.is_local():
            return True
        return self.has_auth_cookie()

    def read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 2_000_000))
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def send_json(self, payload: Any, status: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        # Keep JSON transport deliberately boring and proxy-safe. The v2.13.0
        # HTTP/1.1 + gzip experiment produced real iOS/Tailscale fetch failures.
        raw = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)
        except OSError as exc:
            if _is_client_disconnect(exc):
                self.close_connection = True
                return
            raise

    def send_file(self, path: Path, cache: bool = False) -> None:
        # Stream mosaics/static assets instead of read_bytes(). Large multi-part
        # mosaics can otherwise create several full-JPEG RAM copies when iPhone
        # Safari opens parallel requests.
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=300" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=256 * 1024)
        except OSError as exc:
            if _is_client_disconnect(exc):
                # Browsers routinely cancel stale/reloaded/PWA requests. On Windows this is
                # commonly WinError 10053. It is a client disconnect, not a server failure.
                self.close_connection = True
                return
            raise

    def send_range_file(self, path: Path, diag_id: str = "", head_only: bool = False) -> None:
        """Stream the original local video with browser-correct HTTP Range semantics."""
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404); return
        if size <= 0:
            self.send_error(404); return
        mime = video_content_type(path)
        range_header = str(self.headers.get("Range", ""))
        transfer_started = time.perf_counter()
        parsed = _parse_http_byte_range(range_header, size)
        if range_header and parsed is None:
            STATE.preview_diagnostic_event(diag_id, "server_range", {
                "method": "HEAD" if head_only else "GET", "range": range_header, "status": 416,
                "size": size, "content_type": mime, "error": "unsatisfiable_or_invalid_range",
            })
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = parsed if parsed is not None else (0, size - 1)
        status = 206 if range_header else 200
        length = end - start + 1
        STATE.preview_diagnostic_event(diag_id, "server_range", {
            "method": "HEAD" if head_only else "GET", "range": range_header, "status": status,
            "start": start, "end": end, "length": length, "size": size, "content_type": mime,
        })
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(path.name)}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        remaining = length
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            STATE.preview_diagnostic_event(diag_id, "server_range_complete", {
                "elapsed_ms": round((time.perf_counter() - transfer_started) * 1000.0, 1),
                "bytes_sent": length - remaining, "requested_bytes": length,
            })
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            STATE.preview_diagnostic_event(diag_id, "server_range_disconnect", {
                "elapsed_ms": round((time.perf_counter() - transfer_started) * 1000.0, 1),
                "bytes_sent": length - remaining, "requested_bytes": length, "error": type(exc).__name__,
            })

    def send_preview_stream(self, path: Path, start_seconds: float, transcode: bool = False, diag_id: str = "") -> None:
        ffmpeg, _ffprobe = STATE.review.resolve_ffmpeg(dict(STATE.review_settings))
        if ffmpeg is None:
            self.send_json({"error": "ffmpeg was not found for phone preview."}, 500); return
        start = max(0.0, float(start_seconds or 0.0))
        STATE.preview_diagnostic_event(diag_id, "server_stream_start", {"start_seconds": start, "transcode": bool(transcode), "path_suffix": path.suffix.casefold()})
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        ]
        if start > 0: command += ["-ss", f"{start:.3f}"]
        command += ["-i", str(path), "-map", "0:v:0?", "-map", "0:a:0?"]
        if transcode:
            command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-c", "copy"]
        command += ["-avoid_negative_ts", "make_zero", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        stream_started = time.perf_counter()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=creationflags)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            assert process.stdout is not None
            while True:
                block = process.stdout.read(256 * 1024)
                if not block: break
                self.wfile.write(block); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try: process.terminate()
            except Exception: pass
            try: process.wait(timeout=1)
            except Exception: pass
            STATE.preview_diagnostic_event(diag_id, "server_stream_end", {
                "transcode": bool(transcode), "start_seconds": start,
                "elapsed_ms": round((time.perf_counter() - stream_started) * 1000.0, 1),
                "returncode": process.poll(),
            })

    def require_auth(self) -> bool:
        if self.is_authenticated():
            return True
        self.send_json({"error": "Authentication required."}, 401)
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/":
            self.send_file(STATIC_DIR / "index.html")
            return
        if path == "/service-worker.js":
            self.send_file(STATIC_DIR / "service-worker.js", cache=False)
            return
        if path.startswith("/static/"):
            relative = path[len("/static/"):]
            candidate = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in candidate.parents:
                self.send_error(403)
                return
            self.send_file(candidate, cache=True)
            return
        if path == "/api/auth":
            authenticated = self.is_authenticated()
            self.send_json({
                "authenticated": authenticated,
                "tailscale_user": self.headers.get("Tailscale-User-Login", ""),
                "local": self.is_local(),
                "paired_token_supported": True,
                "device_token": STATE.device_token_value if authenticated else "",
            })
            return
        if not self.require_auth():
            return
        if path == "/api/bootstrap":
            with STATE.lock:
                catalog_status = dict(STATE.catalog_status)
            self.send_json({
                "drives": STATE.drives(),
                "catalog_status": catalog_status,
                "pin_fallback": str(STATE.config.get("access_pin", "")),
                "roots_file": str(STATE.roots_path),
                "library_background": STATE.library_status_payload(),
                "non_nsfw_background": STATE.nsfw_status_payload(),
                "background_scheduler": STATE._scheduler_settings(),
                "action_queue": STATE.action_queue_summary(),
            })
            return
        if path == "/api/catalog":
            mode = str((query.get("mode") or ["original"])[0])
            model_filter = str((query.get("filter") or [""])[0])
            drives_raw = str((query.get("drives") or [""])[0])
            drives = {value.upper() for value in drives_raw.split(",") if value.strip()}
            started = time.perf_counter()
            try:
                models = STATE.catalog_models(mode, drives, model_filter)
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                metric = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "mode": mode,
                    "filter": model_filter,
                    "drives": sorted(drives),
                    "model_count": len(models),
                    "elapsed_ms": elapsed_ms,
                    "ok": True,
                }
                with STATE.lock:
                    STATE.last_catalog_request = metric
                if elapsed_ms >= 500:
                    append_log(
                        f"Catalog request slow: mode={mode} filter={model_filter or '-'} "
                        f"drives={','.join(sorted(drives)) or 'ALL'} models={len(models)} elapsed_ms={elapsed_ms}"
                    )
                self.send_json({
                    "mode": mode,
                    "filter": model_filter,
                    "models": models,
                    "status": STATE.catalog_status,
                    "request_ms": elapsed_ms,
                })
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                with STATE.lock:
                    STATE.last_catalog_request = {
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "mode": mode, "filter": model_filter, "drives": sorted(drives),
                        "elapsed_ms": elapsed_ms, "ok": False, "error": str(exc),
                    }
                append_log(f"Catalog request failed after {elapsed_ms} ms: {exc}\n{traceback.format_exc()}")
                self.send_json({"error": f"Catalog request failed: {exc}"}, 500)
            return
        if path == "/api/diagnostics/status":
            with STATE.lock:
                catalog_counts = {name: len(rows) for name, rows in STATE.catalog.items()}
                last_catalog = dict(STATE.last_catalog_request)
                catalog_status = dict(STATE.catalog_status)
            self.send_json({
                "server_version": self.server_version,
                "catalog_counts": catalog_counts,
                "catalog_status": catalog_status,
                "last_catalog_request": last_catalog,
                "roots_file": str(STATE.roots_path),
                "root_count": len(STATE.roots),
                "roots_available": sum(1 for root in STATE.roots if Path(root).exists()),
                "log_file": str(LOG_PATH),
                "catalog_cache_file": str(CATALOG_PATH),
                "ready_index_file": str(READY_INDEX_PATH),
            })
            return
        if path == "/api/ready-suggestions":
            mode = str((query.get("mode") or ["original"])[0])
            drives_raw = str((query.get("drives") or [""])[0])
            drives = {value.upper() for value in drives_raw.split(",") if value.strip()}
            exclude = str((query.get("exclude") or [""])[0])
            limit = int((query.get("limit") or [str(STATE.config.get("speed_mode", {}).get("ready_suggestions", 8))])[0])
            self.send_json({"suggestions": STATE.ready_suggestions(mode, drives, exclude, limit)})
            return
        if path == "/api/offline/pack":
            mode = str((query.get("mode") or ["original"])[0])
            drives_raw = str((query.get("drives") or [""])[0])
            drives = {value.upper() for value in drives_raw.split(",") if value.strip()}
            speed = STATE.config.get("speed_mode", {})
            limit = int((query.get("limit") or [str(speed.get("offline_pack_chunks", 12))])[0])
            max_mb = float((query.get("max_mb") or [str(speed.get("offline_pack_max_mb", 750))])[0])
            model_values = [urllib.parse.unquote(value) for value in (query.get("model") or []) if str(value).strip()]
            self.send_json(STATE.offline_pack(mode, drives, limit, max_mb, model_values))
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/status", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            try:
                self.send_json(STATE.queue_status_payload(queue_id))
            except KeyError:
                self.send_json({"error": "Queue not found."}, 404)
            return
        if path == "/api/model-admin":
            model = str((query.get("model") or [""])[0]).strip()
            if not model:
                self.send_json({"error": "Model is required."}, 400)
            else:
                try:
                    self.send_json(STATE.model_admin_payload(model))
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/settings":
            self.send_json(STATE.mobile_settings_payload())
            return
        if path == "/api/work-status":
            self.send_json(STATE.work_status_payload())
            return
        if path == "/api/action-queue/jobs":
            self.send_json({"jobs": STATE.action_queue_jobs_payload()})
            return
        if path == "/api/background-status":
            with STATE.lock:
                catalog_status = dict(STATE.catalog_status)
            self.send_json({
                "library_background": STATE.library_status_payload(),
                "non_nsfw_background": STATE.nsfw_status_payload(),
                "background_scheduler": STATE._scheduler_settings(),
                "action_queue": STATE.action_queue_summary(),
                "catalog_status": catalog_status,
            })
            return
        if path == "/api/recu/status":
            self.send_json(STATE.recu_status())
            return
        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with STATE.lock:
                record = STATE.tasks.get(task_id)
            if record is None:
                self.send_json({"error": "Task not found."}, 404)
            else:
                self.send_json(record.payload())
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/current", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            try:
                self.send_json(STATE.current_payload(queue_id))
            except KeyError:
                self.send_json({"error": "Queue not found."}, 404)
            return
        match = re.fullmatch(r"/api/offline-media/([^/]+)/(\d+)", path)
        if match:
            media = STATE.offline_media_path(urllib.parse.unquote(match.group(1)), int(match.group(2)))
            if media is None:
                self.send_error(404)
            else:
                self.send_file(media, cache=True)
            return
        if path == "/api/preview-diagnostics/export":
            diag_id = str((query.get("id") or [""])[0]).strip()
            try:
                raw = STATE.preview_diagnostic_zip(diag_id, self.headers.get("User-Agent", ""))
                filename = f"CTBRec_Preview_Diagnostics_{diag_id[:12] or 'unknown'}.zip"
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 404)
            return
        match = re.fullmatch(r"/api/video-hls/([^/]+)/([^/]+)/(\d+)/index\.m3u8", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1)); signature = urllib.parse.unquote(match.group(2)); index = int(match.group(3))
            media = STATE.preview_source(queue_id, signature, index)
            if media is None:
                self.send_error(404); return
            try:
                start_hint = float((query.get("start") or ["0"])[0] or 0)
                playlist, _duration, _segment_seconds = STATE.preview_hls_playlist(media, start_hint=start_hint)
                raw = playlist.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                if _is_client_disconnect(exc):
                    self.close_connection = True
                    return
                append_log(f"HLS preview playlist failed for {media}: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return
        match = re.fullmatch(r"/api/video-hls/([^/]+)/([^/]+)/(\d+)/(\d+)\.ts", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1)); signature = urllib.parse.unquote(match.group(2)); index = int(match.group(3)); segment_index = int(match.group(4))
            media = STATE.preview_source(queue_id, signature, index)
            if media is None:
                self.send_error(404); return
            try:
                segment = STATE.preview_hls_segment(media, segment_index)
                try:
                    size = segment.stat().st_size
                except OSError:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    with segment.open("rb") as handle:
                        shutil.copyfileobj(handle, self.wfile, length=256 * 1024)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            except Exception as exc:
                append_log(f"HLS preview segment failed for {media} segment {segment_index}: {exc}")
                self.send_json({"error": str(exc)}, 500)
            return
        match = re.fullmatch(r"/api/video/([^/]+)/([^/]+)/(\d+)", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1)); signature = urllib.parse.unquote(match.group(2)); index = int(match.group(3))
            media = STATE.preview_source(queue_id, signature, index)
            if media is None:
                self.send_error(404); return
            direct = str((query.get("direct") or [""])[0]) == "1"
            seekable = str((query.get("seekable") or [""])[0]) == "1"
            start = float((query.get("start") or ["0"])[0] or 0)
            transcode = str((query.get("transcode") or [""])[0]) == "1"
            diag_id = str((query.get("diag") or [""])[0]).strip()
            if seekable and not transcode:
                try:
                    cached = STATE.preview_seekable_copy(media, diag_id=diag_id)
                    self.send_range_file(cached, diag_id=diag_id)
                except Exception as exc:
                    STATE.preview_diagnostic_event(diag_id, "seekable_cache_http_error", {"error": str(exc)})
                    self.send_json({"error": str(exc)}, 500)
            elif direct and not transcode:
                self.send_range_file(media, diag_id=diag_id)
            else:
                self.send_preview_stream(media, start, transcode=transcode, diag_id=diag_id)
            return
        match = re.fullmatch(r"/media/([^/]+)/([^/]+)/(\d+)", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            signature = urllib.parse.unquote(match.group(2))
            media = STATE.media_path(queue_id, signature, int(match.group(3)))
            if media is None:
                self.send_error(404)
            else:
                self.send_file(media, cache=True)
            return
        self.send_json({"error": "Not found."}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self.read_json()
        if path == "/api/auth/recover":
            supplied = str(payload.get("device_token", "")).strip()
            if supplied and hmac.compare_digest(supplied, STATE.device_token_value):
                cookie = f"ctbrec_auth={STATE.auth_cookie_value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                self.send_json({"ok": True, "authenticated": True}, headers={"Set-Cookie": cookie})
            else:
                self.send_json({"error": "Paired-device credential is invalid or expired."}, 403)
            return
        if path == "/api/login":
            submitted = str(payload.get("pin", "")).strip()
            expected = str(STATE.config.get("access_pin", "")).strip()
            if submitted and hmac.compare_digest(submitted, expected):
                cookie = f"ctbrec_auth={STATE.auth_cookie_value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
                self.send_json({"ok": True, "device_token": STATE.device_token_value}, headers={"Set-Cookie": cookie})
            else:
                self.send_json({"error": "Incorrect PIN."}, 403)
            return
        if not self.require_auth():
            return
        if path == "/api/catalog/rescan":
            self.send_json({"task_id": STATE.start_catalog_scan()})
            return
        if path == "/api/model-admin/update":
            try:
                result = STATE.update_model_admin(payload)
                STATE.invalidate_catalog_views()
                self.send_json(result)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/settings":
            try:
                self.send_json(STATE.update_mobile_settings(payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/library-background/run":
            self.send_json(STATE.force_library_generation())
            return
        if path == "/api/non-nsfw/run":
            self.send_json(STATE.force_nsfw_generation())
            return
        if path == "/api/keep-last/run":
            try:
                self.send_json(STATE.queue_keep_last_all())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/offline/prepare":
            try:
                mode = str(payload.get("mode", "original"))
                drives = {str(value).upper() for value in payload.get("drives", []) if str(value).strip()}
                models = [str(value).strip() for value in payload.get("models", []) if str(value).strip()]
                limit = int(payload.get("limit", STATE.config.get("speed_mode", {}).get("offline_pack_chunks", 12)) or 12)
                max_mb = float(payload.get("max_mb", STATE.config.get("speed_mode", {}).get("offline_pack_max_mb", 750)) or 750)
                self.send_json({"task_id": STATE.prepare_offline_pack_task(mode, drives, limit, max_mb, models)})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/action-queue/retry":
            self.send_json(STATE.retry_blocked_actions())
            return
        if path == "/api/action-queue/reorder":
            try: self.send_json(STATE.reorder_action_job(str(payload.get("job_id", "")), str(payload.get("direction", "top"))))
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/action-queue/cancel":
            try: self.send_json(STATE.cancel_action_job(str(payload.get("job_id", ""))))
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/background/cancel":
            try: self.send_json(STATE.cancel_background_work(str(payload.get("kind", ""))))
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/tasks/cancel":
            try: self.send_json(STATE.cancel_task(str(payload.get("task_id", ""))))
            except Exception as exc: self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/recu/launch":
            try:
                self.send_json(STATE.recu_browser.launch())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/recu/capture":
            try:
                self.send_json(STATE.recu_browser.capture_session())
            except Exception as exc:
                append_log(f"Recu capture endpoint failed: {type(exc).__name__}: {exc}")
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/recu/test":
            self.send_json({"task_id": STATE.test_recu_session()})
            return
        if path == "/api/recu/clear":
            self.send_json(STATE.clear_recu_session())
            return
        if path == "/api/sorting/heartbeat":
            active = bool(payload.get("active", True))
            queue_id = str(payload.get("queue_id", "") or "")
            self.send_json(STATE.mark_sort_session_active(queue_id=queue_id) if active else STATE.mark_sort_session_inactive())
            return
        if path == "/api/queues/open-fast":
            mode = str(payload.get("mode", "original"))
            model = str(payload.get("model", "")).strip()
            drives = {str(value).upper() for value in payload.get("drives", []) if str(value).strip()}
            if not model:
                self.send_json({"error": "Choose a model."}, 400)
                return
            try:
                self.send_json(STATE.open_model_sort_first(mode, model, drives))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/queues/quick-load":
            mode = str(payload.get("mode", "original"))
            model = str(payload.get("model", "")).strip()
            drives = {str(value).upper() for value in payload.get("drives", []) if str(value).strip()}
            if not model:
                self.send_json({"error": "Choose a model."}, 400)
                return
            try:
                self.send_json(STATE.quick_load_queue(mode, model, drives))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/queues/load":
            mode = str(payload.get("mode", "original"))
            model = str(payload.get("model", "")).strip()
            drives = {str(value).upper() for value in payload.get("drives", []) if str(value).strip()}
            if not model:
                self.send_json({"error": "Choose a model."}, 400)
                return
            self.send_json({"task_id": STATE.load_queue(mode, model, drives)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/draft", path)
        if match:
            accepted = STATE.save_draft(urllib.parse.unquote(match.group(1)), payload)
            self.send_json({"ok": True, "accepted": bool(accepted)})
            return
        if path == "/api/preview-diagnostics/event":
            STATE.preview_diagnostic_event(
                str(payload.get("diagnostic_id", "")),
                str(payload.get("event", "client_event")),
                payload.get("data") if isinstance(payload.get("data"), dict) else {},
            )
            self.send_json({"ok": True})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/preview", path)
        if match:
            try:
                self.send_json(STATE.preview_items(urllib.parse.unquote(match.group(1)), payload.get("indices", [])))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/submit-fast", path)
        if match:
            try:
                self.send_json(STATE.submit_current_fast(urllib.parse.unquote(match.group(1)), payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/skip-fast", path)
        if match:
            try:
                self.send_json(STATE.skip_current_fast(urllib.parse.unquote(match.group(1))))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/submit", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.submit_current(queue_id, payload)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/skip", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.skip_current(queue_id)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/back-fast", path)
        if match:
            try:
                self.send_json(STATE.back_current_fast(urllib.parse.unquote(match.group(1))))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/back", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.back_current(queue_id)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/regenerate", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.mosaic_task(queue_id, True)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/rebuild-chunk", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.rebuild_current_chunk_task(queue_id)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/prefetch", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            self.send_json({"task_id": STATE.prefetch_task(queue_id)})
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/recu-refresh", path)
        if match:
            queue_id = urllib.parse.unquote(match.group(1))
            task_id = STATE.start_recu_for_queue(queue_id, force=True)
            if task_id:
                self.send_json({"task_id": task_id})
            else:
                self.send_json({"error": "Recu is available only for Originals/Review queues."}, 400)
            return
        match = re.fullmatch(r"/api/queues/([^/]+)/delete-preview", path)
        if match:
            try:
                self.send_json(STATE.prepare_permanent_delete(urllib.parse.unquote(match.group(1)), payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/deletion-models/preview":
            try:
                self.send_json(STATE.prepare_model_permanent_delete(payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if path == "/api/offline/sync":
            try:
                self.send_json(STATE.sync_offline_action(payload))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 409)
            return
        if path == "/api/delete-confirm":
            token = str(payload.get("confirmation_token", ""))
            phrase = str(payload.get("phrase", ""))
            with STATE.lock:
                confirmation = STATE.delete_confirmations.get(token)
            if isinstance(confirmation, dict) and str(confirmation.get("kind", "")) == "models_fast":
                try:
                    self.send_json(STATE.confirm_model_permanent_delete_fast(token, phrase))
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 400)
            else:
                self.send_json({"task_id": STATE.confirm_permanent_delete(token, phrase)})
            return
        self.send_json({"error": "Not found."}, 404)


def main() -> None:
    port = int(STATE.config.get("port", 8787))
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    print("\nCTBRec Mobile Reviewer")
    print(f"Local dashboard: http://127.0.0.1:{port}")
    print(f"Fallback PIN: {STATE.config.get('access_pin')}")
    print("The server is loopback-only. Use Tailscale Serve for iPhone access.\n")
    append_log(f"Server starting on 127.0.0.1:{port}")
    server = ResilientThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        STATE.stop_event.set()
        with STATE.lock:
            nsfw_process = STATE.nsfw_process
        if nsfw_process is not None and nsfw_process.poll() is None:
            try:
                nsfw_process.terminate()
                nsfw_process.wait(timeout=5)
            except Exception:
                try: nsfw_process.kill()
                except Exception: pass
        STATE.executor.shutdown(wait=False, cancel_futures=True)
        STATE.interactive_executor.shutdown(wait=False, cancel_futures=True)
        try:
            STATE.live_bridge.stop()
        except Exception:
            pass
        STATE.priority_executor.shutdown(wait=False, cancel_futures=True)
        STATE.prefetch_executor.shutdown(wait=False, cancel_futures=True)
        STATE.library_executor.shutdown(wait=False, cancel_futures=True)
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        append_log("Server stopped.")


if __name__ == "__main__":
    main()
