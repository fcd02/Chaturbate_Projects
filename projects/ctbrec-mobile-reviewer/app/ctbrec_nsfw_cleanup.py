#!/usr/bin/env python3
"""Idle-safe non-explicit-content scanner for CTBRec Mobile Reviewer.

This module is intentionally self-contained so the expensive ONNX/NudeNet
runtime can live in a low-priority child process instead of the phone-facing
HTTP server. It scans only direct recording files in each configured physical
model folder. A whole recording is offered for cleanup only when *every sampled
frame* is free of the configured explicit-nudity classes. Any explicit hit or
scan error excludes the entire recording from cleanup.

The final "one model mosaic" is visually continuous in the phone UI. Under the
hood it is paged into several JPEGs when necessary so iOS does not have to
decode one enormous bitmap. Those JPEGs are rendered back-to-back with no
paging interaction, and each tile carries an exact file-level hit map.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except Exception:
    Image = ImageDraw = ImageFont = ImageOps = None

VIDEO_EXTS = {
    ".mp4", ".ts", ".mpegts", ".m2ts", ".mts", ".mov", ".mkv",
    ".m4v", ".avi", ".webm",
}
TIMESTAMP_RE = re.compile(
    r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})[_T -](\d{2})[-.](\d{2})[-.](\d{2})(?!\d)"
)
MOSAIC_DIRNAME = "._non_nsfw_cleanup_mosaics"
SCAN_CACHE_FILENAME = "._non_nsfw_scan_cache.json"
REVIEWED_FILENAME = "._non_nsfw_cleanup_reviewed.json"
MANIFEST_FILENAME = "non_nsfw_cleanup_manifest.json"
SCHEMA_VERSION = 2
DEFAULT_EXPLICIT_CLASSES = [
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
]


@dataclass
class CleanupVideo:
    path: Path
    start: datetime
    size: int
    mtime: float
    duration: float
    sample_times: List[float] = field(default_factory=list)
    segment: Optional[int] = None
    timeline_start: Optional[datetime] = None

    @property
    def effective_start(self) -> datetime:
        return self.timeline_start or self.start

    @property
    def end(self) -> datetime:
        return self.effective_start + timedelta(seconds=max(0.1, float(self.duration)))


@dataclass
class CleanupChunk:
    folder: Path
    model_folder: Path
    files: List[CleanupVideo]
    start: datetime
    end: datetime
    source_bytes: int
    signature: str
    mosaics: List[Path] = field(default_factory=list)
    manifest_path: Optional[Path] = None


def normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def human_size(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} PB"


def atomic_write_json(path: Path, payload: Any) -> None:
    """Windows-friendly atomic JSON save with transient-lock retries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    last = None
    for delay in (0.0, 0.05, 0.10, 0.20, 0.40, 0.80):
        if delay:
            time.sleep(delay)
        try:
            os.replace(temp, path)
            return
        except PermissionError as exc:
            last = exc
    try:
        temp.unlink(missing_ok=True)
    except Exception:
        pass
    if last is not None:
        raise last


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_start(path: Path, mtime: float) -> datetime:
    match = TIMESTAMP_RE.search(path.name)
    if match:
        try:
            return datetime(*(int(value) for value in match.groups()))
        except Exception:
            pass
    return datetime.fromtimestamp(mtime)


SEGMENT_RE = re.compile(r"(?i)[_-]segment[_-]?(\d+)")

def parse_segment(path: Path) -> Optional[int]:
    match = SEGMENT_RE.search(path.stem)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None

def prepare_chronology(files: List[CleanupVideo]) -> None:
    """Assign a true chronological timeline, including numeric CTBRec segments."""
    groups: Dict[Tuple[str, datetime], List[CleanupVideo]] = {}
    for video in files:
        video.timeline_start = video.start
        if video.segment is not None:
            groups.setdefault((normalized(video.path.parent), video.start), []).append(video)
    for values in groups.values():
        values.sort(key=lambda v: (int(v.segment or 0), v.path.name.casefold()))
        elapsed = 0.0
        for video in values:
            video.timeline_start = video.start + timedelta(seconds=elapsed)
            elapsed += max(0.1, float(video.duration))
    files.sort(key=lambda v: (v.effective_start, int(v.segment or 0), v.path.name.casefold(), normalized(v.path)))

def direct_videos(folder: Path, extensions: Sequence[str]) -> List[Tuple[Path, os.stat_result]]:
    allowed = {str(ext).lower() for ext in extensions}
    rows: List[Tuple[Path, os.stat_result]] = []
    try:
        with os.scandir(folder) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() not in allowed:
                        continue
                    rows.append((path, entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        pass
    rows.sort(key=lambda row: (parse_start(row[0], row[1].st_mtime), parse_segment(row[0]) if parse_segment(row[0]) is not None else -1, row[0].name.casefold()))
    return rows


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def probe_duration(path: Path, ffmpeg: Path, ffprobe: Optional[Path], timeout: int = 20) -> Optional[float]:
    if ffprobe is not None and ffprobe.is_file():
        try:
            result = subprocess.run(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=timeout, creationflags=_creationflags(),
            )
            if result.returncode == 0:
                value = float(result.stdout.strip())
                if value > 0:
                    return value
        except Exception:
            pass
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=timeout, creationflags=_creationflags(),
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if match:
            value = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
            return value if value > 0 else None
    except Exception:
        pass
    return None


def sample_times(duration: float, spacing: int) -> List[float]:
    duration = max(0.1, float(duration))
    spacing = max(15, int(spacing))
    first = min(2.0, max(0.1, duration * 0.15))
    end = max(first, duration - 0.35)
    values: List[float] = []
    current = first
    while current <= end + 1e-6:
        values.append(round(min(current, end), 3))
        current += spacing
    if not values:
        values = [round(min(first, end), 3)]
    last = max(0.1, duration - 2.0)
    # A final sample closes the large blind tail left by a simple interval grid.
    if last > values[-1] + spacing * 0.55:
        values.append(round(last, 3))
    return sorted(set(values))


def extract_frame(ffmpeg: Path, source: Path, seconds: float, output: Path, width: int = 640) -> Tuple[bool, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, seconds):.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", f"scale='min({max(160, int(width))},iw)':-2",
        "-q:v", "4", "-y", str(output),
    ]
    try:
        completed = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_creationflags(), timeout=45, check=False,
        )
        if completed.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return True, ""
        return False, completed.stderr.decode("utf-8", "replace")[-1000:] or f"ffmpeg exited {completed.returncode}"
    except Exception as exc:
        return False, str(exc)


def extract_frames_batch(
    ffmpeg: Path,
    requests: Sequence[Tuple[Path, float, Path]],
    width: int = 640,
    timeout_seconds: int = 45,
) -> Tuple[set[Path], str]:
    """Extract multiple exact timestamps with one ffmpeg process.

    Every timestamp is represented by an independently seeked ffmpeg input.
    This avoids the old one-process-per-image startup overhead while preserving
    the exact sample grid used by the safety scanner. Missing outputs are
    intentionally reported to the caller so only those frames can fall back to
    the conservative single-frame extractor.
    """
    clean = [(Path(source), max(0.0, float(seconds)), Path(output)) for source, seconds, output in requests]
    if not clean:
        return set(), ""
    for _source, _seconds, output in clean:
        output.parent.mkdir(parents=True, exist_ok=True)
    command: List[str] = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-threads", "1", "-y",
    ]
    for source, seconds, _output in clean:
        command.extend(["-ss", f"{seconds:.3f}", "-i", str(source)])
    scale = f"scale='min({max(160, int(width))},iw)':-2"
    for index, (_source, _seconds, output) in enumerate(clean):
        command.extend([
            "-map", f"{index}:v:0", "-an", "-sn", "-dn",
            "-frames:v", "1", "-vf", scale, "-q:v", "4", str(output),
        ])
    timeout = max(8, int(timeout_seconds or 45) + 4 * max(0, len(clean) - 1))
    error = ""
    try:
        completed = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_creationflags(), timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", "replace")[-1000:] or f"ffmpeg exited {completed.returncode}"
    except subprocess.TimeoutExpired:
        error = f"ffmpeg batch timed out after {timeout}s"
    except Exception as exc:
        error = str(exc)
    produced = {
        output for _source, _seconds, output in clean
        if output.is_file() and output.stat().st_size > 0
    }
    return produced, error


def detector_fingerprint(settings: Dict[str, Any]) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "sample_every_seconds": int(settings.get("sample_every_seconds", 300)),
        "confidence_threshold": round(float(settings.get("confidence_threshold", 0.45)), 4),
        "borderline_confidence": round(float(settings.get("borderline_confidence", 0.20)), 4),
        "explicit_classes": sorted(str(value).upper() for value in settings.get("explicit_classes", DEFAULT_EXPLICIT_CLASSES)),
        "model": _detector_mode(settings),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def mosaic_fingerprint(settings: Dict[str, Any]) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "columns": int(settings.get("columns", 4)),
        "tile_width": int(settings.get("tile_width", 260)),
        "max_tiles_per_image": int(settings.get("max_tiles_per_image", 120)),
        "jpeg_quality": int(settings.get("jpeg_quality", 84)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def load_font(size: int) -> Any:
    if ImageFont is None:
        return None
    for name in ("Segoe UI.ttf", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _safe_tile(frame_path: Path, width: int) -> Any:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is required for non-NSFW cleanup mosaics.")
    width = max(160, int(width))
    height = max(90, round(width * 9 / 16))
    with Image.open(frame_path) as opened:
        image = opened.convert("RGB")
    contained = ImageOps.contain(image, (width, height))
    tile = Image.new("RGB", (width, height), (12, 12, 12))
    x = (width - contained.width) // 2
    y = (height - contained.height) // 2
    tile.paste(contained, (x, y))
    return tile


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def cleanup_signature(model_name: str, files: Sequence[CleanupVideo]) -> str:
    payload = {
        "model": model_name.casefold(),
        "files": [
            {
                "path": normalized(video.path), "size": int(video.size),
                "mtime": round(float(video.mtime), 3),
                "duration": round(float(video.duration), 3),
                "sample_times": [round(float(value), 3) for value in video.sample_times],
            }
            for video in files
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def manifest_to_chunk(manifest_path: Path) -> Optional[CleanupChunk]:
    raw = load_json(manifest_path, {})
    if not isinstance(raw, dict) or int(raw.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        return None
    files: List[CleanupVideo] = []
    for row in raw.get("files", []):
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("path", "")))
        try:
            start = datetime.fromisoformat(str(row.get("start", "")))
        except Exception:
            start = parse_start(path, float(row.get("mtime", time.time()) or time.time()))
        video = CleanupVideo(
            path=path,
            start=start,
            size=int(row.get("size", 0) or 0),
            mtime=float(row.get("mtime", 0.0) or 0.0),
            duration=float(row.get("duration", 0.1) or 0.1),
            sample_times=[float(value) for value in row.get("sample_times", [])],
            segment=(int(row.get("segment")) if row.get("segment") is not None else parse_segment(path)),
        )
        try:
            if row.get("timeline_start"):
                video.timeline_start = datetime.fromisoformat(str(row.get("timeline_start")))
        except Exception:
            pass
        files.append(video)
    if not files:
        return None
    outputs = [Path(str(value)) for value in raw.get("outputs", [])]
    # v2.8 manifests carry timeline_start for segmented CTBRec files. Older
    # schema-2 manifests are intentionally invalidated, so this is deterministic.
    files.sort(key=lambda video: (video.effective_start, int(video.segment or 0), video.path.name.casefold()))
    start = min(video.effective_start for video in files)
    end = max(video.end for video in files)
    model_folder = Path(str(raw.get("model_folder", manifest_path.parent.parent)))
    return CleanupChunk(
        folder=model_folder,
        model_folder=model_folder,
        files=files,
        start=start,
        end=end,
        source_bytes=sum(video.size for video in files),
        signature=str(raw.get("signature", cleanup_signature(str(raw.get("model", "")), files))),
        mosaics=outputs,
        manifest_path=manifest_path,
    )


def validate_manifest(manifest_path: Path, settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    raw = load_json(manifest_path, {})
    if not isinstance(raw, dict) or int(raw.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        return None
    if settings is not None and str(raw.get("mosaic_fingerprint", "")) != mosaic_fingerprint(settings):
        return None
    outputs = [Path(str(value)) for value in raw.get("outputs", [])]
    if not outputs or not all(path.is_file() for path in outputs):
        return None
    layout = raw.get("mobile_layout_v2")
    if not isinstance(layout, dict) or str(layout.get("mode", "")) != "cleanup":
        return None
    if len(layout.get("parts", [])) != len(outputs):
        return None
    return raw


def _save_scan_cache(path: Path, payload: Dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(path, payload)


def _scan_one_file(
    detector: Any,
    ffmpeg: Path,
    path: Path,
    stat: os.stat_result,
    duration: float,
    settings: Dict[str, Any],
    temp_root: Path,
) -> Dict[str, Any]:
    spacing = int(settings.get("sample_every_seconds", 300))
    threshold = float(settings.get("confidence_threshold", 0.45))
    borderline = min(threshold, float(settings.get("borderline_confidence", 0.20)))
    explicit_classes = {str(value).upper() for value in settings.get("explicit_classes", DEFAULT_EXPLICIT_CLASSES)}
    times = sample_times(duration, spacing)
    batch_limit = 4 if _detector_mode(settings) == "640m" else 24
    batch_size = max(1, min(batch_limit, int(settings.get("batch_size", 3))))
    explicit_hits: List[Dict[str, Any]] = []
    borderline_hits: List[Dict[str, Any]] = []
    completed_times: List[float] = []
    frame_counter = 0
    for offset in range(0, len(times), batch_size):
        batch_times = times[offset:offset + batch_size]
        frame_paths: List[Path] = []
        valid_times: List[float] = []
        requested: List[Tuple[Path, float, Path]] = []
        for seconds in batch_times:
            frame_counter += 1
            frame = temp_root / f"detect_{hashlib.sha1(normalized(path).encode()).hexdigest()[:10]}_{frame_counter:05d}.jpg"
            frame_paths.append(frame)
            valid_times.append(seconds)
            requested.append((path, seconds, frame))
        frame_batch_size = max(1, min(12, int(settings.get("frame_extract_batch_size", 6) or 6)))
        produced: set[Path] = set()
        batch_error = ""
        for extract_offset in range(0, len(requested), frame_batch_size):
            batch_requested = requested[extract_offset:extract_offset + frame_batch_size]
            batch_produced, current_error = extract_frames_batch(
                ffmpeg, batch_requested, width=640,
                timeout_seconds=int(settings.get("frame_extract_timeout_seconds", 45) or 45),
            )
            produced.update(batch_produced)
            if current_error:
                batch_error = current_error
        for seconds, frame in zip(valid_times, frame_paths):
            if frame in produced:
                continue
            ok, error = extract_frame(ffmpeg, path, seconds, frame, width=640)
            if not ok:
                return {
                    "classification": "uncertain",
                    "reason": f"Could not extract sample at {_clock(seconds)}: {error or batch_error}",
                    "duration": duration,
                    "sample_times": completed_times,
                    "explicit_hits": explicit_hits,
                }
        try:
            detections = detector.detect_batch([str(value) for value in frame_paths])
        except Exception as exc:
            return {
                "classification": "uncertain", "reason": f"NudeNet inference failed: {exc}",
                "duration": duration, "sample_times": completed_times, "explicit_hits": explicit_hits,
            }
        if not isinstance(detections, list) or len(detections) != len(frame_paths):
            return {
                "classification": "uncertain", "reason": "NudeNet returned an unexpected batch result.",
                "duration": duration, "sample_times": completed_times, "explicit_hits": explicit_hits,
            }
        for seconds, rows in zip(valid_times, detections):
            completed_times.append(float(seconds))
            for detection in rows if isinstance(rows, list) else []:
                if not isinstance(detection, dict):
                    continue
                class_name = str(detection.get("class", "")).upper()
                score = float(detection.get("score", 0.0) or 0.0)
                if class_name in explicit_classes and score >= threshold:
                    explicit_hits.append({
                        "time": float(seconds), "class": class_name, "score": round(score, 4),
                    })
                elif class_name in explicit_classes and score >= borderline:
                    borderline_hits.append({
                        "time": float(seconds), "class": class_name, "score": round(score, 4),
                    })
        for frame in frame_paths:
            try:
                frame.unlink()
            except OSError:
                pass
        if explicit_hits:
            # One explicit frame disqualifies the entire recording, so there is
            # no value in burning more CPU on later intervals of the same file.
            break
    classification = "explicit" if explicit_hits else ("uncertain" if borderline_hits else "safe")
    reason = (
        "Explicit nudity detected." if explicit_hits else
        "Borderline explicit-nudity detection; excluded from cleanup for safety." if borderline_hits else
        "No configured explicit-nudity class was detected in sampled frames."
    )
    return {
        "classification": classification,
        "reason": reason,
        "duration": duration,
        "sample_times": times if classification == "safe" else completed_times,
        "explicit_hits": explicit_hits,
        "borderline_hits": borderline_hits,
        "scanned_samples": len(completed_times),
    }


def _compose_mosaic(
    model_name: str,
    files: Sequence[CleanupVideo],
    primary_folder: Path,
    ffmpeg: Path,
    settings: Dict[str, Any],
    status: "StatusWriter",
) -> Tuple[List[Path], Path]:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required. Run install_mobile_reviewer.bat.")
    output_dir = primary_folder / MOSAIC_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)
    signature = cleanup_signature(model_name, files)
    manifest_path = output_dir / MANIFEST_FILENAME
    existing = validate_manifest(manifest_path, settings)
    if existing is not None and str(existing.get("signature", "")) == signature:
        return [Path(str(value)) for value in existing.get("outputs", [])], manifest_path

    # Remove only artifacts owned by this feature.
    for stale in list(output_dir.glob("non_nsfw_cleanup_*.jpg")) + [manifest_path]:
        try:
            if stale.is_file(): stale.unlink()
        except OSError:
            pass

    plan: List[Tuple[int, CleanupVideo, float]] = []
    for file_index, video in enumerate(files):
        for seconds in video.sample_times:
            plan.append((file_index, video, float(seconds)))
    plan.sort(key=lambda item: (item[1].effective_start + timedelta(seconds=float(item[2])), item[0], item[1].path.name.casefold(), item[2]))
    if not plan:
        raise RuntimeError("No non-explicit sample frames were available for the model mosaic.")

    columns = max(1, min(8, int(settings.get("columns", 4))))
    tile_width = max(160, min(800, int(settings.get("tile_width", 260))))
    tile_height = max(90, round(tile_width * 9 / 16))
    header_height = max(38, tile_width // 7)
    max_tiles = max(columns, min(240, int(settings.get("max_tiles_per_image", 120))))
    quality = max(55, min(95, int(settings.get("jpeg_quality", 84))))
    font = load_font(max(13, tile_width // 22))
    small_font = load_font(max(11, tile_width // 28))
    outputs: List[Path] = []
    layout_parts: List[Dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ctbrec_non_nsfw_mosaic_") as temp_raw:
        temp_root = Path(temp_raw)
        for part_index, offset in enumerate(range(0, len(plan), max_tiles), start=1):
            part = plan[offset:offset + max_tiles]
            rows = math.ceil(len(part) / columns)
            canvas = Image.new("RGB", (columns * tile_width, header_height + rows * tile_height), (8, 8, 8))
            draw = ImageDraw.Draw(canvas, "RGBA")
            header = f"{model_name} • non-NSFW cleanup candidates • {len(files)} files • part {part_index}/{math.ceil(len(plan)/max_tiles)}"
            draw.text((8, 8), header, fill=(255, 255, 255), font=font)
            tiles_meta: List[Dict[str, Any]] = []

            # v2.13: build the JPEG source frames in bounded batches before
            # composition. This replaces the old one-ffmpeg-process-per-tile
            # loop while retaining exact independently-seeked timestamps.
            frame_rows: List[Tuple[int, int, CleanupVideo, float, Path]] = [
                (local_index, file_index, video, seconds, temp_root / f"mosaic_{offset + local_index:06d}.jpg")
                for local_index, (file_index, video, seconds) in enumerate(part)
            ]
            grouped: Dict[str, List[Tuple[int, int, CleanupVideo, float, Path]]] = {}
            for row_data in frame_rows:
                grouped.setdefault(normalized(row_data[2].path), []).append(row_data)
            batch_size = max(1, min(12, int(settings.get("frame_extract_batch_size", 6) or 6)))
            frame_timeout = max(5, int(settings.get("frame_extract_timeout_seconds", 45) or 45))
            prepared = 0
            for source_rows in grouped.values():
                for batch_offset in range(0, len(source_rows), batch_size):
                    batch = source_rows[batch_offset:batch_offset + batch_size]
                    status.write(
                        f"Building cleanup mosaic: extracting {len(batch)} frame(s) in one ffmpeg process for {model_name}",
                        offset + prepared, len(plan), phase="mosaic",
                    )
                    produced, batch_error = extract_frames_batch(
                        ffmpeg,
                        [(video.path, seconds, frame) for _local, _file_index, video, seconds, frame in batch],
                        width=max(640, tile_width), timeout_seconds=frame_timeout,
                    )
                    for _local, _file_index, video, seconds, frame in batch:
                        if frame not in produced:
                            ok, error = extract_frame(ffmpeg, video.path, seconds, frame, width=max(640, tile_width))
                            if not ok:
                                # Deliberately leave this frame missing. The
                                # composer below renders a visible FRAME ERROR
                                # tile instead of dropping or reassigning it.
                                _ = error or batch_error
                        prepared += 1
                        status.write(
                            f"Building cleanup mosaic: prepared frame {offset + prepared}/{len(plan)} for {model_name}",
                            offset + prepared, len(plan), phase="mosaic",
                        )

            for local_index, file_index, video, seconds, frame in frame_rows:
                if not frame.is_file():
                    # A missing frame must not cause the file itself to be split.
                    # Represent it visibly instead of silently dropping the tile.
                    tile = Image.new("RGB", (tile_width, tile_height), (45, 20, 20))
                    td = ImageDraw.Draw(tile)
                    td.text((8, 8), "FRAME ERROR", fill=(255, 255, 255), font=font)
                else:
                    tile = _safe_tile(frame, tile_width)
                td = ImageDraw.Draw(tile, "RGBA")
                label = f"#{file_index + 1}  {_clock(seconds)}"
                box = td.textbbox((0, 0), label, font=font)
                td.rectangle((0, 0, min(tile_width, box[2] + 16), max(28, box[3] + 12)), fill=(0, 0, 0, 190))
                td.text((7, 5), label, fill=(255, 255, 255), font=font)
                short_name = video.path.name
                if len(short_name) > 42:
                    short_name = short_name[:39] + "…"
                td.text((7, tile_height - max(22, tile_height // 8)), short_name, fill=(255, 255, 255), font=small_font,
                        stroke_width=2, stroke_fill=(0, 0, 0))
                row, column = divmod(local_index, columns)
                left = column * tile_width
                top = header_height + row * tile_height
                canvas.paste(tile, (left, top))
                tiles_meta.append({
                    "file_index": int(file_index),
                    "left_px": int(left), "top_px": int(top),
                    "width_px": int(tile_width), "height_px": int(tile_height),
                    "sample_seconds": float(seconds),
                    "sample_global_time": (video.effective_start + timedelta(seconds=float(seconds))).isoformat(),
                    "display_number": int(file_index + 1),
                })
                try:
                    frame.unlink()
                except OSError:
                    pass
            output = output_dir / f"non_nsfw_cleanup_{signature[:16]}_part{part_index:03d}.jpg"
            canvas.save(output, quality=quality, optimize=True)
            outputs.append(output)
            layout_parts.append({
                "index": part_index - 1, "output": str(output),
                "width": canvas.width, "height": canvas.height, "tiles": tiles_meta,
            })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "model": model_name,
        "model_folder": str(primary_folder),
        "signature": signature,
        "detector_fingerprint": detector_fingerprint(settings),
        "mosaic_fingerprint": mosaic_fingerprint(settings),
        "candidate_bytes": sum(video.size for video in files),
        "candidate_size": human_size(sum(video.size for video in files)),
        "sample_count": sum(len(video.sample_times) for video in files),
        "files": [
            {
                "path": str(video.path), "start": video.start.isoformat(), "timeline_start": video.effective_start.isoformat(),
                "segment": video.segment, "size": int(video.size),
                "mtime": float(video.mtime), "duration": float(video.duration),
                "sample_times": [float(value) for value in video.sample_times],
            }
            for video in files
        ],
        "outputs": [str(path) for path in outputs],
        "mobile_layout_v2": {
            "version": 2, "mode": "cleanup", "signature": signature, "parts": layout_parts,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_json(manifest_path, manifest)
    return outputs, manifest_path


class StatusWriter:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path

    def write(self, message: str, current: int = 0, total: int = 0, **extra: Any) -> None:
        if self.path is None:
            return
        payload = {
            "message": str(message), "current": int(current), "total": int(total),
            "updated_at": datetime.now().isoformat(timespec="seconds"), **extra,
        }
        try:
            atomic_write_json(self.path, payload)
        except Exception:
            pass


NUDENET_640M_URL = "https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx"
# The upstream release is currently age/sign-in gated for some unauthenticated
# clients. Keep it as the authoritative first source, then fall back to a
# pinned Hugging Face copy that is known to load with NudeNet 3.4.2.
NUDENET_640M_FALLBACK_URL = (
    "https://huggingface.co/spaces/xxparthparekhxx/NudeNet-FastAPI/resolve/"
    "794a185a301917f1a3505ab3b8d55b268ea81f0e/640m.onnx?download=true"
)
NUDENET_640M_FALLBACK_URL_2 = (
    "https://huggingface.co/spaces/Nymbo/Nudity-Censor/resolve/"
    "7955acb6e51995555fe4e5f2fcde7437004053a0/640m.onnx?download=true"
)
NUDENET_640M_FALLBACK_SHA256 = "04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb"
NUDENET_640M_EXPECTED_SIZE = 103_538_690
NUDENET_MODEL_DIR = Path(__file__).resolve().parent / "models"
NUDENET_640M_PATH = NUDENET_MODEL_DIR / "nudenet_640m.onnx"

def _detector_mode(settings: Dict[str, Any]) -> str:
    value = str(settings.get("detector_model", "640m")).strip().casefold()
    return "320n" if value in {"320n", "fast", "fast_320n"} else "640m"

def _looks_like_html_download(path: Path) -> bool:
    try:
        head = path.read_bytes()[:4096].lstrip().lower()
    except Exception:
        return True
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<title>sign in to github" in head


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_640m_model(path: Path, expected_sha256: str = "") -> Tuple[bool, str]:
    """Validate a candidate NudeNet 640m model.

    The published 640m weight has a stable SHA-256.  That cryptographic
    identity is the authoritative validation signal.  v2.8.1 incorrectly
    rejected this *exact* model when ONNX Runtime exposed a dynamic/non-literal
    input shape that did not contain the integer 640.  Do not second-guess a
    file whose hash matches the known published weight.

    For an unknown manually supplied ONNX file we still perform conservative
    size/HTML checks and, when ONNX Runtime is available, verify that it opens
    and has a plausible four-dimensional image input.
    """
    try:
        size = int(path.stat().st_size)
    except Exception as exc:
        return False, f"model file could not be stat'ed: {exc}"
    if size < 90_000_000:
        return False, f"download was only {human_size(size)} (expected about 104 MB)"
    if _looks_like_html_download(path):
        return False, "download is an HTML/login page, not an ONNX model"

    actual = _sha256_file(path)
    known_hash = NUDENET_640M_FALLBACK_SHA256.casefold()
    if expected_sha256 and actual.casefold() != expected_sha256.casefold():
        return False, f"SHA-256 mismatch ({actual})"
    if actual.casefold() == known_hash:
        return True, "known NudeNet 640m SHA-256 verified"

    # Unknown/manual model: verify it at least parses as a plausible image
    # detector.  Dynamic dimensions such as 'height'/'width' are valid and
    # must not be rejected merely because the literal integer 640 is absent.
    try:
        import onnxruntime as ort
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if not inputs:
            return False, "ONNX model has no inputs"
        shape = list(inputs[0].shape or [])
        if len(shape) != 4:
            return False, f"ONNX input does not look like a 4-D image tensor: {shape}"
        channels = shape[1] if len(shape) > 1 else None
        if isinstance(channels, int) and channels not in {3, 4}:
            return False, f"ONNX image input has unexpected channel count: {shape}"
    except ImportError:
        # If ONNX Runtime is missing, NudeNet will surface that dependency
        # error when instantiated.  A large non-HTML manual ONNX is allowed
        # through here rather than causing an endless re-download loop.
        pass
    except Exception as exc:
        return False, f"ONNX validation failed: {exc}"
    return True, f"plausible manual ONNX ({actual})"


def _download_model_source(url: str, temp: Path, status: StatusWriter, label: str, expected_sha256: str = "") -> Tuple[bool, str]:
    """Download one candidate source with retries and strong validation."""
    last = "unknown download failure"
    for attempt in range(1, 4):
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        status.write(f"Downloading NudeNet Accurate 640m ({label}, attempt {attempt}/3)…", 0, 0, phase="model-download")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 CTBRec-Mobile-Reviewer/2.8.2",
                "Accept": "application/octet-stream,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response, temp.open("wb") as handle:
                content_type = str(response.headers.get("Content-Type", "")).casefold()
                total = int(response.headers.get("Content-Length", "0") or 0)
                done = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    status.write(
                        f"Downloading Accurate 640m ({label})… {human_size(done)}" + (f" / {human_size(total)}" if total else ""),
                        done, total, phase="model-download",
                    )
            ok, detail = _validate_640m_model(temp, expected_sha256=expected_sha256)
            if ok:
                return True, "ok"
            last = detail
            # HTML/login responses are deterministic for this source (the
            # upstream GitHub release is sign-in gated for some clients). Do
            # not waste two more attempts downloading the same login page.
            if "text/html" in content_type or _looks_like_html_download(temp):
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass
                return False, "server returned HTML/login content instead of the ONNX model"
            if expected_sha256 and "SHA-256 mismatch" in detail:
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass
                return False, detail
        except Exception as exc:
            last = str(exc)
        if attempt < 3:
            time.sleep(1.5 * attempt)
    try:
        temp.unlink(missing_ok=True)
    except Exception:
        pass
    return False, last


def _manual_640m_candidates() -> List[Path]:
    """Return sensible manually supplied model locations, in priority order."""
    app_dir = Path(__file__).resolve().parent
    candidates = [
        NUDENET_640M_PATH,
        NUDENET_MODEL_DIR / "640m.onnx",
        app_dir / "640m.onnx",
        app_dir / "nudenet_640m.onnx",
    ]
    out: List[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate.resolve(strict=False)).casefold()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _adopt_manual_640m(status: StatusWriter) -> Optional[Path]:
    """Adopt a valid manually placed 640m.onnx before downloading anything."""
    NUDENET_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for candidate in _manual_640m_candidates():
        if not candidate.is_file():
            continue
        ok, detail = _validate_640m_model(candidate)
        if not ok:
            if candidate == NUDENET_640M_PATH:
                status.write(f"Existing Accurate 640m model is invalid ({detail}).", 0, 0, phase="model-download")
            continue
        if candidate != NUDENET_640M_PATH:
            temp_target = NUDENET_640M_PATH.with_name(NUDENET_640M_PATH.name + f".{os.getpid()}.adopt")
            try:
                shutil.copy2(candidate, temp_target)
                os.replace(temp_target, NUDENET_640M_PATH)
            finally:
                try:
                    temp_target.unlink(missing_ok=True)
                except Exception:
                    pass
        status.write(
            f"NudeNet Accurate 640m ready from local file ({human_size(NUDENET_640M_PATH.stat().st_size)}; {detail}).",
            NUDENET_640M_PATH.stat().st_size,
            NUDENET_640M_PATH.stat().st_size,
            phase="model-download",
        )
        return NUDENET_640M_PATH
    return None


def ensure_640m_model(status: StatusWriter) -> Path:
    # Prefer a valid local copy in any supported filename/location.  This is
    # intentionally done before network access so a user can simply drop
    # 640m.onnx into either the app folder or models folder.
    adopted = _adopt_manual_640m(status)
    if adopted is not None:
        return adopted

    # If the canonical target exists but is invalid, quarantine it only after
    # checking alternate manual locations.
    if NUDENET_640M_PATH.is_file():
        ok, detail = _validate_640m_model(NUDENET_640M_PATH)
        if not ok:
            status.write(f"Existing Accurate 640m model is invalid ({detail}); downloading a clean copy…", 0, 0, phase="model-download")
            try:
                bad = NUDENET_640M_PATH.with_name(f"{NUDENET_640M_PATH.name}.invalid-{int(time.time())}")
                os.replace(NUDENET_640M_PATH, bad)
            except Exception:
                try:
                    NUDENET_640M_PATH.unlink(missing_ok=True)
                except Exception:
                    pass

    NUDENET_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    temp = NUDENET_640M_PATH.with_name(NUDENET_640M_PATH.name + f".{os.getpid()}.download")
    sources = [
        (NUDENET_640M_URL, "official GitHub release", ""),
        (NUDENET_640M_FALLBACK_URL, "verified pinned mirror A", NUDENET_640M_FALLBACK_SHA256),
        (NUDENET_640M_FALLBACK_URL_2, "verified pinned mirror B", NUDENET_640M_FALLBACK_SHA256),
    ]
    failures: List[str] = []
    for url, label, expected_hash in sources:
        ok, detail = _download_model_source(url, temp, status, label, expected_sha256=expected_hash)
        if ok:
            os.replace(temp, NUDENET_640M_PATH)
            status.write(
                f"NudeNet Accurate 640m ready ({human_size(NUDENET_640M_PATH.stat().st_size)}; {label}).",
                NUDENET_640M_PATH.stat().st_size,
                NUDENET_640M_PATH.stat().st_size,
                phase="model-download",
            )
            return NUDENET_640M_PATH
        failures.append(f"{label}: {detail}")
    raise RuntimeError(
        "Could not obtain a valid NudeNet Accurate 640m model. "
        + " | ".join(failures)
        + ". You can temporarily select Fast 320n, or manually place a valid 640m.onnx at "
        + str(NUDENET_640M_PATH)
    )

def create_detector(settings: Dict[str, Any], status: StatusWriter) -> Any:
    try:
        from nudenet import NudeDetector
    except Exception as exc:
        raise RuntimeError("NudeNet is not installed or could not load. Run install_mobile_reviewer.bat, then restart the server. " + f"Details: {exc}") from exc
    mode = _detector_mode(settings)
    if mode == "320n":
        status.write("Loading NudeNet Fast 320n…", 0, 0, phase="model-load")
        return NudeDetector()
    model_path = ensure_640m_model(status)
    status.write("Loading NudeNet Accurate 640m…", 0, 0, phase="model-load")
    return NudeDetector(model_path=str(model_path), inference_resolution=640)

def run_request(request: Dict[str, Any], status: StatusWriter, detector: Any = None) -> Dict[str, Any]:
    model_name = str(request.get("model", "")).strip()
    folders = [Path(str(value)) for value in request.get("folders", [])]
    folders = [folder for folder in folders if folder.is_dir()]
    settings = dict(request.get("settings", {}))
    ffmpeg = Path(str(request.get("ffmpeg", "")))
    ffprobe_raw = str(request.get("ffprobe", "")).strip()
    ffprobe = Path(ffprobe_raw) if ffprobe_raw else ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not ffmpeg.is_file():
        raise RuntimeError("ffmpeg was not found for the non-NSFW scanner.")
    if not ffprobe.is_file():
        found = shutil.which("ffprobe.exe" if os.name == "nt" else "ffprobe")
        ffprobe = Path(found) if found else None

    if detector is None:
        detector = create_detector(settings, status)
    detection_key = detector_fingerprint(settings)
    extensions = [str(value).lower() for value in settings.get("extensions", [".mp4", ".ts"])]
    if not extensions:
        extensions = [".mp4", ".ts"]
    grace = max(0, int(settings.get("recent_write_grace_seconds", 120)))
    now = time.time()

    physical_rows: List[Dict[str, Any]] = []
    total_files = sum(len(direct_videos(folder, extensions)) for folder in folders)
    processed = 0
    safe_files: List[CleanupVideo] = []
    explicit_files = 0
    uncertain_files = 0
    reused_files = 0

    with tempfile.TemporaryDirectory(prefix="ctbrec_non_nsfw_scan_") as temp_raw:
        temp_root = Path(temp_raw)
        for folder in folders:
            cache_path = folder / SCAN_CACHE_FILENAME
            reviewed_path = folder / REVIEWED_FILENAME
            reviewed_raw = load_json(reviewed_path, {"version": 1, "files": {}})
            reviewed_files = reviewed_raw.get("files", {}) if isinstance(reviewed_raw, dict) else {}
            if not isinstance(reviewed_files, dict):
                reviewed_files = {}
            raw_cache = load_json(cache_path, {"schema_version": SCHEMA_VERSION, "files": {}})
            if not isinstance(raw_cache, dict):
                raw_cache = {"schema_version": SCHEMA_VERSION, "files": {}}
            cache_files = raw_cache.setdefault("files", {})
            folder_safe_bytes = 0
            folder_safe_count = 0
            for path, stat in direct_videos(folder, extensions):
                processed += 1
                status.write(f"{model_name}: analyzing {processed}/{max(1,total_files)} • {path.name}", processed - 1, total_files, phase="scan")
                if grace and now - float(stat.st_mtime) < grace:
                    # Active recordings are not candidates and should be retried later.
                    uncertain_files += 1
                    continue
                key = normalized(path)
                reviewed_entry = reviewed_files.get(key)
                if isinstance(reviewed_entry, dict):
                    try:
                        if int(reviewed_entry.get("size", -1)) == int(stat.st_size) and abs(float(reviewed_entry.get("mtime", -1)) - float(stat.st_mtime)) < 0.001:
                            # The user previously reviewed this unchanged safe file
                            # and intentionally left it in place. Do not nag them
                            # again until the recording itself changes.
                            continue
                    except Exception:
                        pass
                cached = cache_files.get(key)
                valid_cache = (
                    isinstance(cached, dict)
                    and int(cached.get("size", -1)) == int(stat.st_size)
                    and abs(float(cached.get("mtime", -1)) - float(stat.st_mtime)) < 0.001
                    and str(cached.get("detector_fingerprint", "")) == detection_key
                )
                if valid_cache:
                    result = dict(cached)
                    reused_files += 1
                else:
                    duration = probe_duration(path, ffmpeg, ffprobe)
                    if not duration:
                        result = {
                            "classification": "uncertain", "reason": "Could not determine recording duration.",
                            "duration": 0.0, "sample_times": [], "explicit_hits": [],
                        }
                    else:
                        result = _scan_one_file(detector, ffmpeg, path, stat, duration, settings, temp_root)
                    result.update({
                        "path": str(path), "size": int(stat.st_size), "mtime": float(stat.st_mtime),
                        "detector_fingerprint": detection_key,
                        "scanned_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    cache_files[key] = result
                    # Persist after every recording so terminating the low-priority
                    # worker for phone activity never loses completed inference.
                    raw_cache["schema_version"] = SCHEMA_VERSION
                    raw_cache["detector_fingerprint"] = detection_key
                    _save_scan_cache(cache_path, raw_cache)
                classification = str(result.get("classification", "uncertain"))
                if classification == "safe":
                    duration = float(result.get("duration", 0.1) or 0.1)
                    video = CleanupVideo(
                        path=path,
                        start=parse_start(path, float(stat.st_mtime)),
                        size=int(stat.st_size), mtime=float(stat.st_mtime), duration=duration,
                        sample_times=[float(value) for value in result.get("sample_times", sample_times(duration, int(settings.get("sample_every_seconds", 180))))],
                        segment=parse_segment(path),
                    )
                    safe_files.append(video)
                    folder_safe_bytes += int(stat.st_size)
                    folder_safe_count += 1
                elif classification == "explicit":
                    explicit_files += 1
                else:
                    uncertain_files += 1
                status.write(f"{model_name}: analyzed {processed}/{max(1,total_files)}", processed, total_files, phase="scan")
            physical_rows.append({
                "folder": str(folder), "safe_bytes": folder_safe_bytes, "safe_files": folder_safe_count,
            })

    prepare_chronology(safe_files)
    candidate_bytes = sum(video.size for video in safe_files)
    if not safe_files:
        return {
            "model": model_name, "candidate_files": 0, "candidate_bytes": 0, "candidate_size": "0 B",
            "sample_count": 0, "explicit_files": explicit_files, "uncertain_files": uncertain_files,
            "reused_files": reused_files, "manifest": "", "outputs": [],
            "folders": [str(folder) for folder in folders], "physical": physical_rows,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }

    # Put the aggregate model mosaic on the physical folder contributing the
    # most cleanup bytes so the cache normally lives beside the dominant data.
    primary_row = max(physical_rows, key=lambda row: int(row.get("safe_bytes", 0)))
    primary_folder = Path(str(primary_row["folder"]))
    status.write(f"{model_name}: building one continuous cleanup mosaic…", 0, sum(len(v.sample_times) for v in safe_files), phase="mosaic")
    outputs, manifest = _compose_mosaic(model_name, safe_files, primary_folder, ffmpeg, settings, status)
    return {
        "model": model_name,
        "candidate_files": len(safe_files), "candidate_bytes": candidate_bytes,
        "candidate_size": human_size(candidate_bytes),
        "sample_count": sum(len(video.sample_times) for video in safe_files),
        "explicit_files": explicit_files, "uncertain_files": uncertain_files,
        "reused_files": reused_files,
        "manifest": str(manifest), "outputs": [str(path) for path in outputs],
        "folders": [str(folder) for folder in folders], "physical": physical_rows,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }



def run_batch_request(request: Dict[str, Any], status: StatusWriter, rolling_path: Optional[Path] = None) -> Dict[str, Any]:
    """Scan a full largest-first model pass with one NudeNet model load.

    The rolling file is atomically refreshed after each model so the phone-facing
    server can expose completed cleanup mosaics immediately and can safely kill
    this low-priority process when interactive work arrives.
    """
    models = [row for row in request.get("models", []) if isinstance(row, dict)]
    shared = {
        "settings": dict(request.get("settings", {})),
        "ffmpeg": str(request.get("ffmpeg", "")),
        "ffprobe": str(request.get("ffprobe", "")),
    }
    detector = create_detector(shared["settings"], status)
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for model_index, row in enumerate(models, start=1):
        model = str(row.get("model", "")).strip()
        status.write(
            f"Model {model_index}/{len(models)}: {model}", model_index - 1, len(models),
            phase="model", model=model, model_index=model_index, total_models=len(models),
        )
        sub_request = {**shared, "model": model, "folders": list(row.get("folders", []))}
        try:
            result = run_request(sub_request, status, detector=detector)
            result["source_bytes"] = int(row.get("source_bytes", 0) or 0)
            result["source_size"] = human_size(result["source_bytes"])
            result["drives"] = list(row.get("drives", []))
            result["pass_model_index"] = model_index
            results.append(result)
        except Exception as exc:
            errors.append({"model": model, "error": str(exc), "pass_model_index": model_index})
        if rolling_path is not None:
            atomic_write_json(rolling_path, {
                "ok": True, "complete": False, "results": results, "errors": errors,
                "processed_models": model_index, "total_models": len(models),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
    return {"results": results, "errors": errors, "processed_models": len(models), "total_models": len(models)}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--rolling", default="")
    args = parser.parse_args()
    request_path = Path(args.request)
    result_path = Path(args.result)
    status = StatusWriter(Path(args.status))
    try:
        request = load_json(request_path, {})
        if not isinstance(request, dict):
            raise RuntimeError("Worker request was invalid.")
        rolling = Path(args.rolling) if args.rolling else None
        if isinstance(request.get("models"), list):
            result = run_batch_request(request, status, rolling_path=rolling)
        else:
            result = run_request(request, status)
        atomic_write_json(result_path, {"ok": True, "result": result})
        status.write("Complete.", 1, 1, phase="done")
        return 0
    except Exception as exc:
        import traceback
        atomic_write_json(result_path, {"ok": False, "error": str(exc), "traceback": traceback.format_exc()})
        status.write(f"Failed: {exc}", 0, 0, phase="error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
