#!/usr/bin/env python3
"""Keep-Last planning helpers for CTBRec Mobile Reviewer.

Preserves the established CTBRec sorter semantics:
- rules are ``model_name:minutes`` in keeplasts.txt;
- non-segmented recordings are split into contiguous blocks using the configured gap;
- segmented recordings sharing a base timestamp are treated as one recording group;
- newest COMPLETE files are retained until at least the requested duration is reached;
- retained files are planned for the model's Review folder;
- older complete files are planned for drive-level MARKED_FOR_DELETION/<model>;
- recent/in-progress and timestamp-unparseable files are left untouched.

This module only PLANS work. The mobile server performs the actual moves through
its durable PC action queue.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

VIDEO_EXTS = {'.mp4', '.m4v', '.mkv', '.ts', '.mpegts', '.m2ts', '.mts', '.mov', '.avi', '.webm'}
TAIL_RE = re.compile(r'[_-](?:last_seg[_-])?tail[_-](\d+)m(\d+)s', re.I)
DUP_RE = re.compile(r'(?i)_dup\d+$')
SEG_DOT_RE = re.compile(r'(\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}).*?[_-]segment[_-](\d+)', re.I)
DOT_RE = re.compile(r'(\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2})')
DASH_RE = re.compile(r'_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_')


@dataclass
class Row:
    model: str
    folder: Path
    path: Path
    start: datetime
    segment: Optional[int]
    size: int
    mtime: float
    duration: float = 0.0


def canonical_model(name: str) -> str:
    return DUP_RE.sub('', str(name or '').strip()).casefold()


def display_model(name: str) -> str:
    return DUP_RE.sub('', str(name or '').strip())


def load_rules(path: Path) -> Dict[str, float]:
    rules: Dict[str, float] = {}
    if not path.is_file():
        return rules
    for raw in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or ':' not in line:
            continue
        name, value = line.rsplit(':', 1)
        try:
            minutes = float(value.strip())
        except Exception:
            continue
        key = canonical_model(name)
        if key and minutes > 0:
            rules[key] = minutes
    return rules


def parse_time(path: Path) -> Tuple[Optional[datetime], Optional[int]]:
    """Match the established KeepLast filename timestamp parser.

    Do NOT fall back to mtime here. A file whose recording timestamp cannot be
    parsed is deliberately protected/left untouched.
    """
    name = path.name
    match = SEG_DOT_RE.search(name)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y.%m.%d_%H.%M.%S'), int(match.group(2))
        except Exception:
            pass
    match = DOT_RE.search(name)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y.%m.%d_%H.%M.%S'), None
        except Exception:
            pass
    match = DASH_RE.search(name)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y-%m-%d_%H-%M-%S'), None
        except Exception:
            pass
    return None, None


def labelled_duration(path: Path) -> Optional[float]:
    match = TAIL_RE.search(path.stem)
    if not match:
        return None
    try:
        return float(int(match.group(1)) * 60 + int(match.group(2)))
    except Exception:
        return None


def _estimate_durations(
    rows: Sequence[Row],
    lookup: Optional[Callable[[Path, int, float], Optional[float]]] = None,
) -> None:
    """Use cached exact duration, tail label, then the established size estimate."""
    unresolved: List[Row] = []
    for row in rows:
        value: Optional[float] = None
        if lookup:
            try:
                value = lookup(row.path, row.size, row.mtime)
            except Exception:
                value = None
        if not value:
            value = labelled_duration(row.path)
        if value and float(value) > 0:
            row.duration = float(value)
        else:
            unresolved.append(row)
    if not unresolved:
        return
    largest = max((r.size for r in unresolved), default=0)
    bytes_per_second = largest / 900.0 if largest > 0 else 0.0
    for row in unresolved:
        estimate = row.size / bytes_per_second if bytes_per_second > 0 else 900.0
        row.duration = max(1.0, min(900.0, float(estimate)))


def _nonseg_blocks(rows: Sequence[Row], gap_minutes: float) -> List[List[Row]]:
    ordered = sorted(rows, key=lambda r: (r.start, r.path.name.casefold()))
    gap = timedelta(minutes=max(0.1, float(gap_minutes)))
    blocks: List[List[Row]] = []
    current: List[Row] = []
    for row in ordered:
        if not current or row.start - current[-1].start <= gap:
            current.append(row)
        else:
            blocks.append(current)
            current = [row]
    if current:
        blocks.append(current)
    return blocks


def _plan_nonseg_block(block: Sequence[Row], keep_seconds: float) -> Tuple[List[Row], List[Row]]:
    if not block:
        return [], []
    durations = [max(1.0, float(row.duration)) for row in block]
    if sum(durations) < keep_seconds:
        keep_index = 0
    else:
        cumulative = 0.0
        keep_index = len(block) - 1
        for index in reversed(range(len(block))):
            cumulative += durations[index]
            keep_index = index
            if cumulative >= keep_seconds:
                break
    return list(block[keep_index:]), list(block[:keep_index])


def _plan_segment_group(rows: Sequence[Row], keep_seconds: float) -> Tuple[List[Row], List[Row]]:
    # Latest segment number is newest; keep whole segment files until threshold.
    ordered = sorted(rows, key=lambda r: (int(r.segment or 0), r.path.name.casefold()), reverse=True)
    durations = [max(1.0, float(row.duration)) for row in ordered]
    if sum(durations) < keep_seconds:
        keep_count = len(ordered)
    else:
        cumulative = 0.0
        keep_count = 0
        for row in ordered:
            cumulative += max(1.0, float(row.duration))
            keep_count += 1
            if cumulative >= keep_seconds:
                break
    return ordered[:keep_count], ordered[keep_count:]


def build_plan(
    roots: Iterable[Path],
    rules: Dict[str, float],
    *,
    gap_minutes: float = 30,
    recent_write_grace_seconds: float = 120,
    duration_lookup: Optional[Callable[[Path, int, float], Optional[float]]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    now_ts = float(now if now is not None else time.time())
    by_model: Dict[str, List[Row]] = {key: [] for key in rules}
    display: Dict[str, str] = {}
    skipped_recent = 0
    skipped_no_timestamp = 0
    scan_errors: List[str] = []

    for root in roots:
        root = Path(root)
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            scan_errors.append(f'{root}: {exc}')
            continue
        for folder in entries:
            if not folder.is_dir() or folder.name.casefold() == 'marked_for_deletion':
                continue
            key = canonical_model(folder.name)
            if key not in rules:
                continue
            display.setdefault(key, display_model(folder.name))
            try:
                children = list(folder.iterdir())
            except OSError as exc:
                scan_errors.append(f'{folder}: {exc}')
                continue
            for path in children:
                if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTS:
                    continue
                try:
                    st = path.stat()
                except OSError as exc:
                    scan_errors.append(f'{path}: {exc}')
                    continue
                if (now_ts - float(st.st_mtime)) < float(recent_write_grace_seconds):
                    skipped_recent += 1
                    continue
                start, segment = parse_time(path)
                if start is None:
                    skipped_no_timestamp += 1
                    continue
                by_model[key].append(Row(
                    model=display[key], folder=folder, path=path, start=start,
                    segment=segment, size=int(st.st_size), mtime=float(st.st_mtime),
                ))

    review_moves: List[Dict[str, Any]] = []
    delete_moves: List[Dict[str, Any]] = []
    touched_folders = set()
    models_with_files = 0

    for key, rows in by_model.items():
        if not rows:
            continue
        models_with_files += 1
        # Apply each rule independently inside each physical model folder. This
        # matches the established sorter and prevents a recording block from
        # accidentally spanning duplicate folders or drives.
        by_folder: Dict[str, List[Row]] = {}
        for row in rows:
            by_folder.setdefault(os.path.normcase(os.path.abspath(str(row.folder))), []).append(row)
        for folder_rows in by_folder.values():
            if not folder_rows:
                continue
            touched_folders.add(str(folder_rows[0].folder))
            _estimate_durations(folder_rows, duration_lookup)
            target = max(1.0, float(rules[key]) * 60.0)

            nonseg = [row for row in folder_rows if row.segment is None]
            segmented: Dict[datetime, List[Row]] = {}
            for row in folder_rows:
                if row.segment is not None:
                    segmented.setdefault(row.start, []).append(row)

            keep_rows: List[Row] = []
            delete_rows: List[Row] = []
            for block in _nonseg_blocks(nonseg, gap_minutes):
                keep, delete = _plan_nonseg_block(block, target)
                keep_rows.extend(keep)
                delete_rows.extend(delete)
            for _base, group in sorted(segmented.items(), key=lambda item: item[0]):
                keep, delete = _plan_segment_group(group, target)
                keep_rows.extend(keep)
                delete_rows.extend(delete)

            for row in keep_rows:
                review_moves.append({
                    'model': display.get(key, key), 'source': str(row.path),
                    'bytes': row.size, 'start': row.start.isoformat(),
                    'duration': row.duration, 'folder': str(row.folder),
                    'decision': 'REVIEW',
                })
            for row in delete_rows:
                delete_moves.append({
                    'model': display.get(key, key), 'source': str(row.path),
                    'bytes': row.size, 'start': row.start.isoformat(),
                    'duration': row.duration, 'folder': str(row.folder),
                    'decision': 'DELETE',
                })

    order_key = lambda item: (str(item['model']).casefold(), str(item['start']), str(item['source']).casefold())
    review_moves.sort(key=order_key)
    delete_moves.sort(key=order_key)
    return {
        'review_moves': review_moves,
        'delete_moves': delete_moves,
        # Backward-compatible alias used by early v2.8 drafts.
        'moves': delete_moves,
        'kept': len(review_moves),
        'protected_recent': skipped_recent,
        'skipped_no_timestamp': skipped_no_timestamp,
        'folders': len(touched_folders),
        'rules': len(rules),
        'models': models_with_files,
        'errors': scan_errors,
    }
