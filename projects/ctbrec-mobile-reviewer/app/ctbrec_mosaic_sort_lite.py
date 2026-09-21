#!/usr/bin/env python3
"""CTBRec Mosaic Sort Lite

A deliberately small, on-demand mosaic reviewer for CTBRec recording folders.

Performance design:
- Does not read or write CTBRec models.json files.
- Builds only a shallow, cached folder-name list for the searchable model picker; it never indexes videos or model sizes.
- Does not calculate whole-library sizes.
- Never generates for other models; one shared preparation window bounds duration, Recu timing, and mosaic look-ahead.
- Scans only the requested model's matching folders across configured roots.
- Reads only direct video files inside each model folder (never subfolders).
- Runs scanning, ffmpeg work, selectable raw/voice/dialogue audio-waveform analysis, and file moves outside the Tkinter UI thread.
- Optionally reuses existing mosaics; a setting can force fresh generation.
- Shows the first queued chunk immediately using cached/tail/next-start timing, then probes exact durations incrementally inside the configured preparation window.
- Isolates corrupt/locked/unsupported files and uses a clearly marked tail-tag, next-start, or safe-default duration fallback instead of aborting the model.
- Starts Recu scraping only after the first preview is visible and fetches individual Recu video durations only for chunks inside the shared preparation window.
- Uses a persistent Recu cookie session, deduplicates exported cookies, and automatically retries 403s with paired credentials from nearby older releases/config files.
- Shows per-segment kink markers and a compact comment table; double-clicking either opens the local recording at the exact matched timestamp.
- Orders chunks by the exact summed byte sizes of their current video files; mosaic availability never affects order.
- Supports a reversible Back action that restores moved files and reopens the previous chunk with its prior KEEP choices.
- Reuses a duration cache and source-signature manifest for mosaics generated in this session.
- Provides one tabbed settings window with a permanently visible Save footer and a Regenerate mosaic command that forces a fresh current mosaic.
- Offers a Recu-aware queue order (cumshots, then other kinks, then untagged chunks) and a direct model-page browser button.
- Parses both hyphenated CTBRec timestamps and leading dotted timestamps, and treats model_dup# folders as the same logical model.

The review convention matches the full sorter:
- Every file defaults to DELETE.
- Mark only the files to KEEP.
- KEEP -> <model folder>\\Review\\
- DELETE -> <drive>\\MARKED_FOR_DELETION\\<model>\\
"""

from __future__ import annotations

from array import array

import hashlib
import faulthandler
import gzip
import http.cookiejar
import html as html_lib
import importlib
import importlib.util
import difflib
import json
import math
import os
import queue
import re
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
import wave
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:  # pragma: no cover - only relevant on broken Python installs
    raise SystemExit(f"Tkinter is required: {exc}")

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None


APP_NAME = "CTBRec Mosaic Sort Lite"
APP_VERSION = "2.6.6"
VIDEO_EXTS_DEFAULT = {
    ".mp4", ".ts", ".mpegts", ".m2ts", ".mts", ".mov", ".mkv",
    ".m4v", ".avi", ".webm",
}
# Accept both CTBRec's usual ``model_YYYY-MM-DD_HH-MM-SS_...`` names and
# alternate exports such as ``YYYY.MM.DD_HH.MM.SS_model_segment_...``.
TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})[_T -](\d{2})[-.](\d{2})[-.](\d{2})(?!\d)"
)
DUPLICATE_MODEL_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_dup\d+$", re.IGNORECASE)
MOSAIC_DIRNAME = "._chunk_mosaics"
STATE_FILENAME = "._cb_chunk_reviewer_state.json"
DELETION_BUCKET_NAME = "MARKED_FOR_DELETION"
KINK_FIRST_ORDER = "Kinks first (cumshots first)"
RECURBATE_BASE_URL = "https://recurbate.com"
DEFAULT_SETTINGS: Dict[str, Any] = {
    "roots_file": "recording_roots.txt",
    "ffmpeg_path": "",
    "vlc_path": "",
    "audio_waveform_mode": "Raw audio (adjustable coverage)",
    "audio_raw_coverage_percent": 25,
    "audio_raw_max_windows_per_file": 72,
    "audio_waveform_target_bins": 2400,
    "audio_smoothing_radius": 1,
    # Vosk is the default on legacy Windows CPUs because the user's installed
    # CTranslate2 build aborts during Whisper model construction even under its
    # generic one-thread profile. Faster-whisper remains available as an
    # explicitly selected higher-accuracy backend on compatible computers.
    "speech_engine": "Vosk (fast / legacy compatible)",
    "speech_vosk_model": "vosk-model-small-en-us-0.15",
    "speech_vosk_model_path": "",
    "speech_vosk_model_root": "vosk_models",
    "speech_model": "tiny.en",
    "speech_model_path": "",
    "speech_model_download_root": "whisper_models",
    "speech_language": "en",
    "speech_device": "cpu",
    "speech_compute_type": "int8",
    "speech_cpu_threads": 4,
    "speech_beam_size": 1,
    "speech_vad_filter": True,
    "speech_fuzzy_threshold_percent": 74,
    "speech_cache_enabled": True,
    "speech_preextract_audio": True,
    "speech_cpu_compatibility_mode": "Automatic safe retries",
    "extensions": "mp4,ts",
    "chunk_gap_minutes": 30,
    "recent_write_grace_seconds": 120,
    "skip_recent_writes": True,
    "sample_every_seconds": 300,
    "columns": 3,
    "tile_width": 480,
    "max_tiles_per_image": 120,
    "max_total_frames": 240,
    "auto_generate_current": True,
    "chunks_to_prepare": 3,
    "use_existing_mosaics": True,
    "auto_open_mosaic": True,
    "confirm_moves": True,
    "queue_order": "Largest chunks first",
    "recursive_fallback": False,
    "duration_probe_timeout_seconds": 15,
    # A damaged/network-stalled recording must never leave an ffmpeg frame
    # extraction process running forever and pin background mosaic generation.
    "frame_extract_timeout_seconds": 45,
    # v2.13 batches multiple timestamp outputs into one ffmpeg process. This
    # removes process-startup overhead while keeping batches small enough to
    # yield promptly to interactive work.
    "frame_extract_batch_size": 6,
    "duration_fallback_seconds": 900,
    "max_inferred_duration_seconds": 21600,
    "recu_enabled": True,
    "recu_base_url": "https://recu.me",
    "recu_cookie": "",
    "recu_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "recu_site_timezone": "UTC",
    "recu_local_timezone": "America/New_York",
    "recu_cache_hours": 0,
    "recu_request_timeout_seconds": 20,
    "recu_page_limit": 200,
    "recu_throttle_seconds": 0.35,
    "recu_match_tolerance_seconds": 120,
    "recu_scrape_comments": True,
    "recu_lazy_max_duration_hours": 24,
    # Recu/Cloudflare credentials are often short-lived and tied to the browser
    # User-Agent. Automatic fallback preserves the configured profile first,
    # then tries newer nearby v2.2/config credentials without exposing them.
    "recu_auth_fallback": True,
    "recu_warmup_homepage": True,
    "recu_auth_retry_cooldown_seconds": 30,
    "window_geometry": "1100x760",
    "settings_version": 18,
}

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "mosaic_lite_settings.json"
DURATION_CACHE_PATH = APP_DIR / "mosaic_lite_duration_cache.json"
MODEL_NAME_CACHE_PATH = APP_DIR / "mosaic_lite_model_names.json"
MANIFEST_PATH = APP_DIR / "mosaic_manifest.json"  # compatible name with full sorter
LOG_PATH = APP_DIR / "mosaic_lite.log"
RECU_CACHE_PATH = APP_DIR / "mosaic_lite_recu_cache.json"
TRANSCRIPT_CACHE_PATH = APP_DIR / "mosaic_lite_transcript_cache.json"


# ---------------------------------------------------------------------------
# Small data objects
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    path: Path
    start: datetime
    size: int
    mtime: float
    duration: int
    duration_source: str = "probe"
    duration_warning: str = ""

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=max(1, int(self.duration)))


@dataclass
class Chunk:
    idx: int
    folder: Path
    files: List[VideoInfo]
    start: datetime
    end: datetime
    source_bytes: int
    key: str = ""
    signature: str = ""
    mosaics: List[Path] = field(default_factory=list)
    durations_prepared: bool = False
    preparation_warning: str = ""


@dataclass
class AudioSegment:
    file_index: int
    path: Path
    start_seconds: float
    duration: float
    levels: List[float]
    has_audio: bool

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + max(0.0, float(self.duration))


@dataclass
class AudioWaveform:
    chunk_signature: str
    model_name: str
    total_duration: float
    segments: List[AudioSegment]
    levels: List[float]
    mode: str = "Raw audio (rapid sampled)"
    filter_note: str = ""


@dataclass
class SpeechWord:
    start_seconds: float
    end_seconds: float
    text: str
    probability: float = 0.0


@dataclass
class SpeechUtterance:
    file_index: int
    path: Path
    start_seconds: float
    end_seconds: float
    text: str
    words: List[SpeechWord] = field(default_factory=list)
    average_probability: float = 0.0


@dataclass
class SpeechTranscript:
    chunk_signature: str
    model_name: str
    utterances: List[SpeechUtterance]
    file_indices: Tuple[int, ...]
    engine_label: str
    language: str = ""
    warnings: List[str] = field(default_factory=list)


@dataclass
class SpeechMatch:
    file_index: int
    path: Path
    start_seconds: float
    end_seconds: float
    matched_text: str
    context: str
    score: float = 1.0


@dataclass
class RecuMoment:
    kind: str
    video_id: str
    recording_start_site: datetime
    recording_start_local: datetime
    timestamp_seconds: int
    event_site: datetime
    event_local: datetime
    url: str
    kink_slug: str = ""
    kink_name: str = ""
    emoji: str = "🔥"
    comment: str = ""
    likes: int = 0
    # Recu cards label the recording END time. The actual start and event time
    # are valid only after subtracting the video's listed duration.
    recording_end_site: Optional[datetime] = None
    recording_end_local: Optional[datetime] = None
    recording_duration_seconds: int = 0
    timing_resolved: bool = True
    timing_source: str = ""


@dataclass
class RecuModelData:
    model_name: str
    fetched_at: str
    moments: List[RecuMoment] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    source: str = "live"
    # Stable, reusable Recu durations keyed by video ID.
    video_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Persistent incremental-scan memory.  `known_sessions` is model-wide,
    # while `kink_sessions` is per kink slug so a newly discovered kink is
    # still scanned fully once rather than being stopped by another kink's
    # historical boundary.
    scan_memory: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ReviewHistoryEntry:
    """One reversible review action for the Back button."""

    chunk: Chunk
    action: str
    keep_indices: Set[int] = field(default_factory=set)
    moves: List[Dict[str, Any]] = field(default_factory=list)
    previous_review_state: Optional[str] = None


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def append_log(message: str) -> None:
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def human_size(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} PB"


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def format_clock(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def sanitize_window_geometry(value: Any, fallback: str = "1100x760") -> str:
    """Reject only obviously broken saved geometries such as ``1x1``.

    Narrow user-selected windows remain valid; this merely prevents a prior
    shutdown/minimize race from making the next launch effectively invisible.
    """
    text = str(value or "").strip()
    match = re.match(r"^(\d+)x(\d+)([+-]\d+[+-]\d+)?$", text)
    if not match:
        return fallback
    try:
        width = int(match.group(1))
        height = int(match.group(2))
    except Exception:
        return fallback
    if width < 200 or height < 160:
        return fallback
    return text


def normalized_absolute(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def disk_label(path: Path) -> str:
    text = os.path.abspath(str(path))
    drive, _tail = os.path.splitdrive(text)
    if drive:
        return drive.upper()
    try:
        return path.anchor or "(local)"
    except Exception:
        return "(local)"


def parse_roots_file(path: Path) -> List[Path]:
    """Parse an unlimited one-per-line/semicolon/comma root list."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    output: List[Path] = []
    seen: Set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Comments are allowed after whitespace + #, while preserving # in paths.
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        for token in re.split(r"[;,]", line):
            value = token.strip().strip('"')
            if not value:
                continue
            expanded = os.path.expandvars(os.path.expanduser(value))
            item = Path(expanded)
            key = normalized_absolute(item)
            if key not in seen:
                seen.add(key)
                output.append(item)
    return output


def parse_extensions(text: str) -> Set[str]:
    raw = text.strip().lower()
    if not raw or raw == "all":
        return set(VIDEO_EXTS_DEFAULT)
    output: Set[str] = set()
    for part in re.split(r"[,;\s]+", raw):
        if not part:
            continue
        extension = part if part.startswith(".") else "." + part
        if extension in VIDEO_EXTS_DEFAULT:
            output.add(extension)
    return output or {".mp4", ".ts"}


def canonical_model_name(name: str) -> str:
    """Collapse a physical ``model_dup#`` folder name to its logical model."""
    clean = str(name or "").strip()
    match = DUPLICATE_MODEL_SUFFIX_RE.match(clean)
    if match and match.group("base").strip():
        return match.group("base").strip()
    return clean


def parse_start_from_name(name: str, fallback_mtime: float) -> datetime:
    match = TIMESTAMP_RE.search(name)
    if match:
        try:
            year, month, day, hour, minute, second = (int(value) for value in match.groups())
            return datetime(year, month, day, hour, minute, second)
        except Exception:
            pass
    return datetime.fromtimestamp(fallback_mtime)


def unique_destination(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(1, 100_000):
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose a unique destination for {path}")


def review_folder(folder: Path) -> Path:
    return folder / "Review"


def deletion_folder(folder: Path) -> Path:
    absolute = os.path.abspath(str(folder))
    drive, _tail = os.path.splitdrive(absolute)
    if drive:
        return Path(drive + os.sep) / DELETION_BUCKET_NAME / folder.name
    # Safe fallback for non-Windows/testing environments.
    return folder.parent / DELETION_BUCKET_NAME / folder.name


def open_paths(paths: Iterable[Path]) -> Tuple[bool, str]:
    existing = [Path(path) for path in paths if Path(path).exists()]
    if not existing:
        return False, "No existing file was available to open."
    errors: List[str] = []
    for path in existing:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        return False, "; ".join(errors)
    return True, f"Opened {len(existing)} file(s)."


# ---------------------------------------------------------------------------
# Recu/Recurbate metadata integration
# ---------------------------------------------------------------------------

KINK_EMOJI: Dict[str, str] = {
    "squirt": "💦", "cumshot": "💦", "orgasm": "✨", "blowjob": "👄",
    "pussy-licking": "👅", "anal": "🍑", "deep-throat": "👄", "fisting": "✊",
    "double-penetration": "✌️", "wide-open-pussy": "👐", "milk": "🥛",
    "precum": "💧", "pissing": "🚿", "kissing": "💋", "ahegao": "😵‍💫",
    "titfuck": "🍒", "slapping": "🖐️", "flashes": "⚡", "flexing": "💪",
    "boob-flexing": "💪", "showing-tongue": "👅", "licking-feet": "🦶",
    "licking-own-feet": "🦶", "licking-own-tits": "👅", "strap-on": "🦄",
    "armpit": "🙋", "measuring": "📏", "smoking": "🚬", "clamps": "🗜️",
    "gaping": "⭕", "queef": "💨", "fart": "💨", "belly-roll": "🌀",
}
TAIL_DURATION_RE = re.compile(
    r"_tail_(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?_est",
    re.IGNORECASE,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
RECU_VIDEO_HREF_RE = re.compile(
    r'''href=["'](?P<href>[^"']*/(?:[^/"']+/)?video/(?P<video_id>\d+)/play[^"']*)["']''',
    re.IGNORECASE,
)
RECU_DATE_RE = re.compile(
    r'''class=["'][^"']*\bvideo-date\b[^"']*["'][^>]*>\s*([^<]+?)\s*<''',
    re.IGNORECASE | re.DOTALL,
)
RECU_COMMENT_DATE_RE = re.compile(
    r'''class=["'][^"']*(?<![-\w])comment-date(?![-\w])[^"']*["'][^>]*>(.*?)</span>''',
    re.IGNORECASE | re.DOTALL,
)
RECU_COMMENT_TEXT_RE = re.compile(
    r'''class=["'][^"']*\bcomment-item-mes-text\b[^"']*["'][^>]*>(.*?)</span>''',
    re.IGNORECASE | re.DOTALL,
)
RECU_PAGINATION_RE = re.compile(r'''href=["']([^"']*/page/\d+[^"']*)["']''', re.IGNORECASE)
RECU_PERFORMER_KINK_RE = re.compile(
    r'''href=["']([^"']*/performer/[^/"']+/kinks/([^/"'?#]+)(?:/page/\d+)?[^"']*)["']''',
    re.IGNORECASE,
)
RECU_LIKES_RE = re.compile(
    r'''class=["'][^"']*(?<![-\w])comment-like(?![-\w])[^"']*["'][^>]*>(.*?)</span>''',
    re.IGNORECASE | re.DOTALL,
)

RECU_VIDEO_LENGTH_RE = re.compile(
    r'''<span[^>]*class=["\'][^"\']*\bvideo-length\b[^"\']*["\'][^>]*>(.*?)</span>''',
    re.IGNORECASE | re.DOTALL,
)
RECU_DATA_DURATION_RE = re.compile(
    r'''data-duration=["\']([0-9]+(?:\.[0-9]+)?)["\']''',
    re.IGNORECASE,
)
RECU_VIDEO_TIME_RE = re.compile(
    r'''<div[^>]*class=["\'][^"\']*\bvideo-time\b[^"\']*["\'][^>]*>(.*?)</div>''',
    re.IGNORECASE | re.DOTALL,
)
RECU_VIDEO_ID_BLOCK_RE = re.compile(
    r'''<div[^>]*class=["\'][^"\']*\bvideo-thumb\b[^"\']*["\'][^>]*\bdata-id=["\'](\d+)["\']''',
    re.IGNORECASE,
)


def strip_html(value: str) -> str:
    clean = HTML_TAG_RE.sub(" ", value or "")
    clean = html_lib.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def kink_name_from_slug(slug: str) -> str:
    return str(slug or "").replace("-", " ").strip().title()


def kink_emoji(slug: str) -> str:
    return KINK_EMOJI.get(str(slug or "").casefold(), "🔥")


def parse_tail_duration_from_name(name: str) -> Optional[int]:
    match = TAIL_DURATION_RE.search(name)
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def parse_recu_datetime(value: str, site_timezone_name: str) -> Optional[datetime]:
    text = strip_html(value)
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    name = str(site_timezone_name or "UTC").strip()
    if name.upper() in {"UTC", "GMT", "Z"}:
        return parsed.replace(tzinfo=timezone.utc)
    if ZoneInfo is not None:
        try:
            return parsed.replace(tzinfo=ZoneInfo(name))
        except Exception:
            pass
    # Site times are expected to be UTC by default. An unknown site zone falls
    # back to UTC rather than breaking model loading.
    return parsed.replace(tzinfo=timezone.utc)


def _new_york_utc_offset(at_utc: datetime) -> timedelta:
    """DST-aware Eastern offset without requiring the optional tzdata wheel."""
    year = at_utc.year
    march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
    first_sunday_march = 1 + ((6 - march_first.weekday()) % 7)
    second_sunday_march = first_sunday_march + 7
    dst_start_utc = datetime(year, 3, second_sunday_march, 7, tzinfo=timezone.utc)
    november_first = datetime(year, 11, 1, tzinfo=timezone.utc)
    first_sunday_november = 1 + ((6 - november_first.weekday()) % 7)
    dst_end_utc = datetime(year, 11, first_sunday_november, 6, tzinfo=timezone.utc)
    return timedelta(hours=-4 if dst_start_utc <= at_utc < dst_end_utc else -5)


def recu_to_local(value: datetime, local_timezone_name: str) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    name = str(local_timezone_name or "").strip()
    if name.upper() in {"UTC", "GMT", "Z"}:
        return aware.astimezone(timezone.utc)
    if ZoneInfo is not None and name:
        try:
            return aware.astimezone(ZoneInfo(name))
        except Exception:
            pass
    if name.casefold() in {"america/new_york", "us/eastern", "eastern", "est5edt"}:
        utc_value = aware.astimezone(timezone.utc)
        offset = _new_york_utc_offset(utc_value)
        label = "EDT" if offset == timedelta(hours=-4) else "EST"
        return utc_value.astimezone(timezone(offset, label))
    try:
        return aware.astimezone()
    except Exception:
        return aware.astimezone(timezone.utc)


def format_datetime_zone(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


class RecuMetadataCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        raw = load_json(path, {"version": 1, "models": {}})
        self.data: Dict[str, Any] = raw if isinstance(raw, dict) else {"version": 1, "models": {}}
        self.data.setdefault("version", 1)
        self.data.setdefault("models", {})

    def get_raw(self, model_name: str) -> Optional[Dict[str, Any]]:
        key = model_name.casefold().strip()
        with self.lock:
            entry = self.data.get("models", {}).get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def is_fresh(self, model_name: str, hours: float) -> bool:
        entry = self.get_raw(model_name)
        if not entry:
            return False
        try:
            fetched = datetime.fromisoformat(str(entry.get("fetched_at", "")))
            return datetime.now() - fetched <= timedelta(hours=max(0.0, float(hours)))
        except Exception:
            return False

    def set_raw(self, model_name: str, payload: Dict[str, Any]) -> None:
        key = model_name.casefold().strip()
        with self.lock:
            self.data.setdefault("models", {})[key] = payload
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(self.path, self.data)


def recu_data_to_payload(data: RecuModelData) -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "model_name": data.model_name,
        "fetched_at": data.fetched_at,
        "source": data.source,
        "errors": list(data.errors),
        "video_meta": dict(data.video_meta),
        "scan_memory": dict(data.scan_memory),
        "moments": [
            {
                "kind": item.kind,
                "video_id": item.video_id,
                "recording_start_site": item.recording_start_site.isoformat(),
                "recording_start_local": item.recording_start_local.isoformat(),
                "recording_end_site": item.recording_end_site.isoformat() if item.recording_end_site else "",
                "recording_end_local": item.recording_end_local.isoformat() if item.recording_end_local else "",
                "recording_duration_seconds": int(item.recording_duration_seconds),
                "timing_resolved": bool(item.timing_resolved),
                "timing_source": item.timing_source,
                "timestamp_seconds": item.timestamp_seconds,
                "event_site": item.event_site.isoformat(),
                "event_local": item.event_local.isoformat(),
                "url": item.url,
                "kink_slug": item.kink_slug,
                "kink_name": item.kink_name,
                "emoji": item.emoji,
                "comment": item.comment,
                "likes": item.likes,
            }
            for item in data.moments
        ],
    }


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    try:
        text = str(value or "").strip()
        return datetime.fromisoformat(text) if text else None
    except Exception:
        return None


def _video_meta_duration(meta: Any) -> int:
    if not isinstance(meta, dict):
        return 0
    try:
        value = int(round(float(meta.get("duration_seconds", 0) or 0)))
        return value if 0 < value <= 7 * 24 * 3600 else 0
    except Exception:
        return 0


def _apply_recu_timing(
    moment: RecuMoment,
    duration_seconds: int,
    local_timezone_name: str,
    source: str,
) -> None:
    duration = max(1, int(duration_seconds))
    end_site = moment.recording_end_site or moment.recording_start_site
    if end_site.tzinfo is None:
        end_site = end_site.replace(tzinfo=timezone.utc)
    start_site = end_site - timedelta(seconds=duration)
    event_site = start_site + timedelta(seconds=max(0, int(moment.timestamp_seconds)))
    moment.recording_end_site = end_site
    moment.recording_end_local = recu_to_local(end_site, local_timezone_name)
    moment.recording_start_site = start_site
    moment.recording_start_local = recu_to_local(start_site, local_timezone_name)
    moment.event_site = event_site
    moment.event_local = recu_to_local(event_site, local_timezone_name)
    moment.recording_duration_seconds = duration
    moment.timing_resolved = True
    moment.timing_source = source


def recu_data_from_payload(
    payload: Optional[Dict[str, Any]],
    source: str = "cache",
) -> Optional[RecuModelData]:
    if not isinstance(payload, dict):
        return None
    raw_video_meta = payload.get("video_meta", {})
    video_meta: Dict[str, Dict[str, Any]] = {
        str(video_id): dict(meta)
        for video_id, meta in raw_video_meta.items()
        if isinstance(meta, dict) and _video_meta_duration(meta) > 0
    } if isinstance(raw_video_meta, dict) else {}
    moments: List[RecuMoment] = []
    local_zone = "America/New_York"
    for raw in payload.get("moments", []):
        if not isinstance(raw, dict):
            continue
        try:
            video_id = str(raw.get("video_id", ""))
            old_named_start_site = _parse_iso_datetime(raw.get("recording_start_site"))
            old_named_start_local = _parse_iso_datetime(raw.get("recording_start_local"))
            explicit_end_site = _parse_iso_datetime(raw.get("recording_end_site"))
            explicit_end_local = _parse_iso_datetime(raw.get("recording_end_local"))
            # v2.0 incorrectly stored the site's END label in recording_start_*.
            # Treat that value as the end during cache migration.
            end_site = explicit_end_site or old_named_start_site
            if end_site is None:
                continue
            end_local = explicit_end_local or old_named_start_local or recu_to_local(end_site, local_zone)
            event_site = _parse_iso_datetime(raw.get("event_site")) or end_site
            event_local = _parse_iso_datetime(raw.get("event_local")) or end_local
            duration = int(raw.get("recording_duration_seconds", 0) or 0)
            if duration <= 0:
                duration = _video_meta_duration(video_meta.get(video_id))
            moment = RecuMoment(
                kind=str(raw.get("kind", "")),
                video_id=video_id,
                recording_start_site=old_named_start_site or end_site,
                recording_start_local=old_named_start_local or end_local,
                timestamp_seconds=int(raw.get("timestamp_seconds", 0)),
                event_site=event_site,
                event_local=event_local,
                url=str(raw.get("url", "")),
                kink_slug=str(raw.get("kink_slug", "")),
                kink_name=str(raw.get("kink_name", "")),
                emoji=str(raw.get("emoji", "🔥")),
                comment=str(raw.get("comment", "")),
                likes=int(raw.get("likes", 0) or 0),
                recording_end_site=end_site,
                recording_end_local=end_local,
                recording_duration_seconds=max(0, duration),
                timing_resolved=False,
                timing_source=str(raw.get("timing_source", "legacy cache")),
            )
            if duration > 0:
                _apply_recu_timing(
                    moment,
                    duration,
                    str(payload.get("local_timezone", local_zone)),
                    str(raw.get("timing_source", "cached Recu duration")) or "cached Recu duration",
                )
            moments.append(moment)
        except Exception:
            continue
    return RecuModelData(
        model_name=str(payload.get("model_name", "")),
        fetched_at=str(payload.get("fetched_at", "")),
        moments=moments,
        errors=[str(value) for value in payload.get("errors", [])],
        source=source,
        video_meta=video_meta,
        scan_memory=dict(payload.get("scan_memory", {})) if isinstance(payload.get("scan_memory"), dict) else {},
    )


def _safe_header_value(value: Any) -> str:
    """Return a single-line HTTP header value without logging its contents."""
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _cookie_pairs(cookie_header: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for raw_part in str(cookie_header or "").split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name or any(char in name for char in "\r\n\t ,"):
            continue
        pairs.append((name, value.strip()))
    return pairs


def normalize_cookie_header(cookie_header: str) -> str:
    """Deduplicate exported browser cookies, retaining the newest occurrence."""
    ordered: Dict[str, Tuple[str, str]] = {}
    for name, value in _cookie_pairs(_safe_header_value(cookie_header)):
        key = name.casefold()
        if key in ordered:
            ordered.pop(key, None)
        ordered[key] = (name, value)
    return "; ".join(f"{name}={value}" for name, value in ordered.values())


def merge_cookie_headers(*headers: str) -> str:
    """Merge cookie strings left-to-right; later sources win by cookie name."""
    ordered: Dict[str, Tuple[str, str]] = {}
    for header in headers:
        for name, value in _cookie_pairs(header):
            key = name.casefold()
            if key in ordered:
                ordered.pop(key, None)
            ordered[key] = (name, value)
    return "; ".join(f"{name}={value}" for name, value in ordered.values())


def _extract_recu_header_from_json(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    header = raw.get("header") or raw.get("headers")
    if isinstance(header, dict):
        cookie = _safe_header_value(header.get("Cookie") or header.get("cookie") or "")
        user_agent = _safe_header_value(
            header.get("User-Agent")
            or header.get("user-agent")
            or header.get("user_agent")
            or ""
        )
    else:
        cookie = _safe_header_value(raw.get("recu_cookie") or raw.get("cookie") or "")
        user_agent = _safe_header_value(
            raw.get("recu_user_agent")
            or raw.get("user_agent")
            or raw.get("User-Agent")
            or ""
        )
    if not cookie and not user_agent:
        return None
    return {"Cookie": cookie, "User-Agent": user_agent}


_NEARBY_RECU_HEADERS_LOCK = threading.Lock()
_NEARBY_RECU_HEADERS_CACHED_AT = 0.0
_NEARBY_RECU_HEADERS_CACHE: List[Dict[str, str]] = []


def nearby_recu_headers() -> List[Dict[str, str]]:
    """Collect paired Recu Cookie/User-Agent profiles, newest file first.

    Packages are frequently extracted beside an older working v2.2 folder. The
    old implementation inspected only the first hard-coded config path, so a
    stale configured cookie could permanently mask a newer working one. This
    bounded search keeps the current folder primary but can recover the exact
    credential pair used by a nearby release.
    """
    global _NEARBY_RECU_HEADERS_CACHED_AT, _NEARBY_RECU_HEADERS_CACHE
    with _NEARBY_RECU_HEADERS_LOCK:
        if _NEARBY_RECU_HEADERS_CACHE and time.monotonic() - _NEARBY_RECU_HEADERS_CACHED_AT < 60.0:
            return [dict(item) for item in _NEARBY_RECU_HEADERS_CACHE]

    candidates: List[Path] = [
        APP_DIR / "config.json",
        APP_DIR / "recu_config.json",
        APP_DIR / "recu" / "config.json",
        APP_DIR.parent / "config.json",
        APP_DIR.parent / "recu" / "config.json",
    ]
    for root in (APP_DIR, APP_DIR.parent):
        try:
            candidates.extend(root.glob("mosaic_lite_settings*.json"))
            candidates.extend(root.glob("*/mosaic_lite_settings*.json"))
            candidates.extend(root.glob("*/recu_config.json"))
            candidates.extend(root.glob("*/recu/config.json"))
        except Exception:
            continue
    unique: Dict[str, Path] = {}
    for candidate in candidates:
        try:
            if candidate.is_file():
                unique[normalized_absolute(candidate)] = candidate
        except Exception:
            continue
    ordered_paths = sorted(
        unique.values(),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )[:60]
    profiles: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for candidate in ordered_paths:
        raw = load_json(candidate, {})
        header = _extract_recu_header_from_json(raw)
        if not header:
            continue
        cookie = normalize_cookie_header(header.get("Cookie", ""))
        user_agent = _safe_header_value(header.get("User-Agent", ""))
        fingerprint = hashlib.sha256((user_agent + "\0" + cookie).encode("utf-8", errors="replace")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        profiles.append({
            "Cookie": cookie,
            "User-Agent": user_agent,
            "Source": candidate.name,
        })
    with _NEARBY_RECU_HEADERS_LOCK:
        _NEARBY_RECU_HEADERS_CACHE = [dict(item) for item in profiles]
        _NEARBY_RECU_HEADERS_CACHED_AT = time.monotonic()
    return profiles


def nearby_recu_header() -> Dict[str, str]:
    profiles = nearby_recu_headers()
    return dict(profiles[0]) if profiles else {}


def _valid_browser_user_agent(value: str) -> str:
    user_agent = _safe_header_value(value)
    if user_agent:
        return user_agent
    return str(DEFAULT_SETTINGS["recu_user_agent"])


def recu_header_profiles(settings: Dict[str, Any]) -> List[Dict[str, str]]:
    configured_cookie_raw = _safe_header_value(str(settings.get("recu_cookie", "")))
    configured_cookie = normalize_cookie_header(configured_cookie_raw)
    configured_ua = _valid_browser_user_agent(str(settings.get("recu_user_agent", "")))
    fallback_enabled = bool(settings.get("recu_auth_fallback", True))
    profiles: List[Dict[str, str]] = []

    def add(
        label: str,
        cookie: str,
        user_agent: str,
        *,
        legacy_exact: bool = False,
    ) -> None:
        selected_cookie = _safe_header_value(cookie) if legacy_exact else normalize_cookie_header(cookie)
        normalized_ua = _valid_browser_user_agent(user_agent)
        key_material = (
            ("legacy\0" if legacy_exact else "session\0")
            + normalized_ua
            + "\0"
            + selected_cookie
        )
        key = hashlib.sha256(key_material.encode("utf-8", errors="replace")).hexdigest()
        if any(item.get("key") == key for item in profiles):
            return
        profiles.append({
            "label": label,
            "Cookie": selected_cookie,
            "User-Agent": normalized_ua,
            "key": key,
            "legacy_exact": "1" if legacy_exact else "0",
        })

    # Preserve the exact request shape used by the uploaded working v2.2 build
    # before trying any enhanced session logic.
    add("current settings — v2.2 exact", configured_cookie_raw, configured_ua, legacy_exact=True)
    if configured_cookie != configured_cookie_raw:
        add("current settings — cleaned session", configured_cookie, configured_ua)

    if fallback_enabled:
        nearby = nearby_recu_headers()
        for index, header in enumerate(nearby, start=1):
            nearby_cookie = header.get("Cookie", "")
            nearby_ua = header.get("User-Agent", "") or configured_ua
            add(f"nearby credentials {index} — v2.2 exact", nearby_cookie, nearby_ua, legacy_exact=True)
            add(f"nearby credentials {index} — persistent session", nearby_cookie, nearby_ua)
            if configured_cookie and nearby_cookie:
                # A fresh cf_clearance/fingerprint from the nearby profile wins,
                # while unrelated persistent preferences from current settings
                # remain available.
                add(
                    f"merged nearby credentials {index}",
                    merge_cookie_headers(configured_cookie, nearby_cookie),
                    nearby_ua,
                )
        # Anonymous is useful when the stale/expired Cookie itself triggers the
        # denial while public performer pages remain accessible.
        add("without cookie", "", configured_ua)
    return profiles


def recu_headers(
    settings: Dict[str, Any],
    profile: Optional[Dict[str, str]] = None,
    referer: str = "",
) -> Dict[str, str]:
    if profile is not None:
        selected = profile
    else:
        available_profiles = recu_header_profiles(settings)
        selected = available_profiles[0] if available_profiles else {}
    user_agent = _valid_browser_user_agent(selected.get("User-Agent", ""))
    if str(selected.get("legacy_exact", "0")) == "1":
        # Byte-for-byte header policy from v2.2, including its raw Cookie string.
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        cookie = _safe_header_value(selected.get("Cookie", ""))
        if cookie:
            headers["Cookie"] = cookie
        return headers

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    chrome_match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
    if chrome_match:
        major = chrome_match.group(1)
        headers["sec-ch-ua"] = f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = '"Windows"'
    return headers


class RecuAccessError(RuntimeError):
    def __init__(self, status: int, url: str, profile_label: str, challenge: bool = False) -> None:
        self.status = int(status)
        self.url = url
        self.profile_label = profile_label
        self.challenge = bool(challenge)
        suffix = " (Cloudflare/browser verification)" if challenge else ""
        super().__init__(f"HTTP {self.status}{suffix} for {url}")


class _RecuHttpSession:
    def __init__(self, canonical_base_url: str, profile: Dict[str, str]) -> None:
        self.canonical_base_url = canonical_base_url.rstrip("/")
        self.profile = dict(profile)
        self.cookie_jar = http.cookiejar.CookieJar()
        host = urllib.parse.urlparse(self.canonical_base_url).hostname or "recu.me"
        seed_pairs = (
            []
            if str(self.profile.get("legacy_exact", "0")) == "1"
            else _cookie_pairs(self.profile.get("Cookie", ""))
        )
        for name, value in seed_pairs:
            try:
                self.cookie_jar.set_cookie(http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=host,
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=self.canonical_base_url.casefold().startswith("https://"),
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None},
                    rfc2109=False,
                ))
            except Exception:
                continue
        self.opener = urllib.request.build_opener(
            _CanonicalRecuRedirectHandler(self.canonical_base_url),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
        )
        self.warmed = False
        self.lock = threading.Lock()

    @staticmethod
    def _decode(raw: bytes, headers: Any) -> str:
        encoding = str(headers.get("Content-Encoding", "")).casefold()
        try:
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            elif "deflate" in encoding:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
        charset = headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    def _open_once(self, url: str, settings: Dict[str, Any], timeout: int, referer: str = "") -> str:
        request = urllib.request.Request(
            url,
            headers=recu_headers(settings, self.profile, referer=referer),
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return self._decode(response.read(), response.headers)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(32_768)
                body = self._decode(raw, exc.headers)
            except Exception:
                body = ""
            lower = body.casefold()
            challenge = any(token in lower for token in (
                "cloudflare", "cf-chl-", "just a moment", "verify you are human", "attention required",
            ))
            raise RecuAccessError(
                exc.code,
                url,
                str(self.profile.get("label", "credentials")),
                challenge=challenge,
            ) from None

    def get(self, url: str, settings: Dict[str, Any], timeout: int, cancel: threading.Event) -> str:
        with self.lock:
            if cancel.is_set():
                raise RuntimeError("Recu scraping cancelled.")
            if bool(settings.get("recu_warmup_homepage", True)) and not self.warmed:
                self.warmed = True
                home = self.canonical_base_url + "/"
                if canonicalize_recu_url(url, self.canonical_base_url) != home:
                    try:
                        self._open_once(home, settings, timeout, referer="")
                    except Exception:
                        # Some sites protect the landing page more strictly than
                        # performer pages; a failed warm-up must not block target.
                        pass
            referer = self.canonical_base_url + "/"
            return self._open_once(url, settings, timeout, referer=referer)


_RECU_HTTP_LOCK = threading.Lock()
_RECU_HTTP_SESSIONS: Dict[str, _RecuHttpSession] = {}
_RECU_WORKING_PROFILE: Dict[str, str] = {}
_RECU_DENIED_UNTIL: Dict[str, float] = {}


def reset_recu_http_sessions() -> None:
    global _NEARBY_RECU_HEADERS_CACHED_AT, _NEARBY_RECU_HEADERS_CACHE
    with _RECU_HTTP_LOCK:
        _RECU_HTTP_SESSIONS.clear()
        _RECU_WORKING_PROFILE.clear()
        _RECU_DENIED_UNTIL.clear()
    with _NEARBY_RECU_HEADERS_LOCK:
        _NEARBY_RECU_HEADERS_CACHE = []
        _NEARBY_RECU_HEADERS_CACHED_AT = 0.0


def _recu_session(canonical_base: str, profile: Dict[str, str]) -> _RecuHttpSession:
    key = canonical_base.rstrip("/") + "\0" + str(profile.get("key", ""))
    with _RECU_HTTP_LOCK:
        session = _RECU_HTTP_SESSIONS.get(key)
        if session is None:
            session = _RecuHttpSession(canonical_base, profile)
            _RECU_HTTP_SESSIONS[key] = session
        return session


def canonicalize_recu_url(raw_url: str, canonical_base_url: str) -> str:
    canonical_base = str(canonical_base_url or "https://recu.me").strip().rstrip("/")
    base_parts = urllib.parse.urlparse(canonical_base)
    joined = urllib.parse.urljoin(canonical_base + "/", html_lib.unescape(str(raw_url or "")))
    parts = urllib.parse.urlparse(joined)
    host = (parts.hostname or "").casefold()
    canonical_host = (base_parts.hostname or "recu.me").casefold()
    # Recu occasionally emits locale-prefixed hosts and, in the reported bug,
    # a doubly-prefixed non-existent host such as es.es.recu.me. All Recu-owned
    # hosts are safely put back onto the configured canonical origin.
    if host == "recu.me" or host.endswith(".recu.me") or host == canonical_host:
        scheme = base_parts.scheme or parts.scheme or "https"
        netloc = base_parts.netloc or "recu.me"
        parts = parts._replace(scheme=scheme, netloc=netloc)
    return urllib.parse.urlunparse(parts)


class _CanonicalRecuRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, canonical_base_url: str) -> None:
        super().__init__()
        self.canonical_base_url = canonical_base_url

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        fixed = canonicalize_recu_url(newurl, self.canonical_base_url)
        return super().redirect_request(req, fp, code, msg, headers, fixed)


def fetch_recu_html(
    url: str,
    settings: Dict[str, Any],
    cancel: threading.Event,
    attempts: int = 2,
) -> str:
    """Fetch a Recu page with persistent cookies and automatic 403 recovery.

    The first request profile remains the configured v2.2-compatible Cookie and
    User-Agent. On access denial, the function retries with paired credentials
    discovered from nearby older releases/config files, then without a cookie.
    A successful profile is remembered for the rest of the process.
    """
    timeout = max(5, int(settings.get("recu_request_timeout_seconds", 20)))
    canonical_base = str(settings.get("recu_base_url", "https://recu.me")).strip().rstrip("/")
    fixed_url = canonicalize_recu_url(url, canonical_base)
    profiles = recu_header_profiles(settings)
    if not profiles:
        profiles = [{
            "label": "default browser profile",
            "Cookie": "",
            "User-Agent": str(DEFAULT_SETTINGS["recu_user_agent"]),
            "key": "default",
        }]

    auth_fingerprint = hashlib.sha256(
        (canonical_base + "\0" + "\0".join(str(item.get("key", "")) for item in profiles)).encode("utf-8")
    ).hexdigest()
    cooldown_key = canonical_base + "\0" + auth_fingerprint
    now = time.monotonic()
    with _RECU_HTTP_LOCK:
        denied_until = _RECU_DENIED_UNTIL.get(cooldown_key, 0.0)
        working_key = _RECU_WORKING_PROFILE.get(canonical_base, "")
    if denied_until > now:
        remaining = max(1, int(math.ceil(denied_until - now)))
        raise RuntimeError(
            f"HTTP 403 for {fixed_url}; all available Recu credential profiles were rejected "
            f"({remaining}s retry cooldown)."
        )
    if working_key:
        profiles.sort(key=lambda item: 0 if item.get("key") == working_key else 1)

    last_error: Optional[Exception] = None
    denied_profiles: List[str] = []
    for profile in profiles:
        session = _recu_session(canonical_base, profile)
        for attempt in range(1, max(1, attempts) + 1):
            if cancel.is_set():
                raise RuntimeError("Recu scraping cancelled.")
            try:
                html = session.get(fixed_url, settings, timeout, cancel)
                with _RECU_HTTP_LOCK:
                    _RECU_WORKING_PROFILE[canonical_base] = str(profile.get("key", ""))
                    _RECU_DENIED_UNTIL.pop(cooldown_key, None)
                return html
            except RecuAccessError as exc:
                last_error = exc
                if exc.status in {401, 403}:
                    denied_profiles.append(str(profile.get("label", "credentials")))
                    break  # another retry with identical credentials cannot solve auth
                if exc.status == 404:
                    raise RuntimeError(str(exc)) from None
            except Exception as exc:
                last_error = exc
            if attempt < attempts:
                cancel.wait(min(3.0, 0.8 * attempt))

    if denied_profiles and len(set(denied_profiles)) >= len(profiles):
        cooldown = max(0, int(settings.get("recu_auth_retry_cooldown_seconds", 30)))
        with _RECU_HTTP_LOCK:
            _RECU_DENIED_UNTIL[cooldown_key] = time.monotonic() + cooldown
        challenge_note = (
            " Cloudflare/browser verification rejected the available Cookie/User-Agent pairs."
            if isinstance(last_error, RecuAccessError) and last_error.challenge
            else ""
        )
        raise RuntimeError(
            f"HTTP 403 for {fixed_url} after trying {len(profiles)} Recu credential profile(s)."
            f"{challenge_note} The existing Recu cache remains available; refresh the browser cookie only if the automatic nearby-profile recovery also fails."
        )
    raise RuntimeError(str(last_error or f"Could not fetch {fixed_url}"))


def _pagination_root(path: str) -> str:
    return re.sub(r"/page/\d+/?$", "", str(path or "")).rstrip("/")


def collect_recu_page_links(
    base_url: str,
    page_html: str,
    canonical_base_url: Optional[str] = None,
) -> List[str]:
    canonical = canonical_base_url or base_url
    current = urllib.parse.urlparse(canonicalize_recu_url(base_url, canonical))
    current_root = _pagination_root(current.path)
    seen: Set[str] = set()
    output: List[str] = []
    for raw in RECU_PAGINATION_RE.findall(page_html):
        link = canonicalize_recu_url(urllib.parse.urljoin(base_url, html_lib.unescape(raw)), canonical)
        parsed = urllib.parse.urlparse(link)
        # Do not let unrelated paginated widgets or localized host links leak
        # into the current kink/comments queue.
        if _pagination_root(parsed.path) != current_root:
            continue
        if link not in seen:
            seen.add(link)
            output.append(link)

    def page_number(value: str) -> int:
        match = re.search(r"/page/(\d+)", urllib.parse.urlparse(value).path)
        return int(match.group(1)) if match else 1

    return sorted(output, key=lambda value: (page_number(value), value))


def collect_model_kink_slugs(base_url: str, page_html: str) -> List[str]:
    found: Set[str] = set()
    for _href, slug in RECU_PERFORMER_KINK_RE.findall(page_html):
        clean = urllib.parse.unquote(slug).strip().casefold()
        if clean and clean != "page":
            found.add(clean)
    for match in RECU_VIDEO_HREF_RE.finditer(page_html):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(html_lib.unescape(match.group("href"))).query
        )
        for value in query.get("k", []):
            clean = value.strip().casefold()
            if clean:
                found.add(clean)
    return sorted(found)


def _video_card_blocks(page_html: str) -> List[str]:
    starts = [
        match.start()
        for match in re.finditer(
            r'''<div\s+class=["'][^"']*\bvideo-thumb\b''',
            page_html,
            re.IGNORECASE,
        )
    ]
    if not starts:
        return []
    starts.append(len(page_html))
    return [page_html[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]


def parse_clock_seconds(value: str) -> Optional[int]:
    text = strip_html(value).strip()
    match = re.search(r"(?<!\d)(\d{1,3}:\d{2}(?::\d{2})?)(?!\d)", text)
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def parse_recu_video_duration(page_html: str) -> Tuple[Optional[int], str]:
    match = RECU_DATA_DURATION_RE.search(page_html)
    if match:
        try:
            value = float(match.group(1))
            # Be tolerant if a future layout exposes milliseconds.
            if value > 7 * 24 * 3600 and value / 1000 <= 7 * 24 * 3600:
                value /= 1000
            seconds = int(round(value))
            if 0 < seconds <= 7 * 24 * 3600:
                return seconds, "video page data-duration"
        except Exception:
            pass
    match = RECU_VIDEO_LENGTH_RE.search(page_html)
    if match:
        seconds = parse_clock_seconds(match.group(1))
        if seconds:
            return seconds, "video page video-length"
    for raw in RECU_VIDEO_TIME_RE.findall(page_html):
        seconds = parse_clock_seconds(raw)
        if seconds:
            return seconds, "recording card duration"
    return None, ""


def collect_recu_card_durations(page_html: str) -> Dict[str, Dict[str, Any]]:
    output: Dict[str, Dict[str, Any]] = {}
    for block in _video_card_blocks(page_html):
        id_match = RECU_VIDEO_ID_BLOCK_RE.search(block)
        href_match = RECU_VIDEO_HREF_RE.search(block)
        video_id = id_match.group(1) if id_match else (href_match.group("video_id") if href_match else "")
        if not video_id:
            continue
        duration, source = parse_recu_video_duration(block)
        if duration:
            output[video_id] = {
                "duration_seconds": duration,
                "source": source,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
    return output


def _unresolved_recu_moment(
    kind: str,
    video_id: str,
    recording_end_site: datetime,
    timestamp_seconds: int,
    url: str,
    settings: Dict[str, Any],
    **kwargs: Any,
) -> RecuMoment:
    local_zone = str(settings.get("recu_local_timezone", "America/New_York"))
    end_local = recu_to_local(recording_end_site, local_zone)
    return RecuMoment(
        kind=kind,
        video_id=video_id,
        recording_start_site=recording_end_site,
        recording_start_local=end_local,
        timestamp_seconds=max(0, int(timestamp_seconds)),
        event_site=recording_end_site,
        event_local=end_local,
        url=url,
        recording_end_site=recording_end_site,
        recording_end_local=end_local,
        recording_duration_seconds=0,
        timing_resolved=False,
        timing_source="awaiting Recu video duration",
        **kwargs,
    )


def parse_recu_kink_page(
    page_url: str,
    page_html: str,
    slug: str,
    settings: Dict[str, Any],
) -> List[RecuMoment]:
    output: List[RecuMoment] = []
    site_zone = str(settings.get("recu_site_timezone", "UTC"))
    canonical_base = str(settings.get("recu_base_url", "https://recu.me"))
    for block in _video_card_blocks(page_html):
        href_match = RECU_VIDEO_HREF_RE.search(block)
        date_match = RECU_DATE_RE.search(block)
        if href_match is None or date_match is None:
            continue
        href = canonicalize_recu_url(
            urllib.parse.urljoin(page_url, html_lib.unescape(href_match.group("href"))),
            canonical_base,
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        try:
            timestamp_seconds = int((query.get("t") or ["0"])[0])
        except Exception:
            timestamp_seconds = 0
        recording_end_site = parse_recu_datetime(date_match.group(1), site_zone)
        if recording_end_site is None:
            continue
        active_slug = str((query.get("k") or [slug])[0] or slug).casefold()
        output.append(
            _unresolved_recu_moment(
                kind="kink",
                video_id=href_match.group("video_id"),
                recording_end_site=recording_end_site,
                timestamp_seconds=timestamp_seconds,
                url=href,
                settings=settings,
                kink_slug=active_slug,
                kink_name=kink_name_from_slug(active_slug),
                emoji=kink_emoji(active_slug),
            )
        )
    return output


def _comment_blocks(page_html: str) -> List[str]:
    starts = [
        match.start()
        for match in re.finditer(
            r'''<div\s+class=["'][^"']*(?<![-\w])comment-item(?![-\w])[^"']*["']''',
            page_html,
            re.IGNORECASE,
        )
    ]
    if not starts:
        return []
    starts.append(len(page_html))
    return [page_html[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]


def parse_recu_comments_page(
    page_url: str,
    page_html: str,
    settings: Dict[str, Any],
) -> List[RecuMoment]:
    output: List[RecuMoment] = []
    site_zone = str(settings.get("recu_site_timezone", "UTC"))
    canonical_base = str(settings.get("recu_base_url", "https://recu.me"))
    for block in _comment_blocks(page_html):
        date_match = RECU_COMMENT_DATE_RE.search(block)
        if date_match is None:
            continue
        recording_end_site = parse_recu_datetime(date_match.group(1), site_zone)
        if recording_end_site is None:
            continue
        text_match = RECU_COMMENT_TEXT_RE.search(block)
        comment_text = strip_html(text_match.group(1) if text_match else "")
        comment_text = re.sub(r"^(?:\d{1,2}:)?\d{1,2}:\d{2}\s*", "", comment_text).strip()
        comment_text = re.sub(r"\s*More\s*$", "", comment_text, flags=re.IGNORECASE).strip()
        likes_match = RECU_LIKES_RE.search(block)
        likes_values = re.findall(r"\d+", strip_html(likes_match.group(1) if likes_match else ""))
        likes = int(likes_values[0]) if likes_values else 0
        seen: Set[Tuple[str, int]] = set()
        for href_match in RECU_VIDEO_HREF_RE.finditer(block):
            href = canonicalize_recu_url(
                urllib.parse.urljoin(page_url, html_lib.unescape(href_match.group("href"))),
                canonical_base,
            )
            query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "t" not in query:
                continue
            try:
                timestamp_seconds = int(query["t"][0])
            except Exception:
                continue
            key = (href_match.group("video_id"), timestamp_seconds)
            if key in seen:
                continue
            seen.add(key)
            output.append(
                _unresolved_recu_moment(
                    kind="comment",
                    video_id=href_match.group("video_id"),
                    recording_end_site=recording_end_site,
                    timestamp_seconds=timestamp_seconds,
                    url=href,
                    settings=settings,
                    comment=comment_text or "Commented timestamp",
                    likes=likes,
                    emoji="💬",
                )
            )
    return output


def resolve_recu_moment_timings(
    moments: Sequence[RecuMoment],
    video_meta: Dict[str, Dict[str, Any]],
    settings: Dict[str, Any],
    cancel: threading.Event,
    errors: List[str],
    progress: Optional[Callable[[str], None]] = None,
    only_video_ids: Optional[Set[str]] = None,
    fetch_html_fn: Optional[Callable[..., str]] = None,
) -> None:
    """Resolve Recu recording durations for a bounded set of video IDs.

    The model scrape intentionally leaves most moments unresolved. Individual
    video pages are fetched only when the current or immediately upcoming chunk
    could contain one of those moments.  Mobile Reviewer may supply a verified
    Chrome/CDP-backed fetcher; the desktop sorter continues to use the legacy
    persistent HTTP session when no callback is supplied.
    """
    base = str(settings.get("recu_base_url", "https://recu.me")).rstrip("/")
    local_zone = str(settings.get("recu_local_timezone", "America/New_York"))
    throttle = max(0.0, float(settings.get("recu_throttle_seconds", 0.35)))
    requested = {str(value) for value in (only_video_ids or set()) if str(value)}
    video_ids = sorted({
        item.video_id for item in moments
        if item.video_id and (not requested or item.video_id in requested)
    })

    def fetch_one(url: str, expect_listing: bool = False) -> str:
        if fetch_html_fn is not None:
            return fetch_html_fn(url, expect_listing=expect_listing)
        return fetch_recu_html(url, settings, cancel)

    for index, video_id in enumerate(video_ids, start=1):
        if cancel.is_set():
            raise RuntimeError("Recu scraping cancelled.")
        duration = _video_meta_duration(video_meta.get(video_id))
        if duration <= 0:
            if progress:
                progress(f"Recu timing {index}/{len(video_ids)} — reading duration for video {video_id}…")
            video_url = canonicalize_recu_url(f"{base}/video/{video_id}/play", base)
            try:
                page_html = fetch_one(video_url, expect_listing=False)
                duration, source = parse_recu_video_duration(page_html)
                if not duration:
                    raise RuntimeError("recording duration was not found on the video page")
                video_meta[video_id] = {
                    "duration_seconds": int(duration),
                    "source": source,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                }
            except Exception as exc:
                if getattr(exc, "reauth_required", False):
                    raise
                errors.append(f"{video_url}: timing unresolved: {exc}")
                continue
            if throttle:
                cancel.wait(throttle)
        meta = video_meta.get(video_id, {})
        duration = _video_meta_duration(meta)
        source = str(meta.get("source", "cached Recu duration")) if isinstance(meta, dict) else "cached Recu duration"
        for moment in moments:
            if moment.video_id == video_id and duration > 0:
                _apply_recu_timing(moment, duration, local_zone, source)


def _recu_page_number(url: str) -> int:
    match = re.search(r"/page/(\d+)", urllib.parse.urlparse(str(url)).path)
    return int(match.group(1)) if match else 1


def _recu_memory_from_cached(cached: Optional[RecuModelData]) -> Dict[str, Any]:
    """Return normalized incremental scan memory, migrating old caches safely."""
    memory: Dict[str, Any] = dict(cached.scan_memory) if cached is not None and isinstance(cached.scan_memory, dict) else {}
    known_sessions = memory.get("known_sessions")
    if not isinstance(known_sessions, dict):
        known_sessions = {}
    kink_sessions = memory.get("kink_sessions")
    if not isinstance(kink_sessions, dict):
        kink_sessions = {}
    # v2.14 migration: an older cache has no explicit scan memory.  Its moments
    # still provide a reliable boundary seed, grouped by kink slug.
    if cached is not None:
        for item in cached.moments:
            if not item.video_id:
                continue
            end = item.recording_end_site or item.recording_start_site
            stamp = end.isoformat() if isinstance(end, datetime) else ""
            known_sessions.setdefault(str(item.video_id), stamp)
            slug = str(item.kink_slug or "").casefold().strip()
            if slug:
                bucket = kink_sessions.setdefault(slug, {})
                if isinstance(bucket, dict):
                    bucket.setdefault(str(item.video_id), stamp)
    memory["schema_version"] = 1
    memory["known_sessions"] = known_sessions
    memory["kink_sessions"] = kink_sessions
    return memory


def _recu_moment_stamp(item: RecuMoment) -> str:
    end = item.recording_end_site or item.recording_start_site
    return end.isoformat() if isinstance(end, datetime) else ""


def _take_new_recu_moments_until_known(
    page_moments: Sequence[RecuMoment],
    previous_ids: Set[str],
    previous_dates: Set[str],
) -> Tuple[List[RecuMoment], bool]:
    """Keep newest moments until this ordered page reaches prior scan memory."""
    output: List[RecuMoment] = []
    boundary = False
    for item in page_moments:
        stamp = _recu_moment_stamp(item)
        if (item.video_id and item.video_id in previous_ids) or (stamp and stamp in previous_dates):
            boundary = True
            break
        output.append(item)
    return output, boundary


def scrape_recu_model(
    model_name: str,
    settings: Dict[str, Any],
    cache: RecuMetadataCache,
    cancel: threading.Event,
    force: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    fetch_html_fn: Optional[Callable[..., str]] = None,
    fetch_many_fn: Optional[Callable[..., Dict[str, str]]] = None,
) -> RecuModelData:
    """Incrementally scrape Recu metadata for one model.

    v2.14 keeps durable per-model/per-kink session memory.  Recu listing pages
    are ordered newest-to-oldest, so each kink scan stops as soon as it reaches
    a session/date seen on the previous scan.  Existing moments are merged with
    only newly discovered ones.  Mobile Reviewer can provide Chrome CDP-backed
    single/batch fetchers; the desktop sorter keeps the historical HTTP path.
    """
    model = model_name.strip()
    cached_payload = cache.get_raw(model)
    cached = recu_data_from_payload(cached_payload, source="cache")
    if not bool(settings.get("recu_enabled", True)):
        return cached or RecuModelData(model, datetime.now().isoformat(timespec="seconds"), source="disabled")
    if not force and cache.is_fresh(model, float(settings.get("recu_cache_hours", 0))):
        if cached is not None:
            cached.source = "fresh cache"
            return cached

    base = str(settings.get("recu_base_url", "https://recu.me")).rstrip("/")
    quoted_model = urllib.parse.quote(model, safe="")
    performer_url = canonicalize_recu_url(f"{base}/performer/{quoted_model}", base)
    root_url = canonicalize_recu_url(f"{performer_url}/kinks", base)
    comments_url = canonicalize_recu_url(f"{performer_url}/comments", base)
    errors: List[str] = []
    moments: List[RecuMoment] = list(cached.moments) if cached is not None else []
    page_limit = max(1, int(settings.get("recu_page_limit", 200)))
    throttle = max(0.0, float(settings.get("recu_throttle_seconds", 0.35)))
    concurrency = max(1, min(24, int(settings.get("recu_concurrent_requests", 12))))
    video_meta: Dict[str, Dict[str, Any]] = dict(cached.video_meta) if cached is not None else {}
    scan_memory = _recu_memory_from_cached(cached)
    previous_kink_sessions = {
        str(slug).casefold(): dict(values)
        for slug, values in dict(scan_memory.get("kink_sessions", {})).items()
        if isinstance(values, dict)
    }
    current_kink_sessions = {
        slug: dict(values) for slug, values in previous_kink_sessions.items()
    }
    pages_fetched = 0
    new_session_ids: Set[str] = set()
    stop_boundaries = 0

    def fetch_one(url: str, expect_listing: bool = False) -> str:
        if cancel.is_set():
            raise RuntimeError("Recu scraping cancelled.")
        if fetch_html_fn is not None:
            return fetch_html_fn(url, expect_listing=expect_listing)
        return fetch_recu_html(url, settings, cancel)

    def fetch_many(urls: Sequence[str], expect_listing: bool = True) -> Dict[str, str]:
        ordered = [canonicalize_recu_url(value, base) for value in urls]
        if not ordered:
            return {}
        if fetch_many_fn is not None:
            return fetch_many_fn(ordered, expect_listing=expect_listing, max_workers=concurrency)
        # Desktop fallback: preserve the historical fetch implementation but
        # still use a bounded pool. The underlying session serializes cookie
        # mutations when necessary, so correctness wins over raw concurrency.
        output: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(concurrency, len(ordered))) as pool:
            futures = {pool.submit(fetch_one, value, expect_listing): value for value in ordered}
            for future, value in list(futures.items()):
                output[value] = future.result()
        return output

    # The performer page gives duration metadata for newest recordings without
    # requiring individual video-page fetches.
    try:
        if progress:
            progress(f"Recu: loading performer recording durations for {model}…")
        performer_html = fetch_one(performer_url, expect_listing=False)
        pages_fetched += 1
        video_meta.update(collect_recu_card_durations(performer_html))
    except Exception as exc:
        if getattr(exc, "reauth_required", False):
            raise
        errors.append(f"{performer_url}: duration index warning: {exc}")

    try:
        if progress:
            progress(f"Recu: loading kink index for {model}…")
        root_html = fetch_one(root_url, expect_listing=False)
        pages_fetched += 1
        slugs = collect_model_kink_slugs(root_url, root_html)
        if not slugs:
            slugs = sorted({
                item.kink_slug
                for item in parse_recu_kink_page(root_url, root_html, "kink", settings)
                if item.kink_slug
            })

        # First page of every kink is fetched concurrently through the supplied
        # Chrome/CDP batch fetcher (12 by default).  This is the hot path on a
        # routine incremental refresh because most kinks hit their prior boundary
        # on page 1 and need no further work.
        first_urls = {
            slug: canonicalize_recu_url(f"{root_url}/{urllib.parse.quote(slug, safe='-')}", base)
            for slug in slugs
        }
        first_results: Dict[str, str] = {}
        if first_urls:
            if progress:
                progress(f"Recu: checking {len(first_urls)} kink listing(s), up to {concurrency} at once…")
            try:
                first_results = fetch_many(list(first_urls.values()), expect_listing=True)
                pages_fetched += len(first_results)
            except Exception as exc:
                if getattr(exc, "reauth_required", False):
                    raise
                errors.append(f"{root_url}: concurrent kink fetch warning: {exc}")

        for slug_index, slug in enumerate(slugs, start=1):
            if cancel.is_set():
                raise RuntimeError("Recu scraping cancelled.")
            start_url = first_urls.get(slug) or canonicalize_recu_url(
                f"{root_url}/{urllib.parse.quote(slug, safe='-')}", base
            )
            previous = previous_kink_sessions.get(slug.casefold(), {})
            previous_ids = {str(value) for value in previous.keys()}
            previous_dates = {str(value) for value in previous.values() if str(value)}
            bucket = current_kink_sessions.setdefault(slug.casefold(), dict(previous))
            if not isinstance(bucket, dict):
                bucket = {}
                current_kink_sessions[slug.casefold()] = bucket

            page_html = first_results.get(start_url)
            if page_html is None:
                try:
                    page_html = fetch_one(start_url, expect_listing=True)
                    pages_fetched += 1
                except Exception as exc:
                    if getattr(exc, "reauth_required", False):
                        raise
                    errors.append(f"{start_url}: {exc}")
                    continue

            if progress:
                progress(f"Recu kink {slug_index}/{len(slugs)} — {kink_name_from_slug(slug)} page 1")
            parsed = parse_recu_kink_page(start_url, page_html, slug, settings)
            newly_seen, hit_boundary = _take_new_recu_moments_until_known(parsed, previous_ids, previous_dates)
            moments.extend(newly_seen)
            for item in newly_seen:
                if item.video_id:
                    stamp = _recu_moment_stamp(item)
                    bucket[str(item.video_id)] = stamp
                    new_session_ids.add(str(item.video_id))
            # Also retain previously unseen session identities from the page even
            # if two kink markers deduplicate later.
            if hit_boundary:
                stop_boundaries += 1
                continue

            discovered = [
                value for value in collect_recu_page_links(start_url, page_html, base)
                if _recu_page_number(value) > 1
            ]
            visited: Set[str] = {start_url}
            pending = sorted(set(discovered), key=lambda value: (_recu_page_number(value), value))
            while pending and len(visited) < page_limit and not hit_boundary:
                batch: List[str] = []
                while pending and len(batch) < concurrency and len(visited) + len(batch) < page_limit:
                    value = canonicalize_recu_url(pending.pop(0), base)
                    if value not in visited and value not in batch:
                        batch.append(value)
                if not batch:
                    break
                try:
                    batch_results = fetch_many(batch, expect_listing=True)
                    pages_fetched += len(batch_results)
                except Exception as exc:
                    if getattr(exc, "reauth_required", False):
                        raise
                    errors.append(f"{start_url}: paginated fetch warning: {exc}")
                    break
                # Process strictly newest -> oldest even though HTTP completed
                # concurrently. This preserves the first-known-date stop rule.
                for page_url in sorted(batch, key=lambda value: (_recu_page_number(value), value)):
                    visited.add(page_url)
                    html = batch_results.get(page_url, "")
                    if not html:
                        continue
                    if progress:
                        progress(
                            f"Recu kink {slug_index}/{len(slugs)} — {kink_name_from_slug(slug)} "
                            f"page {_recu_page_number(page_url)}"
                        )
                    parsed = parse_recu_kink_page(page_url, html, slug, settings)
                    newly_seen, page_boundary = _take_new_recu_moments_until_known(parsed, previous_ids, previous_dates)
                    moments.extend(newly_seen)
                    for item in newly_seen:
                        if item.video_id:
                            stamp = _recu_moment_stamp(item)
                            bucket[str(item.video_id)] = stamp
                            new_session_ids.add(str(item.video_id))
                    if page_boundary:
                        hit_boundary = True
                        stop_boundaries += 1
                        break
                    for next_url in collect_recu_page_links(page_url, html, base):
                        next_url = canonicalize_recu_url(next_url, base)
                        if next_url not in visited and next_url not in pending:
                            pending.append(next_url)
                    pending.sort(key=lambda value: (_recu_page_number(value), value))
                if throttle and not fetch_many_fn:
                    cancel.wait(throttle)
    except Exception as exc:
        if getattr(exc, "reauth_required", False):
            raise
        errors.append(f"{root_url}: {exc}")

    # Comments are not used by Mobile Reviewer today. Keep desktop compatibility,
    # but do not let this optional path interfere with kink incremental memory.
    if bool(settings.get("recu_scrape_comments", True)) and not cancel.is_set():
        queue_urls = [comments_url]
        visited: Set[str] = set()
        while queue_urls and len(visited) < page_limit:
            page_url = canonicalize_recu_url(queue_urls.pop(0), base)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                if progress:
                    progress(f"Recu comments page {len(visited)} for {model}…")
                page_html = fetch_one(page_url, expect_listing=False)
                pages_fetched += 1
                moments.extend(parse_recu_comments_page(page_url, page_html, settings))
                for next_url in collect_recu_page_links(page_url, page_html, base):
                    if next_url not in visited and next_url not in queue_urls:
                        queue_urls.append(next_url)
            except Exception as exc:
                if getattr(exc, "reauth_required", False):
                    raise
                errors.append(f"{page_url}: {exc}")
                if page_url == comments_url:
                    break
            if throttle:
                cancel.wait(throttle)

    deduped: Dict[Tuple[str, str, int, str], RecuMoment] = {}
    for item in moments:
        key = (
            item.kind,
            item.video_id,
            item.timestamp_seconds,
            item.kink_slug or item.comment.casefold(),
        )
        previous = deduped.get(key)
        if previous is None or len(item.comment) > len(previous.comment):
            deduped[key] = item
    moments = list(deduped.values())

    local_zone = str(settings.get("recu_local_timezone", "America/New_York"))
    for moment in moments:
        duration = _video_meta_duration(video_meta.get(moment.video_id))
        if duration > 0:
            meta = video_meta.get(moment.video_id, {})
            source = str(meta.get("source", "cached Recu duration")) if isinstance(meta, dict) else "cached Recu duration"
            _apply_recu_timing(moment, duration, local_zone, source)

    known_sessions = dict(scan_memory.get("known_sessions", {})) if isinstance(scan_memory.get("known_sessions"), dict) else {}
    for item in moments:
        if item.video_id:
            known_sessions[str(item.video_id)] = _recu_moment_stamp(item)
    scan_memory.update({
        "schema_version": 1,
        "known_sessions": known_sessions,
        "kink_sessions": current_kink_sessions,
        "last_scan_at": datetime.now().isoformat(timespec="seconds"),
        "last_pages_fetched": int(pages_fetched),
        "last_new_sessions": int(len(new_session_ids)),
        "last_stop_boundaries": int(stop_boundaries),
        "concurrency": int(concurrency),
    })

    data = RecuModelData(
        model_name=model,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        moments=sorted(
            moments,
            key=lambda item: (
                item.event_local,
                item.kind,
                item.kink_slug,
                item.comment,
            ),
        ),
        errors=errors,
        source="live incremental" if cached is not None else "live initial",
        video_meta=video_meta,
        scan_memory=scan_memory,
    )
    if data.moments or cached is not None:
        try:
            payload = recu_data_to_payload(data)
            payload["local_timezone"] = str(settings.get("recu_local_timezone", "America/New_York"))
            cache.set_raw(model, payload)
        except Exception as exc:
            data.errors.append(f"Could not save Recu cache: {exc}")
            append_log(f"Could not save Recu cache for {model}: {exc}")
        return data
    return data

def match_recu_moments(
    chunk: Chunk,
    data: Optional[RecuModelData],
    tolerance_seconds: int,
) -> List[RecuMoment]:
    if data is None:
        return []
    tolerance = timedelta(seconds=max(0, int(tolerance_seconds)))
    start = chunk.start - tolerance
    end = chunk.end + tolerance
    output: List[RecuMoment] = []
    for item in data.moments:
        if not item.timing_resolved:
            continue
        local_naive = item.event_local.replace(tzinfo=None)
        if start <= local_naive <= end:
            output.append(item)
    return sorted(output, key=lambda item: (item.event_local, item.kind, item.kink_slug, item.comment))


def recu_recording_overlaps_chunk(
    chunk: Chunk,
    item: RecuMoment,
    tolerance_seconds: int = 0,
) -> bool:
    if not item.timing_resolved or item.recording_end_local is None:
        return False
    tolerance = timedelta(seconds=max(0, int(tolerance_seconds)))
    chunk_start = chunk.start - tolerance
    chunk_end = chunk.end + tolerance
    recording_start = item.recording_start_local.replace(tzinfo=None)
    recording_end = item.recording_end_local.replace(tzinfo=None)
    return recording_start <= chunk_end and recording_end >= chunk_start


def recu_moment_may_affect_chunk(
    chunk: Chunk,
    item: RecuMoment,
    tolerance_seconds: int,
    maximum_duration_hours: float = 24.0,
) -> bool:
    """Return whether an unresolved Recu moment could fall inside a chunk.

    Recu's card timestamp is the recording end. Before the exact duration is
    fetched, assume the recording began no more than ``maximum_duration_hours``
    earlier. Therefore an event can only match when the listed end lies between
    the chunk start and chunk end plus that maximum duration.
    """
    if item.timing_resolved:
        local_event = item.event_local.replace(tzinfo=None)
        tolerance = timedelta(seconds=max(0, int(tolerance_seconds)))
        return chunk.start - tolerance <= local_event <= chunk.end + tolerance
    end_local = item.recording_end_local or item.recording_start_local
    if end_local is None:
        return False
    end_naive = end_local.replace(tzinfo=None)
    tolerance = timedelta(seconds=max(0, int(tolerance_seconds)))
    maximum = timedelta(hours=max(0.1, float(maximum_duration_hours)))
    return (
        chunk.start - tolerance <= end_naive
        and end_naive <= chunk.end + maximum + tolerance
    )


def candidate_recu_video_ids_for_chunks(
    chunks: Sequence[Chunk],
    data: Optional[RecuModelData],
    tolerance_seconds: int,
    maximum_duration_hours: float = 24.0,
    allowed_kinds: Optional[Set[str]] = None,
) -> Set[str]:
    if data is None or not chunks:
        return set()
    normalized_kinds = {str(value).casefold() for value in (allowed_kinds or set()) if str(value)}
    output: Set[str] = set()
    for item in data.moments:
        if item.timing_resolved or not item.video_id:
            continue
        if normalized_kinds and str(item.kind).casefold() not in normalized_kinds:
            continue
        if any(
            recu_moment_may_affect_chunk(
                chunk,
                item,
                tolerance_seconds,
                maximum_duration_hours,
            )
            for chunk in chunks
        ):
            output.add(item.video_id)
    return output


def is_cumshot_recu_kink(moment: RecuMoment) -> bool:
    """Return whether a Recu kink label represents a cumshot marker."""
    slug = re.sub(r"[^a-z0-9]+", "", str(moment.kink_slug or "").casefold())
    name = re.sub(r"[^a-z0-9]+", "", str(moment.kink_name or "").casefold())
    return slug == "cumshot" or name == "cumshot"


def recu_kink_queue_priority(
    chunk: Chunk,
    data: Optional[RecuModelData],
    tolerance_seconds: int,
) -> Tuple[int, int, int]:
    """Rank a chunk as cumshot, another identified kink, or no known kink.

    Lower tuples sort first. Counts are negated so chunks with more matching
    markers win ties without changing the established size/date fallbacks.
    """
    if data is None:
        return (2, 0, 0)
    kink_items = [
        item for item in match_recu_moments(chunk, data, tolerance_seconds)
        if item.kind == "kink"
    ]
    cumshot_count = sum(1 for item in kink_items if is_cumshot_recu_kink(item))
    other_count = len(kink_items) - cumshot_count
    if cumshot_count:
        return (0, -cumshot_count, -other_count)
    if other_count:
        return (1, 0, -other_count)
    return (2, 0, 0)


def chunk_event_location(
    chunk: Chunk,
    event_local: datetime,
) -> Optional[Tuple[int, VideoInfo, float]]:
    event_naive = event_local.replace(tzinfo=None)
    candidates: List[Tuple[int, VideoInfo, float]] = []
    for index, video in enumerate(chunk.files, start=1):
        if video.start <= event_naive <= video.end:
            offset = max(0.0, min(float(video.duration), (event_naive - video.start).total_seconds()))
            candidates.append((index, video, offset))
    if not candidates:
        return None
    # CTBRec segments can overlap by a few seconds. Prefer the latest segment
    # start so a timestamp at an exact boundary opens the newer file.
    return max(candidates, key=lambda value: (value[1].start, value[0]))


def chunk_event_segment(chunk: Chunk, event_local: datetime) -> Optional[int]:
    location = chunk_event_location(chunk, event_local)
    return location[0] if location is not None else None


def locate_chunk_event(chunk: Chunk, event_local: datetime) -> str:
    event_naive = event_local.replace(tzinfo=None)
    location = chunk_event_location(chunk, event_local)
    if location is not None:
        index, _video, offset = location
        return f"segment #{index} +{format_clock(offset)}"
    delta = (event_naive - chunk.start).total_seconds()
    if delta < 0:
        return f"{format_clock(abs(delta))} before chunk"
    return f"chunk +{format_clock(delta)} (between/outside local segments)"


# ---------------------------------------------------------------------------
# KeepLast integration
# ---------------------------------------------------------------------------

def keeplasts_file_candidates() -> List[Path]:
    """Return candidates in the user's documented layout, then safe fallbacks."""
    return [
        APP_DIR.parent / "ctbrec_v3_plus_release" / "keeplasts.txt",
        APP_DIR / "ctbrec_v3_plus_release" / "keeplasts.txt",
        APP_DIR / "keeplasts.txt",
        APP_DIR.parent / "keeplasts.txt",
    ]


def locate_keeplasts_file() -> Path:
    for candidate in keeplasts_file_candidates():
        if candidate.is_file():
            return candidate
    # Report the documented location even when it does not exist yet.
    return keeplasts_file_candidates()[0]


def load_keeplasts_map(path: Optional[Path] = None) -> Dict[str, float]:
    target = Path(path) if path is not None else locate_keeplasts_file()
    result: Dict[str, float] = {}
    if not target.is_file():
        return result
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or ":" not in clean:
            continue
        name, raw_minutes = clean.split(":", 1)
        try:
            minutes = float(raw_minutes.strip())
        except Exception:
            continue
        if name.strip() and minutes > 0:
            result[name.strip().casefold()] = minutes
    return result


def keep_indices_for_durations(durations: Sequence[float], keep_minutes: float) -> Set[int]:
    """Keep newest complete files until their duration reaches the designation."""
    if not durations:
        return set()
    threshold = max(0.0, float(keep_minutes) * 60.0)
    if threshold <= 0:
        return set()
    cumulative = 0.0
    keep: Set[int] = set()
    for index in reversed(range(len(durations))):
        keep.add(index)
        cumulative += max(1.0, float(durations[index]))
        if cumulative >= threshold:
            break
    return keep


def run_keeplast_for_remaining_chunks(
    chunks: Sequence[Chunk],
    keep_minutes: float,
    settings: Dict[str, Any],
    duration_cache: "DurationCache",
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    dry_run: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Process all remaining chunks, moving retained files into Review."""
    summary: Dict[str, Any] = {
        "chunks": 0, "kept": 0, "deleted": 0, "kept_bytes": 0,
        "deleted_bytes": 0, "errors": [], "dry_run": bool(dry_run),
    }
    ffmpeg, ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
    total = len(chunks)
    for chunk_index, chunk in enumerate(chunks, start=1):
        if cancel.is_set():
            summary["cancelled"] = True
            break
        if progress:
            progress(chunk_index - 1, max(1, total), f"KeepLast chunk {chunk_index}/{total}: reading durations")
        durations: List[float] = []
        for video in chunk.files:
            cached = duration_cache.get(video.path, video.size, video.mtime)
            if cached is not None:
                duration = cached
            elif ffmpeg is not None:
                duration = probe_duration(video, ffmpeg, ffprobe, duration_cache, cancel)
            else:
                duration = float(max(1, video.duration))
            durations.append(float(max(1.0, duration)))
        keep_indices = keep_indices_for_durations(durations, keep_minutes)
        for idx, video in enumerate(chunk.files):
            if idx in keep_indices:
                summary["kept_bytes"] += int(video.size)
            else:
                summary["deleted_bytes"] += int(video.size)
        ok, message, details = process_chunk_files(chunk, keep_indices, dry_run=dry_run)
        summary["chunks"] += 1
        summary["kept"] += len(details.get("kept", []))
        summary["deleted"] += len(details.get("deleted", []))
        if not ok:
            summary["errors"].extend(details.get("errors", []) or [message])
        if progress:
            progress(chunk_index, max(1, total), f"KeepLast chunk {chunk_index}/{total}: {message}")
    duration_cache.save()
    return summary


# ---------------------------------------------------------------------------
# Tiny persistent caches
# ---------------------------------------------------------------------------

class DurationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        raw = load_json(path, {"version": 1, "files": {}})
        self.data: Dict[str, Any] = raw if isinstance(raw, dict) else {"version": 1, "files": {}}
        self.data.setdefault("version", 1)
        self.data.setdefault("files", {})
        self.dirty = False

    def get(self, path: Path, size: int, mtime: float) -> Optional[float]:
        key = normalized_absolute(path)
        with self.lock:
            entry = self.data.get("files", {}).get(key)
            if not isinstance(entry, dict):
                return None
            try:
                if int(entry.get("size", -1)) != int(size):
                    return None
                if abs(float(entry.get("mtime", -1)) - float(mtime)) > 0.001:
                    return None
                duration = float(entry.get("duration", 0))
                return duration if duration > 0 else None
            except Exception:
                return None

    def get_meta(self, path: Path, size: int, mtime: float) -> Tuple[Optional[float], str, str]:
        key = normalized_absolute(path)
        with self.lock:
            entry = self.data.get("files", {}).get(key)
            if not isinstance(entry, dict):
                return None, "", ""
            try:
                if int(entry.get("size", -1)) != int(size):
                    return None, "", ""
                if abs(float(entry.get("mtime", -1)) - float(mtime)) > 0.001:
                    return None, "", ""
                duration = float(entry.get("duration", 0))
                if duration <= 0:
                    return None, "", ""
                return duration, str(entry.get("source", "cache")), str(entry.get("warning", ""))
            except Exception:
                return None, "", ""

    def set(
        self,
        path: Path,
        size: int,
        mtime: float,
        duration: float,
        source: str = "probe",
        warning: str = "",
    ) -> None:
        if duration <= 0:
            return
        key = normalized_absolute(path)
        with self.lock:
            files = self.data.setdefault("files", {})
            files[key] = {
                "size": int(size),
                "mtime": float(mtime),
                "duration": float(duration),
                "source": str(source),
                "warning": str(warning),
            }
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(self.path, self.data)
            self.dirty = False


class MosaicManifest:
    """Source-signature manifest compatible with the full sorter's basic shape."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        raw = load_json(path, {"version": 1, "chunks": {}})
        self.data: Dict[str, Any] = raw if isinstance(raw, dict) else {"version": 1, "chunks": {}}
        self.data.setdefault("version", 1)
        self.data.setdefault("chunks", {})

    def existing(self, signature: str) -> List[Path]:
        with self.lock:
            entry = self.data.get("chunks", {}).get(signature)
            if not isinstance(entry, dict):
                return []
            raw_paths = entry.get("outputs") or entry.get("output_paths") or []
        paths = [Path(str(value)) for value in raw_paths if str(value)]
        return paths if paths and all(path.exists() for path in paths) else []

    def forget(self, signature: str) -> None:
        with self.lock:
            chunks = self.data.setdefault("chunks", {})
            if signature in chunks:
                chunks.pop(signature, None)
                self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                atomic_write_json(self.path, self.data)

    def record(self, chunk: Chunk, outputs: Sequence[Path]) -> None:
        valid = [Path(path) for path in outputs if Path(path).exists()]
        if not valid:
            return
        payload_files = []
        for video in chunk.files:
            payload_files.append({
                "name": video.path.name,
                "path": str(video.path),
                "size": video.size,
                "mtime": video.mtime,
            })
        with self.lock:
            chunks = self.data.setdefault("chunks", {})
            chunks[chunk.signature] = {
                "status": "done",
                "model": chunk.folder.name,
                "folder": str(chunk.folder),
                "chunk_key": chunk.key,
                "start": chunk.start.isoformat(),
                "end": chunk.end.isoformat(),
                "source_bytes": chunk.source_bytes,
                "files": payload_files,
                "outputs": [str(path) for path in valid],
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            }
            if len(chunks) > 20_000:
                ordered = sorted(
                    chunks.items(),
                    key=lambda item: str(item[1].get("generated_at", "")),
                )
                for key, _entry in ordered[: len(chunks) - 18_000]:
                    chunks.pop(key, None)
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                atomic_write_json(self.path, self.data)
            except Exception as exc:
                append_log(f"Could not save mosaic manifest; continuing: {exc}")


# ---------------------------------------------------------------------------
# Persistent on-demand speech transcription cache
# ---------------------------------------------------------------------------

class SpeechTranscriptCache:
    """Cache per-file speech timestamps so later phrase searches are instant."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        raw = load_json(path, {"version": 1, "files": {}})
        self.data: Dict[str, Any] = raw if isinstance(raw, dict) else {"version": 1, "files": {}}
        self.data.setdefault("version", 1)
        self.data.setdefault("files", {})
        self.dirty = False

    @staticmethod
    def _settings_key(settings: Dict[str, Any]) -> str:
        engine = str(settings.get("speech_engine", "Vosk (fast / legacy compatible)")).strip()
        vosk_model = (
            str(settings.get("speech_vosk_model_path", "")).strip()
            or str(settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")).strip()
        )
        whisper_model = (
            str(settings.get("speech_model_path", "")).strip()
            or str(settings.get("speech_model", "tiny.en")).strip()
        )
        payload = {
            "engine": engine.casefold(),
            "vosk_model": vosk_model,
            "whisper_model": whisper_model,
            "language": str(settings.get("speech_language", "en")).strip().lower(),
            "device": str(settings.get("speech_device", "cpu")).strip().lower(),
            "compute_type": str(settings.get("speech_compute_type", "int8")).strip().lower(),
            "compatibility": str(settings.get("speech_cpu_compatibility_mode", "Automatic safe retries")).strip().lower(),
            "beam_size": int(settings.get("speech_beam_size", 1) or 1),
            "vad": bool(settings.get("speech_vad_filter", True)),
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def get(self, video: VideoInfo, settings: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        if not bool(settings.get("speech_cache_enabled", True)):
            return None
        key = normalized_absolute(video.path)
        settings_key = self._settings_key(settings)
        with self.lock:
            entry = self.data.get("files", {}).get(key)
            if not isinstance(entry, dict):
                return None
            try:
                if int(entry.get("size", -1)) != int(video.size):
                    return None
                if abs(float(entry.get("mtime", -1)) - float(video.mtime)) > 0.001:
                    return None
                if str(entry.get("settings_key", "")) != settings_key:
                    return None
                rows = entry.get("utterances")
                return list(rows) if isinstance(rows, list) else None
            except Exception:
                return None

    def set(self, video: VideoInfo, settings: Dict[str, Any], utterances: Sequence[SpeechUtterance]) -> None:
        if not bool(settings.get("speech_cache_enabled", True)):
            return
        key = normalized_absolute(video.path)
        payload: List[Dict[str, Any]] = []
        for utterance in utterances:
            payload.append({
                "start": float(utterance.start_seconds),
                "end": float(utterance.end_seconds),
                "text": str(utterance.text),
                "average_probability": float(utterance.average_probability),
                "words": [
                    {
                        "start": float(word.start_seconds),
                        "end": float(word.end_seconds),
                        "text": str(word.text),
                        "probability": float(word.probability),
                    }
                    for word in utterance.words
                ],
            })
        with self.lock:
            files = self.data.setdefault("files", {})
            files[key] = {
                "size": int(video.size),
                "mtime": float(video.mtime),
                "settings_key": self._settings_key(settings),
                "utterances": payload,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            # Keep the cache bounded while retaining a large practical history.
            if len(files) > 25_000:
                ordered = sorted(files.items(), key=lambda item: str(item[1].get("updated_at", "")))
                for stale_key, _entry in ordered[: len(files) - 22_000]:
                    files.pop(stale_key, None)
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            atomic_write_json(self.path, self.data)
            self.dirty = False


class SpeechEngineManager:
    """Availability helpers for optional local speech-recognition backends.

    Recognition itself always runs in a child process so a native-library abort
    cannot terminate the mosaic review GUI.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.models: Dict[str, Any] = {}

    @staticmethod
    def faster_whisper_available() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    @staticmethod
    def vosk_available() -> bool:
        return importlib.util.find_spec("vosk") is not None

    @staticmethod
    def selected_backend(settings: Optional[Dict[str, Any]] = None) -> str:
        settings = settings or {}
        choice = str(
            settings.get("speech_engine", "Vosk (fast / legacy compatible)")
        ).strip().casefold()
        if "automatic" in choice:
            if SpeechEngineManager.vosk_available():
                return "vosk"
            if SpeechEngineManager.faster_whisper_available():
                return "faster-whisper"
            return "none"
        if "vosk" in choice:
            return "vosk"
        if "whisper" in choice:
            return "faster-whisper"
        return "vosk"

    @staticmethod
    def is_available(settings: Optional[Dict[str, Any]] = None) -> bool:
        backend = SpeechEngineManager.selected_backend(settings)
        if backend == "vosk":
            return SpeechEngineManager.vosk_available()
        if backend == "faster-whisper":
            return SpeechEngineManager.faster_whisper_available()
        return False

    @staticmethod
    def availability_text(settings: Optional[Dict[str, Any]] = None) -> str:
        backend = SpeechEngineManager.selected_backend(settings)
        if backend == "vosk":
            return "Vosk is installed" if SpeechEngineManager.vosk_available() else "Vosk is not installed"
        if backend == "faster-whisper":
            return (
                "faster-whisper is installed"
                if SpeechEngineManager.faster_whisper_available()
                else "faster-whisper is not installed"
            )
        return "No speech engine is installed"

    @staticmethod
    def model_key(settings: Dict[str, Any]) -> str:
        backend = SpeechEngineManager.selected_backend(settings)
        if backend == "vosk":
            model_source = (
                str(settings.get("speech_vosk_model_path", "")).strip()
                or str(settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")).strip()
            )
            return f"vosk|{model_source}|{settings.get('speech_language', 'en')}"
        model_source = (
            str(settings.get("speech_model_path", "")).strip()
            or str(settings.get("speech_model", "tiny.en")).strip()
        )
        return "|".join([
            "faster-whisper",
            model_source,
            str(settings.get("speech_device", "cpu")).strip().lower(),
            str(settings.get("speech_compute_type", "int8")).strip().lower(),
            str(int(settings.get("speech_cpu_threads", 4) or 4)),
        ])


def _normalize_search_tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", str(text).casefold())


def search_speech_transcript(
    transcript: SpeechTranscript,
    query: str,
    match_mode: str = "Exact phrase",
    fuzzy_threshold_percent: int = 74,
) -> List[SpeechMatch]:
    query_tokens = _normalize_search_tokens(query)
    if not query_tokens:
        return []
    mode = str(match_mode or "Exact phrase").strip().casefold()
    threshold = max(0.0, min(1.0, float(fuzzy_threshold_percent) / 100.0))
    results: List[SpeechMatch] = []

    for utterance in transcript.utterances:
        words = [word for word in utterance.words if _normalize_search_tokens(word.text)]
        word_tokens = [_normalize_search_tokens(word.text)[0] for word in words]
        if not word_tokens:
            text_tokens = _normalize_search_tokens(utterance.text)
            joined = " ".join(text_tokens)
            query_joined = " ".join(query_tokens)
            matched = False
            score = 1.0
            if mode.startswith("all"):
                matched = all(token in text_tokens for token in query_tokens)
            elif mode.startswith("any"):
                matched = any(token in text_tokens for token in query_tokens)
            elif mode.startswith("fuzzy"):
                score = difflib.SequenceMatcher(None, query_joined, joined).ratio()
                matched = score >= threshold
            else:
                matched = query_joined in joined
            if matched:
                results.append(SpeechMatch(
                    file_index=utterance.file_index,
                    path=utterance.path,
                    start_seconds=utterance.start_seconds,
                    end_seconds=utterance.end_seconds,
                    matched_text=utterance.text,
                    context=utterance.text,
                    score=score,
                ))
            continue

        query_joined = " ".join(query_tokens)
        if mode.startswith("any"):
            for index, token in enumerate(word_tokens):
                if token in query_tokens:
                    word = words[index]
                    results.append(SpeechMatch(
                        file_index=utterance.file_index,
                        path=utterance.path,
                        start_seconds=word.start_seconds,
                        end_seconds=word.end_seconds,
                        matched_text=word.text.strip(),
                        context=utterance.text,
                        score=max(0.0, float(word.probability)),
                    ))
            continue

        if mode.startswith("all"):
            positions = [
                next((index for index, token in enumerate(word_tokens) if token == query_token), -1)
                for query_token in query_tokens
            ]
            if all(position >= 0 for position in positions):
                left, right = min(positions), max(positions)
                selected = words[left:right + 1]
                results.append(SpeechMatch(
                    file_index=utterance.file_index,
                    path=utterance.path,
                    start_seconds=selected[0].start_seconds,
                    end_seconds=selected[-1].end_seconds,
                    matched_text=" ".join(word.text.strip() for word in selected),
                    context=utterance.text,
                    score=sum(max(0.0, word.probability) for word in selected) / max(1, len(selected)),
                ))
            continue

        lengths = [len(query_tokens)]
        if mode.startswith("fuzzy"):
            lengths = sorted({max(1, len(query_tokens) - 1), len(query_tokens), len(query_tokens) + 1})
        for window_length in lengths:
            for start_index in range(0, max(0, len(word_tokens) - window_length + 1)):
                end_index = start_index + window_length
                selected_tokens = word_tokens[start_index:end_index]
                candidate = " ".join(selected_tokens)
                score = difflib.SequenceMatcher(None, query_joined, candidate).ratio() if mode.startswith("fuzzy") else 1.0
                if (candidate == query_joined) if not mode.startswith("fuzzy") else (score >= threshold):
                    selected = words[start_index:end_index]
                    results.append(SpeechMatch(
                        file_index=utterance.file_index,
                        path=utterance.path,
                        start_seconds=selected[0].start_seconds,
                        end_seconds=selected[-1].end_seconds,
                        matched_text=" ".join(word.text.strip() for word in selected),
                        context=utterance.text,
                        score=score,
                    ))
    # Deduplicate overlapping hits produced by fuzzy windows.
    deduped: List[SpeechMatch] = []
    seen: Set[Tuple[int, int, int, str]] = set()
    for match in sorted(results, key=lambda item: (item.file_index, item.start_seconds, -item.score)):
        key = (match.file_index, int(round(match.start_seconds * 2)), int(round(match.end_seconds * 2)), match.matched_text.casefold())
        if key not in seen:
            seen.add(key)
            deduped.append(match)
    return deduped


def _cached_rows_to_utterances(rows: Sequence[Dict[str, Any]], file_index: int, path: Path) -> List[SpeechUtterance]:
    output: List[SpeechUtterance] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        words: List[SpeechWord] = []
        for raw_word in row.get("words", []) if isinstance(row.get("words", []), list) else []:
            if not isinstance(raw_word, dict):
                continue
            words.append(SpeechWord(
                start_seconds=float(raw_word.get("start", 0.0) or 0.0),
                end_seconds=float(raw_word.get("end", 0.0) or 0.0),
                text=str(raw_word.get("text", "")),
                probability=float(raw_word.get("probability", 0.0) or 0.0),
            ))
        output.append(SpeechUtterance(
            file_index=file_index,
            path=path,
            start_seconds=float(row.get("start", 0.0) or 0.0),
            end_seconds=float(row.get("end", 0.0) or 0.0),
            text=str(row.get("text", "")).strip(),
            words=words,
            average_probability=float(row.get("average_probability", 0.0) or 0.0),
        ))
    return output


def _extract_audio_for_transcription(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
) -> Tuple[bool, str]:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(source), "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-y", str(destination),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process: Optional[subprocess.Popen[Any]] = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creationflags)
        process_holder[0] = process
        while process.poll() is None:
            if cancel.wait(0.2):
                try:
                    process.terminate()
                except Exception:
                    pass
                return False, "Transcription cancelled."
        stderr = b""
        try:
            _stdout, stderr = process.communicate(timeout=2)
        except Exception:
            pass
        if process.returncode == 0 and destination.exists() and destination.stat().st_size > 44:
            return True, ""
        return False, (stderr or b"").decode("utf-8", "replace").strip() or "ffmpeg audio extraction failed."
    except Exception as exc:
        return False, str(exc)
    finally:
        process_holder[0] = None


def _speech_exit_code_label(return_code: Optional[int]) -> str:
    if return_code is None:
        return "unknown"
    value = int(return_code)
    if os.name == "nt":
        unsigned = value & 0xFFFFFFFF
        common = {
            0xC0000005: "access violation",
            0xC000001D: "illegal CPU instruction",
            0xC0000094: "integer divide by zero",
            0xC0000409: "native stack-buffer/security failure",
            0xC0000135: "required DLL not found",
            0xC0000139: "DLL entry point not found",
        }
        description = common.get(unsigned, "native worker failure")
        return f"{value} (0x{unsigned:08X}, {description})"
    return str(value)


def _terminate_process_tree(process: Optional[subprocess.Popen[Any]]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
                timeout=8,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=4)
            except Exception:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _speech_worker_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _speech_worker_status(path: Optional[Path], current: int, total: int, text: str) -> None:
    if path is None:
        return
    try:
        _speech_worker_write_json(path, {"current": int(current), "total": int(total), "text": str(text)})
    except Exception:
        pass


def _speech_worker_extract_wav(ffmpeg: Path, source: Path, destination: Path) -> Tuple[bool, str]:
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(source), "-map", "0:a:0?", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(destination),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            timeout=60 * 60,
            check=False,
        )
        if completed.returncode == 0 and destination.exists() and destination.stat().st_size > 44:
            return True, ""
        error = completed.stderr.decode("utf-8", "replace").strip()
        return False, error or f"ffmpeg exited with code {completed.returncode}"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg audio extraction timed out after 60 minutes"
    except Exception as exc:
        return False, str(exc)


SPEECH_RUNTIME_PROFILE_LABELS: Dict[str, str] = {
    "vosk": "Vosk legacy-compatible",
    "standard": "Standard / fastest",
    "legacy_avx": "Legacy AVX safe",
    "generic": "Generic safest",
    "generic_fresh": "Generic safest + fresh model cache",
}


def _speech_runtime_configuration(settings: Dict[str, Any]) -> Tuple[str, str, int, Dict[str, str]]:
    """Resolve one helper-process runtime profile before importing CTranslate2.

    Native CTranslate2 failures cannot be caught inside Python, so each profile
    is launched in its own process.  The conservative profiles avoid the two
    model-load optimizations most likely to be troublesome on older Windows
    systems: Intel MKL selection and packed-GEMM weight prepacking.
    """
    profile = str(settings.get("speech_runtime_profile", "standard") or "standard").strip().lower()
    device = str(settings.get("speech_device", "cpu") or "cpu").strip().lower()
    requested_compute = str(settings.get("speech_compute_type", "int8") or "int8").strip().lower()
    requested_threads = max(1, int(settings.get("speech_cpu_threads", 2) or 2))
    compute_type = requested_compute
    threads = requested_threads
    overrides: Dict[str, str] = {
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "TQDM_DISABLE": "1",
        "CT2_VERBOSE": "1",
    }

    if device == "cpu" and profile == "legacy_avx":
        threads = min(2, requested_threads)
        # Keep INT8's memory advantage but perform accumulation in float32.
        compute_type = "int8_float32" if requested_compute not in {"float32", "default"} else requested_compute
        overrides.update({
            "CT2_FORCE_CPU_ISA": "AVX",
            "CT2_PACKED_GEMM": "0",
            "CT2_USE_MKL": "0",
            "MKL_ENABLE_INSTRUCTIONS": "AVX",
            "ONEDNN_MAX_CPU_ISA": "AVX",
            "DNNL_MAX_CPU_ISA": "AVX",
            "KMP_AFFINITY": "disabled",
            "KMP_BLOCKTIME": "0",
            "OMP_WAIT_POLICY": "PASSIVE",
        })
    elif device == "cpu" and profile in {"generic", "generic_fresh"}:
        threads = 1
        compute_type = "float32"
        overrides.update({
            "CT2_FORCE_CPU_ISA": "GENERIC",
            "CT2_PACKED_GEMM": "0",
            "CT2_USE_MKL": "0",
            "MKL_ENABLE_INSTRUCTIONS": "AVX",
            "ONEDNN_MAX_CPU_ISA": "AVX",
            "DNNL_MAX_CPU_ISA": "AVX",
            "KMP_AFFINITY": "disabled",
            "KMP_BLOCKTIME": "0",
            "OMP_WAIT_POLICY": "PASSIVE",
        })

    overrides["OMP_NUM_THREADS"] = str(threads)
    overrides["MKL_NUM_THREADS"] = str(threads)
    overrides["OPENBLAS_NUM_THREADS"] = str(threads)
    return profile, compute_type, threads, overrides


def _speech_attempt_profiles(settings: Dict[str, Any]) -> List[str]:
    engine_choice = str(
        settings.get("speech_engine", "Vosk (fast / legacy compatible)")
    ).strip().casefold()

    # Vosk is a different recognizer rather than another CTranslate2 profile.
    # It is intentionally first/default on the user's Ivy-Bridge-era Windows PC
    # where every CTranslate2 model-construction profile aborts natively.
    if "automatic" in engine_choice:
        profiles: List[str] = []
        if SpeechEngineManager.vosk_available():
            profiles.append("vosk")
        if SpeechEngineManager.faster_whisper_available():
            profiles.extend(_whisper_attempt_profiles(settings))
        return profiles
    if "vosk" in engine_choice:
        return ["vosk"]
    return _whisper_attempt_profiles(settings)


def _whisper_attempt_profiles(settings: Dict[str, Any]) -> List[str]:
    mode = str(
        settings.get("speech_cpu_compatibility_mode", "Automatic safe retries")
        or ""
    ).strip().casefold()
    device = str(settings.get("speech_device", "cpu") or "cpu").strip().casefold()
    if "standard" in mode or "fastest" in mode:
        return ["standard"]
    if "legacy" in mode or "avx" in mode:
        return ["legacy_avx"]
    if "generic" in mode or "safest" in mode:
        return ["generic"]
    if device == "cpu":
        profiles = ["legacy_avx", "generic"]
        if not str(settings.get("speech_model_path", "")).strip():
            profiles.append("generic_fresh")
        return profiles
    profiles = ["standard", "generic"]
    if not str(settings.get("speech_model_path", "")).strip():
        profiles.append("generic_fresh")
    return profiles


def _vosk_result_to_row(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one Vosk recognition result into the shared transcript row format."""
    raw_words = payload.get("result", [])
    words: List[Dict[str, Any]] = []
    probabilities: List[float] = []
    if isinstance(raw_words, list):
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            token = str(item.get("word", "")).strip()
            if not token:
                continue
            confidence = float(item.get("conf", 0.0) or 0.0)
            probabilities.append(confidence)
            words.append({
                "start": float(item.get("start", 0.0) or 0.0),
                "end": float(item.get("end", item.get("start", 0.0)) or 0.0),
                "text": (" " + token),
                "probability": confidence,
            })
    transcript_text = str(payload.get("text", "")).strip()
    if not transcript_text and words:
        transcript_text = " ".join(str(word["text"]).strip() for word in words).strip()
    if not transcript_text:
        return None
    if words:
        start_seconds = float(words[0]["start"])
        end_seconds = float(words[-1]["end"])
    else:
        start_seconds = 0.0
        end_seconds = 0.0
    return {
        "start": start_seconds,
        "end": end_seconds,
        "text": transcript_text,
        "average_probability": (
            sum(probabilities) / len(probabilities) if probabilities else 0.0
        ),
        "words": words,
    }


def _run_vosk_speech_worker(
    request: Dict[str, Any],
    settings: Dict[str, Any],
    files: Sequence[Dict[str, Any]],
    status_path: Optional[Path],
    result_path: Path,
    crash_log: Any,
) -> int:
    """Run lightweight Vosk recognition in the already isolated worker process."""
    total = max(1, len(files))
    try:
        _speech_worker_status(status_path, 0, total, "Importing Vosk speech engine…")
        vosk_module = importlib.import_module("vosk")
        Model = getattr(vosk_module, "Model")
        KaldiRecognizer = getattr(vosk_module, "KaldiRecognizer")
        set_log_level = getattr(vosk_module, "SetLogLevel", None)
        if callable(set_log_level):
            set_log_level(-1)
        crash_log.write(f"vosk={getattr(vosk_module, '__version__', 'installed')}\n")

        model_path_raw = str(settings.get("speech_vosk_model_path", "")).strip()
        model_name = str(
            settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")
        ).strip() or "vosk-model-small-en-us-0.15"
        model_root_raw = str(settings.get("speech_vosk_model_root", "vosk_models")).strip()
        model_root = (
            Path(os.path.expandvars(os.path.expanduser(model_root_raw)))
            if model_root_raw
            else APP_DIR / "vosk_models"
        )
        if not model_root.is_absolute():
            model_root = APP_DIR / model_root
        model_root.mkdir(parents=True, exist_ok=True)
        # Vosk searches this directory first and downloads a named model into it
        # when it is absent. Creating the directory avoids a known Windows
        # first-run failure in the package's default cache path.
        os.environ["VOSK_MODEL_PATH"] = str(model_root)

        if model_path_raw:
            model_path = Path(os.path.expandvars(os.path.expanduser(model_path_raw)))
            if not model_path.is_absolute():
                model_path = APP_DIR / model_path
            _speech_worker_status(status_path, 0, total, f"Loading local Vosk model: {model_path.name}…")
            model = Model(model_path=str(model_path))
            model_label = str(model_path)
        else:
            _speech_worker_status(
                status_path,
                0,
                total,
                f"Loading {model_name} (first use may download about 40 MB)…",
            )
            model = Model(model_name=model_name)
            model_label = model_name
        crash_log.write(f"Vosk model initialized successfully: {model_label}\n")

        ffmpeg_raw = str(request.get("ffmpeg", "")).strip()
        ffmpeg = Path(ffmpeg_raw) if ffmpeg_raw else None
        if ffmpeg is None or not ffmpeg.exists():
            raise RuntimeError(
                "Vosk requires ffmpeg to create a temporary mono 16-kHz WAV."
            )

        output_files: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="ctbrec_vosk_worker_") as temp_root_raw:
            temp_root = Path(temp_root_raw)
            for position, item in enumerate(files, start=1):
                file_index = int(item.get("file_index", 0))
                source = Path(str(item.get("path", "")))
                file_result: Dict[str, Any] = {
                    "file_index": file_index,
                    "path": str(source),
                    "ok": False,
                    "utterances": [],
                    "warning": "",
                }
                try:
                    if not source.exists():
                        raise FileNotFoundError(f"Source video no longer exists: {source}")
                    _speech_worker_status(
                        status_path,
                        position - 1,
                        total,
                        f"Extracting audio from segment #{file_index + 1}…",
                    )
                    wav_path = temp_root / f"segment_{file_index + 1:04d}.wav"
                    extracted, extract_message = _speech_worker_extract_wav(
                        ffmpeg, source, wav_path
                    )
                    if not extracted:
                        raise RuntimeError(
                            f"Could not extract the audio track: {extract_message}"
                        )

                    _speech_worker_status(
                        status_path,
                        position - 1,
                        total,
                        f"Rapidly transcribing segment #{file_index + 1} with Vosk…",
                    )
                    rows: List[Dict[str, Any]] = []
                    with wave.open(str(wav_path), "rb") as wav_file:
                        if (
                            wav_file.getnchannels() != 1
                            or wav_file.getsampwidth() != 2
                            or wav_file.getcomptype() != "NONE"
                        ):
                            raise RuntimeError(
                                "Temporary audio was not mono 16-bit PCM WAV."
                            )
                        recognizer = KaldiRecognizer(model, wav_file.getframerate())
                        if hasattr(recognizer, "SetWords"):
                            recognizer.SetWords(True)
                        while True:
                            block = wav_file.readframes(8000)
                            if not block:
                                break
                            if recognizer.AcceptWaveform(block):
                                parsed = json.loads(recognizer.Result())
                                row = _vosk_result_to_row(parsed)
                                if row is not None:
                                    rows.append(row)
                        parsed = json.loads(recognizer.FinalResult())
                        row = _vosk_result_to_row(parsed)
                        if row is not None:
                            rows.append(row)
                    file_result["utterances"] = rows
                    file_result["ok"] = True
                except Exception as exc:
                    file_result["warning"] = f"{type(exc).__name__}: {exc}"
                    crash_log.write(
                        f"Segment #{file_index + 1} failed: {traceback.format_exc()}\n"
                    )
                output_files.append(file_result)
                _speech_worker_status(
                    status_path,
                    position,
                    total,
                    f"Finished segment #{file_index + 1} ({position}/{total})",
                )

        language_raw = str(settings.get("speech_language", "en")).strip().lower()
        _speech_worker_write_json(result_path, {
            "ok": True,
            "files": output_files,
            "engine": f"Vosk / {model_label}",
            "language": language_raw or "en",
            "runtime_profile": "vosk",
            "runtime_profile_label": SPEECH_RUNTIME_PROFILE_LABELS["vosk"],
            "compute_type": "native CPU",
            "threads": 1,
        })
        crash_log.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            "Vosk speech helper completed normally.\n"
        )
        return 0
    except BaseException as exc:
        crash_log.write(
            f"Vosk helper fatal error: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}\n"
        )
        try:
            _speech_worker_write_json(
                result_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )
        except Exception:
            pass
        return 2


def run_speech_worker_cli(request_path: Path, result_path: Path, crash_log_path: Path) -> int:
    """Run one faster-whisper profile outside the GUI process."""
    crash_log_path.parent.mkdir(parents=True, exist_ok=True)
    with crash_log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as crash_log:
        crash_log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] Speech helper starting.\n")
        try:
            faulthandler.enable(file=crash_log, all_threads=True)
        except Exception:
            pass
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            settings = request.get("settings", {}) if isinstance(request, dict) else {}
            files = request.get("files", []) if isinstance(request, dict) else []
            status_raw = request.get("status_path", "") if isinstance(request, dict) else ""
            status_path = Path(status_raw) if status_raw else None
            total = max(1, len(files))

            runtime_profile = str(settings.get("speech_runtime_profile", "")).strip().casefold()
            selected_backend = SpeechEngineManager.selected_backend(settings)
            if runtime_profile == "vosk" or selected_backend == "vosk":
                crash_log.write("Runtime backend: Vosk legacy-compatible\n")
                return _run_vosk_speech_worker(
                    request if isinstance(request, dict) else {},
                    settings,
                    files if isinstance(files, list) else [],
                    status_path,
                    result_path,
                    crash_log,
                )

            profile, compute_type, threads, environment_overrides = _speech_runtime_configuration(settings)
            for key, value in environment_overrides.items():
                # Assignment, not setdefault: a stale parent environment must not
                # defeat the compatibility profile selected for this child.
                os.environ[key] = value
            crash_log.write(
                "Runtime profile: " + SPEECH_RUNTIME_PROFILE_LABELS.get(profile, profile)
                + f"; device={settings.get('speech_device', 'cpu')}; compute={compute_type}; threads={threads}\n"
            )
            for key in (
                "CT2_FORCE_CPU_ISA", "CT2_PACKED_GEMM", "CT2_USE_MKL",
                "MKL_ENABLE_INSTRUCTIONS", "ONEDNN_MAX_CPU_ISA", "DNNL_MAX_CPU_ISA",
                "OMP_NUM_THREADS",
            ):
                if key in os.environ:
                    crash_log.write(f"{key}={os.environ[key]}\n")

            _speech_worker_status(status_path, 0, total, f"Importing speech engine ({SPEECH_RUNTIME_PROFILE_LABELS.get(profile, profile)})…")
            module = importlib.import_module("faster_whisper")
            WhisperModel = getattr(module, "WhisperModel")
            try:
                ctranslate2_module = importlib.import_module("ctranslate2")
                crash_log.write(
                    f"faster-whisper={getattr(module, '__version__', 'unknown')}; "
                    f"ctranslate2={getattr(ctranslate2_module, '__version__', 'unknown')}\n"
                )
            except Exception as version_exc:
                crash_log.write(f"Could not read engine versions: {version_exc}\n")

            model_source = str(settings.get("speech_model_path", "")).strip() or str(settings.get("speech_model", "tiny.en")).strip()
            download_root_raw = str(settings.get("speech_model_download_root", "whisper_models")).strip()
            download_root = Path(os.path.expandvars(os.path.expanduser(download_root_raw))) if download_root_raw else (APP_DIR / "whisper_models")
            if not download_root.is_absolute():
                download_root = APP_DIR / download_root
            download_root.mkdir(parents=True, exist_ok=True)

            _speech_worker_status(
                status_path, 0, total,
                f"Loading {model_source} ({SPEECH_RUNTIME_PROFILE_LABELS.get(profile, profile)})…",
            )
            model = WhisperModel(
                model_source,
                device=str(settings.get("speech_device", "cpu")).strip() or "cpu",
                compute_type=compute_type,
                cpu_threads=threads,
                num_workers=1,
                download_root=str(download_root),
            )
            crash_log.write("Speech model initialized successfully.\n")

            language_raw = str(settings.get("speech_language", "en")).strip().lower()
            language = None if language_raw in {"", "auto", "automatic"} else language_raw
            beam_size = max(1, min(10, int(settings.get("speech_beam_size", 1) or 1)))
            vad_filter = bool(settings.get("speech_vad_filter", True))
            preextract = bool(settings.get("speech_preextract_audio", True))
            ffmpeg_raw = str(request.get("ffmpeg", "")).strip()
            ffmpeg = Path(ffmpeg_raw) if ffmpeg_raw else None
            output_files: List[Dict[str, Any]] = []

            with tempfile.TemporaryDirectory(prefix="ctbrec_speech_worker_") as temp_root_raw:
                temp_root = Path(temp_root_raw)
                for position, item in enumerate(files, start=1):
                    file_index = int(item.get("file_index", 0))
                    source = Path(str(item.get("path", "")))
                    file_result: Dict[str, Any] = {
                        "file_index": file_index,
                        "path": str(source),
                        "ok": False,
                        "utterances": [],
                        "warning": "",
                    }
                    try:
                        if not source.exists():
                            raise FileNotFoundError(f"Source video no longer exists: {source}")
                        source_for_model = source
                        if preextract:
                            if ffmpeg is None or not ffmpeg.exists():
                                raise RuntimeError("Safe speech mode requires ffmpeg, but ffmpeg was not found.")
                            _speech_worker_status(status_path, position - 1, total, f"Extracting audio from segment #{file_index + 1}…")
                            wav_path = temp_root / f"segment_{file_index + 1:04d}.wav"
                            extracted, extract_message = _speech_worker_extract_wav(ffmpeg, source, wav_path)
                            if not extracted:
                                raise RuntimeError(f"Could not extract the audio track: {extract_message}")
                            source_for_model = wav_path

                        _speech_worker_status(status_path, position - 1, total, f"Transcribing segment #{file_index + 1}…")
                        segment_iterator, _info = model.transcribe(
                            str(source_for_model),
                            language=language,
                            task="transcribe",
                            beam_size=beam_size,
                            vad_filter=vad_filter,
                            word_timestamps=True,
                            condition_on_previous_text=False,
                            temperature=0.0,
                        )
                        rows: List[Dict[str, Any]] = []
                        for segment in segment_iterator:
                            words: List[Dict[str, Any]] = []
                            probabilities: List[float] = []
                            for word in getattr(segment, "words", None) or []:
                                probability = float(getattr(word, "probability", 0.0) or 0.0)
                                probabilities.append(probability)
                                words.append({
                                    "start": float(getattr(word, "start", getattr(segment, "start", 0.0)) or 0.0),
                                    "end": float(getattr(word, "end", getattr(segment, "end", 0.0)) or 0.0),
                                    "text": str(getattr(word, "word", "")),
                                    "probability": probability,
                                })
                            text = str(getattr(segment, "text", "")).strip()
                            if text:
                                rows.append({
                                    "start": float(getattr(segment, "start", 0.0) or 0.0),
                                    "end": float(getattr(segment, "end", 0.0) or 0.0),
                                    "text": text,
                                    "average_probability": (sum(probabilities) / len(probabilities) if probabilities else 0.0),
                                    "words": words,
                                })
                        file_result["utterances"] = rows
                        file_result["ok"] = True
                    except Exception as exc:
                        file_result["warning"] = f"{type(exc).__name__}: {exc}"
                        crash_log.write(f"Segment #{file_index + 1} failed: {traceback.format_exc()}\n")
                    output_files.append(file_result)
                    _speech_worker_status(status_path, position, total, f"Finished segment #{file_index + 1} ({position}/{total})")

            _speech_worker_write_json(result_path, {
                "ok": True,
                "files": output_files,
                "engine": f"faster-whisper / {model_source}",
                "language": language_raw or "auto",
                "runtime_profile": profile,
                "runtime_profile_label": SPEECH_RUNTIME_PROFILE_LABELS.get(profile, profile),
                "compute_type": compute_type,
                "threads": threads,
            })
            crash_log.write(f"[{datetime.now().isoformat(timespec='seconds')}] Speech helper completed normally.\n")
            return 0
        except BaseException as exc:
            crash_log.write(f"Speech helper fatal error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}\n")
            try:
                _speech_worker_write_json(result_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass
            return 2


def transcribe_chunk_speech(
    chunk: Chunk,
    file_indices: Sequence[int],
    settings: Dict[str, Any],
    cache: SpeechTranscriptCache,
    engine_manager: SpeechEngineManager,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    progress: Callable[[int, int, str], None],
) -> Tuple[bool, str, Optional[SpeechTranscript]]:
    del engine_manager  # Recognition is deliberately isolated in helper processes.
    indices = sorted({int(index) for index in file_indices if 0 <= int(index) < len(chunk.files)})
    if not indices:
        return False, "No valid chunk segments were selected.", None

    utterances: List[SpeechUtterance] = []
    warnings: List[str] = []
    uncached: List[Tuple[int, VideoInfo]] = []
    for index in indices:
        video = chunk.files[index]
        rows = cache.get(video, settings)
        if rows is None:
            uncached.append((index, video))
        else:
            utterances.extend(_cached_rows_to_utterances(rows, index + 1, video.path))

    successful_profile_label = "cached"
    if uncached:
        if not SpeechEngineManager.is_available(settings):
            backend = SpeechEngineManager.selected_backend(settings)
            if backend == "vosk":
                install_hint = "python -m pip install --upgrade vosk"
                engine_name = "Vosk"
            elif backend == "faster-whisper":
                install_hint = "python -m pip install --upgrade faster-whisper"
                engine_name = "faster-whisper"
            else:
                install_hint = "python -m pip install --upgrade vosk"
                engine_name = "a local speech engine"
            return False, (
                f"{engine_name} is not installed. Use Audio tools → Install speech engine, "
                f"or run: {install_hint}"
            ), None
        ffmpeg, _ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
        if bool(settings.get("speech_preextract_audio", True)) and ffmpeg is None:
            return False, "Safe speech mode could not find ffmpeg. Configure ffmpeg in Settings before transcribing.", None

        profiles = _speech_attempt_profiles(settings)
        diagnostic_paths: List[Path] = []
        worker_result: Optional[Dict[str, Any]] = None
        with tempfile.TemporaryDirectory(prefix="ctbrec_speech_job_") as temp_root_raw:
            temp_root = Path(temp_root_raw)
            creationflags = 0
            if os.name == "nt":
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            for attempt_number, profile in enumerate(profiles, start=1):
                request_path = temp_root / f"request_{attempt_number}.json"
                result_path = temp_root / f"result_{attempt_number}.json"
                status_path = temp_root / f"status_{attempt_number}.json"
                crash_log_path = temp_root / f"speech_worker_{attempt_number}_{profile}.log"
                attempt_settings = dict(settings)
                attempt_settings["speech_runtime_profile"] = profile
                if profile == "generic_fresh":
                    # A damaged/incomplete CTranslate2 model file can also abort
                    # native model construction. Use a separate persistent cache
                    # for the final retry so the suspect files are not reopened.
                    attempt_settings["speech_model_path"] = ""
                    attempt_settings["speech_model_download_root"] = str(APP_DIR / "whisper_models_recovery")
                request_payload = {
                    "settings": attempt_settings,
                    "ffmpeg": str(ffmpeg) if ffmpeg is not None else "",
                    "status_path": str(status_path),
                    "files": [
                        {"file_index": int(index), "path": str(video.path)}
                        for index, video in uncached
                    ],
                }
                request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--speech-worker",
                    str(request_path),
                    str(result_path),
                    str(crash_log_path),
                ]
                environment = os.environ.copy()
                # The worker overwrites profile-specific values before importing
                # CTranslate2. These generic values keep other native libraries
                # conservative from process startup onward.
                requested_threads = max(1, int(settings.get("speech_cpu_threads", 2) or 2))
                environment["OMP_NUM_THREADS"] = str(requested_threads)
                environment["MKL_NUM_THREADS"] = str(requested_threads)
                environment["OPENBLAS_NUM_THREADS"] = str(requested_threads)
                environment["TOKENIZERS_PARALLELISM"] = "false"
                environment["PYTHONFAULTHANDLER"] = "1"

                profile_label = SPEECH_RUNTIME_PROFILE_LABELS.get(profile, profile)
                progress(
                    0,
                    max(1, len(indices)),
                    f"Starting speech helper: {profile_label} (attempt {attempt_number}/{len(profiles)})…",
                )
                worker_log_handle = crash_log_path.open("a", encoding="utf-8", errors="replace")
                process: Optional[subprocess.Popen[Any]] = None
                last_status_text = ""
                return_code: Optional[int] = None
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=worker_log_handle,
                        stderr=worker_log_handle,
                        stdin=subprocess.DEVNULL,
                        creationflags=creationflags,
                        env=environment,
                    )
                    process_holder[0] = process
                    while process.poll() is None:
                        if cancel.wait(0.25):
                            _terminate_process_tree(process)
                            return False, "Transcription cancelled.", None
                        try:
                            if status_path.exists():
                                status = json.loads(status_path.read_text(encoding="utf-8"))
                                text = str(status.get("text", ""))
                                if text and text != last_status_text:
                                    last_status_text = text
                                    progress(
                                        int(status.get("current", 0)),
                                        max(1, int(status.get("total", len(indices)))),
                                        text,
                                    )
                        except Exception:
                            pass
                    return_code = process.returncode
                except Exception as exc:
                    warnings.append(f"{profile_label}: could not start helper: {exc}")
                finally:
                    process_holder[0] = None
                    try:
                        worker_log_handle.close()
                    except Exception:
                        pass

                crash_text = ""
                try:
                    crash_text = crash_log_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

                attempt_result: Optional[Dict[str, Any]] = None
                if result_path.exists():
                    try:
                        parsed = json.loads(result_path.read_text(encoding="utf-8"))
                        if isinstance(parsed, dict):
                            attempt_result = parsed
                    except Exception:
                        attempt_result = None

                if return_code == 0 and attempt_result and bool(attempt_result.get("ok", False)):
                    worker_result = attempt_result
                    successful_profile_label = str(attempt_result.get("runtime_profile_label", profile_label))
                    if attempt_number > 1:
                        warnings.append(f"Speech engine recovered using {successful_profile_label} after an earlier native crash.")
                    break

                diagnostic_dir = APP_DIR / "speech_crash_logs"
                diagnostic_dir.mkdir(parents=True, exist_ok=True)
                diagnostic_path = diagnostic_dir / (
                    f"speech_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{attempt_number}_{profile}.log"
                )
                try:
                    diagnostic_path.write_text(crash_text or "No helper diagnostics were produced.", encoding="utf-8")
                    diagnostic_paths.append(diagnostic_path)
                except Exception:
                    pass
                detail = _speech_exit_code_label(return_code)
                readable_error = ""
                if attempt_result:
                    readable_error = str(attempt_result.get("error", "")).strip()
                warnings.append(
                    f"{profile_label} failed (exit {detail})"
                    + (f": {readable_error}" if readable_error else "")
                )
                if attempt_number < len(profiles):
                    progress(
                        0,
                        max(1, len(indices)),
                        f"{profile_label} failed; retrying with {SPEECH_RUNTIME_PROFILE_LABELS.get(profiles[attempt_number], profiles[attempt_number])}…",
                    )

            if worker_result is None:
                locations = ", ".join(str(path) for path in diagnostic_paths) or "no diagnostic file was produced"
                selected_backend = SpeechEngineManager.selected_backend(settings)
                if selected_backend == "vosk":
                    explanation = (
                        "The isolated Vosk helper failed before producing a transcript. "
                        "The main sorter remained protected from the native failure."
                    )
                else:
                    explanation = (
                        "CTranslate2 crashed while loading the Whisper model under every selected compatibility profile. "
                        "The source video was never reached, so this is not a bad-segment or FFmpeg problem."
                    )
                return False, f"{explanation} Diagnostics: {locations}", None

            by_index = {
                int(item.get("file_index", -1)): item
                for item in worker_result.get("files", [])
                if isinstance(item, dict)
            }
            for position, (index, video) in enumerate(uncached, start=1):
                item = by_index.get(index)
                if item is None:
                    warnings.append(f"#{index + 1} {video.path.name}: helper returned no result")
                    continue
                row_payload = item.get("utterances", []) if isinstance(item.get("utterances", []), list) else []
                segment_rows = _cached_rows_to_utterances(row_payload, index + 1, video.path)
                if bool(item.get("ok", False)):
                    cache.set(video, settings, segment_rows)
                else:
                    warning = str(item.get("warning", "unknown speech-engine error"))
                    warnings.append(f"#{index + 1} {video.path.name}: {warning}")
                utterances.extend(segment_rows)
                progress(position, max(1, len(uncached)), f"Loaded transcript for segment #{index + 1}")

    cache.save()
    utterances.sort(key=lambda item: (item.file_index, item.start_seconds, item.end_seconds))
    selected_backend = SpeechEngineManager.selected_backend(settings)
    if selected_backend == "vosk":
        model_label = (
            str(settings.get("speech_vosk_model_path", "")).strip()
            or str(settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")).strip()
        )
        engine_label = f"Vosk / {model_label} ({successful_profile_label})"
    else:
        model_label = (
            str(settings.get("speech_model_path", "")).strip()
            or str(settings.get("speech_model", "tiny.en")).strip()
        )
        engine_label = f"faster-whisper / {model_label} ({successful_profile_label})"
    language_raw = str(settings.get("speech_language", "en")).strip().lower()
    transcript = SpeechTranscript(
        chunk_signature=chunk.signature,
        model_name=chunk.folder.name,
        utterances=utterances,
        file_indices=tuple(index + 1 for index in indices),
        engine_label=engine_label,
        language=language_raw or "auto",
        warnings=warnings,
    )
    message = f"Transcript contains {len(utterances):,} utterance(s) across {len(indices):,} segment(s)."
    if successful_profile_label not in {"cached", "Standard / fastest"}:
        message += f" Runtime: {successful_profile_label}."
    if warnings:
        message += f" {len(warnings):,} warning(s) were logged."
        append_log("Speech transcription warnings: " + " | ".join(warnings[:30]))
    return True, message, transcript


# ---------------------------------------------------------------------------
# Root/model/chunk discovery
# ---------------------------------------------------------------------------

def find_model_folders(
    roots: Sequence[Path],
    model_text: str,
    recursive_fallback: bool,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Path]:
    text = model_text.strip().strip('"')
    if not text:
        return []

    direct_input = Path(os.path.expandvars(os.path.expanduser(text)))
    if direct_input.is_dir():
        return [direct_input.resolve()]

    requested_name = canonical_model_name(text)
    requested = requested_name.casefold()
    found: Dict[str, Path] = {}

    # Stay shallow, but collect the base folder and every model_dup# sibling so
    # older duplicate folders behave as one logical model until they are merged.
    for index, root in enumerate(roots, start=1):
        if progress and (index == 1 or index % 5 == 0 or index == len(roots)):
            progress(f"Checking root {index}/{len(roots)}: {root}")
        for candidate_name in dict.fromkeys((text, requested_name)):
            candidate = root / candidate_name
            try:
                if candidate.is_dir():
                    found[normalized_absolute(candidate)] = candidate.resolve()
            except Exception:
                pass
        try:
            with os.scandir(root) as iterator:
                for entry in iterator:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if canonical_model_name(entry.name).casefold() != requested:
                            continue
                        path = Path(entry.path).resolve()
                        found[normalized_absolute(path)] = path
                    except OSError:
                        continue
        except Exception:
            continue

    # Explicitly optional: broad recursive search can be expensive.
    if not found and recursive_fallback:
        for index, root in enumerate(roots, start=1):
            if progress:
                progress(f"Recursive fallback {index}/{len(roots)}: {root}")
            try:
                for current, directories, _files in os.walk(root):
                    directories[:] = [
                        name for name in directories
                        if name.casefold() not in {
                            "review", MOSAIC_DIRNAME.casefold(),
                            DELETION_BUCKET_NAME.casefold(),
                        }
                    ]
                    for directory in directories:
                        if canonical_model_name(directory).casefold() == requested:
                            path = (Path(current) / directory).resolve()
                            found[normalized_absolute(path)] = path
            except Exception:
                continue

    return sorted(found.values(), key=lambda path: normalized_absolute(path))

def discover_model_names(
    roots: Sequence[Path],
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[str]:
    """Build a case-insensitive model-name catalog using shallow scans only.

    This lists only immediate child directories of configured recording roots.
    It never walks recursively, reads videos, probes durations, or calculates
    folder sizes.
    """
    excluded = {
        "review",
        MOSAIC_DIRNAME.casefold(),
        DELETION_BUCKET_NAME.casefold(),
    }
    found: Dict[str, str] = {}
    total = len(roots)

    for index, root in enumerate(roots, start=1):
        if progress and (index == 1 or index % 5 == 0 or index == total):
            progress(index, total, str(root))
        try:
            with os.scandir(root) as iterator:
                for entry in iterator:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        physical_name = entry.name.strip()
                        name = canonical_model_name(physical_name)
                        folded = name.casefold()
                        if not name or folded in excluded or physical_name.startswith("."):
                            continue
                        previous = found.get(folded)
                        if previous is None or name < previous:
                            found[folded] = name
                    except OSError:
                        continue
        except OSError:
            continue

    return sorted(found.values(), key=lambda value: (value.casefold(), value))



def chunk_signature(folder: Path, files: Sequence[VideoInfo]) -> str:
    parts: List[Any] = [normalized_absolute(folder)]
    for video in sorted(files, key=lambda item: normalized_absolute(item.path)):
        try:
            relative = os.path.relpath(str(video.path), str(folder))
        except Exception:
            relative = video.path.name
        parts.append([relative, int(video.size), round(float(video.mtime), 3)])
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")
    return hashlib.sha1(encoded).hexdigest()


def chunk_key(chunk: Chunk) -> str:
    return f"{chunk.start.isoformat()}|{chunk.end.isoformat()}|n={len(chunk.files)}"


def mosaic_base_stem(chunk: Chunk) -> str:
    return f"chunk_{chunk.idx:04d}_{chunk.start.strftime('%Y%m%d_%H%M%S')}_MASSIVE"


def load_review_state(folder: Path) -> Dict[str, Any]:
    data = load_json(folder / STATE_FILENAME, {"reviewed": {}})
    if not isinstance(data, dict):
        data = {"reviewed": {}}
    if not isinstance(data.get("reviewed"), dict):
        data["reviewed"] = {}
    return data


def save_review_state(folder: Path, state: Dict[str, Any]) -> None:
    atomic_write_json(folder / STATE_FILENAME, state)


def find_existing_mosaics(
    chunk: Chunk,
    manifest: MosaicManifest,
    use_existing: bool = True,
    session_generated_signatures: Optional[Set[str]] = None,
) -> List[Path]:
    """Return a usable mosaic for this chunk.

    When ``use_existing`` is disabled, disk/manifest mosaics from earlier runs
    are intentionally ignored. Mosaics generated by this running Lite process
    remain reusable so current and look-ahead workers do not duplicate work and
    a same-session rescan does not regenerate an already-fresh mosaic.
    """
    trusted = session_generated_signatures or set()
    if not use_existing and chunk.signature not in trusted:
        return []

    manifest_paths = manifest.existing(chunk.signature)
    if manifest_paths:
        return manifest_paths

    output_dir = chunk.folder / MOSAIC_DIRNAME
    if not output_dir.is_dir():
        return []
    base = mosaic_base_stem(chunk)
    single = output_dir / f"{base}.jpg"
    if single.exists():
        try:
            manifest.record(chunk, [single])
        except Exception as exc:
            append_log(f"Could not index existing mosaic {single}: {exc}")
        return [single]
    parts = sorted(output_dir.glob(f"{base}_part*of*.jpg"))
    if parts:
        try:
            manifest.record(chunk, parts)
        except Exception as exc:
            append_log(f"Could not index existing mosaic parts for {chunk.folder}: {exc}")
        return parts

    # Signature sidecars make the Lite app robust if chunk indices later shift.
    for sidecar in output_dir.glob("*.sources.json"):
        try:
            raw = load_json(sidecar, {})
            if isinstance(raw, dict) and raw.get("signature") == chunk.signature:
                outputs = [Path(value) for value in raw.get("outputs", [])]
                if outputs and all(path.exists() for path in outputs):
                    try:
                        manifest.record(chunk, outputs)
                    except Exception as exc:
                        append_log(f"Could not index sidecar mosaic for {chunk.folder}: {exc}")
                    return outputs
        except Exception:
            continue
    return []


def build_chunks_for_folder(
    folder: Path,
    extensions: Set[str],
    gap_minutes: int,
    recent_write_grace_seconds: int,
    duration_cache: DurationCache,
    ffmpeg: Optional[Path],
    ffprobe: Optional[Path],
    skip_recent_writes: bool = True,
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[str], None]] = None,
    probe_timeout_seconds: int = 15,
    fallback_seconds: int = 900,
    max_inferred_seconds: int = 21600,
    probe_actual_durations: bool = True,
) -> Tuple[List[Chunk], bool, List[str]]:
    """Group direct video files while isolating unreadable-duration failures.

    The normal path uses signature-valid ffprobe/ffmpeg durations. If one file
    is damaged, locked, timed out, or unsupported, the rest of the folder still
    loads. A clearly marked fallback is chosen from a tail tag, the next file's
    timestamp, or the configured safe default.
    """
    rows: List[Tuple[Path, datetime, int, float]] = []
    warnings: List[str] = []
    now = time.time()
    try:
        with os.scandir(folder) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() not in extensions:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    start = parse_start_from_name(path.name, stat.st_mtime)
                    rows.append((path, start, int(stat.st_size), float(stat.st_mtime)))
                except Exception as exc:
                    warnings.append(f"Could not inspect {getattr(entry, 'name', 'file')}: {exc}")
    except Exception as exc:
        return [], False, [f"Could not scan folder {folder}: {exc}"]

    if not rows:
        return [], False, warnings
    rows.sort(key=lambda row: (row[1], row[0].name.casefold()))
    has_recent_write = any(now - row[3] < recent_write_grace_seconds for row in rows)
    if has_recent_write and skip_recent_writes:
        return [], True, warnings

    active_cancel = cancel if cancel is not None else threading.Event()
    videos: List[VideoInfo] = []
    total_rows = len(rows)
    for index, (video_path, start_time, size, mtime) in enumerate(rows, start=1):
        if active_cancel.is_set():
            raise RuntimeError("Duration scan cancelled.")
        duration, source, warning = duration_cache.get_meta(video_path, size, mtime)
        if duration is None and probe_actual_durations and ffmpeg is not None:
            if progress:
                progress(f"Reading actual duration {index}/{total_rows}: {video_path.name}")
            provisional = VideoInfo(
                path=video_path,
                start=start_time,
                size=size,
                mtime=mtime,
                duration=max(1, int(fallback_seconds)),
            )
            duration, source, warning = probe_duration_detailed(
                provisional,
                ffmpeg,
                ffprobe,
                duration_cache,
                active_cancel,
                timeout_seconds=probe_timeout_seconds,
            )

        if duration is None or duration <= 0:
            tail_duration = parse_tail_duration_from_name(video_path.name)
            next_delta: Optional[float] = None
            if index < total_rows:
                next_start = rows[index][1]
                candidate = (next_start - start_time).total_seconds()
                if 1 <= candidate <= max(1, int(max_inferred_seconds)):
                    next_delta = candidate
            if tail_duration is not None:
                duration = float(tail_duration)
                source = "tail-tag fallback" if probe_actual_durations else "pending probe (tail tag)"
            elif next_delta is not None:
                duration = float(next_delta)
                source = "next-start fallback" if probe_actual_durations else "pending probe (next start)"
            else:
                duration = float(max(1, int(fallback_seconds)))
                source = "safe-default fallback" if probe_actual_durations else "pending probe (safe default)"
            if probe_actual_durations:
                detail = warning or "No readable duration metadata."
                warning = (
                    f"{video_path.name}: actual duration unavailable ({detail}); "
                    f"using {format_seconds(int(duration))} from {source}."
                )
                warnings.append(warning)
                append_log("Duration fallback: " + warning)
            else:
                warning = ""

        videos.append(
            VideoInfo(
                path=video_path,
                start=start_time,
                size=size,
                mtime=mtime,
                duration=max(1, int(round(duration))),
                duration_source=source or "cache",
                duration_warning=warning if "fallback" in (source or "") else "",
            )
        )
    try:
        duration_cache.save()
    except Exception as exc:
        warning = f"Could not save duration cache; continuing without persistence: {exc}"
        warnings.append(warning)
        append_log(warning)

    gap = timedelta(minutes=max(0, int(gap_minutes)))
    groups: List[List[VideoInfo]] = []
    current: List[VideoInfo] = []
    latest_end: Optional[datetime] = None
    for video in videos:
        if not current:
            current = [video]
            latest_end = video.end
            continue
        assert latest_end is not None
        if video.start - latest_end <= gap:
            current.append(video)
            if video.end > latest_end:
                latest_end = video.end
        else:
            groups.append(current)
            current = [video]
            latest_end = video.end
    if current:
        groups.append(current)

    chunks: List[Chunk] = []
    for group_index, group in enumerate(groups, start=1):
        chunk_start = min(video.start for video in group)
        chunk_end = max(video.end for video in group)
        chunk = Chunk(
            idx=group_index,
            folder=folder,
            files=list(group),
            start=chunk_start,
            end=chunk_end,
            source_bytes=sum(video.size for video in group),
            durations_prepared=all("pending probe" not in video.duration_source for video in group),
        )
        chunk.signature = chunk_signature(folder, chunk.files)
        chunk.key = chunk_key(chunk)
        chunks.append(chunk)
    return chunks, has_recent_write, warnings

def build_model_queue(
    folders: Sequence[Path],
    settings: Dict[str, Any],
    duration_cache: DurationCache,
    manifest: MosaicManifest,
    include_skipped: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    session_generated_signatures: Optional[Set[str]] = None,
    cancel: Optional[threading.Event] = None,
    probe_actual_durations: bool = True,
) -> Tuple[List[Chunk], List[str]]:
    extensions = parse_extensions(str(settings.get("extensions", "mp4,ts")))
    gap = int(settings.get("chunk_gap_minutes", 30))
    grace = int(settings.get("recent_write_grace_seconds", 120))
    skip_recent = bool(settings.get("skip_recent_writes", True))
    queue_items: List[Chunk] = []
    notes: List[str] = []
    active_cancel = cancel if cancel is not None else threading.Event()

    ffmpeg: Optional[Path] = None
    ffprobe: Optional[Path] = None
    if probe_actual_durations:
        ffmpeg, ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
        if ffmpeg is None:
            notes.append(
                "ffmpeg was not found. Cached durations and safe per-file fallbacks will be used; "
                "mosaic generation still requires ffmpeg."
            )
        else:
            verified, ffmpeg_message = verify_ffmpeg(ffmpeg)
            if not verified:
                notes.append(
                    f"ffmpeg could not run ({ffmpeg_message}). Cached durations and safe fallbacks will be used."
                )
                ffmpeg = None
                ffprobe = None

    for index, folder in enumerate(folders, start=1):
        if active_cancel.is_set():
            raise RuntimeError("Actual-duration scan cancelled.")
        if progress:
            progress(f"Scanning model folder {index}/{len(folders)}: {folder}")
        try:
            chunks, has_recent, duration_warnings = build_chunks_for_folder(
                folder,
                extensions,
                gap,
                grace,
                duration_cache,
                ffmpeg,
                ffprobe,
                skip_recent_writes=skip_recent,
                cancel=active_cancel,
                progress=progress,
                probe_timeout_seconds=int(settings.get("duration_probe_timeout_seconds", 15)),
                fallback_seconds=int(settings.get("duration_fallback_seconds", 900)),
                max_inferred_seconds=int(settings.get("max_inferred_duration_seconds", 21600)),
                probe_actual_durations=probe_actual_durations,
            )
        except RuntimeError as exc:
            if active_cancel.is_set() or "cancel" in str(exc).casefold():
                raise
            notes.append(f"Skipped folder after recoverable scan error: {folder}: {exc}")
            append_log(f"Recoverable folder scan error for {folder}: {exc}\n{traceback.format_exc()}")
            continue
        except Exception as exc:
            notes.append(f"Skipped folder after unexpected scan error: {folder}: {exc}")
            append_log(f"Unexpected folder scan error for {folder}: {exc}\n{traceback.format_exc()}")
            continue
        notes.extend(duration_warnings)
        if has_recent and skip_recent:
            notes.append(f"Skipped recent-write folder: {folder}")
            continue
        try:
            state = load_review_state(folder)
            reviewed = state.get("reviewed", {})
        except Exception as exc:
            reviewed = {}
            notes.append(f"Could not read review state for {folder}; treating chunks as unreviewed: {exc}")
        for chunk in chunks:
            if not include_skipped and reviewed.get(chunk.key) in {"done", "skipped"}:
                continue
            try:
                chunk.mosaics = find_existing_mosaics(
                    chunk,
                    manifest,
                    use_existing=bool(settings.get("use_existing_mosaics", True)),
                    session_generated_signatures=session_generated_signatures,
                )
            except Exception as exc:
                chunk.mosaics = []
                notes.append(f"Could not check existing mosaics for {chunk.folder.name} chunk {chunk.idx}; continuing: {exc}")
            if probe_actual_durations:
                chunk.durations_prepared = True
            queue_items.append(chunk)

    order = str(settings.get("queue_order", "Largest chunks first"))
    if order == "Oldest first":
        queue_items.sort(
            key=lambda chunk: (
                chunk.start,
                -int(chunk.source_bytes),
                normalized_absolute(chunk.folder),
            )
        )
    elif order == "Newest first":
        queue_items.sort(
            key=lambda chunk: (
                -chunk.start.timestamp(),
                -int(chunk.source_bytes),
                normalized_absolute(chunk.folder),
            )
        )
    else:
        # Strict largest-to-smallest order based only on the sum of the
        # current video files' actual os.stat sizes. Existing/missing mosaic
        # status is intentionally excluded from this key.
        queue_items.sort(
            key=lambda chunk: (
                -int(chunk.source_bytes),
                chunk.start,
                normalized_absolute(chunk.folder),
            )
        )
    return queue_items, notes



def prepare_chunk_durations(
    chunk: Chunk,
    settings: Dict[str, Any],
    duration_cache: DurationCache,
    cancel: threading.Event,
    progress: Callable[[int, int, str], None],
) -> Tuple[bool, str, List[str]]:
    """Probe only this chunk's files, preserving a usable fallback per file.

    The queue can therefore be shown immediately using cached/tail/next-start
    timing, while exact durations are filled in incrementally for the configured
    preparation window. A single bad video never aborts the chunk.
    """
    warnings: List[str] = []
    ffmpeg, ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
    if ffmpeg is None:
        chunk.durations_prepared = True
        chunk.preparation_warning = "ffmpeg was not found; provisional durations remain in use."
        return False, chunk.preparation_warning, [chunk.preparation_warning]
    verified, message = verify_ffmpeg(ffmpeg)
    if not verified:
        chunk.durations_prepared = True
        chunk.preparation_warning = f"ffmpeg could not run: {message}"
        return False, chunk.preparation_warning, [chunk.preparation_warning]

    timeout = max(1, int(settings.get("duration_probe_timeout_seconds", 15)))
    fallback_seconds = max(1, int(settings.get("duration_fallback_seconds", 900)))
    total = len(chunk.files)
    for index, video in enumerate(chunk.files, start=1):
        if cancel.is_set():
            return False, "Duration preparation cancelled.", warnings
        progress(index - 1, max(1, total), f"Preparing duration {index}/{total}: {video.path.name}")
        duration, source, warning = probe_duration_detailed(
            video, ffmpeg, ffprobe, duration_cache, cancel, timeout_seconds=timeout
        )
        if duration is not None and duration > 0:
            video.duration = max(1, int(round(duration)))
            video.duration_source = source or "probe"
            video.duration_warning = ""
        else:
            # Keep the provisional duration already visible in the queue.
            video.duration = max(1, int(video.duration or fallback_seconds))
            previous_source = video.duration_source or "safe-default fallback"
            if "pending probe" in previous_source:
                previous_source = previous_source.replace("pending probe", "probe-failed fallback")
            video.duration_source = previous_source
            detail = warning or "No readable duration metadata."
            video.duration_warning = detail
            warnings.append(f"{video.path.name}: {detail}; kept {format_seconds(video.duration)}.")

    chunk.start = min((video.start for video in chunk.files), default=chunk.start)
    chunk.end = max((video.end for video in chunk.files), default=chunk.end)
    chunk.durations_prepared = True
    chunk.preparation_warning = "; ".join(warnings[:3])
    try:
        duration_cache.save()
    except Exception as exc:
        warnings.append(f"Could not save duration cache: {exc}")
    progress(total, max(1, total), "Actual durations prepared.")
    return True, f"Prepared actual durations for {total} file(s).", warnings


# ---------------------------------------------------------------------------
# ffmpeg and mosaic generation
# ---------------------------------------------------------------------------

def resolve_ffmpeg(configured: str = "") -> Tuple[Optional[Path], Optional[Path]]:
    candidates: List[Path] = []
    if configured.strip():
        candidates.append(Path(os.path.expandvars(os.path.expanduser(configured.strip().strip('"')))))
    executable_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates.extend([
        APP_DIR / executable_name,
        APP_DIR / "lib" / "ffmpeg" / executable_name,
        APP_DIR.parent / "ctbrec" / "lib" / "ffmpeg" / executable_name,
        APP_DIR.parent / "lib" / "ffmpeg" / executable_name,
    ])
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))

    ffmpeg: Optional[Path] = None
    for candidate in candidates:
        try:
            if candidate.is_file():
                ffmpeg = candidate.resolve()
                break
        except Exception:
            continue
    if ffmpeg is None:
        return None, None

    probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    probe_candidates = [ffmpeg.with_name(probe_name)]
    probe_which = shutil.which("ffprobe")
    if probe_which:
        probe_candidates.append(Path(probe_which))
    ffprobe = next((path.resolve() for path in probe_candidates if path.is_file()), None)
    return ffmpeg, ffprobe


def resolve_vlc(configured: str = "") -> Optional[Path]:
    candidates: List[Path] = []
    if configured.strip():
        candidates.append(Path(os.path.expandvars(os.path.expanduser(configured.strip().strip('"')))))

    executable_name = "vlc.exe" if os.name == "nt" else "vlc"
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable, "")
        if base:
            candidates.append(Path(base) / "VideoLAN" / "VLC" / executable_name)
    candidates.extend([
        APP_DIR / executable_name,
        APP_DIR.parent / executable_name,
    ])
    which = shutil.which("vlc")
    if which:
        candidates.append(Path(which))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except Exception:
            continue
    return None


def open_in_vlc_at(vlc: Path, source: Path, seconds: float) -> Tuple[bool, str]:
    if not source.exists():
        return False, f"Video no longer exists: {source}"
    command = [
        str(vlc),
        "--no-video-title-show",
        f"--start-time={max(0.0, float(seconds)):.3f}",
        str(source),
    ]
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return True, f"Opened {source.name} at {format_clock(seconds)} in VLC."
    except Exception as exc:
        return False, str(exc)


def verify_ffmpeg(ffmpeg: Path) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(ffmpeg), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=7,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return (result.returncode == 0, str(ffmpeg))
    except Exception as exc:
        return False, str(exc)


def probe_duration_detailed(
    video: VideoInfo,
    ffmpeg: Path,
    ffprobe: Optional[Path],
    duration_cache: DurationCache,
    cancel: threading.Event,
    timeout_seconds: int = 15,
) -> Tuple[Optional[float], str, str]:
    """Try multiple metadata readers without allowing one bad file to abort a scan."""
    cached, cached_source, cached_warning = duration_cache.get_meta(
        video.path, video.size, video.mtime
    )
    if cached is not None:
        return cached, cached_source or "cache", cached_warning
    if cancel.is_set():
        return None, "cancelled", "Duration read cancelled."

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    timeout = max(3, int(timeout_seconds))
    errors: List[str] = []

    if ffprobe is not None:
        # JSON lets us accept either a container duration or a stream duration,
        # which is important for partially repaired or unusual MPEG-TS files.
        try:
            result = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "format=duration:stream=duration",
                    "-of", "json", str(video.path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                creationflags=creationflags,
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
                duration = max((value for value in candidates if value > 0), default=0.0)
                if duration > 0:
                    duration_cache.set(video.path, video.size, video.mtime, duration, "ffprobe")
                    return duration, "ffprobe", ""
            if result.stderr.strip():
                errors.append("ffprobe: " + result.stderr.strip()[:300])
        except subprocess.TimeoutExpired:
            errors.append(f"ffprobe timed out after {timeout}s")
        except Exception as exc:
            errors.append(f"ffprobe: {exc}")

    # Bundled CTBRec packages sometimes include ffmpeg but no ffprobe. ffmpeg's
    # input banner usually reports the actual container duration without a full
    # transcode.
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(video.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if match:
            duration = (
                int(match.group(1)) * 3600
                + int(match.group(2)) * 60
                + float(match.group(3))
            )
            if duration > 0:
                duration_cache.set(video.path, video.size, video.mtime, duration, "ffmpeg")
                return duration, "ffmpeg", ""
        if result.stderr:
            tail = " ".join((result.stderr or "").splitlines()[-4:])
            if tail:
                errors.append("ffmpeg: " + tail[:300])
    except subprocess.TimeoutExpired:
        errors.append(f"ffmpeg timed out after {timeout}s")
    except Exception as exc:
        errors.append(f"ffmpeg: {exc}")

    warning = "; ".join(errors[-3:]) or "No readable duration metadata."
    return None, "unreadable", warning


def probe_duration(
    video: VideoInfo,
    ffmpeg: Path,
    ffprobe: Optional[Path],
    duration_cache: DurationCache,
    cancel: threading.Event,
    strict: bool = False,
    timeout_seconds: int = 15,
) -> float:
    duration, _source, warning = probe_duration_detailed(
        video,
        ffmpeg,
        ffprobe,
        duration_cache,
        cancel,
        timeout_seconds=timeout_seconds,
    )
    if duration is not None and duration > 0:
        return duration
    if strict:
        raise RuntimeError(
            f"Could not read the actual video duration for {video.path.name}. {warning}"
        )
    return float(max(1, video.duration))

def evenly_spaced_times(duration: float, count: int) -> List[int]:
    duration = max(1.0, float(duration))
    count = max(1, int(count))
    if count == 1:
        return [min(max(0, int(duration / 2)), max(0, int(duration - 1)))]
    start = min(2.0, max(0.0, duration * 0.05))
    end = max(start, duration - 1.0)
    if end <= start:
        return [0]
    values = []
    for index in range(count):
        fraction = index / max(1, count - 1)
        values.append(max(0, min(int(end), int(round(start + (end - start) * fraction)))))
    return sorted(set(values)) or [0]


def build_frame_plan(
    chunk: Chunk,
    durations: Sequence[float],
    sample_every: int,
    max_total_frames: int,
) -> List[Tuple[int, VideoInfo, int]]:
    """Build a bounded plan while guaranteeing at least one tile per file."""
    sample_every = max(30, int(sample_every))
    file_count = len(chunk.files)
    if file_count == 0:
        return []

    natural_counts = [max(1, int(math.ceil(max(1.0, duration) / sample_every))) for duration in durations]
    natural_total = sum(natural_counts)
    target_total = max(file_count, min(max(file_count, int(max_total_frames)), natural_total))

    allocations = [1] * file_count
    remaining = target_total - file_count
    if remaining > 0:
        weights = [max(0, count - 1) for count in natural_counts]
        weight_total = sum(weights)
        if weight_total > 0:
            fractional: List[Tuple[float, int]] = []
            assigned = 0
            for index, weight in enumerate(weights):
                exact = remaining * weight / weight_total
                whole = int(math.floor(exact))
                allocations[index] += whole
                assigned += whole
                fractional.append((exact - whole, index))
            for _fraction, index in sorted(fractional, reverse=True)[: remaining - assigned]:
                allocations[index] += 1

    plan: List[Tuple[int, VideoInfo, int]] = []
    for file_index, (video, duration, count) in enumerate(
        zip(chunk.files, durations, allocations), start=1
    ):
        for second in evenly_spaced_times(duration, count):
            plan.append((file_index, video, second))
    return plan


def load_font(size: int) -> Any:
    if ImageFont is None:
        return None
    for name in ("Segoe UI.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def label_tile(image: Any, label: str) -> Any:
    assert ImageDraw is not None
    draw = ImageDraw.Draw(image, "RGBA")
    font = load_font(max(13, image.width // 22))
    padding = 6
    try:
        box = draw.textbbox((0, 0), label, font=font)
        width = box[2] - box[0]
        height = box[3] - box[1]
    except Exception:
        width, height = len(label) * 8, 18
    draw.rectangle(
        [0, 0, min(image.width, width + padding * 2), min(image.height, height + padding * 2)],
        fill=(0, 0, 0, 170),
    )
    draw.text((padding, padding), label, fill=(255, 255, 255, 255), font=font)
    return image


def extract_frame(
    ffmpeg: Path,
    source: Path,
    second: int,
    destination: Path,
    width: int,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    timeout_seconds: int = 45,
) -> bool:
    if cancel.is_set():
        return False
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-probesize", "128k", "-analyzeduration", "0",
        "-ss", str(max(0, int(second))), "-i", str(source),
        "-an", "-sn", "-dn", "-frames:v", "1",
        "-vf", f"scale={max(160, int(width))}:-2",
        "-q:v", "4", "-y", str(destination),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        process_holder[0] = process
        started = time.monotonic()
        timeout = max(5, int(timeout_seconds or 45))
        while process.poll() is None:
            if cancel.wait(0.1):
                _terminate_process_tree(process)
                return False
            if time.monotonic() - started >= timeout:
                append_log(f"ffmpeg frame extraction timed out after {timeout}s: {source}")
                _terminate_process_tree(process)
                return False
        return process.returncode == 0 and destination.exists()
    except Exception:
        return False
    finally:
        process_holder[0] = None


def extract_frames_batch(
    ffmpeg: Path,
    requests: Sequence[Tuple[Path, int, Path]],
    width: int,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    timeout_seconds: int = 45,
) -> Set[Path]:
    """Extract several independent timestamps with one ffmpeg process.

    Each requested timestamp is an independently-seeked input so exact source
    positions are preserved.  Outputs are separate JPEGs; callers may retry only
    missing frames through the legacy one-frame path.
    """
    clean = [(Path(source), max(0, int(second)), Path(destination)) for source, second, destination in requests]
    if not clean or cancel.is_set():
        return set()
    command: List[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-threads", "1", "-probesize", "128k", "-analyzeduration", "0", "-y"]
    for source, second, _destination in clean:
        command.extend(["-ss", str(second), "-i", str(source)])
    for index, (_source, _second, destination) in enumerate(clean):
        command.extend([
            "-map", f"{index}:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
            "-vf", f"scale={max(160, int(width))}:-2", "-q:v", "4", str(destination),
        ])
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
        process_holder[0] = process
        started = time.monotonic()
        timeout = max(8, int(timeout_seconds or 45) + 4 * max(0, len(clean) - 1))
        while process.poll() is None:
            if cancel.wait(0.1):
                _terminate_process_tree(process)
                return set()
            if time.monotonic() - started >= timeout:
                append_log(f"ffmpeg batched frame extraction timed out after {timeout}s ({len(clean)} outputs)")
                _terminate_process_tree(process)
                break
        return {destination for _source, _second, destination in clean if destination.is_file() and destination.stat().st_size > 0}
    except Exception as exc:
        append_log(f"ffmpeg batched frame extraction failed: {exc}")
        return set()
    finally:
        process_holder[0] = None


def generate_mosaic(
    chunk: Chunk,
    settings: Dict[str, Any],
    duration_cache: DurationCache,
    manifest: MosaicManifest,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    progress: Callable[[int, int, str], None],
    session_generated_signatures: Optional[Set[str]] = None,
    force_regenerate: bool = False,
) -> Tuple[bool, str, List[Path]]:
    use_existing = bool(settings.get("use_existing_mosaics", True))
    existing = [] if force_regenerate else find_existing_mosaics(
        chunk,
        manifest,
        use_existing=use_existing,
        session_generated_signatures=session_generated_signatures,
    )
    if existing:
        return True, "Existing mosaic reused.", existing
    if force_regenerate:
        output_dir = chunk.folder / MOSAIC_DIRNAME
        base = mosaic_base_stem(chunk)
        try:
            for old_path in [output_dir / f"{base}.jpg", output_dir / f"{base}.sources.json"]:
                if old_path.exists():
                    old_path.unlink()
            for old_path in output_dir.glob(f"{base}_part*of*.jpg") if output_dir.is_dir() else []:
                old_path.unlink()
            manifest.forget(chunk.signature)
            chunk.mosaics = []
            if session_generated_signatures is not None:
                session_generated_signatures.discard(chunk.signature)
        except Exception as exc:
            append_log(f"Could not fully remove old mosaic before regeneration: {exc}")
    if Image is None or ImageDraw is None or ImageFont is None:
        return False, "Pillow is required. Run install_requirements.bat once.", []

    ffmpeg, ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
    if ffmpeg is None:
        return False, (
            "ffmpeg was not found. Put this Lite package inside a CTBRec folder, "
            "copy ffmpeg.exe beside the script, or select ffmpeg.exe in Settings."
        ), []
    verified, message = verify_ffmpeg(ffmpeg)
    if not verified:
        return False, f"ffmpeg could not run: {message}", []

    durations: List[float] = []
    for index, video in enumerate(chunk.files, start=1):
        if cancel.is_set():
            return False, "Generation cancelled.", []
        progress(index - 1, max(1, len(chunk.files)), f"Reading duration {index}/{len(chunk.files)}: {video.path.name}")
        durations.append(probe_duration(video, ffmpeg, ffprobe, duration_cache, cancel))
    duration_cache.save()

    plan = build_frame_plan(
        chunk,
        durations,
        int(settings.get("sample_every_seconds", 300)),
        int(settings.get("max_total_frames", 240)),
    )
    if not plan:
        return False, "No frames were planned for this chunk.", []

    columns = max(1, int(settings.get("columns", 3)))
    tile_width = max(160, int(settings.get("tile_width", 480)))
    max_tiles_per_image = max(columns, int(settings.get("max_tiles_per_image", 120)))
    output_dir = chunk.folder / MOSAIC_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)
    base = mosaic_base_stem(chunk)
    # We are about to generate a fresh canonical set.  Remove every stale image
    # from an older generation of this same base first, otherwise a prior
    # different part-count can leave orphan JPEGs that appear to duplicate the
    # same source recordings.
    try:
        for stale in list(output_dir.glob(f"{base}.jpg")) + list(output_dir.glob(f"{base}_part*of*.jpg")) + [output_dir / f"{base}.sources.json"]:
            if stale.exists():
                stale.unlink()
    except Exception as exc:
        append_log(f"Could not fully clean stale same-base mosaic artifacts before generation: {exc}")

    tiles: List[Any] = []
    with tempfile.TemporaryDirectory(prefix="ctbrec_mosaic_lite_") as raw_temp:
        temp_dir = Path(raw_temp)
        frame_rows: List[Tuple[int, VideoInfo, int, Path]] = [
            (file_number, video, second, temp_dir / f"frame_{plan_index:05d}.jpg")
            for plan_index, (file_number, video, second) in enumerate(plan, start=1)
        ]
        frame_timeout = max(5, int(settings.get("frame_extract_timeout_seconds", 45) or 45))
        batch_size = max(1, min(12, int(settings.get("frame_extract_batch_size", 6) or 6)))
        # Batch by source file to avoid opening a new ffmpeg process for every
        # image.  A failed/missing output falls back individually, so one odd
        # timestamp never poisons the rest of the mosaic.
        grouped: Dict[str, List[Tuple[int, VideoInfo, int, Path]]] = {}
        for row in frame_rows:
            grouped.setdefault(normalized_absolute(row[1].path), []).append(row)
        completed = 0
        for rows in grouped.values():
            for offset in range(0, len(rows), batch_size):
                if cancel.is_set():
                    return False, "Generation cancelled.", []
                batch = rows[offset:offset + batch_size]
                progress(completed, len(plan), f"Extracting {len(batch)} mosaic frames in one ffmpeg process…")
                extracted = extract_frames_batch(
                    ffmpeg,
                    [(video.path, second, frame_path) for _file_number, video, second, frame_path in batch],
                    tile_width, cancel, process_holder, frame_timeout,
                )
                for _file_number, video, second, frame_path in batch:
                    if frame_path not in extracted and not cancel.is_set():
                        ok = extract_frame(ffmpeg, video.path, second, frame_path, tile_width, cancel, process_holder, frame_timeout)
                        if not ok and second != 0 and not cancel.is_set():
                            extract_frame(ffmpeg, video.path, 0, frame_path, tile_width, cancel, process_holder, frame_timeout)
                    completed += 1
                    progress(completed, len(plan), f"Prepared frame {completed}/{len(plan)}")
        # Compose in the original visual plan order regardless of batching order.
        for file_number, _video, second, frame_path in frame_rows:
            if frame_path.is_file():
                try:
                    with Image.open(frame_path) as opened_frame:
                        tile = opened_frame.convert("RGB")
                except Exception:
                    tile = Image.new("RGB", (tile_width, max(90, tile_width * 9 // 16)), (35, 35, 35))
            else:
                tile = Image.new("RGB", (tile_width, max(90, tile_width * 9 // 16)), (35, 35, 35))
            label = f"#{file_number}  {format_seconds(second)}"
            tiles.append(label_tile(tile, label))

    if not tiles:
        return False, "No frames could be extracted.", []

    parts = [tiles[index:index + max_tiles_per_image] for index in range(0, len(tiles), max_tiles_per_image)]
    outputs: List[Path] = []
    mobile_layout_parts: List[Dict[str, Any]] = []
    for part_index, part_tiles in enumerate(parts, start=1):
        rows = max(1, math.ceil(len(part_tiles) / columns))
        cell_width = max(tile.width for tile in part_tiles)
        cell_height = max(tile.height for tile in part_tiles)
        header_height = 46
        canvas = Image.new(
            "RGB",
            (columns * cell_width, rows * cell_height + header_height),
            (8, 8, 8),
        )
        draw = ImageDraw.Draw(canvas)
        title_font = load_font(18)
        title = (
            f"{chunk.folder.name} | chunk {chunk.idx} | "
            f"{chunk.start:%Y-%m-%d %H:%M:%S} -> {chunk.end:%Y-%m-%d %H:%M:%S} | "
            f"part {part_index}/{len(parts)}"
        )
        draw.text((10, 10), title[:180], fill=(255, 255, 255), font=title_font)
        exact_tiles: List[Dict[str, Any]] = []
        plan_offset = (part_index - 1) * max_tiles_per_image
        for tile_index, tile in enumerate(part_tiles):
            row, column = divmod(tile_index, columns)
            x = column * cell_width + (cell_width - tile.width) // 2
            y = header_height + row * cell_height + (cell_height - tile.height) // 2
            canvas.paste(tile, (x, y))
            file_number, _plan_video, local_second = plan[plan_offset + tile_index]
            file_number = int(file_number)
            exact_tiles.append({
                "file_index": file_number - 1,
                "file_number": file_number,
                "frame_index": int(plan_offset + tile_index),
                "local_seconds": float(local_second),
                "left_px": int(x),
                "top_px": int(y),
                "width_px": int(tile.width),
                "height_px": int(tile.height),
            })

        if len(parts) == 1:
            output = output_dir / f"{base}.jpg"
        else:
            output = output_dir / f"{base}_part{part_index:02d}of{len(parts):02d}.jpg"
        temporary = output.with_suffix(output.suffix + ".tmp")
        canvas.save(temporary, format="JPEG", quality=86, optimize=True)
        os.replace(temporary, output)
        outputs.append(output)
        mobile_layout_parts.append({
            "output": str(output),
            "width": int(canvas.width),
            "height": int(canvas.height),
            "tiles": exact_tiles,
        })

    sidecar = output_dir / f"{base}.sources.json"
    try:
        atomic_write_json(sidecar, {
            "signature": chunk.signature,
            "chunk_key": chunk.key,
            "folder": str(chunk.folder),
            "outputs": [str(path) for path in outputs],
            "files": [
                {"path": str(video.path), "size": video.size, "mtime": video.mtime, "duration": float(durations[index])}
                for index, video in enumerate(chunk.files)
            ],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mobile_layout_v2": {
                "version": 3,
                "mode": "original",
                "signature": chunk.signature,
                "settings": {
                    "sample_every_seconds": int(settings.get("sample_every_seconds", 300)),
                    "columns": columns,
                    "tile_width": tile_width,
                    "max_tiles_per_image": max_tiles_per_image,
                    "max_total_frames": int(settings.get("max_total_frames", 240)),
                },
                "parts": mobile_layout_parts,
            },
        })
    except Exception as exc:
        append_log(f"Mosaic generated but source sidecar could not be saved: {exc}")
    try:
        manifest.record(chunk, outputs)
    except Exception as exc:
        append_log(f"Mosaic generated but manifest could not be updated: {exc}")
    if session_generated_signatures is not None:
        session_generated_signatures.add(chunk.signature)
    progress(len(plan), len(plan), f"Generated {len(outputs)} mosaic image(s).")
    return True, f"Generated {len(outputs)} mosaic image(s).", outputs


# ---------------------------------------------------------------------------
# On-demand audio waveform generation
# ---------------------------------------------------------------------------

AUDIO_SAMPLE_RATE = 200
AUDIO_TARGET_BINS = 2400
AUDIO_DB_FLOOR = -60.0

# Rapid raw mode decodes only short, evenly distributed windows instead of
# reading every second of every file. The displayed waveform is therefore an
# approximate activity overview, which is intentionally optimized for speed.
AUDIO_RAPID_SAMPLE_RATE = 8000
AUDIO_RAPID_WINDOW_SECONDS = 0.18
AUDIO_RAPID_MIN_WINDOWS = 12
AUDIO_RAPID_MAX_WINDOWS = 72
AUDIO_RAPID_SECONDS_PER_WINDOW = 15.0
AUDIO_RAPID_INPUTS_PER_PROCESS = 12

AUDIO_MODE_RAW_ADJUSTABLE = "Raw audio (adjustable coverage)"
AUDIO_MODE_RAW_COMPLETE = "Raw audio (complete)"
AUDIO_MODE_RAW = "Raw audio (rapid sampled)"
AUDIO_MODE_VOICE = "Voice-focused (fast)"
AUDIO_MODE_DIALOGUE = "Dialogue emphasis (experimental)"
AUDIO_MODE_GATE = "Voice gate (strict)"
AUDIO_MODES = (
    AUDIO_MODE_RAW_ADJUSTABLE,
    AUDIO_MODE_RAW_COMPLETE,
    AUDIO_MODE_RAW,
    AUDIO_MODE_VOICE,
    AUDIO_MODE_DIALOGUE,
    AUDIO_MODE_GATE,
)


def normalize_audio_mode(value: Any) -> str:
    clean = str(value or "").strip().casefold()
    aliases = {
        "raw": AUDIO_MODE_RAW_ADJUSTABLE,
        "raw audio": AUDIO_MODE_RAW_ADJUSTABLE,
        "adjustable": AUDIO_MODE_RAW_ADJUSTABLE,
        "balanced": AUDIO_MODE_RAW_ADJUSTABLE,
        "raw audio (adjustable coverage)": AUDIO_MODE_RAW_ADJUSTABLE,
        "raw complete": AUDIO_MODE_RAW_COMPLETE,
        "complete": AUDIO_MODE_RAW_COMPLETE,
        "full": AUDIO_MODE_RAW_COMPLETE,
        "full raw": AUDIO_MODE_RAW_COMPLETE,
        "raw audio (complete)": AUDIO_MODE_RAW_COMPLETE,
        "raw audio (rapid sampled)": AUDIO_MODE_RAW,
        "rapid": AUDIO_MODE_RAW,
        "rapid sampled": AUDIO_MODE_RAW,
        "voice": AUDIO_MODE_VOICE,
        "voice-focused": AUDIO_MODE_VOICE,
        "voice-focused (fast)": AUDIO_MODE_VOICE,
        "dialogue": AUDIO_MODE_DIALOGUE,
        "dialogue emphasis": AUDIO_MODE_DIALOGUE,
        "dialogue emphasis (experimental)": AUDIO_MODE_DIALOGUE,
        "strict": AUDIO_MODE_GATE,
        "voice gate": AUDIO_MODE_GATE,
        "voice gate (strict)": AUDIO_MODE_GATE,
    }
    return aliases.get(clean, AUDIO_MODE_RAW_ADJUSTABLE)


def audio_mode_note(mode: str, settings: Optional[Dict[str, Any]] = None) -> str:
    mode = normalize_audio_mode(mode)
    settings = settings or {}
    if mode == AUDIO_MODE_RAW_ADJUSTABLE:
        coverage = max(1, min(100, int(settings.get("audio_raw_coverage_percent", 25) or 25)))
        return (
            f"Adjustable raw-audio coverage: ffmpeg reads evenly distributed windows totaling about {coverage}% "
            "of each recording. No voice filter, gate, or noise reduction is applied. Higher coverage preserves "
            "more brief sounds but takes longer; 100% is equivalent to complete raw audio."
        )
    if mode == AUDIO_MODE_RAW_COMPLETE:
        return (
            "Complete raw-audio coverage: ffmpeg reads the audio continuously from the beginning "
            "to the end of every segment, with no skipped sample windows, voice filtering, gate, "
            "or noise reduction. The display is condensed only to the available screen pixels; "
            "brief sounds are not omitted merely because they fell between periodic samples."
        )
    if mode == AUDIO_MODE_RAW:
        return (
            "Rapid sampled amplitude: short windows are averaged across the full timeline, "
            "then interpolated for display. No voice filters, gates, or noise reduction are "
            "applied. This is faster than decoding the entire recording, but very brief sounds "
            "between sample windows may be missed."
        )
    if mode == AUDIO_MODE_DIALOGUE:
        return (
            "Uses ffmpeg dialogue enhancement when available, then vocal-band filtering, "
            "FFT noise reduction, and a moderate adaptive gate. This can reduce some music "
            "instrumentation, but television dialogue and sung vocals may still appear."
        )
    if mode == AUDIO_MODE_GATE:
        return (
            "Vocal-band filtering plus FFT noise reduction and a strong adaptive noise gate. "
            "Best for quickly emphasizing active vocal sections, but it may hide whispers, "
            "breathing, or very quiet moaning."
        )
    return (
        "Fast vocal-band view using high/low-pass filtering, FFT noise reduction, "
        "and a light adaptive floor. It reduces rumble, hiss, fans, and many steady sounds, "
        "but cannot reliably distinguish nearby speech from television or music vocals."
    )


def _percentile(values: Sequence[float], fraction: float, fallback: float) -> float:
    cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not cleaned:
        return float(fallback)
    fraction = max(0.0, min(1.0, float(fraction)))
    position = fraction * (len(cleaned) - 1)
    lower = int(math.floor(position))
    upper = min(len(cleaned) - 1, lower + 1)
    weight = position - lower
    return cleaned[lower] * (1.0 - weight) + cleaned[upper] * weight


def _smooth_activity(values: Sequence[float], radius: int = 1) -> List[float]:
    if not values:
        return []
    radius = max(0, int(radius))
    output: List[float] = []
    total = len(values)
    for index in range(total):
        left = max(0, index - radius)
        right = min(total, index + radius + 1)
        window = values[left:right]
        # A blended mean/max preserves short vocal peaks without making clicks
        # as visually dominant as a pure peak envelope.
        mean_value = sum(window) / max(1, len(window))
        max_value = max(window)
        output.append(max(0.0, min(1.0, mean_value * 0.65 + max_value * 0.35)))
    return output



def _allocate_weighted_counts(
    total_count: int,
    weights: Sequence[float],
    minimum_each: int = 1,
) -> List[int]:
    """Allocate an integer count proportionally while preserving a minimum."""
    item_count = len(weights)
    if item_count == 0:
        return []
    minimum_each = max(0, int(minimum_each))
    total_count = max(item_count * minimum_each, int(total_count))
    allocations = [minimum_each] * item_count
    remaining = total_count - sum(allocations)
    if remaining <= 0:
        return allocations

    cleaned = [max(0.0, float(weight)) for weight in weights]
    weight_total = sum(cleaned)
    if weight_total <= 0:
        cleaned = [1.0] * item_count
        weight_total = float(item_count)

    fractions: List[Tuple[float, int]] = []
    assigned = 0
    for index, weight in enumerate(cleaned):
        exact = remaining * weight / weight_total
        whole = int(math.floor(exact))
        allocations[index] += whole
        assigned += whole
        fractions.append((exact - whole, index))
    for _fraction, index in sorted(fractions, reverse=True)[: remaining - assigned]:
        allocations[index] += 1
    return allocations


def _rapid_sample_starts(
    duration: float,
    count: int,
    window_seconds: float,
) -> List[float]:
    """Choose evenly distributed input-seek positions for rapid raw analysis."""
    duration = max(0.01, float(duration))
    count = max(1, int(count))
    window_seconds = max(0.04, min(float(window_seconds), duration))
    latest_start = max(0.0, duration - window_seconds - 0.01)
    if count == 1 or latest_start <= 0:
        return [0.0]
    return [latest_start * index / (count - 1) for index in range(count)]


def _interpolate_levels(values: Sequence[float], output_count: int) -> List[float]:
    """Expand a sparse sampled envelope to the requested display resolution."""
    output_count = max(1, int(output_count))
    if not values:
        return [0.0] * output_count
    if len(values) == 1:
        return [max(0.0, min(1.0, float(values[0])))] * output_count
    if output_count == 1:
        return [max(0.0, min(1.0, float(values[len(values) // 2])))]

    result: List[float] = []
    scale = (len(values) - 1) / (output_count - 1)
    for index in range(output_count):
        position = index * scale
        left = int(math.floor(position))
        right = min(len(values) - 1, left + 1)
        fraction = position - left
        value = float(values[left]) * (1.0 - fraction) + float(values[right]) * fraction
        result.append(max(0.0, min(1.0, value)))
    return result


def _decode_rapid_raw_pcm(
    ffmpeg: Path,
    source: Path,
    duration: float,
    sample_count: int,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    window_seconds_override: Optional[float] = None,
) -> Tuple[bytes, str]:
    """Decode only tiny windows spread across a file.

    Each ffmpeg process opens the source several times with fast input seeking,
    concatenates those short audio windows, and emits low-bandwidth mono PCM.
    It avoids the old full-duration decode, so a 15-minute recording requires
    only a few seconds of source audio to be decoded.
    """
    duration = max(0.01, float(duration))
    sample_count = max(1, int(sample_count))
    if window_seconds_override is None:
        window_seconds = min(
            AUDIO_RAPID_WINDOW_SECONDS,
            max(0.06, duration / max(1.0, sample_count * 3.0)),
        )
    else:
        window_seconds = max(0.06, min(float(window_seconds_override), duration))
    starts = _rapid_sample_starts(duration, sample_count, window_seconds)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    raw_parts: List[bytes] = []
    errors: List[str] = []

    for batch_start in range(0, len(starts), AUDIO_RAPID_INPUTS_PER_PROCESS):
        if cancel.is_set():
            return b"", "Audio analysis cancelled."
        batch = starts[batch_start:batch_start + AUDIO_RAPID_INPUTS_PER_PROCESS]
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-threads", "1",
        ]
        for start in batch:
            command.extend([
                "-ss", f"{max(0.0, start):.3f}",
                "-t", f"{window_seconds:.3f}",
                "-probesize", "128k",
                "-analyzeduration", "250000",
                "-i", str(source),
            ])

        chains: List[str] = []
        labels: List[str] = []
        for index in range(len(batch)):
            label = f"a{index}"
            chains.append(
                f"[{index}:a:0]aresample={AUDIO_RAPID_SAMPLE_RATE},"
                f"aformat=sample_fmts=s16:channel_layouts=mono,"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
            labels.append(f"[{label}]")
        chains.append(
            "".join(labels) + f"concat=n={len(batch)}:v=0:a=1[outa]"
        )
        command.extend([
            "-filter_complex", ";".join(chains),
            "-map", "[outa]",
            "-ac", "1", "-ar", str(AUDIO_RAPID_SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ])

        process: Optional[subprocess.Popen[Any]] = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            process_holder[0] = process
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        try:
                            process.terminate()
                        except Exception:
                            pass
                        try:
                            process.communicate(timeout=2)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                        return b"", "Audio analysis cancelled."
            error_text = (stderr or b"").decode("utf-8", "replace").strip()
            if process.returncode in (0, None) and stdout:
                raw_parts.append(stdout)
            else:
                failure = error_text or f"ffmpeg returned {process.returncode}"
                errors.append(failure)
                lowered = failure.casefold()
                if (
                    "matches no streams" in lowered
                    or "stream specifier" in lowered
                    or "does not contain any stream" in lowered
                ):
                    return b"", "No readable audio stream."
        except Exception as exc:
            errors.append(str(exc))
        finally:
            process_holder[0] = None

    if raw_parts:
        warning = ""
        if errors:
            warning = f"Some rapid sample batches failed: {'; '.join(errors[:3])}"
        return b"".join(raw_parts), warning
    return b"", "; ".join(error for error in errors if error) or "No readable audio stream."

def _audio_levels_from_pcm(
    raw: bytes,
    bin_count: int,
    mode: str = AUDIO_MODE_RAW_COMPLETE,
) -> Tuple[List[float], bool]:
    bin_count = max(1, int(bin_count))
    mode = normalize_audio_mode(mode)
    if not raw:
        return [0.0] * bin_count, False
    if len(raw) % 2:
        raw = raw[:-1]
    if not raw:
        return [0.0] * bin_count, False

    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return [0.0] * bin_count, False

    total = len(samples)
    db_values: List[float] = []
    for index in range(bin_count):
        start = int(index * total / bin_count)
        end = max(start + 1, int((index + 1) * total / bin_count))
        end = min(total, end)
        count = max(1, end - start)
        sum_squares = 0.0
        peak = 0
        for sample in samples[start:end]:
            absolute = abs(int(sample))
            if absolute > peak:
                peak = absolute
            sum_squares += float(sample) * float(sample)
        rms = math.sqrt(sum_squares / count) / 32768.0
        peak_ratio = peak / 32768.0
        activity = max(rms, peak_ratio * 0.25)
        if activity <= 0:
            db_values.append(-96.0)
        else:
            db_values.append(20.0 * math.log10(max(activity, 1e-9)))

    if mode == AUDIO_MODE_RAW:
        levels = [
            max(0.0, min(1.0, (db - AUDIO_DB_FLOOR) / -AUDIO_DB_FLOOR))
            for db in db_values
        ]
        return _smooth_activity(levels, radius=1), any(db > -88.0 for db in db_values)

    audible_db = [db for db in db_values if db > -90.0]
    estimated_floor = _percentile(audible_db, 0.25, -60.0)
    upper_reference = _percentile(
        audible_db,
        0.94,
        max(-20.0, estimated_floor + 24.0),
    )

    # A sustained voice or moan can occupy most of a short recording. In that
    # case the lower percentile is the wanted signal, not the room-noise floor.
    # Keep the inferred floor a mode-dependent distance below the loud reference
    # so steady vocal activity remains visible.
    if mode == AUDIO_MODE_GATE:
        noise_floor = min(estimated_floor, upper_reference - 11.0)
        threshold = max(-52.0, noise_floor + 8.0)
        exponent = 1.45
        minimum_visible = 0.055
        radius = 2
    elif mode == AUDIO_MODE_DIALOGUE:
        noise_floor = min(estimated_floor, upper_reference - 15.0)
        threshold = max(-56.0, noise_floor + 4.5)
        exponent = 1.15
        minimum_visible = 0.05
        radius = 2
    else:
        noise_floor = min(estimated_floor, upper_reference - 19.0)
        threshold = max(-58.0, noise_floor + 2.0)
        exponent = 0.82
        minimum_visible = 0.02
        radius = 1

    ceiling = max(threshold + 14.0, upper_reference)
    span = max(8.0, ceiling - threshold)
    levels: List[float] = []
    for db in db_values:
        normalized = max(0.0, min(1.0, (db - threshold) / span))
        if normalized < minimum_visible:
            normalized = 0.0
        else:
            normalized = normalized ** exponent
        levels.append(normalized)

    levels = _smooth_activity(levels, radius=radius)
    if mode == AUDIO_MODE_GATE:
        # Remove isolated one-bin remnants while keeping sustained speech,
        # moans, or breaths spanning adjacent time bins.
        gated: List[float] = []
        for index, value in enumerate(levels):
            left = levels[index - 1] if index > 0 else 0.0
            right = levels[index + 1] if index + 1 < len(levels) else 0.0
            if value < 0.07 and max(left, right) < 0.06:
                gated.append(0.0)
            else:
                gated.append(value)
        levels = gated

    # The second value indicates that a readable audio stream existed. The
    # waveform itself may legitimately be flat after strict filtering.
    return levels, bool(raw)


def _audio_filter_attempts(mode: str) -> List[Tuple[str, str]]:
    """Return ffmpeg filter attempts from preferred to broadest fallback.

    Each chain converts the filtered sound into a slow amplitude envelope before
    the output is downsampled to AUDIO_SAMPLE_RATE. This preserves voice-band
    activity while keeping the PCM stream tiny and fast to analyze.
    """
    mode = normalize_audio_mode(mode)
    envelope = (
        "aformat=channel_layouts=mono,"
        "aeval=abs(val(0)),"
        "lowpass=f=20"
    )
    raw_envelope = envelope
    if mode in {AUDIO_MODE_RAW_COMPLETE, AUDIO_MODE_RAW}:
        return [(raw_envelope, "")]

    voice_chain = (
        "aformat=channel_layouts=mono,"
        "highpass=f=85,lowpass=f=3600,afftdn=nf=-28,"
        "aeval=abs(val(0)),lowpass=f=20"
    )
    simple_voice = (
        "aformat=channel_layouts=mono,"
        "highpass=f=85,lowpass=f=3600,"
        "aeval=abs(val(0)),lowpass=f=20"
    )

    if mode == AUDIO_MODE_DIALOGUE:
        return [
            (
                "dialoguenhance=original=0.35:enhance=2.5:voice=6,"
                "aformat=channel_layouts=mono,"
                "highpass=f=100,lowpass=f=3400,afftdn=nf=-30,"
                "aeval=abs(val(0)),lowpass=f=20",
                "",
            ),
            (
                voice_chain,
                "The bundled ffmpeg lacks dialogue enhancement; used the standard voice-focused filter instead.",
            ),
            (
                simple_voice,
                "The bundled ffmpeg lacks dialogue enhancement/noise reduction; used vocal-band filtering only.",
            ),
            (
                raw_envelope,
                "The bundled ffmpeg rejected all dialogue filters; displayed the raw activity envelope instead.",
            ),
        ]

    return [
        (voice_chain, ""),
        (
            simple_voice,
            "The bundled ffmpeg lacks FFT noise reduction; used vocal-band filtering only.",
        ),
        (
            raw_envelope,
            "The bundled ffmpeg rejected the voice filters; displayed the raw activity envelope instead.",
        ),
    ]


def _decode_audio_pcm(
    ffmpeg: Path,
    source: Path,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    mode: str = AUDIO_MODE_RAW_COMPLETE,
) -> Tuple[bytes, str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    errors: List[str] = []

    for filter_expression, fallback_warning in _audio_filter_attempts(mode):
        if cancel.is_set():
            return b"", "Audio analysis cancelled."

        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-threads", "1", "-i", str(source),
            "-map", "0:a:0?", "-vn", "-sn", "-dn",
        ]
        if filter_expression:
            command.extend(["-af", filter_expression])
        command.extend([
            "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE),
            "-f", "s16le", "pipe:1",
        ])

        process: Optional[subprocess.Popen[Any]] = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            process_holder[0] = process
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        try:
                            process.terminate()
                        except Exception:
                            pass
                        try:
                            process.communicate(timeout=2)
                        except Exception:
                            try:
                                process.kill()
                            except Exception:
                                pass
                        return b"", "Audio analysis cancelled."

            error_text = (stderr or b"").decode("utf-8", "replace").strip()
            if process.returncode in (0, None) and stdout:
                warning = fallback_warning
                if error_text and not warning:
                    warning = error_text
                return stdout, warning
            errors.append(error_text or f"ffmpeg returned {process.returncode}")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            process_holder[0] = None

    return b"", "; ".join(error for error in errors if error) or "No readable audio stream."


def generate_audio_waveform(
    chunk: Chunk,
    settings: Dict[str, Any],
    duration_cache: DurationCache,
    cancel: threading.Event,
    process_holder: List[Optional[subprocess.Popen[Any]]],
    progress: Callable[[int, int, str], None],
    mode: Optional[str] = None,
) -> Tuple[bool, str, Optional[AudioWaveform]]:
    mode = normalize_audio_mode(mode or settings.get("audio_waveform_mode", AUDIO_MODE_RAW_ADJUSTABLE))
    ffmpeg, ffprobe = resolve_ffmpeg(str(settings.get("ffmpeg_path", "")))
    if ffmpeg is None:
        return False, "ffmpeg was not found. Select it in Settings first.", None
    verified, message = verify_ffmpeg(ffmpeg)
    if not verified:
        return False, f"ffmpeg could not run: {message}", None
    if not chunk.files:
        return False, "The current chunk has no video files.", None

    durations: List[float] = []
    total_files = len(chunk.files)
    for index, video in enumerate(chunk.files, start=1):
        if cancel.is_set():
            return False, "Audio analysis cancelled.", None
        progress(
            index - 1,
            total_files * 2,
            f"Reading duration {index}/{total_files}: {video.path.name}",
        )
        duration = probe_duration(video, ffmpeg, ffprobe, duration_cache, cancel)
        durations.append(max(1.0, float(duration)))
    duration_cache.save()

    total_duration = max(1.0, sum(durations))
    configured_bins = max(300, min(12000, int(settings.get("audio_waveform_target_bins", AUDIO_TARGET_BINS) or AUDIO_TARGET_BINS)))
    target_bins = max(
        300,
        min(12000, max(configured_bins, int(total_duration * 1.5))),
    )
    allocations = [
        max(8, int(round(target_bins * duration / total_duration)))
        for duration in durations
    ]

    rapid_sample_allocations: List[int] = []
    adjustable_window_seconds: List[float] = []
    if mode == AUDIO_MODE_RAW_ADJUSTABLE:
        coverage = max(1, min(100, int(settings.get("audio_raw_coverage_percent", 25) or 25)))
        max_windows = max(1, min(500, int(settings.get("audio_raw_max_windows_per_file", 72) or 72)))
        for duration in durations:
            if coverage >= 100:
                rapid_sample_allocations.append(1)
                adjustable_window_seconds.append(float(duration))
            else:
                covered_seconds = max(0.18, float(duration) * coverage / 100.0)
                count = max(1, min(max_windows, int(math.ceil(covered_seconds / 5.0))))
                rapid_sample_allocations.append(count)
                adjustable_window_seconds.append(max(0.18, covered_seconds / count))
    elif mode == AUDIO_MODE_RAW:
        rapid_window_target = max(
            total_files,
            min(
                AUDIO_RAPID_MAX_WINDOWS,
                max(
                    AUDIO_RAPID_MIN_WINDOWS,
                    int(math.ceil(total_duration / AUDIO_RAPID_SECONDS_PER_WINDOW)),
                ),
            ),
        )
        rapid_sample_allocations = _allocate_weighted_counts(
            rapid_window_target, durations, minimum_each=1
        )

    segments: List[AudioSegment] = []
    all_levels: List[float] = []
    timeline = 0.0
    warnings: List[str] = []
    unreadable_count = 0

    for index, (video, duration, bin_count) in enumerate(
        zip(chunk.files, durations, allocations),
        start=1,
    ):
        if cancel.is_set():
            return False, "Audio analysis cancelled.", None
        progress(
            total_files + index - 1,
            total_files * 2,
            f"Analyzing {mode} {index}/{total_files}: {video.path.name}",
        )
        if mode in {AUDIO_MODE_RAW, AUDIO_MODE_RAW_ADJUSTABLE}:
            sample_count = rapid_sample_allocations[index - 1]
            adjustable_coverage = max(1, min(100, int(settings.get("audio_raw_coverage_percent", 25) or 25)))
            if mode == AUDIO_MODE_RAW_ADJUSTABLE and adjustable_coverage >= 100:
                # At 100%, use the efficient complete-envelope decoder rather than
                # emitting full-rate 8 kHz PCM through the sampled-window path.
                raw, warning = _decode_audio_pcm(
                    ffmpeg, video.path, cancel, process_holder, mode=AUDIO_MODE_RAW_COMPLETE
                )
                levels, has_audio = _audio_levels_from_pcm(raw, bin_count, mode=AUDIO_MODE_RAW)
            else:
                raw, warning = _decode_rapid_raw_pcm(
                    ffmpeg,
                    video.path,
                    duration,
                    sample_count,
                    cancel,
                    process_holder,
                    window_seconds_override=(adjustable_window_seconds[index - 1] if mode == AUDIO_MODE_RAW_ADJUSTABLE else None),
                )
                if mode == AUDIO_MODE_RAW_ADJUSTABLE:
                    sampled_bin_count = max(
                        sample_count,
                        min(bin_count, int(round(bin_count * adjustable_coverage / 100.0))),
                    )
                else:
                    sampled_bin_count = sample_count
                sampled_levels, has_audio = _audio_levels_from_pcm(
                    raw, sampled_bin_count, mode=AUDIO_MODE_RAW
                )
                levels = _interpolate_levels(sampled_levels, bin_count)
        else:
            raw, warning = _decode_audio_pcm(
                ffmpeg,
                video.path,
                cancel,
                process_holder,
                mode=mode,
            )
            levels, has_audio = _audio_levels_from_pcm(
                raw, bin_count, mode=(AUDIO_MODE_RAW if mode == AUDIO_MODE_RAW_COMPLETE else mode)
            )
        smoothing_radius = max(0, min(20, int(settings.get("audio_smoothing_radius", 1) or 0)))
        if smoothing_radius > 1:
            levels = _smooth_activity(levels, radius=smoothing_radius)
        if warning:
            warnings.append(f"#{index} {video.path.name}: {warning}")
        if not raw:
            unreadable_count += 1

        segment = AudioSegment(
            file_index=index,
            path=video.path,
            start_seconds=timeline,
            duration=duration,
            levels=levels,
            has_audio=has_audio,
        )
        segments.append(segment)
        all_levels.extend(levels)
        timeline += duration

    waveform = AudioWaveform(
        chunk_signature=chunk.signature,
        model_name=chunk.folder.name,
        total_duration=timeline,
        segments=segments,
        levels=all_levels,
        mode=mode,
        filter_note=audio_mode_note(mode, settings),
    )
    progress(total_files * 2, total_files * 2, f"{mode} waveform ready.")

    readable = sum(1 for segment in segments if segment.has_audio)
    visible = sum(
        1 for segment in segments
        if any(level > 0.01 for level in segment.levels)
    )
    summary = (
        f"{mode}: analyzed {total_files} file(s); "
        f"{readable} readable audio stream(s), visible activity in {visible}."
    )
    if mode == AUDIO_MODE_RAW_COMPLETE:
        summary += " Read the complete audio timeline continuously with no sampling gaps."
    elif mode == AUDIO_MODE_RAW:
        summary += (
            f" Used {sum(rapid_sample_allocations)} short sample windows "
            f"instead of full-duration decoding."
        )
    if unreadable_count:
        summary += f" {unreadable_count} file(s) had no readable audio stream."
    if warnings:
        summary += f" {len(warnings)} filter/audio warning(s) logged."
        append_log("Audio waveform warnings: " + " | ".join(warnings[:30]))
    return True, summary, waveform


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def move_file_fast(source: Path, destination: Path) -> Path:
    destination = unique_destination(destination)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return destination


def restore_review_state(
    folder: Path,
    chunk_key_value: str,
    previous_value: Optional[str],
) -> None:
    state = load_review_state(folder)
    reviewed = state.setdefault("reviewed", {})
    if previous_value is None:
        reviewed.pop(chunk_key_value, None)
    else:
        reviewed[chunk_key_value] = previous_value
    save_review_state(folder, state)


def process_chunk_files(
    chunk: Chunk,
    keep_indices: Set[int],
    dry_run: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    keep_destination = review_folder(chunk.folder)
    delete_destination = deletion_folder(chunk.folder)
    moved_keep: List[str] = []
    moved_delete: List[str] = []
    missing: List[str] = []
    errors: List[str] = []
    moves: List[Dict[str, Any]] = []
    original_state = load_review_state(chunk.folder)
    previous_review_state = original_state.get("reviewed", {}).get(chunk.key)

    for index, video in enumerate(chunk.files):
        source = video.path
        if not source.exists():
            missing.append(str(source))
            continue
        destination_dir = keep_destination if index in keep_indices else delete_destination
        try:
            if dry_run:
                destination = destination_dir / source.name
            else:
                destination = move_file_fast(source, destination_dir / source.name)
            moves.append({
                "index": index,
                "source": str(source),
                "destination": str(destination),
                "decision": "KEEP" if index in keep_indices else "DELETE",
            })
            if index in keep_indices:
                moved_keep.append(str(destination))
            else:
                moved_delete.append(str(destination))
        except Exception as exc:
            errors.append(f"{source.name}: {exc}")

    if not errors:
        state = load_review_state(chunk.folder)
        state.setdefault("reviewed", {})[chunk.key] = "done"
        if not dry_run:
            save_review_state(chunk.folder, state)

    details = {
        "kept": moved_keep,
        "deleted": moved_delete,
        "missing": missing,
        "errors": errors,
        "dry_run": dry_run,
        "moves": moves,
        "previous_review_state": previous_review_state,
    }
    if errors:
        return False, (
            f"Moved {len(moved_keep)} keep and {len(moved_delete)} delete file(s), "
            f"but {len(errors)} move(s) failed. Close VLC/players and rescan."
        ), details
    prefix = "[dry-run] " if dry_run else ""
    return True, (
        f"{prefix}Kept {len(moved_keep)} in Review; moved {len(moved_delete)} to "
        f"{DELETION_BUCKET_NAME}/{chunk.folder.name}."
    ), details


def mark_chunk_skipped(chunk: Chunk) -> Optional[str]:
    state = load_review_state(chunk.folder)
    reviewed = state.setdefault("reviewed", {})
    previous = reviewed.get(chunk.key)
    reviewed[chunk.key] = "skipped"
    save_review_state(chunk.folder, state)
    return previous


def restore_history_entry(entry: ReviewHistoryEntry) -> Tuple[bool, str, Dict[str, Any]]:
    """Undo a processed/skipped chunk so it can be reviewed again.

    The operation is idempotent: files already returned to their original path
    are accepted, which makes retrying safe after a partial filesystem error.
    """
    restored: List[str] = []
    already_restored: List[str] = []
    errors: List[str] = []

    if entry.action == "processed":
        for move in reversed(entry.moves):
            original = Path(str(move.get("source", "")))
            destination = Path(str(move.get("destination", "")))
            if not str(original) or not str(destination):
                errors.append("A move record was incomplete.")
                continue
            try:
                if destination.exists():
                    if original.exists():
                        errors.append(
                            f"Cannot restore {destination.name}: the original path already exists: {original}"
                        )
                        continue
                    original.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(destination, original)
                    except OSError:
                        shutil.move(str(destination), str(original))
                    restored.append(str(original))
                elif original.exists():
                    already_restored.append(str(original))
                else:
                    errors.append(
                        f"Cannot find either the moved file or its original path: {destination}"
                    )
            except Exception as exc:
                errors.append(f"{destination.name}: {exc}")

    if errors:
        return False, (
            f"Back restored {len(restored):,} file(s), but {len(errors):,} item(s) failed. "
            "Fix the listed conflict and press Back again."
        ), {
            "restored": restored,
            "already_restored": already_restored,
            "errors": errors,
        }

    restore_review_state(
        entry.chunk.folder,
        entry.chunk.key,
        entry.previous_review_state,
    )
    return True, (
        f"Returned to the previous {entry.action} chunk; "
        f"restored {len(restored):,} moved file(s)."
    ), {
        "restored": restored,
        "already_restored": already_restored,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Minimal Tkinter GUI
# ---------------------------------------------------------------------------

class MosaicLiteApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.settings = self._load_settings()
        self.root.geometry(sanitize_window_geometry(self.settings.get("window_geometry", "1100x760")))
        # The main content is scrollable/responsive, so the top-level window
        # may be reduced to a compact utility size instead of being locked to
        # the natural width of the toolbar rows.
        self.root.minsize(280, 220)

        self.duration_cache = DurationCache(DURATION_CACHE_PATH)
        self.manifest = MosaicManifest(MANIFEST_PATH)
        self.recu_cache = RecuMetadataCache(RECU_CACHE_PATH)
        self.transcript_cache = SpeechTranscriptCache(TRANSCRIPT_CACHE_PATH)
        self.speech_engine_manager = SpeechEngineManager()
        # In fresh-generation mode, only signatures created during this running
        # process are trusted. This prevents reuse of faulty mosaics from older
        # runs while still preventing duplicate current/look-ahead work.
        self.session_generated_signatures: Set[str] = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mosaic-lite")
        # One separate, single-worker executor performs bounded look-ahead
        # generation. It never scans other models and never runs more than one
        # pre-generation job at a time.
        self.prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mosaic-lite-prefetch")
        # Separate shallow name-index worker. It never reads video metadata.
        self.model_index_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mosaic-lite-model-index")
        self.recu_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mosaic-lite-recu")
        self.messages: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.busy = False
        self.cancel_event = threading.Event()
        self.process_holder: List[Optional[subprocess.Popen[Any]]] = [None]
        self.prefetch_future: Optional[Any] = None
        self.prefetch_cancel_event: Optional[threading.Event] = None
        self.prefetch_process_holder: Optional[List[Optional[subprocess.Popen[Any]]]] = None
        self.prefetch_generation = 0
        self.prefetch_inflight: Set[str] = set()
        self.duration_prepared_signatures: Set[str] = set()
        self.mosaic_failed_signatures: Set[str] = set()
        self.recu_generation = 0
        self.recu_cancel_event = threading.Event()
        self.recu_future: Optional[Any] = None
        self.recu_model_data: Optional[RecuModelData] = None
        self.recu_model_name = ""
        self.recu_resolution_generation = 0
        self.recu_resolution_cancel_event = threading.Event()
        self.recu_resolution_future: Optional[Any] = None
        self.recu_resolving_video_ids: Set[str] = set()
        self.recu_failed_video_ids: Set[str] = set()
        self.segment_kink_moments: Dict[int, List[RecuMoment]] = {}
        self.recu_comment_moments: Dict[str, RecuMoment] = {}

        self.roots: List[Path] = []
        self.model_folders: List[Path] = []
        self.all_model_folders: List[Path] = []
        self.chunks: List[Chunk] = []
        self.current_chunk: Optional[Chunk] = None
        self.keep_indices: Set[int] = set()
        self.last_model_text = ""
        self.model_catalog: List[str] = []
        self.model_catalog_ready = False
        self.model_catalog_generation = 0
        self.model_filter_after_id: Optional[str] = None
        # Waveforms are generated only on demand and cached for this session.
        self.audio_waveform_cache: Dict[str, AudioWaveform] = {}
        self.speech_transcript_cache: Dict[str, SpeechTranscript] = {}
        self.speech_cancel_event = threading.Event()
        self.audio_tools_state: Dict[str, Any] = {}
        self.review_history: List[ReviewHistoryEntry] = []
        self.pending_back_entry: Optional[ReviewHistoryEntry] = None
        self.pending_restore_keep_indices: Optional[Set[int]] = None

        self._build_ui()
        self._load_roots(show_errors=False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(75, self._poll_messages)

    # ---------------------------- settings ---------------------------------
    def _load_settings(self) -> Dict[str, Any]:
        raw = load_json(SETTINGS_PATH, {})
        result = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            result.update(raw)
            # Migrate the original Lite defaults while preserving deliberate
            # custom values. Version 1 shipped with 320px tiles and 4 columns.
            version = int(raw.get("settings_version", 1) or 1)
            if version < 2:
                if int(raw.get("tile_width", 320) or 320) == 320:
                    result["tile_width"] = 480
                if int(raw.get("columns", 4) or 4) == 4:
                    result["columns"] = 3
            if version < 3:
                # Preserve the historical behavior unless the user explicitly
                # turns off reuse in Settings.
                result.setdefault("use_existing_mosaics", True)
            if version < 5:
                result.setdefault("audio_waveform_mode", AUDIO_MODE_VOICE)
            if version < 6:
                # Version 1.8 makes the no-filter rapid sampled waveform the
                # default so the main View Audio Track button is immediately
                # fast for existing installations as well as new ones.
                result["audio_waveform_mode"] = AUDIO_MODE_RAW
            result["audio_waveform_mode"] = normalize_audio_mode(
                result.get("audio_waveform_mode", AUDIO_MODE_RAW_COMPLETE)
            )
            if version < 11 and result["audio_waveform_mode"] == AUDIO_MODE_RAW:
                # Upgrade the prior rapid-sampled default to continuous full-timeline raw audio.
                result["audio_waveform_mode"] = AUDIO_MODE_RAW_COMPLETE
            if version < 7:
                for key, value in DEFAULT_SETTINGS.items():
                    result.setdefault(key, value)
            if version < 10:
                legacy_mosaic = int(raw.get("pre_generate_count", 1) or 0)
                legacy_recu = int(raw.get("recu_prefetch_chunk_count", 1) or 0) + 1
                result["chunks_to_prepare"] = max(int(result.get("chunks_to_prepare", 3) or 3), 1, legacy_mosaic, legacy_recu)
            if version < 12:
                for key in ("audio_raw_coverage_percent", "audio_raw_max_windows_per_file", "audio_waveform_target_bins", "audio_smoothing_radius"):
                    result.setdefault(key, DEFAULT_SETTINGS[key])
                if result.get("audio_waveform_mode") == AUDIO_MODE_RAW_COMPLETE:
                    result["audio_waveform_mode"] = AUDIO_MODE_RAW_ADJUSTABLE
            if version < 13:
                for key in (
                    "speech_engine", "speech_vosk_model", "speech_vosk_model_path", "speech_vosk_model_root",
                    "speech_model", "speech_model_path", "speech_model_download_root",
                    "speech_language", "speech_device", "speech_compute_type", "speech_cpu_compatibility_mode",
                    "speech_cpu_threads", "speech_beam_size", "speech_vad_filter",
                    "speech_fuzzy_threshold_percent", "speech_cache_enabled",
                ):
                    result.setdefault(key, DEFAULT_SETTINGS[key])
            if version < 15:
                # Older HP/Intel systems can crash inside CTranslate2 while the
                # model is being initialized. Automatic mode starts with a
                # conservative AVX/no-MKL profile and retries with a fully generic
                # float32 profile if the native runtime still exits.
                result.setdefault("speech_cpu_compatibility_mode", DEFAULT_SETTINGS["speech_cpu_compatibility_mode"])
            if version < 16:
                # The user's installed faster-whisper/CTranslate2 combination
                # access-violates during model construction under every CPU
                # profile, including generic float32 and a fresh model cache.
                # Migrate existing installs to the lightweight Vosk backend,
                # while retaining faster-whisper as an explicit optional choice.
                result["speech_engine"] = DEFAULT_SETTINGS["speech_engine"]
                for key in (
                    "speech_vosk_model", "speech_vosk_model_path",
                    "speech_vosk_model_root",
                ):
                    result.setdefault(key, DEFAULT_SETTINGS[key])
            if version < 17:
                if "recu_auth_fallback" not in raw:
                    result["recu_auth_fallback"] = True
                if "recu_warmup_homepage" not in raw:
                    result["recu_warmup_homepage"] = True
                if "recu_auth_retry_cooldown_seconds" not in raw:
                    result["recu_auth_retry_cooldown_seconds"] = 30
                legacy_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36"
                if str(result.get("recu_user_agent", "")).strip() == legacy_ua:
                    result["recu_user_agent"] = DEFAULT_SETTINGS["recu_user_agent"]
            if version < 18:
                valid_orders = {
                    "Largest chunks first", KINK_FIRST_ORDER,
                    "Oldest first", "Newest first",
                }
                if str(result.get("queue_order", "")) not in valid_orders:
                    result["queue_order"] = DEFAULT_SETTINGS["queue_order"]
            result["window_geometry"] = sanitize_window_geometry(
                result.get("window_geometry", DEFAULT_SETTINGS["window_geometry"]),
                DEFAULT_SETTINGS["window_geometry"],
            )
            result["settings_version"] = 18
        return result

    def _save_settings(self) -> None:
        try:
            self.settings["window_geometry"] = self.root.geometry()
            atomic_write_json(SETTINGS_PATH, self.settings)
        except Exception:
            pass

    # ------------------------------- UI -------------------------------------
    def _build_ui(self) -> None:
        # A canvas-backed viewport decouples the top-level window's minimum
        # size from the natural width/height of its controls. At compact sizes,
        # toolbars wrap and the entire content remains reachable by scrollbars.
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        self.main_canvas = tk.Canvas(
            shell,
            highlightthickness=0,
            borderwidth=0,
            takefocus=False,
        )
        self.main_vbar = ttk.Scrollbar(
            shell, orient="vertical", command=self.main_canvas.yview
        )
        self.main_hbar = ttk.Scrollbar(
            shell, orient="horizontal", command=self.main_canvas.xview
        )
        self.main_canvas.configure(
            yscrollcommand=self.main_vbar.set,
            xscrollcommand=self.main_hbar.set,
        )
        self.main_canvas.grid(row=0, column=0, sticky="nsew")
        self.main_vbar.grid(row=0, column=1, sticky="ns")
        self.main_hbar.grid(row=1, column=0, sticky="ew")

        outer = ttk.Frame(self.main_canvas, padding=9)
        self.main_content = outer
        self.main_canvas_window = self.main_canvas.create_window(
            (0, 0), window=outer, anchor="nw"
        )
        self._main_layout_after_id: Optional[str] = None
        self._wrapped_labels: List[ttk.Label] = []

        def wrapped_label(parent: Any, **kwargs: Any) -> ttk.Label:
            label = ttk.Label(parent, **kwargs)
            self._wrapped_labels.append(label)
            return label

        def fit_scrollable_content() -> None:
            self._main_layout_after_id = None
            try:
                viewport_width = max(1, self.main_canvas.winfo_width())
                viewport_height = max(1, self.main_canvas.winfo_height())
                # Keep a tiny internal floor so even an extremely narrow
                # top-level window can pan horizontally rather than clipping.
                target_width = max(300, viewport_width)
                wrap_width = max(160, target_width - 34)
                for label in self._wrapped_labels:
                    try:
                        label.configure(wraplength=wrap_width)
                    except tk.TclError:
                        pass
                self.main_content.update_idletasks()
                target_height = max(
                    viewport_height,
                    self.main_content.winfo_reqheight(),
                )
                self.main_canvas.itemconfigure(
                    self.main_canvas_window,
                    width=target_width,
                    height=target_height,
                )
                self.main_canvas.configure(
                    scrollregion=self.main_canvas.bbox("all")
                )
            except tk.TclError:
                pass

        def schedule_scrollable_fit(_event: Any = None) -> None:
            if self._main_layout_after_id is not None:
                try:
                    self.root.after_cancel(self._main_layout_after_id)
                except Exception:
                    pass
            self._main_layout_after_id = self.root.after_idle(
                fit_scrollable_content
            )

        outer.bind("<Configure>", schedule_scrollable_fit)
        self.main_canvas.bind("<Configure>", schedule_scrollable_fit)

        def register_flow_bar(frame: ttk.Frame, widgets: Sequence[Any]) -> None:
            """Wrap toolbar widgets to additional rows as width decreases."""
            widget_list = list(widgets)

            def layout(event: Any = None) -> None:
                try:
                    available = max(
                        180,
                        int(getattr(event, "width", 0) or frame.winfo_width()),
                    )
                    for child in widget_list:
                        child.grid_forget()
                    for column in range(len(widget_list) + 1):
                        frame.columnconfigure(column, weight=0)

                    row = 0
                    column = 0
                    used = 0
                    for child in widget_list:
                        requested = max(42, child.winfo_reqwidth()) + 8
                        if used and used + requested > available:
                            row += 1
                            column = 0
                            used = 0
                        child.grid(
                            row=row,
                            column=column,
                            sticky="w",
                            padx=2,
                            pady=2,
                        )
                        used += requested
                        column += 1
                    schedule_scrollable_fit()
                except tk.TclError:
                    pass

            frame.bind("<Configure>", layout)
            self.root.after_idle(layout)

        # Roots row: entry expands on wide windows and moves above its buttons
        # as the window narrows.
        roots_line = ttk.Frame(outer)
        roots_line.pack(fill="x")
        roots_label = ttk.Label(roots_line, text="Roots file:")
        self.roots_file_var = tk.StringVar(
            value=str(self.settings.get("roots_file", "recording_roots.txt"))
        )
        roots_entry = ttk.Entry(
            roots_line, textvariable=self.roots_file_var
        )
        roots_browse = ttk.Button(
            roots_line, text="Browse", command=self._browse_roots
        )
        roots_reload = ttk.Button(
            roots_line,
            text="Reload",
            command=lambda: self._load_roots(show_errors=True),
        )
        self.root_count_var = tk.StringVar(value="0 roots")
        roots_count = ttk.Label(
            roots_line, textvariable=self.root_count_var
        )
        roots_widgets = (
            roots_label,
            roots_entry,
            roots_browse,
            roots_reload,
            roots_count,
        )

        def layout_roots(event: Any = None) -> None:
            width = int(getattr(event, "width", 0) or roots_line.winfo_width())
            for child in roots_widgets:
                child.grid_forget()
            for column in range(6):
                roots_line.columnconfigure(column, weight=0)
            if width >= 760:
                roots_label.grid(row=0, column=0, sticky="w")
                roots_entry.grid(
                    row=0, column=1, sticky="ew", padx=5
                )
                roots_browse.grid(row=0, column=2, padx=2)
                roots_reload.grid(row=0, column=3, padx=2)
                roots_count.grid(row=0, column=4, padx=(8, 2))
                roots_line.columnconfigure(1, weight=1)
            elif width >= 470:
                roots_label.grid(row=0, column=0, sticky="w")
                roots_entry.grid(
                    row=0, column=1, columnspan=4, sticky="ew", padx=5
                )
                roots_browse.grid(row=1, column=1, sticky="w", padx=2, pady=2)
                roots_reload.grid(row=1, column=2, sticky="w", padx=2, pady=2)
                roots_count.grid(row=1, column=3, sticky="w", padx=8, pady=2)
                roots_line.columnconfigure(1, weight=1)
            else:
                roots_label.grid(row=0, column=0, sticky="w")
                roots_entry.grid(
                    row=1, column=0, columnspan=2, sticky="ew", pady=2
                )
                roots_browse.grid(row=2, column=0, sticky="w", padx=2, pady=2)
                roots_reload.grid(row=2, column=1, sticky="w", padx=2, pady=2)
                roots_count.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
                roots_line.columnconfigure(0, weight=1)
                roots_line.columnconfigure(1, weight=1)
            schedule_scrollable_fit()

        roots_line.bind("<Configure>", layout_roots)
        self.root.after_idle(layout_roots)

        # Searchable model row. Buttons use their own wrapping sub-toolbar so
        # the editable field remains usable at very narrow widths.
        model_line = ttk.Frame(outer)
        model_line.pack(fill="x", pady=(7, 0))
        model_label = ttk.Label(model_line, text="Model:")
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            model_line,
            textvariable=self.model_var,
            values=[],
            state="normal",
        )
        self.model_combo.bind(
            "<Return>", lambda _event: self._find_model()
        )
        self.model_combo.bind("<KeyRelease>", self._on_model_key_release)
        self.model_combo.bind(
            "<FocusIn>",
            lambda _event: self._schedule_model_filter(False),
        )
        self.model_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.root.after_idle(self._find_model),
        )
        self.model_var.trace_add("write", self._on_model_text_changed)

        model_buttons = ttk.Frame(model_line)
        find_button = ttk.Button(
            model_buttons,
            text="Find across all roots",
            command=self._find_model,
        )
        choose_button = ttk.Button(
            model_buttons,
            text="Choose folder",
            command=self._choose_model_folder,
        )
        refresh_button = ttk.Button(
            model_buttons,
            text="Refresh names",
            command=self._refresh_model_catalog,
        )
        register_flow_bar(
            model_buttons,
            [find_button, choose_button, refresh_button],
        )
        model_widgets = (model_label, self.model_combo, model_buttons)

        def layout_model(event: Any = None) -> None:
            width = int(getattr(event, "width", 0) or model_line.winfo_width())
            for child in model_widgets:
                child.grid_forget()
            for column in range(4):
                model_line.columnconfigure(column, weight=0)
            if width >= 900:
                model_label.grid(row=0, column=0, sticky="w")
                self.model_combo.grid(
                    row=0, column=1, sticky="ew", padx=5
                )
                model_buttons.grid(row=0, column=2, sticky="ew")
                model_line.columnconfigure(1, weight=1)
            elif width >= 470:
                model_label.grid(row=0, column=0, sticky="w")
                self.model_combo.grid(
                    row=0, column=1, sticky="ew", padx=5
                )
                model_buttons.grid(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky="ew",
                    pady=(2, 0),
                )
                model_line.columnconfigure(1, weight=1)
            else:
                model_label.grid(row=0, column=0, sticky="w")
                self.model_combo.grid(
                    row=1, column=0, sticky="ew", pady=2
                )
                model_buttons.grid(
                    row=2, column=0, sticky="ew", pady=(2, 0)
                )
                model_line.columnconfigure(0, weight=1)
            schedule_scrollable_fit()

        model_line.bind("<Configure>", layout_model)
        self.root.after_idle(layout_model)

        self.model_catalog_var = tk.StringVar(
            value=(
                "Type continuously to filter; open the updated result list "
                "with the arrow or Alt+Down. Videos remain untouched."
            )
        )
        wrapped_label(
            outer,
            textvariable=self.model_catalog_var,
        ).pack(anchor="w", fill="x", pady=(2, 0))

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(7, 0))
        drive_label = ttk.Label(options, text="Drive:")
        self.drive_var = tk.StringVar(value="All")
        self.drive_combo = ttk.Combobox(
            options,
            textvariable=self.drive_var,
            values=["All"],
            state="readonly",
            width=10,
        )
        self.drive_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._rebuild_queue_for_scope(),
        )
        order_label = ttk.Label(options, text="Order:")
        self.order_var = tk.StringVar(
            value=str(
                self.settings.get(
                    "queue_order", "Largest chunks first"
                )
            )
        )
        order_combo = ttk.Combobox(
            options,
            textvariable=self.order_var,
            values=[
                "Largest chunks first",
                KINK_FIRST_ORDER,
                "Oldest first",
                "Newest first",
            ],
            state="readonly",
            width=27,
        )
        order_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._change_order(),
        )
        self.dry_run_var = tk.BooleanVar(value=False)
        dry_run_check = ttk.Checkbutton(
            options, text="Dry-run", variable=self.dry_run_var
        )
        self.auto_generate_var = tk.BooleanVar(
            value=bool(
                self.settings.get("auto_generate_current", True)
            )
        )
        auto_generate_check = ttk.Checkbutton(
            options,
            text="Prepare mosaics automatically",
            variable=self.auto_generate_var,
            command=self._sync_quick_settings,
        )
        settings_button = ttk.Button(
            options, text="Settings", command=self._open_settings
        )
        register_flow_bar(
            options,
            [
                drive_label,
                self.drive_combo,
                order_label,
                order_combo,
                dry_run_check,
                auto_generate_check,
                settings_button,
            ],
        )

        self.status_var = tk.StringVar(
            value=(
                "Enter a model name. Nothing is scanned until you ask "
                "for that model."
            )
        )
        wrapped_label(
            outer,
            textvariable=self.status_var,
        ).pack(anchor="w", fill="x", pady=(8, 1))
        self.prefetch_var = tk.StringVar(
            value="Preparation queue: idle"
        )
        wrapped_label(
            outer,
            textvariable=self.prefetch_var,
        ).pack(anchor="w", fill="x", pady=(0, 3))
        self.progress = ttk.Progressbar(
            outer, mode="determinate", maximum=100
        )
        self.progress.pack(fill="x", pady=(0, 7))

        self.recu_status_var = tk.StringVar(value="Recu metadata: no model selected.")
        wrapped_label(
            outer,
            textvariable=self.recu_status_var,
        ).pack(anchor="w", fill="x", pady=(0, 3))

        self.recu_box = ttk.LabelFrame(
            outer, text="Recu matches", padding=7
        )
        self.recu_box.pack(fill="x", pady=(0, 7))
        self.recu_icons_var = tk.StringVar(value="No Recu data loaded.")
        self.recu_icons_label = ttk.Label(
            self.recu_box,
            textvariable=self.recu_icons_var,
            font=("Segoe UI Emoji", 18, "bold"),
            justify="left",
            anchor="w",
            wraplength=1000,
        )
        self.recu_icons_label.pack(fill="x", anchor="w")
        self.recu_box.bind("<Configure>", self._resize_recu_badges)

        self.recu_comment_hint_var = tk.StringVar(value="Comments will appear here when they match this chunk.")
        ttk.Label(
            self.recu_box,
            textvariable=self.recu_comment_hint_var,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(3, 1))

        recu_text_frame = ttk.Frame(self.recu_box)
        recu_text_frame.pack(fill="x", expand=True, pady=(1, 3))
        comment_columns = ("comment", "segment", "at", "likes")
        self.recu_comment_tree = ttk.Treeview(
            recu_text_frame,
            columns=comment_columns,
            show="headings",
            selectmode="browse",
            height=5,
        )
        for column, title, width, stretch in (
            ("comment", "Comment", 520, True),
            ("segment", "Segment", 75, False),
            ("at", "At", 80, False),
            ("likes", "Likes", 55, False),
        ):
            self.recu_comment_tree.heading(column, text=title)
            self.recu_comment_tree.column(column, width=width, stretch=stretch, anchor="w")
        recu_text_scroll = ttk.Scrollbar(
            recu_text_frame,
            orient="vertical",
            command=self.recu_comment_tree.yview,
        )
        recu_text_hscroll = ttk.Scrollbar(
            recu_text_frame,
            orient="horizontal",
            command=self.recu_comment_tree.xview,
        )
        self.recu_comment_tree.configure(
            yscrollcommand=recu_text_scroll.set,
            xscrollcommand=recu_text_hscroll.set,
        )
        self.recu_comment_tree.grid(row=0, column=0, sticky="nsew")
        recu_text_scroll.grid(row=0, column=1, sticky="ns")
        recu_text_hscroll.grid(row=1, column=0, sticky="ew")
        recu_text_frame.columnconfigure(0, weight=1)
        self.recu_comment_tree.bind("<Double-1>", self._open_recu_comment_from_tree)
        self.recu_comment_tree.bind("<Return>", self._open_recu_comment_from_tree)

        recu_controls = ttk.Frame(self.recu_box)
        recu_controls.pack(fill="x")
        self.recu_refresh_button = ttk.Button(
            recu_controls,
            text="Refresh Recu now",
            command=lambda: self._start_recu_scrape(force=True),
        )
        self.recu_open_button = ttk.Button(
            recu_controls,
            text="Open Recu model",
            command=self._open_recu_model_page,
        )
        self.recurbate_open_button = ttk.Button(
            recu_controls,
            text="Open Recurbate model",
            command=self._open_recurbate_model_page,
        )
        recu_settings_button = ttk.Button(
            recu_controls,
            text="Recu settings",
            command=self._open_recu_settings,
        )
        register_flow_bar(
            recu_controls,
            [
                self.recu_refresh_button, self.recu_open_button,
                self.recurbate_open_button, recu_settings_button,
            ],
        )

        self.chunk_var = tk.StringVar(value="No chunk loaded.")
        chunk_box = ttk.LabelFrame(
            outer, text="Current chunk", padding=7
        )
        chunk_box.pack(fill="x", pady=(0, 7))
        wrapped_label(
            chunk_box,
            textvariable=self.chunk_var,
        ).pack(anchor="w", fill="x")

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        columns = (
            "decision",
            "number",
            "markers",
            "file",
            "size",
            "duration",
            "start",
            "root",
        )
        self.file_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=8,
        )
        headings = {
            "decision": "Decision",
            "number": "#",
            "markers": "Kinks",
            "file": "Recording file",
            "size": "Size",
            "duration": "Duration",
            "start": "Start",
            "root": "Folder",
        }
        widths = {
            "decision": 80,
            "number": 42,
            "markers": 90,
            "file": 410,
            "size": 85,
            "duration": 105,
            "start": 145,
            "root": 300,
        }
        for column in columns:
            self.file_tree.heading(column, text=headings[column])
            self.file_tree.column(
                column,
                width=widths[column],
                stretch=(column in {"file", "root"}),
                anchor="w",
            )
        ybar = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self.file_tree.yview,
        )
        xbar = ttk.Scrollbar(
            tree_frame,
            orient="horizontal",
            command=self.file_tree.xview,
        )
        self.file_tree.configure(
            yscrollcommand=ybar.set,
            xscrollcommand=xbar.set,
        )
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.file_tree.bind("<Double-1>", self._on_file_tree_double_click)
        self.file_tree.bind("<space>", self._toggle_selected)
        self.file_tree.bind("<Return>", self._open_selected_videos)

        decision_controls = ttk.Frame(outer)
        decision_controls.pack(fill="x", pady=(7, 0))
        self.generate_button = ttk.Button(
            decision_controls,
            text="Open / generate mosaic",
            command=self._open_or_generate,
        )
        self.regenerate_button = ttk.Button(
            decision_controls,
            text="Regenerate mosaic",
            command=self._regenerate_current_mosaic,
        )
        open_selected_button = ttk.Button(
            decision_controls,
            text="Open selected video",
            command=self._open_selected_videos,
        )
        self.audio_button = ttk.Button(
            decision_controls,
            text="Audio tools",
            command=self._open_audio_tools,
        )
        selected_keep_button = ttk.Button(
            decision_controls,
            text="Selected KEEP",
            command=lambda: self._set_selected_keep(True),
        )
        selected_delete_button = ttk.Button(
            decision_controls,
            text="Selected DELETE",
            command=lambda: self._set_selected_keep(False),
        )
        all_keep_button = ttk.Button(
            decision_controls,
            text="All KEEP",
            command=self._all_keep,
        )
        reset_delete_button = ttk.Button(
            decision_controls,
            text="Reset DELETE",
            command=self._all_delete,
        )
        register_flow_bar(
            decision_controls,
            [
                self.generate_button,
                self.regenerate_button,
                open_selected_button,
                self.audio_button,
                selected_keep_button,
                selected_delete_button,
                all_keep_button,
                reset_delete_button,
            ],
        )

        workflow_controls = ttk.Frame(outer)
        workflow_controls.pack(fill="x", pady=(6, 0))
        self.back_button = ttk.Button(
            workflow_controls,
            text="← Back",
            command=self._back_to_previous,
            state="disabled",
        )
        self.process_button = ttk.Button(
            workflow_controls,
            text="Process & next",
            command=self._process_current,
        )
        self.skip_button = ttk.Button(
            workflow_controls,
            text="Skip chunk & next",
            command=self._skip_current,
        )
        self.rescan_button = ttk.Button(
            workflow_controls,
            text="Rescan this model",
            command=self._rescan_current_model,
        )
        self.keeplast_button = ttk.Button(
            workflow_controls,
            text="Run designated KeepLast",
            command=self._run_designated_keeplast,
        )
        self.cancel_button = ttk.Button(
            workflow_controls,
            text="Cancel current work",
            command=self._cancel_work,
            state="disabled",
        )
        open_folders_button = ttk.Button(
            workflow_controls,
            text="Open model folders",
            command=self._open_model_folders,
        )
        register_flow_bar(
            workflow_controls,
            [
                self.back_button,
                self.process_button,
                self.skip_button,
                self.rescan_button,
                self.keeplast_button,
                self.cancel_button,
                open_folders_button,
            ],
        )

        self.log_var = tk.StringVar(value="Ready.")
        wrapped_label(
            outer,
            textvariable=self.log_var,
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        self.root.after_idle(schedule_scrollable_fit)

    def _update_back_button(self) -> None:
        try:
            state = "normal" if self.review_history and not self.busy else "disabled"
            self.back_button.configure(state=state)
        except Exception:
            pass

    def _clear_review_history(self) -> None:
        self.review_history.clear()
        self.pending_back_entry = None
        self.pending_restore_keep_indices = None
        self._update_back_button()

    def _resize_recu_badges(self, event: Any = None) -> None:
        """Keep the prominent kink badges readable without dominating narrow windows."""
        try:
            width = int(getattr(event, "width", 0) or self.recu_box.winfo_width())
            if width >= 1000:
                size = 18
            elif width >= 760:
                size = 16
            elif width >= 560:
                size = 14
            elif width >= 420:
                size = 12
            elif width >= 320:
                size = 10
            else:
                size = 9
            self.recu_icons_label.configure(
                font=("Segoe UI Emoji", size, "bold"),
                wraplength=max(160, width - 22),
            )
        except Exception:
            pass

    def _set_recu_details(self, text: str) -> None:
        """Clear the compact comment table and show a short status hint."""
        try:
            self.recu_comment_moments.clear()
            for iid in self.recu_comment_tree.get_children():
                self.recu_comment_tree.delete(iid)
            self.recu_comment_hint_var.set(str(text))
        except Exception:
            pass

    def _clear_segment_kink_markers(self) -> None:
        self.segment_kink_moments.clear()
        try:
            for iid in self.file_tree.get_children():
                values = list(self.file_tree.item(iid, "values"))
                if len(values) >= 3:
                    values[2] = ""
                    self.file_tree.item(iid, values=values)
        except Exception:
            pass

    def _open_recu_moment(self, moment: RecuMoment, parent: Optional[tk.Misc] = None) -> None:
        chunk = self.current_chunk
        if chunk is None:
            return
        location = chunk_event_location(chunk, moment.event_local)
        if location is None:
            messagebox.showinfo(
                "Timestamp outside segment",
                "The matched timestamp falls between local segment files or outside the current chunk.",
                parent=parent or self.root,
            )
            return
        segment_number, video, offset = location
        vlc = self._choose_vlc(parent or self.root)
        if vlc is None:
            return
        ok, message = open_in_vlc_at(vlc, video.path, offset)
        self.log_var.set(
            f"{message} (Recu segment #{segment_number}: "
            f"{moment.kink_name or moment.comment or 'timestamp'})"
        )
        if not ok:
            messagebox.showerror("Open Recu timestamp", message, parent=parent or self.root)

    def _open_recu_comment_from_tree(self, event: Any = None) -> str:
        try:
            iid = self.recu_comment_tree.identify_row(event.y) if event is not None and hasattr(event, "y") else ""
            if iid:
                self.recu_comment_tree.selection_set(iid)
            selected = self.recu_comment_tree.selection()
            if not selected:
                return "break"
            moment = self.recu_comment_moments.get(str(selected[0]))
            if moment is not None:
                self._open_recu_moment(moment)
        except Exception as exc:
            self.log_var.set(f"Could not open Recu comment timestamp: {exc}")
        return "break"

    def _open_segment_kink_marker(self, segment_index: int) -> None:
        moments = list(self.segment_kink_moments.get(segment_index, []))
        if not moments:
            return
        if len(moments) == 1:
            self._open_recu_moment(moments[0])
            return

        chooser = tk.Toplevel(self.root)
        chooser.title(f"Kink timestamps — segment #{segment_index + 1}")
        chooser.transient(self.root)
        chooser.geometry("620x280")
        body = ttk.Frame(chooser, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="This segment has multiple Recu kink timestamps. Double-click one to open it in VLC.",
            wraplength=580,
        ).pack(fill="x", anchor="w", pady=(0, 6))
        tree = ttk.Treeview(
            body,
            columns=("kink", "at"),
            show="headings",
            selectmode="browse",
            height=7,
        )
        tree.heading("kink", text="Kink")
        tree.heading("at", text="At in segment")
        tree.column("kink", width=430, stretch=True)
        tree.column("at", width=120, stretch=False)
        tree.pack(fill="both", expand=True)
        lookup: Dict[str, RecuMoment] = {}
        chunk = self.current_chunk
        for number, moment in enumerate(moments):
            location = chunk_event_location(chunk, moment.event_local) if chunk else None
            offset = location[2] if location else 0.0
            iid = f"moment_{number}"
            lookup[iid] = moment
            tree.insert(
                "", "end", iid=iid,
                values=(f"{moment.emoji} {moment.kink_name}", format_clock(offset)),
            )

        def open_selected(_event: Any = None) -> str:
            selected = tree.selection()
            if selected and selected[0] in lookup:
                self._open_recu_moment(lookup[selected[0]], chooser)
            return "break"

        tree.bind("<Double-1>", open_selected)
        tree.bind("<Return>", open_selected)
        ttk.Button(body, text="Open selected", command=open_selected).pack(anchor="e", pady=(6, 0))

    def _on_file_tree_double_click(self, event: Any) -> str:
        iid = self.file_tree.identify_row(event.y)
        if iid:
            self.file_tree.selection_set(iid)
        # Column #3 is the compact Kinks marker column.
        if self.file_tree.identify_column(event.x) == "#3" and iid:
            try:
                self._open_segment_kink_marker(int(iid))
            except Exception as exc:
                self.log_var.set(f"Could not open kink timestamp: {exc}")
            return "break"
        return self._toggle_selected(event)

    def _cancel_recu_chunk_resolution(self) -> None:
        self.recu_resolution_generation += 1
        self.recu_resolution_cancel_event.set()
        self.recu_resolution_cancel_event = threading.Event()
        self.recu_resolution_future = None
        self.recu_resolving_video_ids.clear()

    def _schedule_recu_chunk_resolution(self) -> None:
        """Resolve only Recu videos that can affect the prepared chunk window."""
        if (
            self.current_chunk is None
            or self.recu_model_data is None
            or not bool(self.settings.get("recu_enabled", True))
        ):
            return
        # A full model-page scrape owns the single Recu worker. It will call
        # this method again when the scrape completes.
        if self.recu_future is not None:
            return

        try:
            prepare_count = max(1, int(self.settings.get("chunks_to_prepare", 3)))
        except Exception:
            prepare_count = 3
        candidate_chunks = list(self.chunks[:prepare_count]) or [self.current_chunk]
        tolerance = int(self.settings.get("recu_match_tolerance_seconds", 120))
        maximum_hours = float(self.settings.get("recu_lazy_max_duration_hours", 24))
        # Preserve the existing bounded preparation behavior for all Recu
        # moments. Kinks-first additionally resolves only kink-tagged Recu
        # recordings across the remaining queue, because accurate global kink
        # ordering requires their recording durations. It still does not scan
        # every Recu video or globally resolve comments.
        video_ids = candidate_recu_video_ids_for_chunks(
            candidate_chunks,
            self.recu_model_data,
            tolerance,
            maximum_hours,
        )
        if self.order_var.get() == KINK_FIRST_ORDER:
            video_ids.update(
                candidate_recu_video_ids_for_chunks(
                    list(self.chunks),
                    self.recu_model_data,
                    tolerance,
                    maximum_hours,
                    allowed_kinds={"kink"},
                )
            )
        video_ids -= self.recu_failed_video_ids
        if not video_ids:
            return
        if self.recu_resolution_future is not None:
            if video_ids == self.recu_resolving_video_ids:
                return
            self._cancel_recu_chunk_resolution()

        self.recu_resolution_generation += 1
        generation = self.recu_resolution_generation
        cancel = threading.Event()
        self.recu_resolution_cancel_event = cancel
        self.recu_resolving_video_ids = set(video_ids)
        model_name = self.recu_model_name
        data_snapshot = recu_data_from_payload(
            recu_data_to_payload(self.recu_model_data),
            source=self.recu_model_data.source,
        )
        if data_snapshot is None:
            return
        settings = dict(self.settings)
        self.recu_status_var.set(
            f"Recu: checking {len(video_ids):,} recording(s) relevant to the prepared chunk window…"
        )

        def worker() -> Tuple[RecuModelData, Set[str]]:
            errors: List[str] = []
            resolve_recu_moment_timings(
                data_snapshot.moments,
                data_snapshot.video_meta,
                settings,
                cancel,
                errors,
                progress=lambda text: self.messages.put(
                    ("recu_resolve_progress", (generation, model_name, text))
                ),
                only_video_ids=set(video_ids),
            )
            if errors:
                data_snapshot.errors.extend(errors)
            payload = recu_data_to_payload(data_snapshot)
            payload["local_timezone"] = str(
                settings.get("recu_local_timezone", "America/New_York")
            )
            self.recu_cache.set_raw(model_name, payload)
            return data_snapshot, set(video_ids)

        future = self.recu_executor.submit(worker)
        self.recu_resolution_future = future
        future.add_done_callback(
            lambda finished: self.messages.put(
                ("recu_resolve_done", (generation, model_name, finished))
            )
        )

    def _handle_recu_resolve_progress(self, payload: Any) -> None:
        generation, model_name, text = payload
        if (
            int(generation) != self.recu_resolution_generation
            or str(model_name) != self.recu_model_name
        ):
            return
        self.recu_status_var.set(str(text))

    def _handle_recu_resolve_done(self, payload: Any) -> None:
        generation, model_name, future = payload
        if (
            int(generation) != self.recu_resolution_generation
            or str(model_name) != self.recu_model_name
        ):
            return
        self.recu_resolution_future = None
        self.recu_resolving_video_ids.clear()
        ok, result = self._future_result(future)
        if not ok:
            exc, trace = result
            if "cancel" not in str(exc).casefold():
                append_log(f"Lazy Recu timing resolution failed for {model_name}: {exc}\n{trace}")
                self.recu_status_var.set(
                    f"Recu timestamp check failed: {exc}. Chunk review remains available."
                )
            return
        data, resolved_ids = result
        self.recu_model_data = data
        resolved_now = {
            item.video_id for item in data.moments
            if item.video_id in resolved_ids and item.timing_resolved
        }
        self.recu_failed_video_ids.update(set(resolved_ids) - resolved_now)
        self.recu_status_var.set(
            f"Recu: checked {len(resolved_ids):,} recording(s) relevant to the prepared chunk window."
        )
        for error in data.errors:
            if any(video_id in error for video_id in resolved_ids):
                append_log(f"Recu lazy timing warning for {model_name}: {error}")
        self._resort_remaining_for_recu()
        self._update_recu_panel()

    def _reset_recu_state(self, message: str = "Recu metadata: waiting for model selection.") -> None:
        self.recu_generation += 1
        self.recu_cancel_event.set()
        self.recu_cancel_event = threading.Event()
        self.recu_future = None
        self._cancel_recu_chunk_resolution()
        self.recu_failed_video_ids.clear()
        self.recu_model_data = None
        self.recu_model_name = ""
        self._clear_segment_kink_markers()
        if hasattr(self, "recu_status_var"):
            self.recu_status_var.set(message)
            self.recu_icons_var.set("No Recu data loaded.")
            self._set_recu_details("Select a model to load matching comments.")

    def _start_recu_scrape(self, force: bool = False) -> None:
        if self.all_model_folders:
            model_name = self.all_model_folders[0].name
        elif self.model_folders:
            model_name = self.model_folders[0].name
        else:
            raw = self.last_model_text or self.model_var.get().strip()
            model_name = Path(raw).name if raw else ""
        model_name = canonical_model_name(model_name.strip())
        if not model_name:
            self.recu_status_var.set("Recu metadata: select a model first.")
            return

        self._cancel_recu_chunk_resolution()
        self.recu_failed_video_ids.clear()
        self.recu_generation += 1
        generation = self.recu_generation
        self.recu_cancel_event.set()
        cancel = threading.Event()
        self.recu_cancel_event = cancel
        self.recu_model_name = model_name
        # Mark the full page scrape as active before cached data is rendered so
        # the lazy duration resolver does not race it onto the same worker.
        self.recu_future = "starting"

        cached = recu_data_from_payload(self.recu_cache.get_raw(model_name), source="cache")
        if cached is not None:
            self.recu_model_data = cached
            self.recu_status_var.set(
                f"Recu metadata: showing cached {len(cached.moments):,} moment(s) while refreshing…"
            )
            self._resort_remaining_for_recu()
            self._update_recu_panel()
        else:
            self.recu_model_data = None
            self.recu_status_var.set(f"Recu metadata: scraping {model_name}…")
            self.recu_icons_var.set("🔄 Loading Recu kink/comment metadata…")
            self._set_recu_details("Comments will appear when matching Recu pages are ready.")

        if not bool(self.settings.get("recu_enabled", True)):
            self.recu_future = None
            self.recu_status_var.set("Recu metadata integration is disabled in Recu settings.")
            self._update_recu_panel()
            return

        try:
            self.recu_refresh_button.configure(state="disabled")
        except Exception:
            pass
        settings = dict(self.settings)

        def worker() -> RecuModelData:
            return scrape_recu_model(
                model_name,
                settings,
                self.recu_cache,
                cancel,
                force=force,
                progress=lambda text: self.messages.put(
                    ("recu_progress", (generation, model_name, text))
                ),
            )

        future = self.recu_executor.submit(worker)
        self.recu_future = future
        future.add_done_callback(
            lambda finished: self.messages.put(
                ("recu_done", (generation, model_name, finished))
            )
        )

    def _handle_recu_progress(self, payload: Any) -> None:
        generation, model_name, text = payload
        if int(generation) != self.recu_generation or str(model_name) != self.recu_model_name:
            return
        self.recu_status_var.set(str(text))

    def _handle_recu_done(self, payload: Any) -> None:
        generation, model_name, future = payload
        if int(generation) != self.recu_generation or str(model_name) != self.recu_model_name:
            return
        self.recu_future = None
        try:
            self.recu_refresh_button.configure(state="normal")
        except Exception:
            pass
        ok, result = self._future_result(future)
        if not ok:
            exc, trace = result
            append_log(f"Recu scrape crashed for {model_name}: {exc}\n{trace}")
            cached = recu_data_from_payload(self.recu_cache.get_raw(str(model_name)), source="stale cache")
            if cached is not None:
                self.recu_model_data = cached
                self.recu_status_var.set(
                    f"Recu live scrape failed; using {len(cached.moments):,} cached moment(s). See log."
                )
                self._resort_remaining_for_recu()
            else:
                self.recu_model_data = None
                self.recu_status_var.set(f"Recu scrape failed: {exc}. Chunk review remains available.")
            self._update_recu_panel()
            return

        data: RecuModelData = result
        self.recu_model_data = data
        error_suffix = f"; {len(data.errors):,} scrape/timing warning(s) logged" if data.errors else ""
        self.recu_status_var.set(
            f"Recu metadata: {len(data.moments):,} moment(s) for {data.model_name} from {data.source}{error_suffix}."
        )
        for error in data.errors:
            append_log(f"Recu warning for {data.model_name}: {error}")
        self._resort_remaining_for_recu()
        self._update_recu_panel()

    def _update_recu_panel(self) -> None:
        chunk = self.current_chunk
        self._clear_segment_kink_markers()
        self._set_recu_details("")
        if chunk is None:
            self.recu_icons_var.set("No current chunk.")
            self.recu_comment_hint_var.set("Load a chunk to see matching comments.")
            return
        if self.recu_model_data is None:
            self.recu_icons_var.set("🔄 Recu metadata not ready yet.")
            self.recu_comment_hint_var.set("Comments will appear when the Recu page scan is ready.")
            return

        tolerance_seconds = int(self.settings.get("recu_match_tolerance_seconds", 120))
        maximum_hours = float(self.settings.get("recu_lazy_max_duration_hours", 24))
        matches = match_recu_moments(chunk, self.recu_model_data, tolerance_seconds)
        unresolved_for_current = candidate_recu_video_ids_for_chunks(
            [chunk],
            self.recu_model_data,
            tolerance_seconds,
            maximum_hours,
        ) - self.recu_failed_video_ids

        kink_items = [item for item in matches if item.kind == "kink"]
        comment_items = [item for item in matches if item.kind == "comment"]

        # Put compact markers directly beside the matching local segment rows.
        for item in kink_items:
            location = chunk_event_location(chunk, item.event_local)
            if location is None:
                continue
            segment_number, _video, _offset = location
            self.segment_kink_moments.setdefault(segment_number - 1, []).append(item)
        for segment_index, moments in self.segment_kink_moments.items():
            iid = str(segment_index)
            if not self.file_tree.exists(iid):
                continue
            marker_parts: List[str] = []
            seen: Set[Tuple[str, str]] = set()
            for moment in sorted(
                moments,
                key=lambda value: (value.event_local, value.kink_name.casefold()),
            ):
                key = (moment.emoji, moment.kink_name.casefold())
                if key in seen:
                    continue
                seen.add(key)
                marker_parts.append(moment.emoji)
            values = list(self.file_tree.item(iid, "values"))
            if len(values) >= 3:
                values[2] = " ".join(marker_parts)
                self.file_tree.item(iid, values=values)

        # Prominent badges remain descriptive, while their font shrinks with
        # the window. Comments are intentionally kept out of this badge row.
        kink_groups: Dict[str, List[RecuMoment]] = {}
        for item in kink_items:
            kink_groups.setdefault(item.kink_slug or item.kink_name.casefold(), []).append(item)
        badges: List[str] = []
        for _slug, items in sorted(
            kink_groups.items(),
            key=lambda pair: pair[1][0].kink_name.casefold(),
        ):
            first = items[0]
            segments = sorted({
                location[0]
                for location in (chunk_event_location(chunk, item.event_local) for item in items)
                if location is not None
            })
            segment_text = (
                "seg " + ", ".join(f"#{segment}" for segment in segments)
                if segments else "near boundary"
            )
            badges.append(f"{first.emoji} {first.kink_name} — {segment_text}")

        if badges:
            self.recu_icons_var.set("     ".join(badges))
        elif unresolved_for_current:
            self.recu_icons_var.set("⏳ Checking Recu timestamps for this chunk…")
        else:
            self.recu_icons_var.set("No matching Recu kink timestamps for this chunk.")

        # Compact comment table: no site/local recording dates or verbose
        # timing formulas. "At" is the exact offset inside the local segment.
        self.recu_comment_moments.clear()
        for iid in self.recu_comment_tree.get_children():
            self.recu_comment_tree.delete(iid)
        for number, item in enumerate(comment_items):
            location = chunk_event_location(chunk, item.event_local)
            segment_text = "—"
            offset_text = "—"
            if location is not None:
                segment_number, _video, offset = location
                segment_text = f"#{segment_number}"
                offset_text = format_clock(offset)
            iid = f"comment_{number}"
            self.recu_comment_moments[iid] = item
            self.recu_comment_tree.insert(
                "", "end", iid=iid,
                values=(
                    item.comment or "Commented timestamp",
                    segment_text,
                    offset_text,
                    item.likes if item.likes else "",
                ),
            )
        if comment_items:
            self.recu_comment_hint_var.set(
                f"{len(comment_items):,} matching comment{'s' if len(comment_items) != 1 else ''}. "
                "Double-click a row to open that exact point in VLC."
            )
        elif unresolved_for_current:
            self.recu_comment_hint_var.set("Checking relevant Recu recording duration(s)…")
        else:
            self.recu_comment_hint_var.set("No matching comments for this chunk.")

        if unresolved_for_current:
            self._schedule_recu_chunk_resolution()

    def _selected_recu_model_name(self) -> str:
        if self.recu_model_name.strip():
            return canonical_model_name(self.recu_model_name.strip())
        if self.all_model_folders:
            return canonical_model_name(self.all_model_folders[0].name.strip())
        if self.model_folders:
            return canonical_model_name(self.model_folders[0].name.strip())
        raw = (self.last_model_text or self.model_var.get()).strip()
        return canonical_model_name(Path(raw).name.strip()) if raw else ""

    def _open_external_model_page(self, base_url: str, label: str) -> None:
        model_name = self._selected_recu_model_name()
        if not model_name:
            messagebox.showinfo(
                f"Open {label} model",
                "Select a model first.",
                parent=self.root,
            )
            return
        base = str(base_url or "https://recu.me").strip().rstrip("/")
        url = canonicalize_recu_url(
            f"{base}/performer/{urllib.parse.quote(model_name, safe='')}",
            base,
        )
        try:
            opened = bool(webbrowser.open_new_tab(url))
            self.log_var.set(f"Opened {label} model page for {model_name}: {url}")
            if not opened:
                messagebox.showinfo(
                    f"Open {label} model",
                    f"The browser did not confirm that it opened. You can open this address manually:\n\n{url}",
                    parent=self.root,
                )
        except Exception as exc:
            messagebox.showerror(
                f"Open {label} model",
                f"Could not open the model page:\n\n{exc}",
                parent=self.root,
            )

    def _open_recu_model_page(self) -> None:
        self._open_external_model_page(
            str(self.settings.get("recu_base_url", "https://recu.me") or "https://recu.me"),
            "Recu",
        )

    def _open_recurbate_model_page(self) -> None:
        self._open_external_model_page(RECURBATE_BASE_URL, "Recurbate")

    def _open_recu_settings(self) -> None:
        self._open_settings(initial_tab="Recu")

    def _sync_quick_settings(self) -> None:
        self.settings["auto_generate_current"] = bool(self.auto_generate_var.get())
        self._save_settings()

    def _browse_roots(self) -> None:
        initial = self._resolve_roots_path()
        path = filedialog.askopenfilename(
            title="Choose recording_roots.txt",
            initialdir=str(initial.parent if initial else APP_DIR),
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.roots_file_var.set(path)
            self._load_roots(show_errors=True)

    def _resolve_roots_path(self) -> Path:
        raw = self.roots_file_var.get().strip().strip('"') or "recording_roots.txt"
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = APP_DIR / path
        return path

    def _load_roots(self, show_errors: bool) -> None:
        path = self._resolve_roots_path()
        try:
            roots = parse_roots_file(path)
            self.roots = roots
            self.settings["roots_file"] = str(path)
            self.root_count_var.set(f"{len(roots):,} roots")
            self.status_var.set(
                f"Loaded {len(roots):,} roots from {path}. "
                "Refreshing the shallow model-name list in the background; no videos are scanned."
            )
            self._save_settings()
            self._load_cached_model_catalog()
            self._refresh_model_catalog()
        except Exception as exc:
            self.roots = []
            self.model_catalog = []
            self.model_catalog_ready = False
            self.root_count_var.set("0 roots")
            self.model_catalog_var.set(
                "Model-name list unavailable because roots could not be loaded."
            )
            self.status_var.set(f"Could not load roots: {exc}")
            if show_errors:
                messagebox.showerror("Roots file", str(exc), parent=self.root)

    def _catalog_roots_key(self) -> List[str]:
        return [normalized_absolute(root) for root in self.roots]

    def _load_cached_model_catalog(self) -> None:
        cached = load_json(MODEL_NAME_CACHE_PATH, {})
        if not isinstance(cached, dict):
            return
        cached_roots = cached.get("roots")
        cached_names = cached.get("names")
        if cached_roots != self._catalog_roots_key() or not isinstance(cached_names, list):
            return
        names = sorted(
            {
                str(name).strip()
                for name in cached_names
                if isinstance(name, str) and str(name).strip()
            },
            key=lambda value: (value.casefold(), value),
        )
        if names:
            self.model_catalog = names
            self.model_catalog_ready = True
            self.model_catalog_var.set(
                f"{len(names):,} cached model name(s) available; refreshing shallow root listings…"
            )
            self._schedule_model_filter(False)

    def _refresh_model_catalog(self) -> None:
        self.model_catalog_generation += 1
        generation = self.model_catalog_generation
        roots = list(self.roots)
        if not roots:
            self.model_catalog = []
            self.model_catalog_ready = False
            self.model_combo.configure(values=[])
            self.model_catalog_var.set("Load a roots file to build the model-name list.")
            return

        if not self.model_catalog:
            self.model_catalog_ready = False
            self.model_catalog_var.set(
                f"Indexing immediate model-folder names across {len(roots):,} roots…"
            )
        else:
            self.model_catalog_var.set(
                f"{len(self.model_catalog):,} cached model name(s) available; refreshing…"
            )

        def worker() -> List[str]:
            return discover_model_names(
                roots,
                progress=lambda current, total, root_text: self.messages.put(
                    ("model_catalog_progress", (generation, current, total, root_text))
                ),
            )

        future = self.model_index_executor.submit(worker)
        future.add_done_callback(
            lambda finished: self.messages.put(
                ("model_catalog_done", (generation, finished))
            )
        )

    def _on_model_text_changed(self, *_args: Any) -> None:
        self._schedule_model_filter(False)

    def _on_model_key_release(self, event: Any) -> None:
        key = str(getattr(event, "keysym", ""))
        if key in {
            "Return", "Escape", "Tab", "Up", "Down", "Prior", "Next",
            "Home", "End", "Left", "Right", "Shift_L", "Shift_R",
            "Control_L", "Control_R", "Alt_L", "Alt_R",
        }:
            return
        # Update the combobox values only. Do not programmatically post the
        # native pop-down: on Windows, posting it steals focus from the entry.
        self._schedule_model_filter(False)

    def _schedule_model_filter(self, open_dropdown: bool = False) -> None:
        # ``open_dropdown`` remains accepted for compatibility with older
        # call sites, but it is intentionally ignored. Users may open the
        # already-filtered list with the arrow or Alt+Down without losing
        # their insertion cursor while typing.
        if self.model_filter_after_id is not None:
            try:
                self.root.after_cancel(self.model_filter_after_id)
            except Exception:
                pass
        self.model_filter_after_id = self.root.after(70, self._apply_model_filter)

    def _apply_model_filter(self) -> None:
        self.model_filter_after_id = None

        # Preserve both the insertion cursor and focus while updating Tcl's
        # value list. ttk.Combobox.configure(values=...) normally preserves
        # them, but explicitly restoring them prevents theme-specific quirks.
        had_focus = self.root.focus_get() == self.model_combo
        try:
            cursor_index = int(self.model_combo.index(tk.INSERT))
        except Exception:
            cursor_index = len(self.model_var.get())

        query_raw = self.model_var.get().strip()
        query = query_raw.casefold()

        if (
            any(separator in query_raw for separator in (os.sep, "/", "\\"))
            or (len(query_raw) >= 2 and query_raw[1:2] == ":")
        ):
            self.model_combo.configure(values=[])
            self.model_catalog_var.set(
                "Full folder path entered; press Enter to load it."
            )
            return

        if not self.model_catalog_ready and not self.model_catalog:
            self.model_combo.configure(values=[])
            self.model_catalog_var.set(
                "Model names are still being indexed in the background…"
            )
            return

        if query:
            starts = [
                name for name in self.model_catalog
                if name.casefold().startswith(query)
            ]
            contains = [
                name for name in self.model_catalog
                if query in name.casefold()
                and not name.casefold().startswith(query)
            ]
            matches = starts + contains
        else:
            matches = list(self.model_catalog)

        # Avoid sending many thousands of values into Tcl on each keystroke.
        # The catalog stays complete; more typing narrows any truncated display.
        display_limit = 1500 if query else 500
        displayed = matches[:display_limit]
        self.model_combo.configure(values=displayed)

        if had_focus:
            try:
                self.model_combo.focus_set()
                self.model_combo.icursor(min(cursor_index, len(self.model_var.get())))
            except tk.TclError:
                pass

        if query:
            suffix = (
                f" Showing the first {len(displayed):,}."
                if len(matches) > len(displayed)
                else ""
            )
            self.model_catalog_var.set(
                f"{len(matches):,} matching model name(s) across all roots.{suffix}"
            )
        else:
            self.model_catalog_var.set(
                f"{len(self.model_catalog):,} model name(s) indexed. "
                "Start typing to filter."
            )

    def _handle_model_catalog_progress(self, payload: Any) -> None:
        generation, current, total, root_text = payload
        if int(generation) != self.model_catalog_generation:
            return
        self.model_catalog_var.set(
            f"Indexing model names: root {int(current):,}/{int(total):,} — {root_text}"
        )

    def _handle_model_catalog_done(self, payload: Any) -> None:
        generation, future = payload
        if int(generation) != self.model_catalog_generation:
            return
        ok, result = self._future_result(future)
        if not ok:
            exc, trace = result
            append_log(f"Model-name indexing failed: {exc}\n{trace}")
            if self.model_catalog:
                self.model_catalog_ready = True
                self.model_catalog_var.set(
                    f"Using {len(self.model_catalog):,} cached model name(s); "
                    f"refresh failed: {exc}"
                )
            else:
                self.model_catalog_ready = False
                self.model_catalog_var.set(f"Model-name indexing failed: {exc}")
            return

        names = list(result)
        self.model_catalog = names
        self.model_catalog_ready = True
        try:
            atomic_write_json(
                MODEL_NAME_CACHE_PATH,
                {
                    "roots": self._catalog_roots_key(),
                    "names": names,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
        except Exception as exc:
            append_log(f"Could not save model-name cache: {exc}")
        self.model_catalog_var.set(
            f"{len(names):,} model name(s) indexed across {len(self.roots):,} roots. "
            "Start typing to filter."
        )
        self._schedule_model_filter(False)

    def _choose_model_folder(self) -> None:
        path = filedialog.askdirectory(title="Choose one model folder", parent=self.root)
        if path:
            self.model_var.set(path)
            self._find_model()

    def _change_order(self) -> None:
        self._cancel_prefetch(terminate=True)
        self._cancel_recu_chunk_resolution()
        self.settings["queue_order"] = self.order_var.get()
        self._save_settings()
        if self.chunks:
            self._sort_queue()
            self._show_current()
            self.root.after(25, self._schedule_recu_chunk_resolution)

    def _current_scope_folders(self) -> List[Path]:
        scope = self.drive_var.get()
        return [folder for folder in self.all_model_folders if scope == "All" or disk_label(folder) == scope]

    def _rebuild_queue_for_scope(self) -> None:
        if not self.all_model_folders:
            return
        self._clear_review_history()
        self.model_folders = self._current_scope_folders()
        self._scan_selected_folders()

    def _find_model(self) -> None:
        if self.busy:
            return
        self._cancel_prefetch(terminate=True)
        model_text = self.model_var.get().strip()
        if not model_text:
            messagebox.showinfo("Model", "Enter a model folder name or choose a folder.", parent=self.root)
            return
        if not self.roots and not Path(model_text).is_dir():
            messagebox.showerror("No roots", "Load a recording_roots.txt file first.", parent=self.root)
            return
        self._clear_review_history()
        self.mosaic_failed_signatures.clear()
        self.duration_prepared_signatures.clear()
        self._reset_recu_state(f"Recu metadata: preparing to load {Path(model_text).name or model_text}…")
        self.last_model_text = model_text
        self._start_job(
            "Finding model folders…",
            lambda: find_model_folders(
                self.roots,
                model_text,
                bool(self.settings.get("recursive_fallback", False)),
                progress=lambda text: self.messages.put(("status", text)),
            ),
            "folders_found",
        )

    def _rescan_current_model(self) -> None:
        if self.last_model_text:
            self.model_var.set(self.last_model_text)
            self._find_model()

    def _scan_selected_folders(self) -> None:
        if self.busy:
            return
        self._cancel_prefetch(terminate=True)
        folders = list(self.model_folders)
        if not folders:
            self._clear_queue("No model folders are selected for this drive scope.")
            return
        settings = dict(self.settings)
        settings["queue_order"] = self.order_var.get()
        self._start_job(
            f"Quickly indexing {len(folders)} matching model folder(s)…",
            lambda: build_model_queue(
                folders,
                settings,
                self.duration_cache,
                self.manifest,
                include_skipped=False,
                progress=lambda text: self.messages.put(("status", text)),
                session_generated_signatures=self.session_generated_signatures,
                cancel=self.cancel_event,
                probe_actual_durations=False,
            ),
            "queue_built",
        )

    def _sort_queue(self, preserve_current: bool = False) -> None:
        order = self.order_var.get()
        current: Optional[Chunk] = None
        sortable = list(self.chunks)
        if preserve_current and self.current_chunk is not None:
            current_signature = self.current_chunk.signature
            for index, chunk in enumerate(sortable):
                if chunk.signature == current_signature:
                    current = sortable.pop(index)
                    break

        if order == "Oldest first":
            sortable.sort(
                key=lambda chunk: (
                    chunk.start,
                    -int(chunk.source_bytes),
                    normalized_absolute(chunk.folder),
                )
            )
        elif order == "Newest first":
            sortable.sort(
                key=lambda chunk: (
                    -chunk.start.timestamp(),
                    -int(chunk.source_bytes),
                    normalized_absolute(chunk.folder),
                )
            )
        elif order == KINK_FIRST_ORDER:
            tolerance = int(self.settings.get("recu_match_tolerance_seconds", 120))
            sortable.sort(
                key=lambda chunk: (
                    *recu_kink_queue_priority(chunk, self.recu_model_data, tolerance),
                    -int(chunk.source_bytes),
                    chunk.start,
                    normalized_absolute(chunk.folder),
                )
            )
        else:
            # Mosaic availability never changes queue priority. The current
            # stat-derived sum of video bytes is the sole primary criterion.
            sortable.sort(
                key=lambda chunk: (
                    -int(chunk.source_bytes),
                    chunk.start,
                    normalized_absolute(chunk.folder),
                )
            )
        self.chunks = ([current] if current is not None else []) + sortable

    def _resort_remaining_for_recu(self) -> None:
        """Refresh kink priority without replacing the chunk already on screen."""
        if self.order_var.get() != KINK_FIRST_ORDER or not self.chunks:
            return
        before = [chunk.signature for chunk in self.chunks]
        self._sort_queue(preserve_current=True)
        after = [chunk.signature for chunk in self.chunks]
        if before != after:
            self.prefetch_var.set(
                "Preparation queue: remaining chunks reordered from resolved Recu kink timestamps."
            )

    def _clear_queue(self, message: str) -> None:
        self._clear_review_history()
        self._cancel_prefetch(terminate=True)
        self.chunks = []
        self.current_chunk = None
        self.keep_indices.clear()
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self.chunk_var.set("No chunk loaded.")
        self.status_var.set(message)
        self._update_recu_panel()

    def _show_current(self) -> None:
        self.keep_indices.clear()
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        if not self.chunks:
            self.current_chunk = None
            self.chunk_var.set("No unreviewed chunks remain for this model and drive scope.")
            self.status_var.set("Review complete. Choose another model or rescan.")
            self.progress["value"] = 0
            self._update_recu_panel()
            return

        chunk = self.chunks[0]
        self.current_chunk = chunk
        restored_keep = (
            set(self.pending_restore_keep_indices)
            if self.pending_restore_keep_indices is not None
            else set()
        )
        self.pending_restore_keep_indices = None
        self.keep_indices = {
            index for index in restored_keep if 0 <= index < len(chunk.files)
        }
        for index, video in enumerate(chunk.files):
            self.file_tree.insert(
                "", "end", iid=str(index),
                values=(
                    "KEEP" if index in self.keep_indices else "DELETE",
                    index + 1, "", video.path.name, human_size(video.size),
                    format_seconds(video.duration)
                    + (" ⏳" if "pending probe" in video.duration_source else (f" ⚠ {video.duration_source.replace(' fallback', '')}" if "fallback" in video.duration_source else "")),
                    video.start.strftime("%Y-%m-%d %H:%M:%S"),
                    str(chunk.folder),
                ),
            )
        if chunk.mosaics:
            if chunk.signature in self.session_generated_signatures:
                mosaic_text = "fresh mosaic ready"
            else:
                mosaic_text = "existing mosaic"
        elif bool(self.settings.get("use_existing_mosaics", True)):
            mosaic_text = "mosaic missing"
        else:
            mosaic_text = "fresh mosaic required"
        fallback_items = [
            f"#{index + 1} {video.duration_source}"
            for index, video in enumerate(chunk.files)
            if "fallback" in video.duration_source and "pending probe" not in video.duration_source
        ]
        pending_count = sum(1 for video in chunk.files if "pending probe" in video.duration_source)
        fallback_line = ("\n⚠ Duration fallback: " + ", ".join(fallback_items)) if fallback_items else ""
        self.chunk_var.set(
            f"Remaining: {len(self.chunks):,} | {disk_label(chunk.folder)} | "
            f"{human_size(chunk.source_bytes)} | {len(chunk.files):,} file(s) | {mosaic_text} | "
            f"{'actual durations ready' if chunk.durations_prepared else 'duration preview'}\n"
            f"{chunk.start:%Y-%m-%d %H:%M:%S} → {chunk.end:%Y-%m-%d %H:%M:%S}\n"
            f"{chunk.folder}{fallback_line}"
        )
        fallback_count = len(fallback_items)
        if pending_count:
            self.status_var.set(
                f"Preview ready immediately. ⏳ {pending_count} duration(s) are queued for exact probing; "
                "you can begin reviewing while the shared preparation window runs."
            )
        elif fallback_count:
            self.status_var.set(
                f"Every file defaults to DELETE. ⚠ {fallback_count} duration(s) used a safe fallback; "
                "the chunk remains reviewable and details are marked in the Duration column."
            )
        else:
            self.status_var.set("Every file defaults to DELETE. Actual durations are ready; mark only the files worth keeping.")
        self.progress["value"] = 0
        self._update_recu_panel()
        if chunk.mosaics and bool(self.settings.get("auto_open_mosaic", True)):
            self.root.after(100, lambda: open_paths(chunk.mosaics))
        # Always hand off to the unified preparation queue after the preview is
        # on screen. It prepares durations, Recu timing, and mosaics for one
        # shared number of chunks without blocking this first view.
        self.root.after(120, self._schedule_prefetch)

    def _refresh_current_chunk_display(self) -> None:
        """Refresh prepared durations/matches without losing KEEP decisions."""
        chunk = self.current_chunk
        if chunk is None:
            return
        for index, video in enumerate(chunk.files):
            iid = str(index)
            pending = "pending probe" in video.duration_source
            failed = "fallback" in video.duration_source and not pending
            suffix = " ⏳" if pending else (f" ⚠ {video.duration_source.replace(' fallback', '')}" if failed else "")
            values = (
                "KEEP" if index in self.keep_indices else "DELETE",
                index + 1,
                "",
                video.path.name,
                human_size(video.size),
                format_seconds(video.duration) + suffix,
                video.start.strftime("%Y-%m-%d %H:%M:%S"),
                str(chunk.folder),
            )
            if self.file_tree.exists(iid):
                self.file_tree.item(iid, values=values)
            else:
                self.file_tree.insert("", "end", iid=iid, values=values)
        mosaic_text = "mosaic ready" if chunk.mosaics else "mosaic preparing/missing"
        duration_text = "actual durations ready" if chunk.durations_prepared else "duration preview; exact probe queued"
        self.chunk_var.set(
            f"Remaining: {len(self.chunks):,} | {disk_label(chunk.folder)} | "
            f"{human_size(chunk.source_bytes)} | {len(chunk.files):,} file(s) | {mosaic_text} | {duration_text}\n"
            f"{chunk.start:%Y-%m-%d %H:%M:%S} → {chunk.end:%Y-%m-%d %H:%M:%S}\n"
            f"{chunk.folder}"
        )
        self._update_recu_panel()

    def _selected_indices(self) -> List[int]:
        output: List[int] = []
        for iid in self.file_tree.selection():
            try:
                output.append(int(iid))
            except Exception:
                continue
        return output

    def _redraw_decision(self, index: int) -> None:
        iid = str(index)
        if not self.file_tree.exists(iid):
            return
        values = list(self.file_tree.item(iid, "values"))
        values[0] = "KEEP" if index in self.keep_indices else "DELETE"
        self.file_tree.item(iid, values=values)

    def _set_selected_keep(self, keep: bool) -> None:
        for index in self._selected_indices():
            if keep:
                self.keep_indices.add(index)
            else:
                self.keep_indices.discard(index)
            self._redraw_decision(index)

    def _toggle_selected(self, event: Any = None) -> str:
        if event is not None and getattr(event, "y", None) is not None:
            iid = self.file_tree.identify_row(event.y)
            if iid:
                self.file_tree.selection_set(iid)
        for index in self._selected_indices():
            if index in self.keep_indices:
                self.keep_indices.remove(index)
            else:
                self.keep_indices.add(index)
            self._redraw_decision(index)
        return "break"

    def _all_keep(self) -> None:
        if not self.current_chunk:
            return
        self.keep_indices = set(range(len(self.current_chunk.files)))
        for index in self.keep_indices:
            self._redraw_decision(index)

    def _all_delete(self) -> None:
        previous = list(self.keep_indices)
        self.keep_indices.clear()
        for index in previous:
            self._redraw_decision(index)

    def _open_selected_videos(self, _event: Any = None) -> str:
        if not self.current_chunk:
            return "break"
        indices = self._selected_indices()
        paths = [self.current_chunk.files[index].path for index in indices]
        if not paths:
            return "break"
        ok, message = open_paths(paths)
        self.log_var.set(message)
        if not ok:
            messagebox.showerror("Open video", message, parent=self.root)
        return "break"

    def _speech_scope_indices(self, scope_text: str) -> List[int]:
        chunk = self.current_chunk
        if chunk is None:
            return []
        clean = str(scope_text or "").casefold()
        selected = self._selected_indices()
        if "selected" in clean and selected:
            return sorted(set(selected))
        return list(range(len(chunk.files)))

    def _speech_scope_key(self, chunk: Chunk, indices: Sequence[int]) -> str:
        settings_key = SpeechTranscriptCache._settings_key(self.settings)
        return f"{chunk.signature}|{','.join(str(index) for index in sorted(indices))}|{settings_key}"

    def _open_audio_tools(self) -> None:
        chunk = self.current_chunk
        if chunk is None:
            return
        existing = self.audio_tools_state.get("window")
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            pass

        window = tk.Toplevel(self.root)
        window.title(f"Audio tools — {chunk.folder.name}")
        window.geometry("1050x760")
        window.minsize(650, 480)
        window.transient(self.root)
        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        waveform_tab = ttk.Frame(notebook, padding=10)
        speech_tab = ttk.Frame(notebook, padding=10)
        notebook.add(waveform_tab, text="Waveform")
        notebook.add(speech_tab, text="Speech search & transcript")

        # Waveform launcher: the established viewer remains separate so its large
        # responsive canvas is not squeezed by the speech controls.
        ttk.Label(
            waveform_tab,
            text=(
                "Build the chunk waveform using the selected coverage method. The waveform viewer opens "
                "with its existing timestamp navigation and segment table."
            ),
            wraplength=900,
            justify="left",
        ).pack(fill="x", anchor="w", pady=(0, 8))
        waveform_controls = ttk.Frame(waveform_tab)
        waveform_controls.pack(fill="x", anchor="w")
        waveform_mode_var = tk.StringVar(value=normalize_audio_mode(self.settings.get("audio_waveform_mode", AUDIO_MODE_RAW_ADJUSTABLE)))
        ttk.Label(waveform_controls, text="Waveform method:").pack(side="left")
        ttk.Combobox(
            waveform_controls,
            textvariable=waveform_mode_var,
            values=list(AUDIO_MODES),
            state="readonly",
            width=38,
        ).pack(side="left", padx=5)
        ttk.Button(
            waveform_controls,
            text="Build / open waveform",
            command=lambda: self._view_audio_track(waveform_mode_var.get()),
        ).pack(side="left", padx=4)
        ttk.Button(
            waveform_controls,
            text="Waveform settings",
            command=lambda: self._open_settings("Audio & speech"),
        ).pack(side="left", padx=4)
        waveform_note = tk.StringVar(value=audio_mode_note(waveform_mode_var.get(), self.settings))
        ttk.Label(waveform_tab, textvariable=waveform_note, wraplength=900, justify="left").pack(fill="x", anchor="w", pady=(12, 0))
        waveform_mode_var.trace_add("write", lambda *_args: waveform_note.set(audio_mode_note(waveform_mode_var.get(), self.settings)))

        # Speech tools.
        controls = ttk.LabelFrame(speech_tab, text="On-demand local speech recognition", padding=8)
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)
        query_var = tk.StringVar()
        match_mode_var = tk.StringVar(value="Exact phrase")
        scope_var = tk.StringVar(value="Selected segment(s), otherwise current chunk")
        ttk.Label(controls, text="Word or phrase:").grid(row=0, column=0, sticky="w", pady=3)
        query_entry = ttk.Entry(controls, textvariable=query_var)
        query_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=5, pady=3)
        ttk.Label(controls, text="Match:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(
            controls,
            textvariable=match_mode_var,
            values=["Exact phrase", "Fuzzy phrase", "All words", "Any word"],
            state="readonly",
            width=20,
        ).grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(controls, text="Scope:").grid(row=1, column=2, sticky="e", pady=3)
        ttk.Combobox(
            controls,
            textvariable=scope_var,
            values=["Selected segment(s), otherwise current chunk", "Current chunk"],
            state="readonly",
            width=38,
        ).grid(row=1, column=3, sticky="w", padx=5, pady=3)

        button_row = ttk.Frame(controls)
        button_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(7, 2))
        search_button = ttk.Button(button_row, text="Search phrase")
        transcribe_button = ttk.Button(button_row, text="Transcribe scope")
        cancel_button = ttk.Button(button_row, text="Cancel", state="disabled", command=self._cancel_speech_job)
        install_button = ttk.Button(button_row, text="Install speech engine", command=lambda: self._install_speech_engine(window))
        settings_button = ttk.Button(button_row, text="Speech settings", command=lambda: self._open_settings("Audio & speech"))
        search_button.pack(side="left", padx=3)
        transcribe_button.pack(side="left", padx=3)
        cancel_button.pack(side="left", padx=3)
        settings_button.pack(side="left", padx=3)
        install_button.pack(side="right", padx=3)

        engine_available = SpeechEngineManager.is_available(self.settings)
        selected_backend = SpeechEngineManager.selected_backend(self.settings)
        if selected_backend == "vosk":
            selected_model = (
                self.settings.get("speech_vosk_model_path")
                or self.settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")
            )
            ready_text = f"Engine ready: Vosk / {selected_model}"
        else:
            selected_model = (
                self.settings.get("speech_model_path")
                or self.settings.get("speech_model", "tiny.en")
            )
            ready_text = f"Engine ready: faster-whisper / {selected_model}"
        engine_var = tk.StringVar(value=(
            ready_text
            if engine_available else
            f"{SpeechEngineManager.availability_text(self.settings)}. Click Install speech engine; waveform tools continue to work without it."
        ))
        ttk.Label(controls, textvariable=engine_var, wraplength=900, justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=(5, 2))
        status_var = tk.StringVar(value=(
            "Select one or more segments first for the fastest run. Vosk is the legacy-CPU-safe default; completed file transcripts are cached for instant later searches."
        ))
        ttk.Label(controls, textvariable=status_var, wraplength=900, justify="left").grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 4))
        speech_progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        speech_progress.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(2, 3))

        results_box = ttk.LabelFrame(speech_tab, text="Phrase matches — double-click to open in VLC", padding=5)
        results_box.pack(fill="both", expand=True, pady=(8, 4))
        results_tree = ttk.Treeview(
            results_box,
            columns=("segment", "position", "match", "context", "score"),
            show="headings",
            selectmode="browse",
            height=7,
        )
        for column, title, width in (
            ("segment", "Segment", 70),
            ("position", "Position", 95),
            ("match", "Matched words", 180),
            ("context", "Transcript context", 520),
            ("score", "Score", 70),
        ):
            results_tree.heading(column, text=title)
            results_tree.column(column, width=width, stretch=(column == "context"), anchor="w")
        result_scroll = ttk.Scrollbar(results_box, orient="vertical", command=results_tree.yview)
        results_tree.configure(yscrollcommand=result_scroll.set)
        results_tree.grid(row=0, column=0, sticky="nsew")
        result_scroll.grid(row=0, column=1, sticky="ns")
        results_box.rowconfigure(0, weight=1)
        results_box.columnconfigure(0, weight=1)

        transcript_box = ttk.LabelFrame(speech_tab, text="Transcript — double-click any row to open its timestamp", padding=5)
        transcript_box.pack(fill="both", expand=True, pady=(4, 0))
        transcript_tree = ttk.Treeview(
            transcript_box,
            columns=("segment", "start", "end", "text"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        for column, title, width in (
            ("segment", "Segment", 70),
            ("start", "Start", 80),
            ("end", "End", 80),
            ("text", "Transcript", 700),
        ):
            transcript_tree.heading(column, text=title)
            transcript_tree.column(column, width=width, stretch=(column == "text"), anchor="w")
        transcript_scroll = ttk.Scrollbar(transcript_box, orient="vertical", command=transcript_tree.yview)
        transcript_tree.configure(yscrollcommand=transcript_scroll.set)
        transcript_tree.grid(row=0, column=0, sticky="nsew")
        transcript_scroll.grid(row=0, column=1, sticky="ns")
        transcript_box.rowconfigure(0, weight=1)
        transcript_box.columnconfigure(0, weight=1)

        self.audio_tools_state = {
            "window": window,
            "query_var": query_var,
            "match_mode_var": match_mode_var,
            "scope_var": scope_var,
            "status_var": status_var,
            "engine_var": engine_var,
            "progress": speech_progress,
            "search_button": search_button,
            "transcribe_button": transcribe_button,
            "cancel_button": cancel_button,
            "results_tree": results_tree,
            "transcript_tree": transcript_tree,
            "result_map": {},
            "transcript_map": {},
        }
        search_button.configure(command=lambda: self._start_speech_job(search_only=True))
        transcribe_button.configure(command=lambda: self._start_speech_job(search_only=False))
        query_entry.bind("<Return>", lambda _event: self._start_speech_job(search_only=True))
        results_tree.bind("<Double-Button-1>", self._open_speech_result)
        transcript_tree.bind("<Double-Button-1>", self._open_transcript_row)

        def on_close() -> None:
            self._cancel_speech_job()
            self.audio_tools_state = {}
            window.destroy()
        window.protocol("WM_DELETE_WINDOW", on_close)
        query_entry.focus_set()

    def _install_speech_engine(self, parent: tk.Misc) -> None:
        backend = SpeechEngineManager.selected_backend(self.settings)
        package = "vosk" if backend == "vosk" else "faster-whisper"
        command = [sys.executable, "-m", "pip", "install", "--upgrade", package]
        try:
            kwargs: Dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(command, cwd=str(APP_DIR), **kwargs)
            extra = (
                "The first Vosk transcription may download the selected small language model."
                if package == "vosk"
                else "The first transcription may also download the selected Whisper model."
            )
            messagebox.showinfo(
                "Speech engine installation",
                f"An installer window was opened for {package}. Wait for it to report success, then restart this sorter. {extra}",
                parent=parent,
            )
        except Exception as exc:
            messagebox.showerror(
                "Could not start installer",
                f"Run this command manually:\n\n{sys.executable} -m pip install --upgrade {package}\n\n{exc}",
                parent=parent,
            )


    def _set_speech_controls_busy(self, busy: bool) -> None:
        state = self.audio_tools_state
        try:
            state["search_button"].configure(state="disabled" if busy else "normal")
            state["transcribe_button"].configure(state="disabled" if busy else "normal")
            state["cancel_button"].configure(state="normal" if busy else "disabled")
        except Exception:
            pass

    def _cancel_speech_job(self) -> None:
        self.speech_cancel_event.set()
        process = self.process_holder[0]
        if process is not None:
            _terminate_process_tree(process)
        state = self.audio_tools_state
        status = state.get("status_var")
        if status is not None:
            try:
                status.set("Cancellation requested. The current recognition block may finish first.")
            except Exception:
                pass

    def _start_speech_job(self, search_only: bool) -> None:
        chunk = self.current_chunk
        state = self.audio_tools_state
        if chunk is None or self.busy or not state:
            return
        query = str(state["query_var"].get()).strip()
        if search_only and not query:
            messagebox.showinfo("Speech search", "Enter a word or phrase to search for.", parent=state.get("window", self.root))
            return
        indices = self._speech_scope_indices(str(state["scope_var"].get()))
        if not indices:
            messagebox.showinfo("Speech scope", "Select at least one valid segment or use the current chunk scope.", parent=state.get("window", self.root))
            return
        scope_key = self._speech_scope_key(chunk, indices)
        cached = self.speech_transcript_cache.get(scope_key)
        if cached is not None:
            matches = search_speech_transcript(
                cached,
                query,
                str(state["match_mode_var"].get()),
                int(self.settings.get("speech_fuzzy_threshold_percent", 74) or 74),
            ) if query else []
            self._populate_speech_tools(cached, matches, "Used the in-memory transcript; no audio was reread.")
            return

        self._cancel_prefetch(terminate=True)
        self.speech_cancel_event.clear()
        self.cancel_event.clear()
        self._set_speech_controls_busy(True)
        self._set_busy(True, "Transcribing requested audio only…")
        selected_backend = SpeechEngineManager.selected_backend(self.settings)
        if selected_backend == "vosk":
            selected_model = (
                self.settings.get("speech_vosk_model_path")
                or self.settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15")
            )
        else:
            selected_model = (
                self.settings.get("speech_model_path")
                or self.settings.get("speech_model", "tiny.en")
            )
        state["status_var"].set(
            f"Preparing {len(indices)} segment(s) with {selected_backend} / {selected_model}…"
        )
        settings = dict(self.settings)
        match_mode = str(state["match_mode_var"].get())

        def worker() -> Tuple[str, str, str, Tuple[bool, str, Optional[SpeechTranscript]]]:
            result = transcribe_chunk_speech(
                chunk,
                indices,
                settings,
                self.transcript_cache,
                self.speech_engine_manager,
                self.speech_cancel_event,
                self.process_holder,
                progress=lambda current, total, text: self.messages.put(("speech_progress", (current, total, text))),
            )
            return scope_key, query, match_mode, result

        future = self.executor.submit(worker)
        future.add_done_callback(lambda finished: self.messages.put(("speech_done", finished)))

    def _populate_speech_tools(
        self,
        transcript: SpeechTranscript,
        matches: Sequence[SpeechMatch],
        message: str,
    ) -> None:
        state = self.audio_tools_state
        if not state:
            return
        results_tree = state["results_tree"]
        transcript_tree = state["transcript_tree"]
        for item in results_tree.get_children():
            results_tree.delete(item)
        for item in transcript_tree.get_children():
            transcript_tree.delete(item)
        result_map: Dict[str, SpeechMatch] = {}
        transcript_map: Dict[str, SpeechUtterance] = {}
        for number, match in enumerate(matches, start=1):
            iid = f"match-{number}"
            result_map[iid] = match
            results_tree.insert("", "end", iid=iid, values=(
                f"#{match.file_index}",
                format_clock(match.start_seconds),
                match.matched_text,
                match.context,
                f"{match.score:.0%}",
            ))
        for number, utterance in enumerate(transcript.utterances, start=1):
            iid = f"utterance-{number}"
            transcript_map[iid] = utterance
            transcript_tree.insert("", "end", iid=iid, values=(
                f"#{utterance.file_index}",
                format_clock(utterance.start_seconds),
                format_clock(utterance.end_seconds),
                utterance.text,
            ))
        state["result_map"] = result_map
        state["transcript_map"] = transcript_map
        status_text = message
        if matches:
            status_text += f" Found {len(matches):,} matching timestamp(s)."
        elif str(state["query_var"].get()).strip():
            status_text += " No matches were found; try Fuzzy phrase or a larger model if the wording may have been misheard."
        if transcript.warnings:
            status_text += f" {len(transcript.warnings):,} warning(s) were logged."
        state["status_var"].set(status_text)
        state["progress"]["value"] = 100

    def _open_speech_result(self, _event: Any = None) -> None:
        state = self.audio_tools_state
        tree = state.get("results_tree")
        if tree is None:
            return
        selected = tree.selection()
        if not selected:
            return
        match = state.get("result_map", {}).get(selected[0])
        if match is None:
            return
        vlc = self._choose_vlc(state.get("window", self.root))
        if vlc is None:
            return
        ok, message = open_in_vlc_at(vlc, match.path, max(0.0, match.start_seconds - 0.5))
        state["status_var"].set(message)
        if not ok:
            messagebox.showerror("Open transcript timestamp", message, parent=state.get("window", self.root))

    def _open_transcript_row(self, _event: Any = None) -> None:
        state = self.audio_tools_state
        tree = state.get("transcript_tree")
        if tree is None:
            return
        selected = tree.selection()
        if not selected:
            return
        utterance = state.get("transcript_map", {}).get(selected[0])
        if utterance is None:
            return
        vlc = self._choose_vlc(state.get("window", self.root))
        if vlc is None:
            return
        ok, message = open_in_vlc_at(vlc, utterance.path, max(0.0, utterance.start_seconds - 0.5))
        state["status_var"].set(message)
        if not ok:
            messagebox.showerror("Open transcript timestamp", message, parent=state.get("window", self.root))

    def _view_audio_track(self, mode_override: Optional[str] = None) -> None:
        chunk = self.current_chunk
        if chunk is None or self.busy:
            return

        mode = normalize_audio_mode(
            mode_override or self.settings.get("audio_waveform_mode", AUDIO_MODE_RAW_COMPLETE)
        )
        if mode_override is not None:
            self.settings["audio_waveform_mode"] = mode
            self._save_settings()

        cache_key = (
            f"{chunk.signature}|{mode}|cov={self.settings.get('audio_raw_coverage_percent', 25)}"
            f"|win={self.settings.get('audio_raw_max_windows_per_file', 72)}"
            f"|bins={self.settings.get('audio_waveform_target_bins', 2400)}"
            f"|smooth={self.settings.get('audio_smoothing_radius', 1)}"
        )
        cached = self.audio_waveform_cache.get(cache_key)
        if cached is not None:
            self._show_audio_waveform(cached)
            return

        # Avoid competing ffmpeg jobs and disk reads while the waveform is built.
        self._cancel_prefetch(terminate=True)
        self.mosaic_failed_signatures.discard(chunk.signature)
        settings = dict(self.settings)
        settings["audio_waveform_mode"] = mode
        self.cancel_event.clear()
        self._set_busy(True, f"Building {mode} audio activity for the current chunk…")

        def worker() -> Tuple[str, Tuple[bool, str, Optional[AudioWaveform]]]:
            result = generate_audio_waveform(
                chunk,
                settings,
                self.duration_cache,
                self.cancel_event,
                self.process_holder,
                progress=lambda current, total, text: self.messages.put(
                    ("progress", (current, total, text))
                ),
                mode=mode,
            )
            return cache_key, result

        future = self.executor.submit(worker)
        future.add_done_callback(
            lambda finished: self.messages.put(("audio_done", finished))
        )

    def _choose_vlc(self, parent: tk.Misc) -> Optional[Path]:
        configured = str(self.settings.get("vlc_path", ""))
        vlc = resolve_vlc(configured)
        if vlc is not None:
            return vlc
        chosen = filedialog.askopenfilename(
            title="Choose VLC executable",
            parent=parent,
            filetypes=[
                ("VLC", "vlc.exe" if os.name == "nt" else "vlc"),
                ("All files", "*.*"),
            ],
        )
        if not chosen:
            return None
        path = Path(chosen)
        if not path.is_file():
            messagebox.showerror("VLC", "The selected VLC executable does not exist.", parent=parent)
            return None
        self.settings["vlc_path"] = str(path)
        self._save_settings()
        return path

    def _show_audio_waveform(self, waveform: AudioWaveform) -> None:
        window = tk.Toplevel(self.root)
        window.title(f"Audio Track — {waveform.model_name} — {waveform.mode}")
        window.geometry("1200x690")
        window.minsize(560, 380)
        window.transient(self.root)

        outer = ttk.Frame(window, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=(
                f"Concatenated chunk audio: {format_clock(waveform.total_duration)} across "
                f"{len(waveform.segments)} file(s). Double-click anywhere on the waveform "
                "to open the corresponding file and timestamp in VLC."
            ),
            wraplength=1120,
            justify="left",
        ).pack(fill="x", anchor="w")

        mode_controls = ttk.Frame(outer)
        mode_controls.pack(fill="x", pady=(6, 2))
        ttk.Label(mode_controls, text="Display mode:").pack(side="left")
        mode_var = tk.StringVar(value=waveform.mode)
        mode_combo = ttk.Combobox(
            mode_controls,
            textvariable=mode_var,
            values=list(AUDIO_MODES),
            state="readonly",
            width=34,
        )
        mode_combo.pack(side="left", padx=5)

        def rebuild_mode() -> None:
            selected_mode = normalize_audio_mode(mode_var.get())
            self.settings["audio_waveform_mode"] = selected_mode
            self._save_settings()
            window.destroy()
            self.root.after(25, lambda: self._view_audio_track(selected_mode))

        ttk.Button(
            mode_controls,
            text="Build / show selected mode",
            command=rebuild_mode,
        ).pack(side="left", padx=3)
        ttk.Label(
            outer,
            text=waveform.filter_note,
            wraplength=1120,
            justify="left",
        ).pack(fill="x", anchor="w", pady=(0, 3))

        hover_var = tk.StringVar(value="Move across the waveform to inspect its exact file and timestamp.")
        ttk.Label(outer, textvariable=hover_var).pack(fill="x", anchor="w", pady=(4, 6))

        canvas_frame = ttk.Frame(outer)
        canvas_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, background="white", highlightthickness=1)
        canvas.pack(fill="both", expand=True)

        segment_frame = ttk.LabelFrame(outer, text="Chunk segments", padding=5)
        segment_frame.pack(fill="x", pady=(8, 0))
        segment_tree = ttk.Treeview(
            segment_frame,
            columns=("number", "timeline", "duration", "audio", "file"),
            show="headings",
            height=min(6, max(2, len(waveform.segments))),
            selectmode="browse",
        )
        for column, title, width in (
            ("number", "#", 42),
            ("timeline", "Chunk timeline", 150),
            ("duration", "Duration", 80),
            ("audio", "Audio", 70),
            ("file", "Recording file", 650),
        ):
            segment_tree.heading(column, text=title)
            segment_tree.column(column, width=width, stretch=(column == "file"), anchor="w")
        segment_scroll = ttk.Scrollbar(segment_frame, orient="vertical", command=segment_tree.yview)
        segment_tree.configure(yscrollcommand=segment_scroll.set)
        segment_tree.grid(row=0, column=0, sticky="nsew")
        segment_scroll.grid(row=0, column=1, sticky="ns")
        segment_frame.columnconfigure(0, weight=1)
        for segment in waveform.segments:
            segment_tree.insert(
                "", "end", iid=str(segment.file_index),
                values=(
                    segment.file_index,
                    f"{format_clock(segment.start_seconds)}–{format_clock(segment.end_seconds)}",
                    format_clock(segment.duration),
                    "Yes" if segment.has_audio else "None",
                    segment.path.name,
                ),
            )

        plot: Dict[str, float] = {}
        redraw_after: List[Optional[str]] = [None]

        def segment_for_time(total_seconds: float) -> Tuple[Optional[AudioSegment], float]:
            if not waveform.segments:
                return None, 0.0
            value = max(0.0, min(float(total_seconds), waveform.total_duration))
            for segment in waveform.segments:
                if value < segment.end_seconds or segment is waveform.segments[-1]:
                    return segment, max(0.0, min(segment.duration, value - segment.start_seconds))
            return waveform.segments[-1], waveform.segments[-1].duration

        def event_position(event: Any) -> Tuple[Optional[AudioSegment], float, float]:
            left = plot.get("left", 0.0)
            right = plot.get("right", 0.0)
            if right <= left or waveform.total_duration <= 0:
                return None, 0.0, 0.0
            x = max(left, min(right, float(canvas.canvasx(event.x))))
            total_seconds = (x - left) / (right - left) * waveform.total_duration
            segment, offset = segment_for_time(total_seconds)
            return segment, offset, total_seconds

        def draw() -> None:
            redraw_after[0] = None
            canvas.delete("all")
            width = max(300, canvas.winfo_width())
            height = max(220, canvas.winfo_height())
            left, right = 58.0, float(width - 18)
            top, bottom = 32.0, float(height - 92)
            if right <= left or bottom <= top:
                return
            plot.update({"left": left, "right": right, "top": top, "bottom": bottom})
            plot_width = right - left
            plot_height = bottom - top
            center = top + plot_height / 2.0

            for segment in waveform.segments:
                x0 = left + plot_width * segment.start_seconds / waveform.total_duration
                x1 = left + plot_width * segment.end_seconds / waveform.total_duration
                fill = "#f3f6fa" if segment.file_index % 2 else "#e8eef5"
                canvas.create_rectangle(x0, top, x1, bottom, fill=fill, outline="")
                canvas.create_line(x0, top, x0, bottom + 50, fill="#7b8794")
                canvas.create_text(x0 + 4, top + 4, anchor="nw", text=f"#{segment.file_index}", fill="#263442")
                band_text = f"#{segment.file_index} {segment.path.name}"
                available_chars = max(3, int((x1 - x0) / 7))
                if len(band_text) > available_chars:
                    band_text = band_text[:max(1, available_chars - 1)] + "…"
                canvas.create_rectangle(x0, bottom + 12, x1, bottom + 48, fill=fill, outline="#9aa6b2")
                canvas.create_text((x0 + x1) / 2, bottom + 30, text=band_text, width=max(5, x1 - x0 - 4))

            canvas.create_line(left, center, right, center, fill="#7e8791")
            levels = waveform.levels
            pixel_count = max(1, int(plot_width))
            if levels:
                half = max(4.0, plot_height / 2.0 - 5.0)
                total_levels = len(levels)
                for pixel in range(pixel_count):
                    start = int(pixel * total_levels / pixel_count)
                    end = max(start + 1, int((pixel + 1) * total_levels / pixel_count))
                    amplitude = max(levels[start:min(total_levels, end)], default=0.0)
                    x = left + pixel
                    canvas.create_line(
                        x, center - amplitude * half,
                        x, center + amplitude * half,
                        fill="#1769aa",
                    )

            tick_count = 8
            for tick in range(tick_count + 1):
                fraction = tick / tick_count
                x = left + fraction * plot_width
                seconds = fraction * waveform.total_duration
                canvas.create_line(x, bottom, x, bottom + 5, fill="#46515c")
                canvas.create_text(x, bottom + 7, anchor="n", text=format_clock(seconds), fill="#263442")
            canvas.create_line(right, top, right, bottom + 50, fill="#7b8794")

        def schedule_draw(_event: Any = None) -> None:
            if redraw_after[0] is not None:
                try:
                    window.after_cancel(redraw_after[0])
                except Exception:
                    pass
            redraw_after[0] = window.after(80, draw)

        def motion(event: Any) -> None:
            segment, offset, total_seconds = event_position(event)
            canvas.delete("audio_cursor")
            if segment is None:
                return
            x = float(canvas.canvasx(event.x))
            canvas.create_line(
                x, plot.get("top", 0), x, plot.get("bottom", 0),
                fill="#c0392b", width=1, tags="audio_cursor",
            )
            hover_var.set(
                f"Chunk {format_clock(total_seconds)} — file #{segment.file_index} "
                f"{segment.path.name} at {format_clock(offset)}"
            )
            try:
                segment_tree.selection_set(str(segment.file_index))
                segment_tree.see(str(segment.file_index))
            except tk.TclError:
                pass

        def open_position(event: Any) -> None:
            segment, offset, _total_seconds = event_position(event)
            if segment is None:
                return
            vlc = self._choose_vlc(window)
            if vlc is None:
                hover_var.set("VLC was not selected.")
                return
            ok, message = open_in_vlc_at(vlc, segment.path, offset)
            hover_var.set(message)
            self.log_var.set(message)
            if not ok:
                messagebox.showerror("Open in VLC", message, parent=window)

        def open_selected_segment(_event: Any = None) -> None:
            selected = segment_tree.selection()
            if not selected:
                return
            try:
                number = int(selected[0])
            except Exception:
                return
            segment = next((item for item in waveform.segments if item.file_index == number), None)
            if segment is None:
                return
            vlc = self._choose_vlc(window)
            if vlc is None:
                return
            ok, message = open_in_vlc_at(vlc, segment.path, 0.0)
            hover_var.set(message)
            if not ok:
                messagebox.showerror("Open in VLC", message, parent=window)

        canvas.bind("<Configure>", schedule_draw)
        canvas.bind("<Motion>", motion)
        canvas.bind("<Double-Button-1>", open_position)
        segment_tree.bind("<Double-Button-1>", open_selected_segment)
        window.after_idle(draw)

    def _open_model_folders(self) -> None:
        folders = self._current_scope_folders() or self.model_folders
        ok, message = open_paths(folders)
        self.log_var.set(message)
        if not ok:
            messagebox.showerror("Open folders", message, parent=self.root)

    def _open_or_generate(self) -> None:
        chunk = self.current_chunk
        if chunk is None or self.busy:
            return
        existing = find_existing_mosaics(
            chunk,
            self.manifest,
            use_existing=bool(self.settings.get("use_existing_mosaics", True)),
            session_generated_signatures=self.session_generated_signatures,
        )
        if existing:
            chunk.mosaics = existing
            ok, message = open_paths(existing)
            self.log_var.set(message)
            if not ok:
                messagebox.showerror("Open mosaic", message, parent=self.root)
            self.root.after(100, self._schedule_prefetch)
            return

        if chunk.signature in self.prefetch_inflight:
            self.status_var.set("This mosaic is already being generated by the look-ahead worker…")
            return

        self.mosaic_failed_signatures.discard(chunk.signature)
        settings = dict(self.settings)
        self.cancel_event.clear()
        self._set_busy(True, "Generating only the current mosaic…")

        def worker() -> Tuple[bool, str, List[Path]]:
            return generate_mosaic(
                chunk,
                settings,
                self.duration_cache,
                self.manifest,
                self.cancel_event,
                self.process_holder,
                progress=lambda current, total, text: self.messages.put(
                    ("progress", (current, total, text))
                ),
                session_generated_signatures=self.session_generated_signatures,
            )

        future = self.executor.submit(worker)
        future.add_done_callback(lambda finished: self.messages.put(("mosaic_done", finished)))

    def _regenerate_current_mosaic(self) -> None:
        chunk = self.current_chunk
        if chunk is None or self.busy:
            return
        if not messagebox.askyesno(
            "Regenerate mosaic",
            "Delete the current chunk's existing mosaic image(s) and build them again using the current mosaic settings?",
            parent=self.root,
        ):
            return
        self._cancel_prefetch(terminate=True)
        settings = dict(self.settings)
        self.cancel_event.clear()
        self._set_busy(True, "Forcing a fresh mosaic for the current chunk…")

        def worker() -> Tuple[bool, str, List[Path]]:
            return generate_mosaic(
                chunk,
                settings,
                self.duration_cache,
                self.manifest,
                self.cancel_event,
                self.process_holder,
                progress=lambda current, total, text: self.messages.put(
                    ("progress", (current, total, "Regenerating — " + text))
                ),
                session_generated_signatures=self.session_generated_signatures,
                force_regenerate=True,
            )

        future = self.executor.submit(worker)
        future.add_done_callback(lambda finished: self.messages.put(("mosaic_done", finished)))

    def _cancel_prefetch(self, terminate: bool = False) -> None:
        self.prefetch_generation += 1
        event = self.prefetch_cancel_event
        if event is not None:
            event.set()
        if terminate and self.prefetch_process_holder:
            process = self.prefetch_process_holder[0]
            if process is not None:
                try:
                    process.terminate()
                except Exception:
                    pass
        self.prefetch_inflight.clear()
        # The worker retains its local cancellation event/process holder. Clear
        # the app references so a later settings change can schedule fresh work;
        # the single-worker executor guarantees the replacement waits its turn.
        self.prefetch_future = None
        self.prefetch_cancel_event = None
        self.prefetch_process_holder = None
        self.prefetch_var.set("Preparation queue: idle")

    def _schedule_prefetch(self) -> None:
        """Prepare one shared window of chunks after the first preview appears.

        Preparation is staged per chunk: actual durations first, then mosaic
        generation. Recu's model indexes run on their separate worker and use
        the same window size when resolving individual recording durations.
        """
        if not self.current_chunk or not self.chunks:
            return
        try:
            prepare_count = max(0, int(self.settings.get("chunks_to_prepare", 3)))
        except Exception:
            prepare_count = 3
        if prepare_count <= 0:
            self._cancel_prefetch(terminate=False)
            self.prefetch_var.set("Preparation queue: disabled")
            return

        window = list(self.chunks[:prepare_count])
        auto_mosaics = bool(self.auto_generate_var.get())
        candidates: List[Chunk] = []
        for candidate in window:
            try:
                existing = find_existing_mosaics(
                    candidate,
                    self.manifest,
                    use_existing=bool(self.settings.get("use_existing_mosaics", True)),
                    session_generated_signatures=self.session_generated_signatures,
                )
            except Exception as exc:
                existing = []
                append_log(f"Could not inspect prepared mosaic for {candidate.folder}: {exc}")
            if existing:
                candidate.mosaics = existing
            needs_duration = not candidate.durations_prepared
            needs_mosaic = auto_mosaics and not candidate.mosaics and candidate.signature not in self.mosaic_failed_signatures
            if needs_duration or needs_mosaic:
                candidates.append(candidate)

        if not candidates:
            self.prefetch_var.set(
                f"Preparation queue: first {len(window):,} chunk(s) ready"
                if window else "Preparation queue: no queued chunks"
            )
            self._schedule_recu_chunk_resolution()
            return
        if self.prefetch_future is not None:
            return

        self.prefetch_generation += 1
        generation = self.prefetch_generation
        cancel = threading.Event()
        process_holder: List[Optional[subprocess.Popen[Any]]] = [None]
        self.prefetch_cancel_event = cancel
        self.prefetch_process_holder = process_holder
        self.prefetch_inflight = {chunk.signature for chunk in candidates}
        settings = dict(self.settings)
        self.prefetch_var.set(
            f"Preparation queue: preparing {len(candidates):,} of the first {len(window):,} chunk(s)…"
        )

        def worker() -> List[Tuple[str, bool, str, List[str], bool, str, List[Path]]]:
            results: List[Tuple[str, bool, str, List[str], bool, str, List[Path]]] = []
            total_items = len(candidates)
            for item_number, candidate in enumerate(candidates, start=1):
                if cancel.is_set():
                    break
                duration_ok = candidate.durations_prepared
                duration_message = "Actual durations already prepared."
                duration_warnings: List[str] = []
                if not candidate.durations_prepared:
                    duration_ok, duration_message, duration_warnings = prepare_chunk_durations(
                        candidate,
                        settings,
                        self.duration_cache,
                        cancel,
                        progress=lambda current, total, text, n=item_number, sig=candidate.signature: self.messages.put(
                            ("prefetch_progress", (generation, n, total_items, current, total, "Durations — " + text, sig))
                        ),
                    )
                if cancel.is_set():
                    break

                mosaic_ok = True
                mosaic_message = "Mosaic preparation disabled."
                outputs: List[Path] = list(candidate.mosaics)
                if auto_mosaics and not outputs:
                    mosaic_ok, mosaic_message, generated = generate_mosaic(
                        candidate,
                        settings,
                        self.duration_cache,
                        self.manifest,
                        cancel,
                        process_holder,
                        progress=lambda current, total, text, n=item_number, sig=candidate.signature: self.messages.put(
                            ("prefetch_progress", (generation, n, total_items, current, total, "Mosaic — " + text, sig))
                        ),
                        session_generated_signatures=self.session_generated_signatures,
                    )
                    outputs = list(generated)

                result = (
                    candidate.signature,
                    duration_ok,
                    duration_message,
                    duration_warnings,
                    mosaic_ok,
                    mosaic_message,
                    outputs,
                )
                results.append(result)
                self.messages.put(
                    (
                        "prefetch_item_done",
                        (generation, item_number, total_items, *result),
                    )
                )
            return results

        future = self.prefetch_executor.submit(worker)
        self.prefetch_future = future
        future.add_done_callback(
            lambda finished, gen=generation: self.messages.put(("prefetch_done", (gen, finished)))
        )

    def _process_current(self) -> None:
        chunk = self.current_chunk
        if chunk is None or self.busy:
            return
        if chunk.signature in self.prefetch_inflight:
            messagebox.showinfo(
                "Chunk still preparing",
                "The current chunk is still reading durations or building its mosaic. Wait for that preparation item to finish before moving its files.",
                parent=self.root,
            )
            return
        delete_count = len(chunk.files) - len(self.keep_indices)
        if bool(self.settings.get("confirm_moves", True)):
            confirmed = messagebox.askyesno(
                "Process chunk",
                f"KEEP {len(self.keep_indices):,} file(s) in Review and move "
                f"{delete_count:,} file(s) to {DELETION_BUCKET_NAME}/{chunk.folder.name}?",
                parent=self.root,
            )
            if not confirmed:
                return
        keep = set(self.keep_indices)
        dry_run = bool(self.dry_run_var.get())
        self.cancel_event.clear()
        self._set_busy(True, "Moving files outside the UI thread…")
        def worker() -> Tuple[Chunk, Set[int], Tuple[bool, str, Dict[str, Any]]]:
            return chunk, keep, process_chunk_files(chunk, keep, dry_run)

        future = self.executor.submit(worker)
        future.add_done_callback(lambda finished: self.messages.put(("process_done", finished)))

    def _run_designated_keeplast(self) -> None:
        if self.busy:
            return
        folders = list(self.all_model_folders or self.model_folders)
        if not folders:
            messagebox.showinfo("KeepLast", "Load a model first.", parent=self.root)
            return
        physical_model_name = self.current_chunk.folder.name if self.current_chunk is not None else folders[0].name
        model_name = canonical_model_name(physical_model_name)
        keep_file = locate_keeplasts_file()
        mapping = load_keeplasts_map(keep_file)
        minutes = mapping.get(model_name.casefold())
        if minutes is None:
            messagebox.showinfo(
                "No KeepLast designation",
                f"{model_name} is not designated in:\n{keep_file}\n\nAssign KeepLast in the full sorter first; this Lite button only runs existing designations.",
                parent=self.root,
            )
            return
        dry_run = bool(self.dry_run_var.get())
        if bool(self.settings.get("confirm_moves", True)):
            verb = "preview" if dry_run else "process"
            if not messagebox.askyesno(
                "Run designated KeepLast",
                f"{model_name} is designated to keep the newest {minutes:g} minute(s) per remaining chunk.\n\n"
                f"This will {verb} every remaining chunk across all {len(folders):,} matching root folder(s). "
                "Retained files go to Review; older files go to MARKED_FOR_DELETION. Continue?",
                parent=self.root,
            ):
                return

        self._clear_review_history()
        self._cancel_prefetch(terminate=True)
        settings = dict(self.settings)
        cancel = self.cancel_event
        cancel.clear()
        self._set_busy(True, f"Building all remaining chunks for {model_name} before KeepLast…")

        def worker() -> Dict[str, Any]:
            chunks, notes = build_model_queue(
                folders,
                settings,
                self.duration_cache,
                self.manifest,
                include_skipped=False,
                progress=lambda text: self.messages.put(("status", text)),
                session_generated_signatures=self.session_generated_signatures,
                cancel=cancel,
            )
            summary = run_keeplast_for_remaining_chunks(
                chunks,
                minutes,
                settings,
                self.duration_cache,
                cancel,
                self.process_holder,
                dry_run=dry_run,
                progress=lambda current, total, text: self.messages.put(("progress", (current, total, text))),
            )
            summary.update({"model": model_name, "minutes": minutes, "keeplasts_file": str(keep_file), "notes": notes})
            return summary

        future = self.executor.submit(worker)
        future.add_done_callback(lambda finished: self.messages.put(("keeplast_done", finished)))

    def _skip_current(self) -> None:
        chunk = self.current_chunk
        if chunk is None or self.busy:
            return
        if chunk.signature in self.prefetch_inflight:
            messagebox.showinfo(
                "Chunk still preparing",
                "Wait for the current preparation item to finish before skipping this chunk.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Skip chunk",
            "Mark this chunk skipped and continue? The recordings will not move.",
            parent=self.root,
        ):
            return
        try:
            previous_state = mark_chunk_skipped(chunk)
            self.review_history.append(
                ReviewHistoryEntry(
                    chunk=chunk,
                    action="skipped",
                    keep_indices=set(self.keep_indices),
                    previous_review_state=previous_state,
                )
            )
            self.chunks.pop(0)
            self._show_current()
            self._update_back_button()
        except Exception as exc:
            messagebox.showerror("Skip failed", str(exc), parent=self.root)

    def _back_to_previous(self) -> None:
        if self.busy or not self.review_history:
            return
        entry = self.review_history[-1]
        if entry.action == "processed":
            prompt = (
                "Restore every file from the previous processed chunk to its original model folder, "
                "then reopen that chunk so you can change KEEP/DELETE choices?"
            )
        else:
            prompt = (
                "Unskip and reopen the previous chunk so you can change its KEEP/DELETE choices?"
            )
        if not messagebox.askyesno("Back to previous chunk", prompt, parent=self.root):
            return

        self._cancel_prefetch(terminate=True)
        self.pending_back_entry = entry
        self.cancel_event.clear()
        self._set_busy(True, "Restoring the previous chunk and its original file locations…")
        future = self.executor.submit(restore_history_entry, entry)
        future.add_done_callback(lambda finished: self.messages.put(("back_done", finished)))

    def _cancel_work(self) -> None:
        self.cancel_event.set()
        self.speech_cancel_event.set()
        process = self.process_holder[0]
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass
        self.status_var.set("Cancellation requested. The current filesystem call may finish first.")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.generate_button, self.audio_button, self.process_button,
            self.skip_button, self.rescan_button, self.keeplast_button,
        ):
            button.configure(state=state)
        self.back_button.configure(
            state="disabled" if busy or not self.review_history else "normal"
        )
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if text:
            self.status_var.set(text)
        if not busy:
            self.progress["value"] = 0

    def _start_job(self, status: str, function: Callable[[], Any], event_name: str) -> None:
        self.cancel_event.clear()
        self._set_busy(True, status)
        future = self.executor.submit(function)
        future.add_done_callback(lambda finished: self.messages.put((event_name, finished)))

    def _poll_messages(self) -> None:
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == "status":
                    self.status_var.set(str(payload))
                elif event == "progress":
                    current, total, text = payload
                    self.progress["maximum"] = max(1, int(total))
                    self.progress["value"] = max(0, int(current))
                    self.status_var.set(str(text))
                elif event == "model_catalog_progress":
                    self._handle_model_catalog_progress(payload)
                elif event == "model_catalog_done":
                    self._handle_model_catalog_done(payload)
                elif event == "folders_found":
                    self._handle_folders_found(payload)
                elif event == "queue_built":
                    self._handle_queue_built(payload)
                elif event == "mosaic_done":
                    self._handle_mosaic_done(payload)
                elif event == "audio_done":
                    self._handle_audio_done(payload)
                elif event == "speech_progress":
                    self._handle_speech_progress(payload)
                elif event == "speech_done":
                    self._handle_speech_done(payload)
                elif event == "process_done":
                    self._handle_process_done(payload)
                elif event == "back_done":
                    self._handle_back_done(payload)
                elif event == "keeplast_done":
                    self._handle_keeplast_done(payload)
                elif event == "prefetch_progress":
                    self._handle_prefetch_progress(payload)
                elif event == "prefetch_item_done":
                    self._handle_prefetch_item_done(payload)
                elif event == "prefetch_done":
                    self._handle_prefetch_done(payload)
                elif event == "recu_progress":
                    self._handle_recu_progress(payload)
                elif event == "recu_done":
                    self._handle_recu_done(payload)
                elif event == "recu_resolve_progress":
                    self._handle_recu_resolve_progress(payload)
                elif event == "recu_resolve_done":
                    self._handle_recu_resolve_done(payload)
        except queue.Empty:
            pass
        except Exception as exc:
            append_log(f"UI event error: {exc}\n{traceback.format_exc()}")
        finally:
            try:
                self.root.after(75, self._poll_messages)
            except Exception:
                pass

    @staticmethod
    def _future_result(future: Any) -> Tuple[bool, Any]:
        try:
            return True, future.result()
        except Exception as exc:
            return False, (exc, traceback.format_exc())

    def _handle_folders_found(self, future: Any) -> None:
        ok, result = self._future_result(future)
        self._set_busy(False)
        if not ok:
            exc, trace = result
            append_log(f"Folder search failed: {exc}\n{trace}")
            messagebox.showerror("Folder search failed", str(exc), parent=self.root)
            return
        folders: List[Path] = list(result)
        if not folders:
            self.all_model_folders = []
            self.model_folders = []
            self.recu_status_var.set("Recu metadata: model folder was not found, so scraping was not started.")
            self._clear_queue(f"No folder named '{self.last_model_text}' was found across {len(self.roots):,} roots.")
            return
        self.all_model_folders = folders
        drives = sorted({disk_label(folder) for folder in folders})
        self.drive_combo.configure(values=["All"] + drives)
        if self.drive_var.get() not in ["All"] + drives:
            self.drive_var.set("All")
        self.model_folders = self._current_scope_folders()
        self.status_var.set(f"Found {len(folders):,} matching folder(s) across {len(drives):,} drive(s). Building a quick preview first…")
        self._scan_selected_folders()

    def _handle_queue_built(self, future: Any) -> None:
        ok, result = self._future_result(future)
        self._set_busy(False)
        if not ok:
            exc, trace = result
            append_log(f"Queue scan failed: {exc}\n{trace}")
            if self.cancel_event.is_set() or "cancel" in str(exc).casefold():
                self.status_var.set("Model scan cancelled. Previously loaded data remains unchanged.")
                return
            messagebox.showerror("Model scan failed", str(exc), parent=self.root)
            return
        chunks, notes = result
        self.chunks = list(chunks)
        self._sort_queue()
        for note in notes:
            append_log(note)
        fallback_notes = [note for note in notes if "actual duration unavailable" in note]
        recent_notes = [note for note in notes if note.startswith("Skipped recent-write folder:")]
        other_notes = [note for note in notes if note not in fallback_notes and note not in recent_notes]
        suffix_parts: List[str] = []
        if fallback_notes:
            suffix_parts.append(f"{len(fallback_notes):,} duration fallback(s)")
        if recent_notes:
            suffix_parts.append(f"{len(recent_notes):,} recent-write folder(s) skipped")
        if other_notes:
            suffix_parts.append(f"{len(other_notes):,} other warning(s)")
        suffix = ("; " + "; ".join(suffix_parts) + ".") if suffix_parts else ""
        self.log_var.set(
            f"Found {len(self.chunks):,} unreviewed chunk(s) in {len(self.model_folders):,} folder(s){suffix}"
        )
        self._show_current()
        # The first chunk is now visible. Only after that preview is rendered do
        # duration probing, Recu scraping/resolution, and mosaic preparation begin.
        self.root.after(40, lambda: self._start_recu_scrape(force=False))
        self.root.after(80, self._schedule_prefetch)

    def _handle_mosaic_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        if not ok_future:
            exc, trace = result
            append_log(f"Mosaic generation crashed: {exc}\n{trace}")
            messagebox.showerror("Mosaic generation failed", str(exc), parent=self.root)
            return
        ok, message, outputs = result
        self.log_var.set(message)
        if self.current_chunk is not None and outputs:
            self.current_chunk.mosaics = list(outputs)
        if ok and outputs:
            if self.current_chunk is not None:
                self.mosaic_failed_signatures.discard(self.current_chunk.signature)
            if bool(self.settings.get("auto_open_mosaic", True)):
                opened, open_message = open_paths(outputs)
                if not opened:
                    messagebox.showerror("Open mosaic", open_message, parent=self.root)
            self.root.after(100, self._schedule_prefetch)
        elif not self.cancel_event.is_set():
            if self.current_chunk is not None:
                self.mosaic_failed_signatures.add(self.current_chunk.signature)
            messagebox.showerror("Mosaic generation failed", message, parent=self.root)

    def _handle_prefetch_progress(self, payload: Any) -> None:
        generation, item_number, item_total, current, total, text, _signature = payload
        if int(generation) != self.prefetch_generation:
            return
        percent = int((max(0, int(current)) / max(1, int(total))) * 100)
        self.prefetch_var.set(
            f"Preparation {item_number}/{item_total}: {percent}% — {text}"
        )

    def _handle_prefetch_item_done(self, payload: Any) -> None:
        (
            generation,
            item_number,
            item_total,
            signature,
            duration_ok,
            duration_message,
            duration_warnings,
            mosaic_ok,
            mosaic_message,
            outputs,
        ) = payload
        if int(generation) != self.prefetch_generation:
            return
        self.prefetch_inflight.discard(str(signature))
        output_paths = [Path(path) for path in outputs]
        target = next((chunk for chunk in self.chunks if chunk.signature == signature), None)
        if target is not None:
            if duration_ok:
                target.durations_prepared = True
                self.duration_prepared_signatures.add(str(signature))
            if output_paths:
                target.mosaics = list(output_paths)
        for warning in duration_warnings:
            append_log(f"Prepared-duration warning: {warning}")
        if mosaic_ok and output_paths:
            self.mosaic_failed_signatures.discard(str(signature))
        elif not mosaic_ok and "cancel" not in str(mosaic_message).casefold():
            self.mosaic_failed_signatures.add(str(signature))
            append_log(f"Prepared mosaic failed: {mosaic_message}")

        self.prefetch_var.set(
            f"Preparation {int(item_number):,}/{int(item_total):,} complete; continuing…"
        )
        current = self.current_chunk
        if current is not None and current.signature == signature:
            self._refresh_current_chunk_display()
            if output_paths and bool(self.settings.get("auto_open_mosaic", True)):
                opened, open_message = open_paths(output_paths)
                if not opened:
                    append_log(f"Could not open prepared current mosaic: {open_message}")
                    self.log_var.set(open_message)
        self._schedule_recu_chunk_resolution()

    def _handle_prefetch_done(self, payload: Any) -> None:
        generation, future = payload
        if int(generation) != self.prefetch_generation:
            return
        ok_future, result = self._future_result(future)
        self.prefetch_inflight.clear()
        self.prefetch_future = None
        self.prefetch_cancel_event = None
        self.prefetch_process_holder = None
        if not ok_future:
            exc, trace = result
            append_log(f"Preparation queue crashed: {exc}\n{trace}")
            self.prefetch_var.set(f"Preparation queue failed: {exc}")
            return
        prepared = len(result)
        duration_failures = sum(1 for _sig, duration_ok, _dm, _dw, _mo, _mm, _out in result if not duration_ok)
        mosaic_failures = sum(1 for _sig, _do, _dm, _dw, mosaic_ok, _mm, _out in result if not mosaic_ok)
        suffix: List[str] = []
        if duration_failures:
            suffix.append(f"{duration_failures} duration warning(s)")
        if mosaic_failures:
            suffix.append(f"{mosaic_failures} mosaic failure(s)")
        self.prefetch_var.set(
            f"Preparation queue: {prepared:,} chunk(s) completed"
            + (("; " + ", ".join(suffix)) if suffix else "")
        )
        self._schedule_recu_chunk_resolution()
        # The user may have advanced while the worker was running. Refill the
        # newly shifted preparation window without blocking the UI.
        if self.current_chunk is not None:
            self.root.after(100, self._schedule_prefetch)

    def _handle_speech_progress(self, payload: Any) -> None:
        current, total, text = payload
        self.progress["maximum"] = max(1, int(total))
        self.progress["value"] = max(0, int(current))
        self.status_var.set(str(text))
        state = self.audio_tools_state
        if state:
            try:
                state["progress"]["maximum"] = max(1, int(total))
                state["progress"]["value"] = max(0, int(current))
                state["status_var"].set(str(text))
            except Exception:
                pass

    def _handle_speech_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        self._set_speech_controls_busy(False)
        if not ok_future:
            exc, trace = result
            append_log(f"Speech transcription crashed: {exc}\n{trace}")
            state = self.audio_tools_state
            if state:
                state["status_var"].set(f"Speech transcription failed: {exc}")
            messagebox.showerror("Speech transcription failed", str(exc), parent=(state.get("window", self.root) if state else self.root))
            return
        scope_key, query, match_mode, speech_result = result
        ok, message, transcript = speech_result
        state = self.audio_tools_state
        if not ok or transcript is None:
            if state:
                state["status_var"].set(message)
            if not self.speech_cancel_event.is_set():
                messagebox.showerror("Speech transcription failed", message, parent=(state.get("window", self.root) if state else self.root))
            return
        self.speech_transcript_cache[str(scope_key)] = transcript
        matches = search_speech_transcript(
            transcript,
            str(query),
            str(match_mode),
            int(self.settings.get("speech_fuzzy_threshold_percent", 74) or 74),
        ) if str(query).strip() else []
        self._populate_speech_tools(transcript, matches, message)
        self.log_var.set(message)
        if self.current_chunk is not None:
            self.root.after(100, self._schedule_prefetch)

    def _handle_audio_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        if not ok_future:
            exc, trace = result
            append_log(f"Audio waveform generation crashed: {exc}\n{trace}")
            messagebox.showerror("Audio waveform failed", str(exc), parent=self.root)
            return
        cache_key, audio_result = result
        ok, message, waveform = audio_result
        self.log_var.set(message)
        if ok and waveform is not None:
            self.audio_waveform_cache[str(cache_key)] = waveform
            self._show_audio_waveform(waveform)
            if self.current_chunk is not None:
                self.root.after(100, self._schedule_prefetch)
        elif not self.cancel_event.is_set():
            messagebox.showerror("Audio waveform failed", message, parent=self.root)

    def _handle_process_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        if not ok_future:
            exc, trace = result
            append_log(f"File processing crashed: {exc}\n{trace}")
            messagebox.showerror("Processing failed", str(exc), parent=self.root)
            return
        chunk, keep_indices, process_result = result
        ok, message, details = process_result
        append_log(message + " " + json.dumps(details, ensure_ascii=False))
        self.log_var.set(message)
        if not ok:
            messagebox.showerror(
                "Some moves failed",
                message + "\n\n" + "\n".join(details.get("errors", [])[:12]),
                parent=self.root,
            )
            # A partial move changes the chunk. Rescan only this selected model.
            self._scan_selected_folders()
            return
        if not bool(details.get("dry_run", False)) and self.chunks:
            self.review_history.append(
                ReviewHistoryEntry(
                    chunk=chunk,
                    action="processed",
                    keep_indices=set(keep_indices),
                    moves=list(details.get("moves", [])),
                    previous_review_state=details.get("previous_review_state"),
                )
            )
            self.chunks.pop(0)
        self._show_current()
        self._update_back_button()

    def _handle_back_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        entry = self.pending_back_entry
        self.pending_back_entry = None
        if not ok_future:
            exc, trace = result
            append_log(f"Back restore crashed: {exc}\n{trace}")
            messagebox.showerror("Back failed", str(exc), parent=self.root)
            self._update_back_button()
            return
        ok, message, details = result
        append_log(message + " " + json.dumps(details, ensure_ascii=False))
        self.log_var.set(message)
        if not ok or entry is None:
            messagebox.showerror(
                "Back could not finish",
                message + "\n\n" + "\n".join(details.get("errors", [])[:12]),
                parent=self.root,
            )
            self._update_back_button()
            return

        if self.review_history and self.review_history[-1] is entry:
            self.review_history.pop()
        self.chunks = [
            queued for queued in self.chunks
            if queued.signature != entry.chunk.signature
        ]
        self.chunks.insert(0, entry.chunk)
        self.pending_restore_keep_indices = set(entry.keep_indices)
        self._show_current()
        self.status_var.set(
            "Previous chunk restored. Its earlier KEEP choices are shown; change them and process again."
        )
        self._update_back_button()

    def _handle_keeplast_done(self, future: Any) -> None:
        ok_future, result = self._future_result(future)
        self._set_busy(False)
        if not ok_future:
            exc, trace = result
            append_log(f"KeepLast batch crashed: {exc}\n{trace}")
            messagebox.showerror("KeepLast failed", str(exc), parent=self.root)
            return
        summary = dict(result)
        append_log("KeepLast batch: " + json.dumps(summary, ensure_ascii=False, default=str))
        dry_run = bool(summary.get("dry_run", False))
        self.log_var.set(
            f"KeepLast {'preview' if dry_run else 'complete'}: {summary.get('kept', 0)} retained to Review; "
            f"{summary.get('deleted', 0)} older files to deletion across {summary.get('chunks', 0)} chunk(s)."
        )
        if summary.get("errors"):
            messagebox.showwarning(
                "KeepLast completed with warnings",
                self.log_var.get() + "\n\n" + "\n".join(str(v) for v in summary.get("errors", [])[:12]),
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "KeepLast complete",
                self.log_var.get() + f"\n\nDesignation source:\n{summary.get('keeplasts_file', '')}",
                parent=self.root,
            )
        if not dry_run:
            self._scan_selected_folders()
        else:
            self._show_current()

    def _open_settings(self, initial_tab: str = "Preparation") -> None:
        window = tk.Toplevel(self.root)
        window.title("Mosaic Sort Lite settings")
        window.geometry("880x720")
        window.minsize(640, 500)
        window.transient(self.root)
        window.grab_set()

        shell = ttk.Frame(window, padding=10)
        shell.pack(fill="both", expand=True)
        # Pack the footer first so Tk always reserves its height. The notebook
        # shrinks inside the remaining area instead of pushing Save/Cancel
        # below the visible edge on short settings windows.
        buttons = ttk.Frame(shell)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Separator(shell, orient="horizontal").pack(side="bottom", fill="x", pady=(8, 0))
        notebook = ttk.Notebook(shell)
        notebook.pack(side="top", fill="both", expand=True)

        tabs: Dict[str, ttk.Frame] = {}
        for name in ("Preparation", "Video & durations", "Mosaic", "Audio & speech", "Recu", "Playback & workflow"):
            frame = ttk.Frame(notebook, padding=12)
            notebook.add(frame, text=name)
            tabs[name] = frame
            frame.columnconfigure(1, weight=1)
        try:
            notebook.select(tabs.get(initial_tab, tabs["Preparation"]))
        except Exception:
            pass

        variables: Dict[str, Any] = {
            "chunks_to_prepare": tk.StringVar(value=str(self.settings.get("chunks_to_prepare", 3))),
            "auto_generate_current": tk.BooleanVar(value=bool(self.settings.get("auto_generate_current", True))),
            "ffmpeg_path": tk.StringVar(value=str(self.settings.get("ffmpeg_path", ""))),
            "vlc_path": tk.StringVar(value=str(self.settings.get("vlc_path", ""))),
            "audio_waveform_mode": tk.StringVar(value=normalize_audio_mode(self.settings.get("audio_waveform_mode", AUDIO_MODE_RAW_ADJUSTABLE))),
            "audio_raw_coverage_percent": tk.StringVar(value=str(self.settings.get("audio_raw_coverage_percent", 25))),
            "audio_raw_max_windows_per_file": tk.StringVar(value=str(self.settings.get("audio_raw_max_windows_per_file", 72))),
            "audio_waveform_target_bins": tk.StringVar(value=str(self.settings.get("audio_waveform_target_bins", 2400))),
            "audio_smoothing_radius": tk.StringVar(value=str(self.settings.get("audio_smoothing_radius", 1))),
            "speech_engine": tk.StringVar(value=str(self.settings.get("speech_engine", "Vosk (fast / legacy compatible)"))),
            "speech_vosk_model": tk.StringVar(value=str(self.settings.get("speech_vosk_model", "vosk-model-small-en-us-0.15"))),
            "speech_vosk_model_path": tk.StringVar(value=str(self.settings.get("speech_vosk_model_path", ""))),
            "speech_vosk_model_root": tk.StringVar(value=str(self.settings.get("speech_vosk_model_root", "vosk_models"))),
            "speech_model": tk.StringVar(value=str(self.settings.get("speech_model", "tiny.en"))),
            "speech_model_path": tk.StringVar(value=str(self.settings.get("speech_model_path", ""))),
            "speech_model_download_root": tk.StringVar(value=str(self.settings.get("speech_model_download_root", "whisper_models"))),
            "speech_language": tk.StringVar(value=str(self.settings.get("speech_language", "en"))),
            "speech_device": tk.StringVar(value=str(self.settings.get("speech_device", "cpu"))),
            "speech_compute_type": tk.StringVar(value=str(self.settings.get("speech_compute_type", "int8"))),
            "speech_cpu_compatibility_mode": tk.StringVar(value=str(self.settings.get("speech_cpu_compatibility_mode", "Automatic safe retries"))),
            "speech_cpu_threads": tk.StringVar(value=str(self.settings.get("speech_cpu_threads", 4))),
            "speech_beam_size": tk.StringVar(value=str(self.settings.get("speech_beam_size", 1))),
            "speech_vad_filter": tk.BooleanVar(value=bool(self.settings.get("speech_vad_filter", True))),
            "speech_fuzzy_threshold_percent": tk.StringVar(value=str(self.settings.get("speech_fuzzy_threshold_percent", 74))),
            "speech_cache_enabled": tk.BooleanVar(value=bool(self.settings.get("speech_cache_enabled", True))),
            "speech_preextract_audio": tk.BooleanVar(value=bool(self.settings.get("speech_preextract_audio", True))),
            "extensions": tk.StringVar(value=str(self.settings.get("extensions", "mp4,ts"))),
            "chunk_gap_minutes": tk.StringVar(value=str(self.settings.get("chunk_gap_minutes", 30))),
            "recent_write_grace_seconds": tk.StringVar(value=str(self.settings.get("recent_write_grace_seconds", 120))),
            "duration_probe_timeout_seconds": tk.StringVar(value=str(self.settings.get("duration_probe_timeout_seconds", 15))),
            "duration_fallback_seconds": tk.StringVar(value=str(self.settings.get("duration_fallback_seconds", 900))),
            "max_inferred_duration_seconds": tk.StringVar(value=str(self.settings.get("max_inferred_duration_seconds", 21600))),
            "skip_recent_writes": tk.BooleanVar(value=bool(self.settings.get("skip_recent_writes", True))),
            "sample_every_seconds": tk.StringVar(value=str(self.settings.get("sample_every_seconds", 300))),
            "columns": tk.StringVar(value=str(self.settings.get("columns", 3))),
            "tile_width": tk.StringVar(value=str(self.settings.get("tile_width", 480))),
            "max_tiles_per_image": tk.StringVar(value=str(self.settings.get("max_tiles_per_image", 120))),
            "max_total_frames": tk.StringVar(value=str(self.settings.get("max_total_frames", 240))),
            "use_existing_mosaics": tk.BooleanVar(value=bool(self.settings.get("use_existing_mosaics", True))),
            "auto_open_mosaic": tk.BooleanVar(value=bool(self.settings.get("auto_open_mosaic", True))),
            "confirm_moves": tk.BooleanVar(value=bool(self.settings.get("confirm_moves", True))),
            "recursive_fallback": tk.BooleanVar(value=bool(self.settings.get("recursive_fallback", False))),
            "recu_enabled": tk.BooleanVar(value=bool(self.settings.get("recu_enabled", True))),
            "recu_scrape_comments": tk.BooleanVar(value=bool(self.settings.get("recu_scrape_comments", True))),
            "recu_base_url": tk.StringVar(value=str(self.settings.get("recu_base_url", "https://recu.me"))),
            "recu_cookie": tk.StringVar(value=str(self.settings.get("recu_cookie", ""))),
            "recu_user_agent": tk.StringVar(value=str(self.settings.get("recu_user_agent", DEFAULT_SETTINGS["recu_user_agent"]))),
            "recu_site_timezone": tk.StringVar(value=str(self.settings.get("recu_site_timezone", "UTC"))),
            "recu_local_timezone": tk.StringVar(value=str(self.settings.get("recu_local_timezone", "America/New_York"))),
            "recu_cache_hours": tk.StringVar(value=str(self.settings.get("recu_cache_hours", 0))),
            "recu_request_timeout_seconds": tk.StringVar(value=str(self.settings.get("recu_request_timeout_seconds", 20))),
            "recu_page_limit": tk.StringVar(value=str(self.settings.get("recu_page_limit", 200))),
            "recu_throttle_seconds": tk.StringVar(value=str(self.settings.get("recu_throttle_seconds", 0.35))),
            "recu_match_tolerance_seconds": tk.StringVar(value=str(self.settings.get("recu_match_tolerance_seconds", 120))),
            "recu_lazy_max_duration_hours": tk.StringVar(value=str(self.settings.get("recu_lazy_max_duration_hours", 24))),
            "recu_auth_fallback": tk.BooleanVar(value=bool(self.settings.get("recu_auth_fallback", True))),
            "recu_warmup_homepage": tk.BooleanVar(value=bool(self.settings.get("recu_warmup_homepage", True))),
            "recu_auth_retry_cooldown_seconds": tk.StringVar(value=str(self.settings.get("recu_auth_retry_cooldown_seconds", 30))),
        }

        def add_field(tab: str, row: int, key: str, label: str, width: int = 24, secret: bool = False) -> int:
            ttk.Label(tabs[tab], text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(tabs[tab], textvariable=variables[key], width=width, show="*" if secret else "").grid(row=row, column=1, sticky="ew", pady=4)
            return row + 1

        prep = tabs["Preparation"]
        ttk.Label(prep, text="Number of chunks to prepare:", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(prep, textvariable=variables["chunks_to_prepare"], width=10).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Checkbutton(prep, text="Create mosaics automatically inside this preparation window", variable=variables["auto_generate_current"]).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(
            prep,
            text=(
                "The first queued chunk is shown immediately using cached/provisional timing. After that preview appears, "
                "the app prepares this many chunks in queue order: actual video durations, relevant Recu timing, and mosaics. "
                "This one value replaces the former separate mosaic and Recu look-ahead counts."
            ),
            wraplength=760,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 4))

        video = tabs["Video & durations"]
        row = 0
        row = add_field("Video & durations", row, "ffmpeg_path", "ffmpeg.exe (blank = auto-detect):", 60)
        def browse_ffmpeg() -> None:
            path = filedialog.askopenfilename(title="Choose ffmpeg executable", parent=window, filetypes=[("ffmpeg", "ffmpeg.exe" if os.name == "nt" else "ffmpeg"), ("All files", "*.*")])
            if path: variables["ffmpeg_path"].set(path)
        ttk.Button(video, text="Browse", command=browse_ffmpeg).grid(row=0, column=2, padx=5)
        for key, label in (
            ("extensions", "Video extensions:"),
            ("chunk_gap_minutes", "Chunk gap, minutes:"),
            ("recent_write_grace_seconds", "Recent-write grace, seconds:"),
            ("duration_probe_timeout_seconds", "Duration probe timeout, seconds:"),
            ("duration_fallback_seconds", "Safe fallback duration, seconds:"),
            ("max_inferred_duration_seconds", "Maximum next-start inference, seconds:"),
        ):
            row = add_field("Video & durations", row, key, label)
        ttk.Checkbutton(video, text="Skip folders with files still being written", variable=variables["skip_recent_writes"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)

        mosaic = tabs["Mosaic"]
        row = 0
        for key, label in (
            ("sample_every_seconds", "Sample spacing, seconds:"),
            ("columns", "Mosaic columns:"),
            ("tile_width", "Tile width, pixels:"),
            ("max_tiles_per_image", "Maximum tiles per image:"),
            ("max_total_frames", "Maximum sampled frames per chunk:"),
        ):
            row = add_field("Mosaic", row, key, label)
        ttk.Checkbutton(mosaic, text="Reuse existing mosaics", variable=variables["use_existing_mosaics"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=4); row += 1
        ttk.Checkbutton(mosaic, text="Open the current mosaic automatically when ready", variable=variables["auto_open_mosaic"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(mosaic, text="Use the main-window Regenerate mosaic button to ignore reuse and force a fresh current mosaic.", wraplength=760).grid(row=row+1, column=0, columnspan=2, sticky="w", pady=(12, 4))

        recu = tabs["Recu"]
        row = 0
        ttk.Checkbutton(recu, text="Enable Recu kink/comment scraping", variable=variables["recu_enabled"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1
        ttk.Checkbutton(recu, text="Scrape performer comments and pagination", variable=variables["recu_scrape_comments"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1
        ttk.Checkbutton(recu, text="Automatically recover from 403 using nearby/legacy Recu credentials", variable=variables["recu_auth_fallback"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1
        ttk.Checkbutton(recu, text="Warm up a persistent Recu browser session before performer requests", variable=variables["recu_warmup_homepage"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1
        for key, label, secret in (
            ("recu_base_url", "Base URL:", False),
            ("recu_cookie", "Cookie (optional):", True),
            ("recu_user_agent", "Browser User-Agent:", False),
            ("recu_site_timezone", "Site timestamp timezone:", False),
            ("recu_local_timezone", "Local recording timezone:", False),
            ("recu_cache_hours", "Minimum refresh interval, hours:", False),
            ("recu_request_timeout_seconds", "Request timeout, seconds:", False),
            ("recu_page_limit", "Maximum pages per section:", False),
            ("recu_throttle_seconds", "Delay between pages, seconds:", False),
            ("recu_match_tolerance_seconds", "Chunk-match tolerance, seconds:", False),
            ("recu_lazy_max_duration_hours", "Maximum assumed recording length, hours:", False),
            ("recu_auth_retry_cooldown_seconds", "403 retry cooldown, seconds:", False),
        ):
            row = add_field("Recu", row, key, label, 68, secret)
        ttk.Label(recu, text="Individual Recu recording pages are resolved only for the shared prepared-chunk window.", wraplength=760).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 3))

        audio_tab = tabs["Audio & speech"]
        row = 0
        ttk.Label(audio_tab, text="Default waveform method:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(audio_tab, textvariable=variables["audio_waveform_mode"], values=list(AUDIO_MODES), state="readonly", width=38).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        for key, label in (
            ("audio_raw_coverage_percent", "Adjustable raw coverage, percent (1–100):"),
            ("audio_raw_max_windows_per_file", "Maximum sample windows per file:"),
            ("audio_waveform_target_bins", "Waveform detail bins:"),
            ("audio_smoothing_radius", "Display averaging radius (0 = none):"),
        ):
            row = add_field("Audio & speech", row, key, label)
        ttk.Label(
            audio_tab,
            text=(
                "Waveform coverage controls how much raw audio is read. Display averaging only smooths the graph."
            ),
            wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 10)); row += 1

        ttk.Separator(audio_tab, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=6); row += 1
        ttk.Label(audio_tab, text="On-demand speech search / transcription", font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 6)); row += 1
        ttk.Label(audio_tab, text="Recognition engine:").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(
            audio_tab,
            textvariable=variables["speech_engine"],
            values=[
                "Vosk (fast / legacy compatible)",
                "Automatic (Vosk first, Whisper optional)",
                "faster-whisper (higher accuracy)",
            ],
            state="readonly",
            width=42,
        ).grid(row=row, column=1, sticky="w", pady=3); row += 1
        row = add_field("Audio & speech", row, "speech_language", "Language code (en or auto):", 24)
        row = add_field("Audio & speech", row, "speech_fuzzy_threshold_percent", "Fuzzy-match threshold, percent:")
        ttk.Checkbutton(audio_tab, text="Cache transcripts per video file for instant later searches", variable=variables["speech_cache_enabled"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1
        ttk.Checkbutton(audio_tab, text="Extract a temporary mono WAV before recognition", variable=variables["speech_preextract_audio"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3); row += 1

        ttk.Separator(audio_tab, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=6); row += 1
        ttk.Label(audio_tab, text="Vosk — recommended for this older PC", font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 5)); row += 1
        row = add_field("Audio & speech", row, "speech_vosk_model", "Vosk model name:", 38)
        row = add_field("Audio & speech", row, "speech_vosk_model_path", "Local Vosk model folder (optional):", 48)
        row = add_field("Audio & speech", row, "speech_vosk_model_root", "Downloaded Vosk models folder:", 48)
        ttk.Label(
            audio_tab,
            text=(
                "The default small English model is about 40 MB and is designed for low-resource devices. "
                "It is faster and less accurate than Whisper, but it avoids the CTranslate2 access violation shown in all three crash logs."
            ),
            wraplength=760, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(3, 7)); row += 1

        ttk.Separator(audio_tab, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=6); row += 1
        ttk.Label(audio_tab, text="Optional faster-whisper backend", font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 5)); row += 1
        ttk.Label(audio_tab, text="Whisper model preset:").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(audio_tab, textvariable=variables["speech_model"], values=["tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small"], state="normal", width=24).grid(row=row, column=1, sticky="w", pady=3); row += 1
        for key, label in (
            ("speech_model_path", "Local Whisper model folder/path:"),
            ("speech_model_download_root", "Downloaded Whisper models folder:"),
        ):
            row = add_field("Audio & speech", row, key, label, 48)
        ttk.Label(audio_tab, text="Device:").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(audio_tab, textvariable=variables["speech_device"], values=["cpu", "auto", "cuda"], state="readonly", width=16).grid(row=row, column=1, sticky="w", pady=3); row += 1
        ttk.Label(audio_tab, text="Compute type:").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(audio_tab, textvariable=variables["speech_compute_type"], values=["int8", "int8_float32", "int8_float16", "float16", "float32", "default"], state="normal", width=18).grid(row=row, column=1, sticky="w", pady=3); row += 1
        ttk.Label(audio_tab, text="CPU compatibility:").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(
            audio_tab,
            textvariable=variables["speech_cpu_compatibility_mode"],
            values=["Automatic safe retries", "Standard / fastest", "Legacy AVX safe", "Generic safest"],
            state="readonly",
            width=28,
        ).grid(row=row, column=1, sticky="w", pady=3); row += 1
        for key, label in (
            ("speech_cpu_threads", "Whisper CPU threads:"),
            ("speech_beam_size", "Whisper beam size (1 = fastest):"),
        ):
            row = add_field("Audio & speech", row, key, label)
        ttk.Checkbutton(audio_tab, text="Use Whisper voice-activity detection to skip silence", variable=variables["speech_vad_filter"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)


        play = tabs["Playback & workflow"]
        row = 0
        row = add_field("Playback & workflow", row, "vlc_path", "vlc.exe (blank = auto-detect):", 60)
        def browse_vlc() -> None:
            path = filedialog.askopenfilename(title="Choose VLC executable", parent=window, filetypes=[("VLC", "vlc.exe" if os.name == "nt" else "vlc"), ("All files", "*.*")])
            if path: variables["vlc_path"].set(path)
        ttk.Button(play, text="Browse", command=browse_vlc).grid(row=0, column=2, padx=5)
        ttk.Checkbutton(play, text="Confirm before moving each chunk", variable=variables["confirm_moves"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=4); row += 1
        ttk.Checkbutton(play, text="Allow recursive model-folder fallback search", variable=variables["recursive_fallback"]).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)

        def save() -> None:
            try:
                previous_use_existing = bool(self.settings.get("use_existing_mosaics", True))
                integer_keys = (
                    "chunks_to_prepare", "chunk_gap_minutes", "recent_write_grace_seconds",
                    "duration_probe_timeout_seconds", "duration_fallback_seconds", "max_inferred_duration_seconds",
                    "sample_every_seconds", "columns", "tile_width", "max_tiles_per_image", "max_total_frames",
                    "audio_raw_coverage_percent", "audio_raw_max_windows_per_file", "audio_waveform_target_bins", "audio_smoothing_radius",
                    "speech_cpu_threads", "speech_beam_size", "speech_fuzzy_threshold_percent",
                    "recu_cache_hours", "recu_request_timeout_seconds", "recu_page_limit",
                    "recu_match_tolerance_seconds", "recu_lazy_max_duration_hours",
                    "recu_auth_retry_cooldown_seconds",
                )
                for key in integer_keys:
                    value = int(float(str(variables[key].get()).strip()))
                    if value < 0:
                        raise ValueError(f"{key} cannot be negative")
                    self.settings[key] = value
                throttle = float(str(variables["recu_throttle_seconds"].get()).strip())
                if throttle < 0:
                    raise ValueError("Recu throttle cannot be negative")
                self.settings["recu_throttle_seconds"] = throttle
                for key in (
                    "ffmpeg_path", "vlc_path", "extensions",
                    "speech_engine", "speech_vosk_model", "speech_vosk_model_path", "speech_vosk_model_root",
                    "speech_model", "speech_model_path", "speech_model_download_root",
                    "speech_language", "speech_device", "speech_compute_type", "speech_cpu_compatibility_mode",
                    "recu_base_url", "recu_cookie", "recu_user_agent", "recu_site_timezone", "recu_local_timezone",
                ):
                    self.settings[key] = str(variables[key].get()).strip()
                self.settings["audio_waveform_mode"] = normalize_audio_mode(variables["audio_waveform_mode"].get())
                for key in (
                    "auto_generate_current", "skip_recent_writes", "use_existing_mosaics", "auto_open_mosaic",
                    "confirm_moves", "recursive_fallback", "recu_enabled", "recu_scrape_comments",
                    "recu_auth_fallback", "recu_warmup_homepage",
                    "speech_vad_filter", "speech_cache_enabled", "speech_preextract_audio",
                ):
                    self.settings[key] = bool(variables[key].get())
                if not 1 <= int(self.settings["audio_raw_coverage_percent"]) <= 100:
                    raise ValueError("Audio raw coverage must be between 1 and 100 percent")
                if int(self.settings["audio_raw_max_windows_per_file"]) < 1:
                    raise ValueError("Maximum audio windows per file must be at least 1")
                if int(self.settings["speech_cpu_threads"]) < 1:
                    raise ValueError("Speech CPU threads must be at least 1")
                if not 1 <= int(self.settings["speech_beam_size"]) <= 10:
                    raise ValueError("Speech beam size must be between 1 and 10")
                if not 1 <= int(self.settings["speech_fuzzy_threshold_percent"]) <= 100:
                    raise ValueError("Speech fuzzy threshold must be between 1 and 100 percent")
                self.settings["settings_version"] = 18
                self.settings.pop("pre_generate_count", None)
                self.settings.pop("recu_prefetch_chunk_count", None)
                self.auto_generate_var.set(bool(self.settings["auto_generate_current"]))
                self._save_settings()
                reset_recu_http_sessions()
                self._cancel_prefetch(terminate=True)
                self.mosaic_failed_signatures.clear()
                self._cancel_recu_chunk_resolution()
                if previous_use_existing and not bool(self.settings["use_existing_mosaics"]):
                    for queued_chunk in self.chunks:
                        if queued_chunk.signature not in self.session_generated_signatures:
                            queued_chunk.mosaics = []
                window.grab_release()
                window.destroy()
                if self.current_chunk is not None:
                    self.root.after(50, self._schedule_prefetch)
                    self.root.after(80, lambda: self._start_recu_scrape(force=True))
            except Exception as exc:
                messagebox.showerror("Invalid settings", str(exc), parent=window)

        ttk.Button(buttons, text="Save", command=save).pack(side="right", padx=3)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="right", padx=3)

    def _on_close(self) -> None:
        self.cancel_event.set()
        process = self.process_holder[0]
        if process is not None:
            _terminate_process_tree(process)
        self.duration_cache.save()
        self.transcript_cache.save()
        self.speech_cancel_event.set()
        self._save_settings()
        self._cancel_prefetch(terminate=True)
        self.recu_cancel_event.set()
        self.recu_resolution_cancel_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.prefetch_executor.shutdown(wait=False, cancel_futures=True)
        self.model_index_executor.shutdown(wait=False, cancel_futures=True)
        self.recu_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> int:
    if "--speech-worker" in sys.argv:
        try:
            position = sys.argv.index("--speech-worker")
            request_path = Path(sys.argv[position + 1])
            result_path = Path(sys.argv[position + 2])
            crash_log_path = Path(sys.argv[position + 3])
        except Exception:
            return 64
        return run_speech_worker_cli(request_path, result_path, crash_log_path)

    if "--self-test" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}: import/self-test OK")
        print(f"App directory: {APP_DIR}")
        print(f"Pillow available: {Image is not None}")
        ffmpeg, ffprobe = resolve_ffmpeg("")
        print(f"ffmpeg: {ffmpeg}")
        print(f"ffprobe: {ffprobe}")
        print(f"Vosk available: {SpeechEngineManager.vosk_available()}")
        print(f"faster-whisper available: {SpeechEngineManager.faster_whisper_available()}")
        print(f"Selected speech backend: {SpeechEngineManager.selected_backend(DEFAULT_SETTINGS)}")
        return 0

    root = tk.Tk()
    MosaicLiteApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
