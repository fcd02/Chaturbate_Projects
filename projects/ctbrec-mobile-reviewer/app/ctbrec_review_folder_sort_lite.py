#!/usr/bin/env python3
"""CTBRec Review Sort Lite

A focused second-pass reviewer for files already moved into model/Review folders.

Design goals:
- Index only Review folders beneath configured recording roots, grouping model_dup# siblings under the base model.
- Rank models by the actual byte size of direct video files in Review folders.
- Queue clip creation and file movement until the computer reaches the configured idle time.
- Generate mosaics only for the selected model and bounded look-ahead chunks.
- Keep the configured number of upcoming mosaics prepared and delete each after processing.
- Use one continuous chunk timeline and globally spaced timestamps across source recordings.
- Route approved files and any number of timestamped clips into model/Cumshots or model/Misc Hot Scenes.
- Preserve and recalculate source _tail_... tags independently for every exact clip.
- Move rejected source recordings to the per-drive MARKED_FOR_DELETION bucket.
"""

from __future__ import annotations

import hashlib
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None


APP_NAME = "CTBRec Review Sort Lite"
APP_VERSION = "1.5.0"
APP_DIR = Path(__file__).resolve().parent

SETTINGS_PATH = APP_DIR / "review_sort_lite_settings.json"
ROOTS_DEFAULT_PATH = APP_DIR / "recording_roots.txt"
DURATION_CACHE_PATH = APP_DIR / "review_sort_duration_cache.json"
MODEL_SIZE_CACHE_PATH = APP_DIR / "review_model_size_cache.json"
MANIFEST_PATH = APP_DIR / "review_mosaic_manifest.json"
LOG_PATH = APP_DIR / "review_sort_lite.log"

REVIEW_FOLDER_NAME = "Review"
CUMSHOTS_FOLDER_NAME = "Cumshots"
MISC_FOLDER_NAME = "Misc Hot Scenes"
MOSAIC_DIRNAME = "._review_chunk_mosaics"
DELETION_BUCKET_NAME = "MARKED_FOR_DELETION"

VIDEO_EXTENSIONS = {
    ".mp4", ".ts", ".mpegts", ".m2ts", ".mts", ".mov", ".mkv",
    ".m4v", ".avi", ".webm",
}
TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})[_T -](\d{2})[-.](\d{2})[-.](\d{2})(?!\d)"
)
DUPLICATE_MODEL_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_dup\d+$", re.IGNORECASE)
TAIL_TAG_RE = re.compile(
    r"_tail_(?P<minutes>\d+)m(?P<seconds>\d{1,2})s(?P<estimated>_est)?",
    re.IGNORECASE,
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "roots_file": "recording_roots.txt",
    "ffmpeg_path": "",
    "vlc_path": "",
    "extensions": "mp4,ts",
    "chunk_gap_minutes": 30,
    "sample_every_seconds": 180,
    "columns": 3,
    "tile_width": 480,
    "max_tiles_per_image": 120,
    "max_total_frames": 300,
    "frame_extract_timeout_seconds": 45,
    "frame_extract_batch_size": 6,
    "pre_generate_count": 1,
    "use_existing_mosaics": True,
    "auto_open_mosaic": True,
    "processing_idle_minutes": 10,
    "auto_open_created_clips": False,
    "confirm_moves": True,
    "window_geometry": "1120x780",
}


@dataclass
class VideoInfo:
    path: Path
    start: datetime
    size: int
    mtime: float
    duration: float

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=max(0.1, self.duration))


@dataclass
class Chunk:
    folder: Path                 # Review folder
    model_folder: Path
    files: List[VideoInfo]
    start: datetime
    end: datetime
    source_bytes: int
    signature: str
    mosaics: List[Path] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(max(0.1, file.duration) for file in self.files)


@dataclass
class ProcessingJob:
    chunk: Chunk
    decisions: Dict[int, str]
    clip_specs: List[Tuple[float, float, str]]
    dry_run: bool
    queued_at: float = field(default_factory=time.time)


def mosaic_base(chunk: Chunk) -> str:
    model = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", chunk.model_folder.name).strip(" ._")
    model = (model or "model")[:80]
    return (
        f"review_{model}_{chunk.start:%Y-%m-%d_%H-%M-%S}_to_"
        f"{chunk.end:%Y-%m-%d_%H-%M-%S}_{chunk.signature[:14]}"
    )


def mosaic_outputs(chunk: Chunk) -> List[Path]:
    """Return the complete, current mosaic set for a chunk, or an empty list."""
    output_dir = chunk.model_folder / MOSAIC_DIRNAME
    base = mosaic_base(chunk)
    manifest = output_dir / f"{base}.json"
    data = load_json(manifest, {})
    if isinstance(data, dict) and data.get("signature") == chunk.signature:
        outputs = [Path(value) for value in data.get("outputs", []) if value]
        if outputs and all(path.exists() for path in outputs):
            return outputs
    return []


def delete_chunk_mosaics(chunk: Chunk) -> Tuple[int, List[str]]:
    """Delete only mosaic artifacts belonging to this exact processed chunk."""
    output_dir = chunk.model_folder / MOSAIC_DIRNAME
    if not output_dir.exists():
        return 0, []
    bases = {
        mosaic_base(chunk),
        f"review_labels_v2_{chunk.signature[:14]}",
        f"review_{chunk.signature[:14]}",
    }
    removed = 0
    errors: List[str] = []
    candidates: Set[Path] = set()
    for base in bases:
        candidates.update(output_dir.glob(base + "*.jpg"))
        candidates.add(output_dir / f"{base}.json")
    for path in candidates:
        if not path.exists():
            continue
        try:
            path.unlink()
            removed += 1
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    try:
        if output_dir.exists() and not any(output_dir.iterdir()):
            output_dir.rmdir()
    except Exception:
        pass
    return removed, errors


def computer_idle_seconds() -> Optional[float]:
    """Read Windows' system-wide last-input timer; return None when unavailable."""
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


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def append_log(message: str) -> None:
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except Exception:
        pass


def human_size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.2f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.2f} TB"


def format_clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_eta(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "calculating…"
    rounded = int(round(seconds))
    if rounded < 2:
        return "less than 2 seconds"
    if rounded < 60:
        return f"about {rounded} seconds"
    return f"about {format_clock(rounded)}"


def parse_timecode(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        raise ValueError("A timecode is required.")
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid timecode: {text}") from exc
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        seconds = numbers[0] * 60 + numbers[1]
    elif len(numbers) == 3:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    else:
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS.")
    if seconds < 0:
        raise ValueError("Timecodes cannot be negative.")
    return seconds


def normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def parse_roots_file(path: Path) -> List[Path]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    output: List[Path] = []
    seen: Set[str] = set()
    for line in text.splitlines():
        clean = re.split(r"\s+#", line.strip(), maxsplit=1)[0].strip()
        if not clean or clean.startswith("#"):
            continue
        for token in re.split(r"[;,]", clean):
            value = token.strip().strip('"')
            if not value:
                continue
            root = Path(os.path.expandvars(os.path.expanduser(value)))
            key = normalized(root)
            if key not in seen:
                seen.add(key)
                output.append(root)
    return output


def parse_extensions(value: str) -> Set[str]:
    parts = re.split(r"[,;\s]+", str(value or "").strip().lower())
    result = {(part if part.startswith(".") else "." + part) for part in parts if part}
    return {ext for ext in result if ext in VIDEO_EXTENSIONS} or {".mp4", ".ts"}


def canonical_model_name(name: str) -> str:
    """Collapse a trailing ``_dup#`` folder suffix into its logical model name."""
    clean = str(name or "").strip()
    match = DUPLICATE_MODEL_SUFFIX_RE.match(clean)
    return match.group("base").strip() if match and match.group("base").strip() else clean


def parse_start(path: Path, mtime: float) -> datetime:
    """Read both CTBRec and legacy dotted filename timestamps.

    Supported examples include:
      model_2026-07-19_00-22-02_...mp4
      2023.01.03_13.09.05_model_segment_...mp4
    """
    match = TIMESTAMP_RE.search(path.name)
    if match:
        try:
            year, month, day, hour, minute, second = (int(value) for value in match.groups())
            return datetime(year, month, day, hour, minute, second)
        except Exception:
            pass
    return datetime.fromtimestamp(mtime)


def review_video_files(review_folder: Path, extensions: Set[str]) -> List[Tuple[Path, os.stat_result]]:
    rows: List[Tuple[Path, os.stat_result]] = []
    try:
        with os.scandir(review_folder) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() not in extensions:
                        continue
                    rows.append((path, entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        pass
    return rows


def scan_review_models(
    roots: Sequence[Path],
    extensions: Set[str],
    progress: Callable[[int, int, str], None],
) -> List[Tuple[str, int, List[Path]]]:
    """Return logical model name, total Review bytes, matching physical folders.

    A folder such as ``model_dup2`` is grouped with ``model`` while every
    physical Review folder remains available for chunk building.

    The scan has a discovery phase so the progress bar can use a real total,
    followed by a sizing phase with an elapsed-time based ETA.
    """
    candidates: List[Tuple[str, Path, Path]] = []
    total_roots = len(roots)

    for root_index, root in enumerate(roots, start=1):
        progress(
            0,
            0,
            f"Discovering Review folders in root {root_index}/{max(1, total_roots)}: {root}",
        )
        try:
            with os.scandir(root) as iterator:
                for model_entry in iterator:
                    try:
                        if not model_entry.is_dir(follow_symlinks=False):
                            continue
                        model_folder = Path(model_entry.path)
                        review_folder = model_folder / REVIEW_FOLDER_NAME
                        if review_folder.is_dir():
                            candidates.append((model_entry.name, model_folder, review_folder))
                    except OSError:
                        continue
        except OSError:
            continue

    totals: Dict[str, int] = {}
    folders: Dict[str, List[Path]] = {}
    spellings: Dict[str, str] = {}
    total_candidates = len(candidates)
    started = time.monotonic()

    if total_candidates == 0:
        progress(1, 1, "No Review folders with direct video files were found.")
        return []

    for index, (model_name, model_folder, review_folder) in enumerate(candidates, start=1):
        size = sum(
            int(stat.st_size)
            for _path, stat in review_video_files(review_folder, extensions)
        )
        if size > 0:
            logical_name = canonical_model_name(model_name)
            key = logical_name.casefold()
            totals[key] = totals.get(key, 0) + size
            folders.setdefault(key, []).append(model_folder)
            spellings.setdefault(key, logical_name)

        elapsed = max(0.001, time.monotonic() - started)
        remaining = max(0, total_candidates - index)
        eta_seconds = (elapsed / index) * remaining
        progress(
            index,
            total_candidates,
            (
                f"{model_name} | elapsed {format_clock(elapsed)} | "
                f"ETA {format_eta(eta_seconds)}"
            ),
        )

    rows = [
        (spellings[key], totals[key], sorted(folders[key], key=normalized))
        for key in totals
    ]
    rows.sort(key=lambda row: (-row[1], row[0].casefold()))
    return rows


class DurationCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = load_json(path, {"files": {}})
        self.data = raw if isinstance(raw, dict) else {"files": {}}
        self.data.setdefault("files", {})
        self.lock = threading.Lock()
        self.dirty = False

    def get(self, path: Path, size: int, mtime: float) -> Optional[float]:
        with self.lock:
            row = self.data["files"].get(normalized(path))
            if not isinstance(row, dict):
                return None
            if int(row.get("size", -1)) != int(size):
                return None
            if abs(float(row.get("mtime", -1)) - float(mtime)) > 0.001:
                return None
            duration = float(row.get("duration", 0))
            return duration if duration > 0 else None

    def set(self, path: Path, size: int, mtime: float, duration: float) -> None:
        if duration <= 0:
            return
        with self.lock:
            self.data["files"][normalized(path)] = {
                "size": int(size), "mtime": float(mtime), "duration": float(duration)
            }
            self.dirty = True

    def save(self) -> None:
        with self.lock:
            if self.dirty:
                atomic_write_json(self.path, self.data)
                self.dirty = False


def resolve_binary(configured: str, name: str, common_paths: Sequence[Path]) -> Optional[Path]:
    candidates: List[Path] = []
    if configured.strip():
        candidates.append(Path(os.path.expandvars(os.path.expanduser(configured.strip().strip('"')))))
    candidates.extend(common_paths)
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def resolve_ffmpeg(settings: Dict[str, Any]) -> Tuple[Optional[Path], Optional[Path]]:
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffmpeg = resolve_binary(
        str(settings.get("ffmpeg_path", "")),
        executable,
        [
            APP_DIR / executable,
            APP_DIR / "lib" / "ffmpeg" / executable,
            APP_DIR.parent / "ctbrec" / "lib" / "ffmpeg" / executable,
        ],
    )
    if ffmpeg is None:
        return None, None
    probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    ffprobe = resolve_binary("", probe_name, [ffmpeg.with_name(probe_name)])
    return ffmpeg, ffprobe


def resolve_vlc(settings: Dict[str, Any]) -> Optional[Path]:
    name = "vlc.exe" if os.name == "nt" else "vlc"
    return resolve_binary(
        str(settings.get("vlc_path", "")),
        name,
        [
            Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
            Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
        ],
    )


def probe_duration(
    path: Path,
    size: int,
    mtime: float,
    ffmpeg: Path,
    ffprobe: Optional[Path],
    cache: DurationCache,
) -> float:
    cached = cache.get(path, size, mtime)
    if cached:
        return cached
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    duration = 0.0
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", str(path),
                ],
                capture_output=True, text=True, timeout=15,
                creationflags=creationflags,
            )
            if result.returncode == 0:
                duration = float(result.stdout.strip())
        except Exception:
            pass
    if duration <= 0:
        try:
            result = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=15, creationflags=creationflags,
            )
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
            if match:
                duration = (
                    int(match.group(1)) * 3600
                    + int(match.group(2)) * 60
                    + float(match.group(3))
                )
        except Exception:
            pass
    if duration <= 0:
        duration = 900.0
    cache.set(path, size, mtime, duration)
    return duration


def chunk_signature(files: Sequence[VideoInfo]) -> str:
    payload = [
        (normalized(file.path), int(file.size), round(file.mtime, 3))
        for file in files
    ]
    return hashlib.sha1(json.dumps(payload).encode("utf-8")).hexdigest()


def build_chunks(
    model_folders: Sequence[Path],
    settings: Dict[str, Any],
    cache: DurationCache,
    progress: Callable[[str], None],
    probe_actual_durations: bool = True,
) -> List[Chunk]:
    """Build Review chunks.

    Desktop callers retain exact duration probing by default. The mobile server
    passes ``probe_actual_durations=False`` so a model can become navigable
    immediately; exact durations are then prepared lazily only for a chunk that
    actually needs a new mosaic.
    """
    extensions = parse_extensions(settings.get("extensions", "mp4,ts"))
    ffmpeg: Optional[Path] = None
    ffprobe: Optional[Path] = None
    if probe_actual_durations:
        ffmpeg, ffprobe = resolve_ffmpeg(settings)
        if ffmpeg is None:
            raise RuntimeError("ffmpeg was not found. Select it in Settings.")
    gap = timedelta(minutes=max(0, int(settings.get("chunk_gap_minutes", 30))))
    chunks: List[Chunk] = []

    for folder_index, model_folder in enumerate(model_folders, start=1):
        review_folder = model_folder / REVIEW_FOLDER_NAME
        rows = review_video_files(review_folder, extensions)
        largest = max((int(stat.st_size) for _path, stat in rows), default=0)
        bytes_per_second = largest / 900.0 if largest > 0 else 0.0
        videos: List[VideoInfo] = []
        for file_index, (path, stat) in enumerate(rows, start=1):
            progress(
                f"{'Reading' if probe_actual_durations else 'Indexing'} Review file {file_index}/{len(rows)} "
                f"in model folder {folder_index}/{len(model_folders)}"
            )
            cached = cache.get(path, int(stat.st_size), float(stat.st_mtime))
            if cached:
                duration = float(cached)
            elif probe_actual_durations:
                assert ffmpeg is not None
                duration = probe_duration(
                    path, int(stat.st_size), float(stat.st_mtime),
                    ffmpeg, ffprobe, cache,
                )
            elif bytes_per_second > 0:
                duration = max(1.0, min(900.0, int(stat.st_size) / bytes_per_second))
            else:
                duration = 900.0
            videos.append(
                VideoInfo(
                    path=path,
                    start=parse_start(path, stat.st_mtime),
                    size=int(stat.st_size),
                    mtime=float(stat.st_mtime),
                    duration=float(duration),
                )
            )
        videos.sort(key=lambda item: (item.start, item.path.name.casefold()))

        groups: List[List[VideoInfo]] = []
        current: List[VideoInfo] = []
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

        for group in groups:
            chunks.append(
                Chunk(
                    folder=review_folder,
                    model_folder=model_folder,
                    files=group,
                    start=min(file.start for file in group),
                    end=max(file.end for file in group),
                    source_bytes=sum(file.size for file in group),
                    signature=chunk_signature(group),
                )
            )

    chunks.sort(key=lambda chunk: (-chunk.source_bytes, chunk.start, normalized(chunk.folder)))
    if probe_actual_durations:
        cache.save()
    return chunks


def prepare_chunk_durations(
    chunk: Chunk,
    settings: Dict[str, Any],
    cache: DurationCache,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """Exact-probe only one Review chunk immediately before new mosaic work."""
    ffmpeg, ffprobe = resolve_ffmpeg(settings)
    if ffmpeg is None:
        raise RuntimeError("ffmpeg was not found. Select it in Settings.")
    total = len(chunk.files)
    for index, video in enumerate(chunk.files, start=1):
        if progress:
            progress(index - 1, total, f"Preparing exact duration {index}/{total} for current Review chunk")
        duration = probe_duration(
            video.path, int(video.size), float(video.mtime),
            ffmpeg, ffprobe, cache,
        )
        video.duration = max(0.1, float(duration))
    if chunk.files:
        chunk.start = min(video.start for video in chunk.files)
        chunk.end = max(video.end for video in chunk.files)
    cache.save()
    if progress:
        progress(total, total, "Current Review chunk durations ready.")


def load_font(size: int) -> Any:
    if ImageFont is None:
        return None
    for name in ("Segoe UI.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def open_paths(paths: Iterable[Path]) -> Tuple[bool, str]:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return False, "Nothing exists to open."
    try:
        for path in existing:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        return True, f"Opened {len(existing)} file(s)."
    except Exception as exc:
        return False, str(exc)



def build_continuous_sample_plan(
    files: Sequence[VideoInfo],
    spacing_seconds: int,
    max_frames: int,
) -> List[Tuple[VideoInfo, float, float]]:
    """Map globally spaced chunk timestamps to source files/local times."""
    if not files:
        return []

    spacing = max(1, int(spacing_seconds))
    limit = max(1, int(max_frames))
    durations = [max(0.1, float(video.duration)) for video in files]
    total_duration = sum(durations)

    global_times: List[float] = []
    current = 0.0
    while current < total_duration:
        global_times.append(current)
        current += spacing
    if not global_times:
        global_times = [0.0]

    if len(global_times) > limit:
        if limit == 1:
            global_times = [global_times[0]]
        else:
            selected: List[float] = []
            for index in range(limit):
                source_index = int(
                    round(index * (len(global_times) - 1) / max(1, limit - 1))
                )
                selected.append(global_times[source_index])
            global_times = selected

    plan: List[Tuple[VideoInfo, float, float]] = []
    file_index = 0
    file_start = 0.0
    file_end = durations[0]

    for chunk_seconds in global_times:
        while (
            file_index + 1 < len(files)
            and chunk_seconds >= file_end - 1e-9
        ):
            file_start = file_end
            file_index += 1
            file_end += durations[file_index]

        video = files[file_index]
        local_seconds = max(0.0, chunk_seconds - file_start)
        local_seconds = min(
            local_seconds,
            max(0.0, float(video.duration) - 0.1),
        )
        plan.append((video, local_seconds, chunk_seconds))

    return plan


def source_at_chunk_time(
    chunk: Chunk,
    chunk_seconds: float,
) -> Tuple[VideoInfo, float]:
    """Return the source recording and local offset containing a chunk time."""
    if not chunk.files:
        raise ValueError("The chunk has no source recordings.")

    target = max(0.0, min(float(chunk_seconds), chunk.total_duration))
    cumulative = 0.0
    for index, video in enumerate(chunk.files):
        duration = max(0.1, float(video.duration))
        next_boundary = cumulative + duration
        if target < next_boundary - 1e-9 or index == len(chunk.files) - 1:
            return video, max(0.0, min(duration, target - cumulative))
        cumulative = next_boundary

    return chunk.files[-1], max(0.0, float(chunk.files[-1].duration))


def adjusted_tail_tag_for_trim(
    chunk: Chunk,
    start_seconds: float,
) -> str:
    """Carry forward a source tail tag adjusted to the kept starting point.

    Example:
      source: *_tail_34m14s_est.mp4
      trim starts 2:00 into that source
      output: *_tail_32m14s_est.mp4
    """
    source, local_offset = source_at_chunk_time(chunk, start_seconds)
    match = TAIL_TAG_RE.search(source.path.stem)
    if match is None:
        return ""

    original_seconds = (
        int(match.group("minutes")) * 60
        + int(match.group("seconds"))
    )
    remaining_seconds = max(
        0,
        int(round(original_seconds - local_offset)),
    )
    minutes, seconds = divmod(remaining_seconds, 60)
    estimated = "_est" if match.group("estimated") else ""
    return f"_tail_{minutes}m{seconds:02d}s{estimated}"


def _end_relative_tag(chunk: Chunk, start_seconds: float, end_seconds: float) -> str:
    """Filename-safe position measured backward from the end of the chunk.

    Example: keeping the final 15 minutes of a two-hour chunk becomes
    ``_endminus_15m00s_to_0m00s``.  End-relative position is deliberately
    preserved because it is useful when deciding future Keep Last rules.
    """
    total = max(0.0, float(chunk.total_duration))
    start_from_end = max(0, int(round(total - max(0.0, float(start_seconds)))))
    end_from_end = max(0, int(round(total - max(0.0, float(end_seconds)))))
    sm, ss = divmod(start_from_end, 60)
    em, es = divmod(end_from_end, 60)
    return f"_endminus_{sm}m{ss:02d}s_to_{em}m{es:02d}s"


def build_trim_output_name(
    chunk: Chunk,
    start_seconds: float,
    end_seconds: float,
) -> str:
    tail_tag = adjusted_tail_tag_for_trim(chunk, start_seconds)
    end_tag = _end_relative_tag(chunk, start_seconds, end_seconds)
    return (
        f"{chunk.model_folder.name}_{chunk.start:%Y%m%d_%H%M%S}_"
        f"{format_clock(start_seconds).replace(':', '-')}_to_"
        f"{format_clock(end_seconds).replace(':', '-')}"
        f"{end_tag}{tail_tag}.mp4"
    )


def extract_review_frames_batch(
    ffmpeg: Path,
    requests: Sequence[Tuple[Path, float, Path]],
    width: int,
    timeout_seconds: int,
) -> Set[Path]:
    """Extract several Review mosaic frames in one ffmpeg process."""
    clean = [(Path(source), max(0.0, float(second)), Path(destination)) for source, second, destination in requests]
    if not clean:
        return set()
    command: List[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-threads", "1", "-y"]
    for source, second, _destination in clean:
        command.extend(["-ss", f"{second:.3f}", "-i", str(source)])
    for index, (_source, _second, destination) in enumerate(clean):
        command.extend([
            "-map", f"{index}:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
            "-vf", f"scale={max(160, int(width))}:-2", "-q:v", "4", str(destination),
        ])
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    timeout = max(8, int(timeout_seconds or 45) + 4 * max(0, len(clean) - 1))
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, timeout=timeout)
    except subprocess.TimeoutExpired:
        append_log(f"Review batched ffmpeg frame extraction timed out after {timeout}s ({len(clean)} outputs)")
    except Exception as exc:
        append_log(f"Review batched ffmpeg frame extraction failed: {exc}")
    return {destination for _source, _second, destination in clean if destination.is_file() and destination.stat().st_size > 0}


def extract_review_frame_single(ffmpeg: Path, source: Path, second: float, destination: Path, width: int, timeout_seconds: int) -> bool:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-ss", f"{max(0.0, float(second)):.3f}", "-i", str(source), "-frames:v", "1",
        "-vf", f"scale={max(160, int(width))}:-2", "-q:v", "4", "-y", str(destination),
    ]
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, timeout=max(5, int(timeout_seconds or 45)))
        return destination.is_file() and destination.stat().st_size > 0
    except subprocess.TimeoutExpired:
        append_log(f"Review ffmpeg frame extraction timed out after {timeout_seconds}s: {source}")
    except Exception as exc:
        append_log(f"Review ffmpeg frame extraction failed for {source}: {exc}")
    return False


def generate_mosaic(
    chunk: Chunk,
    settings: Dict[str, Any],
    cache: DurationCache,
    progress: Callable[[int, int, str], None],
) -> Tuple[bool, str, List[Path]]:
    if Image is None:
        return False, "Pillow is required. Run install_requirements.bat.", []
    ffmpeg, _ffprobe = resolve_ffmpeg(settings)
    if ffmpeg is None:
        return False, "ffmpeg was not found.", []

    output_dir = chunk.model_folder / MOSAIC_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)
    base = mosaic_base(chunk)
    existing = mosaic_outputs(chunk)
    if settings.get("use_existing_mosaics", True) and existing:
        return True, "Existing review mosaic reused.", existing
    # A missing/invalid manifest means any same-base images are incomplete or stale.
    for stale in list(output_dir.glob(base + "*.jpg")) + [output_dir / f"{base}.json"]:
        try:
            if stale.exists():
                stale.unlink()
        except Exception:
            pass

    spacing = max(15, int(settings.get("sample_every_seconds", 180)))
    max_frames = max(1, int(settings.get("max_total_frames", 300)))
    plan = build_continuous_sample_plan(
        chunk.files,
        spacing_seconds=spacing,
        max_frames=max_frames,
    )

    tile_width = max(160, int(settings.get("tile_width", 480)))
    columns = max(1, int(settings.get("columns", 3)))
    max_tiles = max(columns, int(settings.get("max_tiles_per_image", 120)))
    tiles: List[Any] = []
    file_numbers = {id(video): index for index, video in enumerate(chunk.files, start=1)}
    total_files = len(chunk.files)

    with tempfile.TemporaryDirectory(prefix="review_mosaic_") as temp:
        temp_dir = Path(temp)
        frame_rows: List[Tuple[VideoInfo, float, float, Path]] = [
            (video, float(local_seconds), float(chunk_seconds), temp_dir / f"{index:05d}.jpg")
            for index, (video, local_seconds, chunk_seconds) in enumerate(plan, start=1)
        ]
        frame_timeout = max(5, int(settings.get("frame_extract_timeout_seconds", 45) or 45))
        batch_size = max(1, min(12, int(settings.get("frame_extract_batch_size", 6) or 6)))
        grouped: Dict[str, List[Tuple[VideoInfo, float, float, Path]]] = {}
        for row in frame_rows:
            grouped.setdefault(normalized(row[0].path), []).append(row)
        completed = 0
        for rows in grouped.values():
            for offset in range(0, len(rows), batch_size):
                batch = rows[offset:offset + batch_size]
                progress(completed, len(plan), f"Extracting {len(batch)} Review frames in one ffmpeg process…")
                extracted = extract_review_frames_batch(
                    ffmpeg,
                    [(video.path, local_seconds, frame) for video, local_seconds, _chunk_seconds, frame in batch],
                    tile_width, frame_timeout,
                )
                for video, local_seconds, _chunk_seconds, frame in batch:
                    if frame not in extracted:
                        extract_review_frame_single(ffmpeg, video.path, local_seconds, frame, tile_width, frame_timeout)
                    completed += 1
                    progress(completed, len(plan), f"Prepared frame {completed}/{len(plan)}")

        for video, local_seconds, chunk_seconds, frame in frame_rows:
            if frame.is_file():
                try:
                    with Image.open(frame) as opened:
                        tile = opened.convert("RGB")
                except Exception:
                    tile = Image.new("RGB", (tile_width, tile_width * 9 // 16), (35, 35, 35))
            else:
                tile = Image.new("RGB", (tile_width, tile_width * 9 // 16), (35, 35, 35))
            draw = ImageDraw.Draw(tile, "RGBA")
            file_number = file_numbers.get(id(video), 1)
            labels = (
                f"Chunk {format_clock(chunk_seconds)}",
                f"File {file_number}/{total_files} | {format_clock(local_seconds)}",
            )
            font = load_font(max(13, tile.width // 26))
            line_gap = max(2, tile.width // 160)
            text_boxes = [draw.textbbox((0, 0), label, font=font) for label in labels]
            line_heights = [max(1, box[3] - box[1]) for box in text_boxes]
            text_width = max(max(1, box[2] - box[0]) for box in text_boxes)
            overlay_width = min(tile.width, text_width + 16)
            overlay_height = sum(line_heights) + line_gap + 14
            draw.rectangle((0, 0, overlay_width, overlay_height), fill=(0, 0, 0, 190))
            y = 6
            for label, line_height in zip(labels, line_heights):
                draw.text((7, y), label, fill=(255, 255, 255), font=font)
                y += line_height + line_gap
            tiles.append(tile)

    outputs: List[Path] = []
    mobile_layout_parts: List[Dict[str, Any]] = []
    for part_index, offset in enumerate(range(0, len(tiles), max_tiles), start=1):
        part = tiles[offset:offset + max_tiles]
        rows = math.ceil(len(part) / columns)
        cell_w = max(tile.width for tile in part)
        cell_h = max(tile.height for tile in part)
        canvas = Image.new("RGB", (columns * cell_w, rows * cell_h + 44), (8, 8, 8))
        draw = ImageDraw.Draw(canvas)
        draw.text(
            (8, 10),
            f"{chunk.model_folder.name} | {format_clock(chunk.total_duration)} | part {part_index}",
            fill=(255, 255, 255),
            font=load_font(18),
        )
        exact_tiles: List[Dict[str, Any]] = []
        for tile_index, tile in enumerate(part):
            row, column = divmod(tile_index, columns)
            x = column * cell_w
            y = 44 + row * cell_h
            canvas.paste(tile, (x, y))
            plan_index = offset + tile_index
            video, local_seconds, chunk_seconds = plan[plan_index]
            file_number = int(file_numbers.get(id(video), 1))
            interval_end_seconds = (
                float(plan[plan_index + 1][2])
                if plan_index + 1 < len(plan)
                else float(chunk.total_duration)
            )
            exact_tiles.append({
                "file_index": file_number - 1,
                "file_number": file_number,
                "frame_index": int(plan_index),
                "chunk_seconds": float(chunk_seconds),
                "local_seconds": float(local_seconds),
                "interval_end_seconds": float(max(chunk_seconds, interval_end_seconds)),
                "left_px": int(x),
                "top_px": int(y),
                "width_px": int(tile.width),
                "height_px": int(tile.height),
            })
        suffix = "" if len(tiles) <= max_tiles else f"_part{part_index:02d}"
        output = output_dir / f"{base}{suffix}.jpg"
        canvas.save(output, quality=86, optimize=True)
        outputs.append(output)
        mobile_layout_parts.append({
            "output": str(output),
            "width": int(canvas.width),
            "height": int(canvas.height),
            "tiles": exact_tiles,
        })

    atomic_write_json(
        output_dir / f"{base}.json",
        {
            "signature": chunk.signature,
            "outputs": [str(path) for path in outputs],
            "generated_at": datetime.now().isoformat(),
            "start": chunk.start.isoformat(),
            "end": chunk.end.isoformat(),
            "source_bytes": int(chunk.source_bytes),
            "folder": str(chunk.folder),
            "model_folder": str(chunk.model_folder),
            "files": [
                {
                    "path": str(video.path),
                    "start": video.start.isoformat(),
                    "size": int(video.size),
                    "mtime": float(video.mtime),
                    "duration": float(video.duration),
                }
                for video in chunk.files
            ],
            "label_schema": 3,
            "tile_labels": ["chunk_time", "file_number", "file_local_time"],
            "mobile_layout_v2": {
                "version": 3,
                "mode": "review",
                "signature": chunk.signature,
                "settings": {
                    "sample_every_seconds": spacing,
                    "columns": columns,
                    "tile_width": tile_width,
                    "max_tiles_per_image": max_tiles,
                    "max_total_frames": max_frames,
                },
                "parts": mobile_layout_parts,
            },
        },
    )
    progress(len(plan), len(plan), "Review mosaic ready.")
    return True, f"Generated {len(outputs)} review mosaic image(s).", outputs


def unique_destination(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    for number in range(2, 100000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose destination for {path}")


def unique_reserved_destination(path: Path, reserved: Set[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path
    number = 2
    while candidate.exists() or normalized(candidate) in reserved:
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
        if number >= 100000:
            raise RuntimeError(f"Could not choose destination for {path}")
    reserved.add(normalized(candidate))
    return candidate


def deletion_folder(model_folder: Path) -> Path:
    drive, _tail = os.path.splitdrive(os.path.abspath(str(model_folder)))
    if drive:
        return Path(drive + os.sep) / DELETION_BUCKET_NAME / model_folder.name / REVIEW_FOLDER_NAME
    return model_folder.parent / DELETION_BUCKET_NAME / model_folder.name / REVIEW_FOLDER_NAME


def move_fast(source: Path, destination: Path) -> Path:
    destination = unique_destination(destination)
    try:
        os.replace(source, destination)
    except OSError:
        shutil.move(str(source), str(destination))
    return destination


def process_file_decisions(
    chunk: Chunk,
    decisions: Dict[int, str],
    dry_run: bool,
) -> Tuple[bool, str]:
    moved = {"Leave for review": 0, "Cumshots": 0, "Misc Hot Scenes": 0, "DELETE": 0}
    errors: List[str] = []
    for index, video in enumerate(chunk.files):
        decision = decisions.get(index, "Leave for review")
        if decision == "Leave for review":
            moved[decision] += 1
            continue
        if decision == "Cumshots":
            destination = chunk.model_folder / CUMSHOTS_FOLDER_NAME / video.path.name
        elif decision == "Misc Hot Scenes":
            destination = chunk.model_folder / MISC_FOLDER_NAME / video.path.name
        else:
            destination = deletion_folder(chunk.model_folder) / video.path.name
        try:
            if not dry_run:
                move_fast(video.path, destination)
            moved[decision] = moved.get(decision, 0) + 1
        except Exception as exc:
            errors.append(f"{video.path.name}: {exc}")
    if errors:
        return False, "\n".join(errors[:12])
    prefix = "[dry-run] " if dry_run else ""
    return True, (
        f"{prefix}Left for review {moved['Leave for review']}; Cumshots {moved['Cumshots']}; "
        f"Misc Hot Scenes {moved['Misc Hot Scenes']}; deleted {moved['DELETE']}."
    )


def write_concat_list(files: Sequence[VideoInfo], path: Path) -> None:
    lines: List[str] = []
    for video in files:
        escaped = str(video.path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def trim_chunk_ranges(
    chunk: Chunk,
    settings: Dict[str, Any],
    clip_specs: Sequence[Tuple[float, float, str]],
    dry_run: bool,
    progress: Callable[[int, int, str], None],
    move_originals_to_deletion: bool = True,
) -> Tuple[bool, str, List[Path]]:
    if not clip_specs:
        return False, "Add at least one clip range.", []

    validated: List[Tuple[float, float, str]] = []
    for index, (start_seconds, end_seconds, destination_label) in enumerate(clip_specs, start=1):
        if end_seconds <= start_seconds:
            return False, f"Clip {index}: end must be greater than start.", []
        if start_seconds >= chunk.total_duration:
            return False, f"Clip {index}: start is beyond the end of the chunk.", []
        if destination_label not in {"Cumshots", "Misc Hot Scenes"}:
            return False, f"Clip {index}: choose a valid destination.", []
        validated.append(
            (start_seconds, min(end_seconds, chunk.total_duration), destination_label)
        )

    reserved: Set[str] = set()
    planned_outputs: List[Path] = []
    for start_seconds, end_seconds, destination_label in validated:
        destination_dir = (
            chunk.model_folder / CUMSHOTS_FOLDER_NAME
            if destination_label == "Cumshots"
            else chunk.model_folder / MISC_FOLDER_NAME
        )
        output_name = build_trim_output_name(chunk, start_seconds, end_seconds)
        planned_outputs.append(
            unique_reserved_destination(destination_dir / output_name, reserved)
        )

    if dry_run:
        listing = "\n".join(str(path) for path in planned_outputs)
        return True, f"[dry-run] Would create {len(planned_outputs)} clip(s):\n{listing}", planned_outputs

    ffmpeg, _ffprobe = resolve_ffmpeg(settings)
    if ffmpeg is None:
        return False, "ffmpeg was not found.", []
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    temp_outputs: List[Path] = []

    with tempfile.TemporaryDirectory(prefix="review_trim_") as temp:
        temp_dir = Path(temp)
        concat_file = temp_dir / "sources.txt"
        write_concat_list(chunk.files, concat_file)

        for index, ((start_seconds, end_seconds, destination_label), final_output) in enumerate(
            zip(validated, planned_outputs), start=1
        ):
            progress(
                index - 1,
                len(validated),
                (
                    f"Creating clip {index}/{len(validated)}: "
                    f"{format_clock(start_seconds)}–{format_clock(end_seconds)} "
                    f"→ {destination_label}"
                ),
            )
            temp_output = temp_dir / f"clip_{index:04d}.mp4"
            duration = end_seconds - start_seconds
            command = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-ss", f"{start_seconds:.3f}", "-t", f"{duration:.3f}",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", "-y", str(temp_output),
            ]
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            if result.returncode != 0 or not temp_output.exists():
                error = result.stderr.decode("utf-8", "replace")[-2000:]
                return False, f"Clip {index} failed:\n{error}", []
            temp_outputs.append(temp_output)

        created_outputs: List[Path] = []
        for temp_output, final_output in zip(temp_outputs, planned_outputs):
            final_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_output), str(final_output))
            created_outputs.append(final_output)

    if move_originals_to_deletion:
        progress(
            len(validated), len(validated),
            "All clips created. Moving original Review recordings to deletion…",
        )
        errors: List[str] = []
        for video in chunk.files:
            try:
                move_fast(video.path, deletion_folder(chunk.model_folder) / video.path.name)
            except Exception as exc:
                errors.append(f"{video.path.name}: {exc}")
        if errors:
            return False, (
                f"Created {len(created_outputs)} clip(s), but some originals could not be moved:\n"
                + "\n".join(errors[:12])
            ), created_outputs

    counts = {
        "Cumshots": sum(1 for _start, _end, label in validated if label == "Cumshots"),
        "Misc Hot Scenes": sum(
            1 for _start, _end, label in validated if label == "Misc Hot Scenes"
        ),
    }
    return True, (
        f"Created {len(created_outputs)} exact clip(s): "
        f"Cumshots {counts['Cumshots']}; Misc Hot Scenes {counts['Misc Hot Scenes']}."
    ), created_outputs


def run_processing_job(
    job: ProcessingJob,
    settings: Dict[str, Any],
    progress: Callable[[int, int, str], None],
) -> Tuple[bool, str, List[Path]]:
    """Create optional clips first, then apply every whole-file decision."""
    clip_outputs: List[Path] = []
    messages: List[str] = []
    if job.clip_specs:
        ok, message, clip_outputs = trim_chunk_ranges(
            job.chunk,
            settings,
            job.clip_specs,
            job.dry_run,
            progress,
            move_originals_to_deletion=False,
        )
        messages.append(message)
        if not ok:
            return False, "\n".join(messages), clip_outputs

    progress(0, 1, "Applying whole-file destinations…")
    ok, move_message = process_file_decisions(
        job.chunk, job.decisions, job.dry_run
    )
    messages.append(move_message)
    if not ok:
        return False, "\n".join(messages), clip_outputs

    if not job.dry_run:
        removed, errors = delete_chunk_mosaics(job.chunk)
        if errors:
            messages.append(
                "Processing succeeded, but some chunk mosaics could not be deleted:\n"
                + "\n".join(errors[:12])
            )
        else:
            messages.append(f"Removed {removed} processed-chunk mosaic artifact(s).")
    return True, "\n".join(messages), clip_outputs


class ReviewSortApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = dict(DEFAULT_SETTINGS)
        raw = load_json(SETTINGS_PATH, {})
        if isinstance(raw, dict):
            self.settings.update(raw)

        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry(str(self.settings.get("window_geometry", "1120x780")))
        self.root.minsize(350, 260)

        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-sort")
        self.prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-prefetch")
        self.messages: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.duration_cache = DurationCache(DURATION_CACHE_PATH)

        self.roots: List[Path] = []
        self.model_rows: List[Tuple[str, int, List[Path]]] = []
        self.model_lookup: Dict[str, Tuple[str, int, List[Path]]] = {}
        self.filtered_names: List[str] = []
        self.chunks: List[Chunk] = []
        self.current_chunk: Optional[Chunk] = None
        self.decisions: Dict[int, str] = {}
        self.busy = False
        self.prefetch_future: Optional[Any] = None
        self.processing_future: Optional[Any] = None
        self.processing_queue: List[ProcessingJob] = []
        self.model_filter_after: Optional[str] = None
        self.progress_indeterminate = False
        self.clip_rows: List[Dict[str, Any]] = []
        self.last_layout_width = 0
        self.last_local_activity = time.monotonic()
        self.ui_ready = False

        self._build_ui()
        self.root.bind_all("<KeyPress>", self._record_user_activity, add="+")
        self.root.bind_all("<ButtonPress>", self._record_user_activity, add="+")
        self.root.bind_all("<Motion>", self._record_user_activity, add="+")
        self._load_roots()
        self.root.after(75, self._poll)
        self.root.after(1000, self._check_idle_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        self.outer_canvas = tk.Canvas(shell, highlightthickness=0)
        vbar = ttk.Scrollbar(shell, orient="vertical", command=self.outer_canvas.yview)
        hbar = ttk.Scrollbar(shell, orient="horizontal", command=self.outer_canvas.xview)
        self.outer_canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.outer_canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")

        self.outer = ttk.Frame(self.outer_canvas, padding=9)
        self.outer_window = self.outer_canvas.create_window((0, 0), window=self.outer, anchor="nw")

        def fit(event: Any = None) -> None:
            width = max(330, self.outer_canvas.winfo_width())
            self.outer_canvas.itemconfigure(self.outer_window, width=width)
            self.outer_canvas.configure(scrollregion=self.outer_canvas.bbox("all"))
            if self.ui_ready:
                self._reflow_controls(width)

        self.outer_canvas.bind("<Configure>", fit)
        self.outer.bind(
            "<Configure>",
            lambda _event: self.outer_canvas.configure(scrollregion=self.outer_canvas.bbox("all")),
        )

        self.roots_row = ttk.Frame(self.outer)
        self.roots_row.pack(fill="x")
        self.roots_label = ttk.Label(self.roots_row, text="Roots file:")
        self.roots_var = tk.StringVar(value=str(self.settings.get("roots_file", "recording_roots.txt")))
        self.roots_entry = ttk.Entry(self.roots_row, textvariable=self.roots_var)
        self.roots_browse_button = ttk.Button(self.roots_row, text="Browse", command=self._browse_roots)
        self.roots_scan_button = ttk.Button(
            self.roots_row, text="Rescan Review sizes", command=self._load_roots
        )

        self.model_row = ttk.Frame(self.outer)
        self.model_row.pack(fill="x", pady=(7, 0))
        self.model_label = ttk.Label(self.model_row, text="Model by Review size:")
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(self.model_row, textvariable=self.model_var, state="normal")
        self.model_combo.bind("<KeyRelease>", self._model_typed)
        self.model_combo.bind("<Return>", lambda _event: self._load_model())
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_model())
        self.model_load_button = ttk.Button(self.model_row, text="Load model", command=self._load_model)
        self.settings_button = ttk.Button(self.model_row, text="Settings", command=self._settings)

        self.catalog_var = tk.StringVar(value="Review folders have not been sized yet.")
        self.catalog_label = ttk.Label(self.outer, textvariable=self.catalog_var, wraplength=1060)
        self.catalog_label.pack(fill="x", anchor="w")

        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = ttk.Label(self.outer, textvariable=self.status_var, wraplength=1060)
        self.status_label.pack(fill="x", anchor="w", pady=(7, 2))
        self.progress = ttk.Progressbar(self.outer, mode="determinate")
        self.progress.pack(fill="x")
        self.progress_detail_var = tk.StringVar(value="")
        self.progress_detail_label = ttk.Label(
            self.outer, textvariable=self.progress_detail_var, wraplength=1060
        )
        self.progress_detail_label.pack(fill="x", anchor="w", pady=(2, 7))

        chunk_box = ttk.LabelFrame(self.outer, text="Current Review chunk", padding=7)
        chunk_box.pack(fill="x")
        self.chunk_var = tk.StringVar(value="No Review chunk loaded.")
        self.chunk_label = ttk.Label(chunk_box, textvariable=self.chunk_var, wraplength=1060)
        self.chunk_label.pack(fill="x", anchor="w")

        table_frame = ttk.Frame(self.outer)
        table_frame.pack(fill="both", expand=True, pady=(7, 0))
        columns = ("decision", "number", "file", "size", "duration", "chunk_start")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended", height=10
        )
        widths = {
            "decision": 130, "number": 42, "file": 430,
            "size": 90, "duration": 95, "chunk_start": 110,
        }
        labels = {
            "decision": "Destination", "number": "#", "file": "Review recording",
            "size": "Size", "duration": "Duration", "chunk_start": "Chunk start",
        }
        for column in columns:
            self.tree.heading(column, text=labels[column])
            self.tree.column(column, width=widths[column], stretch=column == "file")
        ybar2 = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xbar2 = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar2.set, xscrollcommand=xbar2.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar2.grid(row=0, column=1, sticky="ns")
        xbar2.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.action_row = ttk.Frame(self.outer)
        self.action_row.pack(fill="x", pady=(7, 0))
        self.action_buttons = [
            ttk.Button(self.action_row, text="Open / generate mosaic", command=self._mosaic),
            ttk.Button(self.action_row, text="Open selected video", command=self._open_selected),
            ttk.Button(
                self.action_row, text="Selected → Cumshots",
                command=lambda: self._set_selected("Cumshots"),
            ),
            ttk.Button(
                self.action_row, text="Selected → Misc Hot Scenes",
                command=lambda: self._set_selected("Misc Hot Scenes"),
            ),
            ttk.Button(
                self.action_row, text="Selected → DELETE",
                command=lambda: self._set_selected("DELETE"),
            ),
        ]

        self.clip_box = ttk.LabelFrame(
            self.outer,
            text="Optional exact clips",
            padding=7,
        )
        self.clip_box.pack(fill="x", pady=(7, 0))
        clip_toolbar = ttk.Frame(self.clip_box)
        clip_toolbar.pack(fill="x")
        self.add_clip_button = ttk.Button(clip_toolbar, text="Add clip", command=self._add_clip)
        self.add_clip_button.pack(side="left")
        ttk.Label(
            clip_toolbar,
            text="No clips by default. Add only the ranges you want included in processing.",
        ).pack(side="left", padx=(8, 0))

        clip_scroll_shell = ttk.Frame(self.clip_box)
        clip_scroll_shell.pack(fill="x", expand=True, pady=(6, 0))
        clip_scroll_shell.rowconfigure(0, weight=1)
        clip_scroll_shell.columnconfigure(0, weight=1)
        self.clip_canvas = tk.Canvas(clip_scroll_shell, height=34, highlightthickness=0)
        clip_vbar = ttk.Scrollbar(
            clip_scroll_shell, orient="vertical", command=self.clip_canvas.yview
        )
        self.clip_canvas.configure(yscrollcommand=clip_vbar.set)
        self.clip_canvas.grid(row=0, column=0, sticky="nsew")
        clip_vbar.grid(row=0, column=1, sticky="ns")
        self.clip_inner = ttk.Frame(self.clip_canvas)
        self.clip_window = self.clip_canvas.create_window(
            (0, 0), window=self.clip_inner, anchor="nw"
        )
        self.clip_inner.bind(
            "<Configure>",
            lambda _event: self.clip_canvas.configure(scrollregion=self.clip_canvas.bbox("all")),
        )
        self.clip_canvas.bind(
            "<Configure>",
            lambda event: self._resize_clip_inner(event.width),
        )

        self.clip_help_label = ttk.Label(
            self.clip_box,
            text=(
                "Timecodes use the continuous chunk timeline shown on the mosaic. "
                "Optional clips are created first, then whole files move to the destinations above."
            ),
            wraplength=1000,
        )
        self.clip_help_label.pack(fill="x", anchor="w", pady=(5, 0))

        self.bottom = ttk.Frame(self.outer)
        self.bottom.pack(fill="x", pady=(7, 0))
        self.dry_run_var = tk.BooleanVar(value=False)
        self.bottom_controls = [
            ttk.Checkbutton(self.bottom, text="Dry-run", variable=self.dry_run_var),
            ttk.Button(
                self.bottom,
                text="Queue processing & next",
                command=self._queue_processing,
            ),
            ttk.Button(self.bottom, text="Skip chunk", command=self._skip),
            ttk.Button(self.bottom, text="Rescan current model", command=self._load_model),
        ]

        self.log_var = tk.StringVar(value="Ready.")
        self.log_label = ttk.Label(self.outer, textvariable=self.log_var, wraplength=1060)
        self.log_label.pack(fill="x", anchor="w", pady=(7, 0))
        self.ui_ready = True
        self.root.after_idle(lambda: self._reflow_controls(self.outer_canvas.winfo_width()))

    def _resize_clip_inner(self, width: int) -> None:
        self.clip_canvas.itemconfigure(self.clip_window, width=max(280, width))
        self._reflow_clip_rows(max(280, width))

    def _grid_wrapped(self, container: ttk.Frame, widgets: Sequence[Any], width: int, min_cell: int) -> None:
        for widget in widgets:
            widget.grid_forget()
        for column in range(8):
            container.columnconfigure(column, weight=0, uniform="")
        columns = max(1, min(len(widgets), int(max(1, width) / min_cell)))
        for column in range(columns):
            container.columnconfigure(column, weight=1, uniform=f"{id(container)}")
        for index, widget in enumerate(widgets):
            row, column = divmod(index, columns)
            widget.grid(row=row, column=column, sticky="ew", padx=2, pady=2)

    def _reflow_controls(self, width: int) -> None:
        width = max(330, int(width))
        if abs(width - self.last_layout_width) < 6:
            return
        self.last_layout_width = width
        content_width = max(280, width - 24)
        wrap = max(260, content_width - 20)
        for label in (
            self.catalog_label, self.status_label, self.progress_detail_label,
            self.chunk_label, self.clip_help_label, self.log_label,
        ):
            label.configure(wraplength=wrap)

        for widget in (
            self.roots_label, self.roots_entry, self.roots_browse_button,
            self.roots_scan_button,
        ):
            widget.grid_forget()
        for column in range(4):
            self.roots_row.columnconfigure(column, weight=0)
        if width >= 850:
            self.roots_row.columnconfigure(1, weight=1)
            self.roots_label.grid(row=0, column=0, sticky="w")
            self.roots_entry.grid(row=0, column=1, sticky="ew", padx=5)
            self.roots_browse_button.grid(row=0, column=2, sticky="ew", padx=2)
            self.roots_scan_button.grid(row=0, column=3, sticky="ew", padx=2)
        elif width >= 520:
            self.roots_row.columnconfigure(1, weight=1)
            self.roots_label.grid(row=0, column=0, sticky="w")
            self.roots_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=5)
            self.roots_browse_button.grid(row=1, column=2, sticky="ew", padx=2, pady=(4, 0))
            self.roots_scan_button.grid(row=1, column=3, sticky="ew", padx=2, pady=(4, 0))
        else:
            self.roots_row.columnconfigure(0, weight=1)
            self.roots_row.columnconfigure(1, weight=1)
            self.roots_label.grid(row=0, column=0, columnspan=2, sticky="w")
            self.roots_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            self.roots_browse_button.grid(row=2, column=0, sticky="ew", padx=(0, 2), pady=(4, 0))
            self.roots_scan_button.grid(row=2, column=1, sticky="ew", padx=(2, 0), pady=(4, 0))

        for widget in (
            self.model_label, self.model_combo, self.model_load_button, self.settings_button,
        ):
            widget.grid_forget()
        for column in range(4):
            self.model_row.columnconfigure(column, weight=0)
        if width >= 850:
            self.model_row.columnconfigure(1, weight=1)
            self.model_label.grid(row=0, column=0, sticky="w")
            self.model_combo.grid(row=0, column=1, sticky="ew", padx=5)
            self.model_load_button.grid(row=0, column=2, sticky="ew", padx=2)
            self.settings_button.grid(row=0, column=3, sticky="ew", padx=2)
        elif width >= 520:
            self.model_row.columnconfigure(1, weight=1)
            self.model_label.grid(row=0, column=0, sticky="w")
            self.model_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=5)
            self.model_load_button.grid(row=1, column=2, sticky="ew", padx=2, pady=(4, 0))
            self.settings_button.grid(row=1, column=3, sticky="ew", padx=2, pady=(4, 0))
        else:
            self.model_row.columnconfigure(0, weight=1)
            self.model_row.columnconfigure(1, weight=1)
            self.model_label.grid(row=0, column=0, columnspan=2, sticky="w")
            self.model_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            self.model_load_button.grid(row=2, column=0, sticky="ew", padx=(0, 2), pady=(4, 0))
            self.settings_button.grid(row=2, column=1, sticky="ew", padx=(2, 0), pady=(4, 0))

        self._grid_wrapped(self.action_row, self.action_buttons, content_width, 215)
        self._grid_wrapped(self.bottom, self.bottom_controls, content_width, 225)
        self._reflow_clip_rows(max(280, self.clip_canvas.winfo_width()))

    def _reflow_clip_rows(self, width: int) -> None:
        for row in self.clip_rows:
            frame = row["frame"]
            widgets = row["widgets"]
            for widget in widgets.values():
                widget.grid_forget()
            for column in range(8):
                frame.columnconfigure(column, weight=0)
            if width >= 790:
                frame.columnconfigure(1, weight=1)
                frame.columnconfigure(3, weight=1)
                frame.columnconfigure(5, weight=1)
                widgets["start_label"].grid(row=0, column=0, sticky="w")
                widgets["start_entry"].grid(row=0, column=1, sticky="ew", padx=(3, 10))
                widgets["end_label"].grid(row=0, column=2, sticky="w")
                widgets["end_entry"].grid(row=0, column=3, sticky="ew", padx=(3, 10))
                widgets["destination_label"].grid(row=0, column=4, sticky="w")
                widgets["destination_combo"].grid(row=0, column=5, sticky="ew", padx=(3, 10))
                widgets["remove"].grid(row=0, column=6, sticky="e")
            elif width >= 500:
                frame.columnconfigure(1, weight=1)
                frame.columnconfigure(3, weight=1)
                widgets["start_label"].grid(row=0, column=0, sticky="w")
                widgets["start_entry"].grid(row=0, column=1, sticky="ew", padx=(3, 8))
                widgets["end_label"].grid(row=0, column=2, sticky="w")
                widgets["end_entry"].grid(row=0, column=3, sticky="ew", padx=(3, 0))
                widgets["destination_label"].grid(row=1, column=0, sticky="w", pady=(5, 0))
                widgets["destination_combo"].grid(
                    row=1, column=1, columnspan=3, sticky="ew", padx=(3, 8), pady=(5, 0)
                )
                widgets["remove"].grid(row=1, column=4, sticky="e", pady=(5, 0))
            else:
                frame.columnconfigure(1, weight=1)
                widgets["start_label"].grid(row=0, column=0, sticky="w")
                widgets["start_entry"].grid(row=0, column=1, sticky="ew", padx=(5, 0))
                widgets["end_label"].grid(row=1, column=0, sticky="w", pady=(5, 0))
                widgets["end_entry"].grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(5, 0))
                widgets["destination_label"].grid(row=2, column=0, sticky="w", pady=(5, 0))
                widgets["destination_combo"].grid(
                    row=2, column=1, sticky="ew", padx=(5, 0), pady=(5, 0)
                )
                widgets["remove"].grid(row=3, column=1, sticky="e", pady=(5, 0))

    def _add_clip(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        destination: str = "Cumshots",
    ) -> None:
        if start is None:
            start = "0:00" if not self.clip_rows else ""
        if end is None:
            end = (
                format_clock(self.current_chunk.total_duration)
                if not self.clip_rows and self.current_chunk
                else ""
            )
        frame = ttk.LabelFrame(self.clip_inner, text="Clip", padding=6)
        frame.pack(fill="x", expand=True, pady=(0, 5))
        start_var = tk.StringVar(value="" if start is None else start)
        end_var = tk.StringVar(value="" if end is None else end)
        destination_var = tk.StringVar(value=destination)
        widgets: Dict[str, Any] = {
            "start_label": ttk.Label(frame, text="Start:"),
            "start_entry": ttk.Entry(frame, textvariable=start_var, width=12),
            "end_label": ttk.Label(frame, text="End:"),
            "end_entry": ttk.Entry(frame, textvariable=end_var, width=12),
            "destination_label": ttk.Label(frame, text="Destination:"),
            "destination_combo": ttk.Combobox(
                frame,
                textvariable=destination_var,
                values=["Cumshots", "Misc Hot Scenes"],
                state="readonly",
                width=19,
            ),
        }
        row: Dict[str, Any] = {
            "frame": frame,
            "start_var": start_var,
            "end_var": end_var,
            "destination_var": destination_var,
            "widgets": widgets,
        }
        remove_button = ttk.Button(frame, text="Remove", command=lambda: self._remove_clip(row))
        widgets["remove"] = remove_button
        self.clip_rows.append(row)
        self._renumber_clip_rows()
        self._reflow_clip_rows(max(280, self.clip_canvas.winfo_width()))
        self.clip_canvas.update_idletasks()
        self.clip_canvas.configure(scrollregion=self.clip_canvas.bbox("all"))
        self.clip_canvas.configure(height=min(185, max(70, self.clip_inner.winfo_reqheight())))
        self.clip_canvas.yview_moveto(1.0)

    def _remove_clip(self, row: Dict[str, Any]) -> None:
        if row not in self.clip_rows:
            return
        row["frame"].destroy()
        self.clip_rows.remove(row)
        self._renumber_clip_rows()
        self.clip_canvas.update_idletasks()
        self.clip_canvas.configure(
            height=34 if not self.clip_rows else min(185, max(70, self.clip_inner.winfo_reqheight()))
        )

    def _renumber_clip_rows(self) -> None:
        for index, row in enumerate(self.clip_rows, start=1):
            row["frame"].configure(text=f"Clip {index}")

    def _reset_clip_rows(self, end_time: str = "") -> None:
        for row in self.clip_rows:
            row["frame"].destroy()
        self.clip_rows = []
        self.clip_canvas.configure(height=34)
        self.clip_canvas.yview_moveto(0.0)

    def _clip_specs(self) -> List[Tuple[float, float, str]]:
        specs: List[Tuple[float, float, str]] = []
        for index, row in enumerate(self.clip_rows, start=1):
            try:
                start = parse_timecode(row["start_var"].get())
                end = parse_timecode(row["end_var"].get())
            except Exception as exc:
                raise ValueError(f"Clip {index}: {exc}") from exc
            specs.append((start, end, row["destination_var"].get()))
        return specs

    def _resolve_roots_path(self) -> Path:
        path = Path(
            os.path.expandvars(
                os.path.expanduser(self.roots_var.get().strip().strip('"') or "recording_roots.txt")
            )
        )
        return path if path.is_absolute() else APP_DIR / path

    def _browse_roots(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.roots_var.set(path)
            self._load_roots()

    def _set_busy(self, value: bool, text: str = "") -> None:
        self.busy = value
        if text:
            self.status_var.set(text)

    def _set_progress_indeterminate(self, text: str) -> None:
        if not self.progress_indeterminate:
            self.progress.stop()
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.progress_indeterminate = True
        self.progress_detail_var.set(text)

    def _set_progress_determinate(self, current: int, total: int, text: str) -> None:
        if self.progress_indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress_indeterminate = False
        self.progress["maximum"] = max(1, total)
        self.progress["value"] = max(0, min(current, max(1, total)))
        self.progress_detail_var.set(text)

    def _clear_progress(self) -> None:
        if self.progress_indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress_indeterminate = False
        self.progress["value"] = 0
        self.progress_detail_var.set("")

    def _load_roots(self) -> None:
        if self.busy:
            return
        try:
            self.roots = parse_roots_file(self._resolve_roots_path())
        except Exception as exc:
            messagebox.showerror("Roots", str(exc), parent=self.root)
            return
        extensions = parse_extensions(self.settings.get("extensions", "mp4,ts"))
        self._set_busy(True, "Discovering and sizing direct video files inside Review folders…")
        self._set_progress_indeterminate("Discovering Review folders…")
        future = self.executor.submit(
            scan_review_models,
            self.roots,
            extensions,
            lambda current, total, text: self.messages.put(
                ("catalog_progress", (current, total, text))
            ),
        )
        future.add_done_callback(lambda done: self.messages.put(("catalog_done", done)))

    def _model_typed(self, event: Any) -> None:
        if event.keysym in {"Return", "Up", "Down", "Escape", "Tab"}:
            return
        if self.model_filter_after:
            try:
                self.root.after_cancel(self.model_filter_after)
            except Exception:
                pass
        self.model_filter_after = self.root.after(75, self._apply_filter)

    def _apply_filter(self) -> None:
        self.model_filter_after = None
        query = self.model_var.get().strip().casefold()
        names = [
            name for name, _size, _folders in self.model_rows
            if not query or query in name.casefold()
        ]
        self.filtered_names = names
        self.model_combo.configure(values=names[:1500])
        self.catalog_var.set(
            f"{len(names):,} matching model(s). Dropdown remains ordered by descending Review-folder bytes."
        )

    def _load_model(self) -> None:
        if self.busy:
            return
        key = self.model_var.get().strip().casefold()
        row = self.model_lookup.get(key)
        if not row:
            messagebox.showinfo(
                "Model", "Choose a model from the Review-size dropdown.", parent=self.root
            )
            return
        name, _size, folders = row
        self.model_var.set(name)
        self._set_busy(True, f"Building Review chunks for {name}…")
        self._set_progress_indeterminate(f"Reading durations for {name}…")
        future = self.executor.submit(
            build_chunks,
            folders,
            dict(self.settings),
            self.duration_cache,
            lambda text: self.messages.put(("status", text)),
        )
        future.add_done_callback(lambda done: self.messages.put(("chunks_done", done)))

    def _show_current(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.decisions = {}
        if not self.chunks:
            self.current_chunk = None
            self.chunk_var.set("No Review chunks remain for this model.")
            self._reset_clip_rows("")
            return
        chunk = self.chunks[0]
        self.current_chunk = chunk
        cumulative = 0.0
        for index, video in enumerate(chunk.files):
            self.decisions[index] = "DELETE"
            self.tree.insert(
                "", "end", iid=str(index),
                values=(
                    "DELETE", index + 1, video.path.name, human_size(video.size),
                    format_clock(video.duration), format_clock(cumulative),
                ),
            )
            cumulative += video.duration
        self._reset_clip_rows(format_clock(chunk.total_duration))
        self.chunk_var.set(
            f"{chunk.model_folder.name} | {human_size(chunk.source_bytes)} | "
            f"{len(chunk.files)} file(s) | continuous duration {format_clock(chunk.total_duration)}\n"
            f"{chunk.folder}"
        )
        existing = mosaic_outputs(chunk)
        chunk.mosaics = existing
        if existing:
            if self.settings.get("auto_open_mosaic", True):
                self.root.after(100, lambda: open_paths(existing))
            self.root.after(150, self._schedule_prefetch)
        else:
            self.root.after(100, self._mosaic)

    def _selected(self) -> List[int]:
        result: List[int] = []
        for iid in self.tree.selection():
            try:
                result.append(int(iid))
            except ValueError:
                pass
        return result

    def _set_selected(self, decision: str) -> None:
        for index in self._selected():
            self.decisions[index] = decision
            values = list(self.tree.item(str(index), "values"))
            values[0] = decision
            self.tree.item(str(index), values=values)

    def _open_selected(self) -> None:
        if not self.current_chunk:
            return
        paths = [self.current_chunk.files[index].path for index in self._selected()]
        ok, message = open_paths(paths)
        self.log_var.set(message)
        if not ok:
            messagebox.showerror("Open", message, parent=self.root)

    def _mosaic(self) -> None:
        chunk = self.current_chunk
        if not chunk or self.busy:
            return
        existing = mosaic_outputs(chunk)
        if self.settings.get("use_existing_mosaics", True) and existing:
            chunk.mosaics = existing
            open_paths(existing)
            self._schedule_prefetch()
            return
        self._set_busy(True, "Generating continuous-timeline Review mosaic…")
        future = self.executor.submit(
            generate_mosaic,
            chunk,
            dict(self.settings),
            self.duration_cache,
            lambda current, total, text: self.messages.put(
                ("progress", (current, total, text))
            ),
        )
        future.add_done_callback(lambda done: self.messages.put(("mosaic_done", done)))

    def _schedule_prefetch(self) -> None:
        if self.busy:
            return
        count = max(0, int(self.settings.get("pre_generate_count", 1)))
        if count <= 0 or len(self.chunks) <= 1:
            if count > 0:
                self.log_var.set("Mosaic queue: no upcoming chunks to prepare.")
            return
        if self.prefetch_future is not None and not self.prefetch_future.done():
            return

        upcoming = list(self.chunks[1:])
        settings = dict(self.settings)

        def worker() -> Tuple[int, int, List[str]]:
            ready = 0
            messages: List[str] = []
            for chunk in upcoming:
                if ready >= count:
                    break
                try:
                    existing = mosaic_outputs(chunk)
                    if settings.get("use_existing_mosaics", True) and existing:
                        chunk.mosaics = existing
                        ready += 1
                        continue
                    ok, message, outputs = generate_mosaic(
                        chunk,
                        settings,
                        self.duration_cache,
                        lambda _current, _total, _text: None,
                    )
                    append_log(
                        f"Review mosaic look-ahead: {chunk.model_folder.name}: {message}"
                    )
                    if ok:
                        chunk.mosaics = outputs
                        ready += 1
                    else:
                        messages.append(f"{chunk.model_folder.name}: {message}")
                except Exception:
                    append_log(traceback.format_exc())
                    messages.append(f"{chunk.model_folder.name}: mosaic generation failed")
            return ready, min(count, len(upcoming)), messages

        self.prefetch_future = self.prefetch_executor.submit(worker)
        self.prefetch_future.add_done_callback(
            lambda done: self.messages.put(("prefetch_done", done))
        )

    def _record_user_activity(self, _event: Any = None) -> None:
        self.last_local_activity = time.monotonic()

    def _idle_seconds(self) -> float:
        system_idle = computer_idle_seconds()
        if system_idle is not None:
            return system_idle
        return max(0.0, time.monotonic() - self.last_local_activity)

    def _queue_processing(self) -> None:
        if not self.current_chunk or self.busy:
            return
        try:
            specs = self._clip_specs()
        except Exception as exc:
            messagebox.showerror("Clip ranges", str(exc), parent=self.root)
            return
        chunk = self.current_chunk
        for index, (start, end, destination) in enumerate(specs, start=1):
            if end <= start:
                messagebox.showerror(
                    "Clip ranges", f"Clip {index}: end must be greater than start.",
                    parent=self.root,
                )
                return
            if start >= chunk.total_duration:
                messagebox.showerror(
                    "Clip ranges", f"Clip {index}: start is beyond the chunk.",
                    parent=self.root,
                )
                return
            if destination not in {"Cumshots", "Misc Hot Scenes"}:
                messagebox.showerror(
                    "Clip ranges", f"Clip {index}: choose a valid destination.",
                    parent=self.root,
                )
                return

        decisions = dict(self.decisions)
        counts = {
            label: sum(1 for value in decisions.values() if value == label)
            for label in ("Cumshots", "Misc Hot Scenes", "DELETE")
        }
        clip_summary = "\n".join(
            f"  Clip {index}: {format_clock(start)}–{format_clock(end)} → {destination}"
            for index, (start, end, destination) in enumerate(specs, start=1)
        ) or "  No exact clips"
        idle_minutes = max(0.0, float(self.settings.get("processing_idle_minutes", 10)))
        if self.settings.get("confirm_moves", True) and not messagebox.askyesno(
            "Queue Review processing",
            (
                f"Queue this chunk until the computer has been idle for "
                f"{idle_minutes:g} minute(s)?\n\n"
                f"{clip_summary}\n\n"
                f"Whole files: Cumshots {counts['Cumshots']}; "
                f"Misc Hot Scenes {counts['Misc Hot Scenes']}; "
                f"DELETE {counts['DELETE']}."
            ),
            parent=self.root,
        ):
            return

        job = ProcessingJob(
            chunk,
            decisions,
            list(specs),
            bool(self.dry_run_var.get()),
        )
        self.processing_queue.append(job)
        if self.chunks and self.chunks[0] is chunk:
            if job.dry_run:
                self.chunks.append(self.chunks.pop(0))
            else:
                self.chunks.pop(0)
        self.log_var.set(
            f"Queued {chunk.model_folder.name}; {len(self.processing_queue)} job(s) "
            f"waiting for {idle_minutes:g} idle minute(s)."
        )
        self._show_current()

    def _check_idle_queue(self) -> None:
        try:
            active = self.processing_future is not None and not self.processing_future.done()
            prefetch_active = self.prefetch_future is not None and not self.prefetch_future.done()
            if self.processing_queue and not active and not prefetch_active:
                idle_seconds = self._idle_seconds()
                required = max(
                    0.0, float(self.settings.get("processing_idle_minutes", 10)) * 60.0
                )
                if idle_seconds >= required:
                    job = self.processing_queue.pop(0)
                    self.log_var.set(
                        f"Computer idle for {format_eta(idle_seconds)}; processing "
                        f"{job.chunk.model_folder.name}."
                    )
                    self.processing_future = self.executor.submit(
                        run_processing_job,
                        job,
                        dict(self.settings),
                        lambda _current, _total, text: self.messages.put(
                            ("queued_progress", text)
                        ),
                    )
                    self.processing_future.add_done_callback(
                        lambda done, queued_job=job: self.messages.put(
                            ("queued_process_done", (done, queued_job))
                        )
                    )
                else:
                    remaining = max(0.0, required - idle_seconds)
                    self.log_var.set(
                        f"{len(self.processing_queue)} processing job(s) queued; "
                        f"waiting about {format_eta(remaining)} more idle time."
                    )
        except Exception:
            append_log(traceback.format_exc())
        finally:
            try:
                self.root.after(1000, self._check_idle_queue)
            except tk.TclError:
                pass

    def _skip(self) -> None:
        if self.chunks:
            self.chunks.append(self.chunks.pop(0))
            self._show_current()

    def _settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Review Sort Lite settings")
        window.transient(self.root)
        window.geometry("720x560")
        window.minsize(420, 300)

        shell = ttk.Frame(window)
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        canvas = tk.Canvas(shell, highlightthickness=0)
        vbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(shell, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        body = ttk.Frame(canvas, padding=12)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(body_window, width=max(390, event.width)),
        )
        body.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        vars_: Dict[str, Any] = {
            key: tk.StringVar(value=str(self.settings.get(key, "")))
            for key in (
                "ffmpeg_path", "vlc_path", "extensions", "chunk_gap_minutes",
                "sample_every_seconds", "columns", "tile_width",
                "max_tiles_per_image", "max_total_frames", "pre_generate_count",
                "processing_idle_minutes",
            )
        }
        checks = {
            "use_existing_mosaics": tk.BooleanVar(
                value=bool(self.settings.get("use_existing_mosaics", True))
            ),
            "auto_open_mosaic": tk.BooleanVar(
                value=bool(self.settings.get("auto_open_mosaic", True))
            ),
            "auto_open_created_clips": tk.BooleanVar(
                value=bool(self.settings.get("auto_open_created_clips", False))
            ),
            "confirm_moves": tk.BooleanVar(
                value=bool(self.settings.get("confirm_moves", True))
            ),
        }
        fields = [
            ("ffmpeg_path", "ffmpeg.exe:"),
            ("vlc_path", "vlc.exe:"),
            ("extensions", "Video extensions:"),
            ("chunk_gap_minutes", "Chunk gap minutes:"),
            ("sample_every_seconds", "Mosaic sample spacing seconds:"),
            ("columns", "Frames per row:"),
            ("tile_width", "Tile width pixels:"),
            ("max_tiles_per_image", "Maximum tiles per image:"),
            ("max_total_frames", "Maximum frames per chunk:"),
            ("pre_generate_count", "Upcoming mosaics to prepare:"),
            ("processing_idle_minutes", "Idle minutes before queued processing:"),
        ]
        for row_index, (key, label) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row_index, column=0, sticky="w", pady=3)
            ttk.Entry(
                body,
                textvariable=vars_[key],
                width=54 if "path" in key else 18,
            ).grid(row=row_index, column=1, sticky="ew", pady=3)
        row_index = len(fields)
        for key, label in [
            ("use_existing_mosaics", "Use existing Review mosaics"),
            ("auto_open_mosaic", "Open mosaics automatically"),
            ("auto_open_created_clips", "Open newly created clips automatically"),
            ("confirm_moves", "Confirm before processing"),
        ]:
            ttk.Checkbutton(body, text=label, variable=checks[key]).grid(
                row=row_index, column=0, columnspan=2, sticky="w"
            )
            row_index += 1

        def save() -> None:
            try:
                for key in vars_:
                    if key in {
                        "chunk_gap_minutes", "sample_every_seconds", "columns",
                        "tile_width", "max_tiles_per_image", "max_total_frames",
                        "pre_generate_count",
                    }:
                        self.settings[key] = int(vars_[key].get())
                    elif key == "processing_idle_minutes":
                        self.settings[key] = max(0.0, float(vars_[key].get()))
                    else:
                        self.settings[key] = vars_[key].get().strip()
                for key, variable in checks.items():
                    self.settings[key] = bool(variable.get())
                atomic_write_json(SETTINGS_PATH, self.settings)
                window.destroy()
            except Exception as exc:
                messagebox.showerror("Settings", str(exc), parent=window)

        ttk.Button(body, text="Save", command=save).grid(
            row=row_index, column=1, sticky="e", pady=(8, 0)
        )
        body.columnconfigure(1, weight=1)

    def _poll(self) -> None:
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == "status":
                    self.status_var.set(str(payload))
                    if self.progress_indeterminate:
                        self.progress_detail_var.set(str(payload))
                elif event == "catalog_progress":
                    current, total, text = payload
                    if total <= 0:
                        self._set_progress_indeterminate(text)
                        self.status_var.set("Discovering Review folders before sizing…")
                    else:
                        self._set_progress_determinate(current, total, text)
                        self.status_var.set(
                            f"Sizing Review folders {current}/{total}"
                        )
                elif event == "progress":
                    current, total, text = payload
                    self._set_progress_determinate(current, total, text)
                    self.status_var.set(text)
                elif event == "queued_progress":
                    self.log_var.set(str(payload))
                elif event == "catalog_done":
                    self._catalog_done(payload)
                elif event == "chunks_done":
                    self._chunks_done(payload)
                elif event == "mosaic_done":
                    self._mosaic_done(payload)
                elif event == "prefetch_done":
                    self._prefetch_done(payload)
                elif event == "queued_process_done":
                    self._queued_process_done(payload)
                elif event == "process_done":
                    self._process_done(payload)
                elif event == "trim_done":
                    self._trim_done(payload)
        except queue.Empty:
            pass
        except Exception:
            append_log(traceback.format_exc())
        finally:
            self.root.after(75, self._poll)

    @staticmethod
    def _result(future: Any) -> Tuple[bool, Any]:
        try:
            return True, future.result()
        except Exception as exc:
            return False, (exc, traceback.format_exc())

    def _catalog_done(self, future: Any) -> None:
        ok, result = self._result(future)
        self._set_busy(False)
        self._clear_progress()
        if not ok:
            exc, trace = result
            append_log(trace)
            messagebox.showerror("Review sizing failed", str(exc), parent=self.root)
            return
        self.model_rows = list(result)
        self.model_lookup = {
            name.casefold(): row for row in self.model_rows for name in [row[0]]
        }
        names = [row[0] for row in self.model_rows]
        self.model_combo.configure(values=names[:1500])
        self.catalog_var.set(
            f"{len(names):,} model(s) with Review videos, ordered largest to smallest. "
            f"Total Review bytes: {human_size(sum(row[1] for row in self.model_rows))}."
        )
        self.status_var.set("Review folder sizing complete.")
        atomic_write_json(
            MODEL_SIZE_CACHE_PATH,
            {
                "updated_at": datetime.now().isoformat(),
                "models": [
                    {
                        "name": name,
                        "review_bytes": size,
                        "folders": [str(path) for path in folders],
                    }
                    for name, size, folders in self.model_rows
                ],
            },
        )

    def _chunks_done(self, future: Any) -> None:
        ok, result = self._result(future)
        self._set_busy(False)
        self._clear_progress()
        if not ok:
            exc, trace = result
            append_log(trace)
            messagebox.showerror("Review chunks", str(exc), parent=self.root)
            return
        self.chunks = list(result)
        self.status_var.set("Review chunks ready.")
        self._show_current()

    def _mosaic_done(self, future: Any) -> None:
        ok_future, result = self._result(future)
        self._set_busy(False)
        self._clear_progress()
        if not ok_future:
            exc, trace = result
            append_log(trace)
            messagebox.showerror("Mosaic", str(exc), parent=self.root)
            return
        ok, message, outputs = result
        self.log_var.set(message)
        self.status_var.set(message)
        if ok and self.current_chunk:
            self.current_chunk.mosaics = outputs
            if self.settings.get("auto_open_mosaic", True):
                open_paths(outputs)
            self._schedule_prefetch()
        elif not ok:
            messagebox.showerror("Mosaic", message, parent=self.root)

    def _prefetch_done(self, future: Any) -> None:
        ok, result = self._result(future)
        if not ok:
            _exc, trace = result
            append_log(trace)
            self.log_var.set("Mosaic ready queue encountered an error; see the log.")
            return
        ready, target, errors = result
        if errors:
            self.log_var.set(
                f"Mosaic queue: {ready}/{target} upcoming mosaics ready. "
                f"{len(errors)} generation error(s) logged."
            )
        else:
            self.log_var.set(
                f"Mosaic queue: {ready}/{target} upcoming mosaics prepared and ready."
            )

    def _queued_process_done(self, payload: Tuple[Any, ProcessingJob]) -> None:
        future, job = payload
        self.processing_future = None
        ok_future, result = self._result(future)
        if not ok_future:
            exc, trace = result
            append_log(trace)
            messagebox.showerror(
                "Queued processing failed",
                f"{job.chunk.model_folder.name}: {exc}",
                parent=self.root,
            )
            return
        ok, message, outputs = result
        self.log_var.set(message)
        self.status_var.set(
            f"Queued processing finished for {job.chunk.model_folder.name}."
            if ok else
            f"Queued processing failed for {job.chunk.model_folder.name}."
        )
        append_log(
            f"Queued processing: {job.chunk.model_folder.name}: {message}"
        )
        if ok:
            existing_outputs = [path for path in outputs if path.exists()]
            if (
                existing_outputs
                and self.settings.get("auto_open_created_clips", False)
            ):
                open_paths(existing_outputs)
            self._schedule_prefetch()
        else:
            messagebox.showerror("Queued processing", message, parent=self.root)

    def _process_done(self, future: Any) -> None:
        ok_future, result = self._result(future)
        self._set_busy(False)
        self._clear_progress()
        if not ok_future:
            exc, trace = result
            append_log(trace)
            messagebox.showerror("Process", str(exc), parent=self.root)
            return
        ok, message = result
        self.log_var.set(message)
        self.status_var.set(message)
        if not ok:
            messagebox.showerror("Process", message, parent=self.root)
            return
        if not self.dry_run_var.get() and self.chunks:
            self.chunks.pop(0)
        self._show_current()

    def _trim_done(self, future: Any) -> None:
        ok_future, result = self._result(future)
        self._set_busy(False)
        self._clear_progress()
        if not ok_future:
            exc, trace = result
            append_log(trace)
            messagebox.showerror("Clips", str(exc), parent=self.root)
            return
        ok, message, outputs = result
        self.log_var.set(message)
        self.status_var.set(message)
        if not ok:
            messagebox.showerror("Clips", message, parent=self.root)
            return
        existing_outputs = [path for path in outputs if path.exists()]
        if existing_outputs and self.settings.get("auto_open_created_clips", False):
            open_paths(existing_outputs)
        if not self.dry_run_var.get() and self.chunks:
            self.chunks.pop(0)
        self._show_current()

    def _close(self) -> None:
        active_processing = (
            self.processing_future is not None and not self.processing_future.done()
        )
        if (self.processing_queue or active_processing) and not messagebox.askyesno(
            "Queued processing is waiting",
            (
                f"{len(self.processing_queue)} processing job(s) have not started"
                + (" and one job is currently running. " if active_processing else ". ")
                + "Closing now discards waiting jobs; a running file operation may "
                "still finish. Keep the app open (it can be minimized) for idle "
                "processing.\n\nClose anyway?"
            ),
            parent=self.root,
        ):
            return
        self.settings["window_geometry"] = self.root.geometry()
        atomic_write_json(SETTINGS_PATH, self.settings)
        self.duration_cache.save()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.prefetch_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ReviewSortApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
