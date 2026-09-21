from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from .models import normalize_username, priority_to_label
from .store import DiscoveryStore

NAME_KEYS = (
    "username", "model", "model_name", "modelName", "name", "performer", "performer_name",
    "display_name", "displayName", "room", "room_name", "roomName",
)
PRIORITY_KEYS = (
    "priority", "sort_priority", "sortPriority", "classification", "tier", "label", "review_state",
    "reviewState", "interest_tier", "interestTier",
)

# These names are structural in known Mobile Reviewer caches. They must never become
# synthetic model usernames merely because they are dictionary keys.
CATALOG_CONTAINER_KEYS = {
    "catalog", "models", "items", "entries", "data", "rows", "roots", "root", "modes", "mode",
    "original", "review", "deletion", "deleted", "cleanup", "hidden", "easy_sort", "ez_sort",
    "by_root", "by_mode", "folders", "drives", "drive", "records",
}
MODEL_RECORD_HINT_KEYS = {
    "folder", "path", "root", "drive", "size_bytes", "sizeBytes", "bytes", "total_bytes", "totalBytes",
    "file_count", "fileCount", "priority", "classification", "tier", "label", "last_seen", "lastSeen",
    "last_active", "lastActive", "first_seen", "firstSeen", "tags", "gender", "platform", "site",
}


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _drive_from_path(value: Any) -> str:
    raw = str(value or "")
    drive, _ = os.path.splitdrive(raw)
    return drive.upper() if drive else ""


def _looks_container_key(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    folded = text.casefold().replace("-", "_").replace(" ", "_")
    if folded in CATALOG_CONTAINER_KEYS:
        return True
    if folded.endswith(("_models", "_rows", "_entries", "_catalog", "_folders")):
        return True
    # A Windows/UNC/POSIX path is a root/folder key, not a username.
    if "\\" in text or "/" in text or (len(text) >= 2 and text[1:2] == ":"):
        return True
    return False


def _explicit_name(record: Dict[str, Any]) -> Optional[str]:
    for key in NAME_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = record.get("identity") or record.get("ctbrec")
    if isinstance(nested, dict):
        for key in NAME_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _looks_model_record_dict(record: Dict[str, Any]) -> bool:
    if _explicit_name(record):
        return True
    return bool(MODEL_RECORD_HINT_KEYS.intersection(record.keys()))


def _catalog_record_from_value(name: str, value: Any) -> Dict[str, Any]:
    """Summarize one model's Mobile Reviewer cache rows into a light account record."""
    if isinstance(value, dict):
        row = dict(value)
        row.setdefault("username", name)
        return row
    out: Dict[str, Any] = {"username": name}
    if not isinstance(value, list):
        return out
    out["folder_count"] = len(value)
    total = 0
    file_count = 0
    per_drive: Dict[str, int] = {}
    last_seen = None
    first_seen = None
    for item in value:
        if not isinstance(item, dict):
            continue
        size = 0
        for key in ("size_bytes", "sizeBytes", "bytes", "total_bytes", "totalBytes", "total_size_bytes", "totalSizeBytes"):
            if key in item:
                size = _safe_int(item.get(key))
                break
        total += size
        file_count += _safe_int(item.get("file_count", item.get("fileCount", 0)))
        root = item.get("root") or item.get("folder") or item.get("path") or item.get("drive")
        drive = _drive_from_path(root) or str(item.get("drive") or "").strip()
        if drive and size:
            per_drive[drive] = per_drive.get(drive, 0) + size
        candidate_seen = item.get("last_seen") or item.get("lastSeen") or item.get("last_active") or item.get("lastActive") or item.get("mtime")
        if candidate_seen and (last_seen is None or str(candidate_seen) > str(last_seen)):
            last_seen = candidate_seen
        candidate_first = item.get("first_seen") or item.get("firstSeen")
        if candidate_first and (first_seen is None or str(candidate_first) < str(first_seen)):
            first_seen = candidate_first
    if total:
        out["size_bytes"] = total
    if file_count:
        out["file_count"] = file_count
    if per_drive:
        out["per_drive"] = per_drive
    if last_seen is not None:
        out["last_seen"] = last_seen
    if first_seen is not None:
        out["first_seen"] = first_seen
    return out


def _walk_catalog(node: Any, fallback: Optional[str] = None) -> Iterator[Tuple[Optional[str], Dict[str, Any]]]:
    """Recursively walk current and historical Mobile Reviewer cache shapes.

    The user's v2.15.5 cache has three outer catalog buckets. Earlier Discovery v0.4.0
    mistook those three structural keys for usernames. This walker only treats a mapping
    key as a username when its child looks like a model/folder record; structural/mode/root
    containers are recursively descended.
    """
    if isinstance(node, list):
        # A list attached to a plausible username is commonly that model's per-root folder rows.
        if fallback and not _looks_container_key(fallback):
            explicit_names = {_explicit_name(x) for x in node if isinstance(x, dict) and _explicit_name(x)}
            explicit_names.discard(None)
            if not explicit_names or explicit_names == {fallback}:
                yield fallback, _catalog_record_from_value(fallback, node)
                return
        for item in node:
            if isinstance(item, dict):
                explicit = _explicit_name(item)
                if explicit:
                    yield explicit, item
                else:
                    yield from _walk_catalog(item, None)
        return

    if not isinstance(node, dict):
        return

    explicit = _explicit_name(node)
    if explicit and _looks_model_record_dict(node):
        yield explicit, node
        return

    # A plausible username key whose dict itself is a record is a direct model map entry.
    if fallback and not _looks_container_key(fallback) and _looks_model_record_dict(node):
        yield fallback, _catalog_record_from_value(fallback, node)
        return

    for key, child in node.items():
        if key in {"updated_at", "roots_file", "version", "schema_version", "meta", "metadata"}:
            continue
        key_text = str(key)
        if isinstance(child, (dict, list)):
            if _looks_container_key(key_text):
                yield from _walk_catalog(child, None)
            elif isinstance(child, dict) and _looks_model_record_dict(child):
                explicit_child = _explicit_name(child)
                yield explicit_child or key_text, _catalog_record_from_value(explicit_child or key_text, child)
            elif isinstance(child, list):
                # If rows contain explicit names, the key is another container. Otherwise it is
                # very likely a username -> [folder row, ...] mapping.
                names = [_explicit_name(x) for x in child if isinstance(x, dict)]
                names = [x for x in names if x]
                if names and (len(set(names)) > 1 or any(normalize_username(x) != normalize_username(key_text) for x in names)):
                    yield from _walk_catalog(child, None)
                else:
                    yield key_text, _catalog_record_from_value(key_text, child)
            else:
                # Ambiguous nested map: recurse and let leaves prove they are records.
                yield from _walk_catalog(child, key_text)


def _iter_records(data: Any) -> Iterator[Tuple[Optional[str], Dict[str, Any]]]:
    if isinstance(data, dict) and "catalog" in data:
        yield from _walk_catalog(data.get("catalog"), None)
        return
    # Generic non-Mobile-Reviewer inputs remain supported.
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield _explicit_name(item), item
        return
    if not isinstance(data, dict):
        return
    for key in ("models", "items", "entries", "data", "rows"):
        if key in data and isinstance(data[key], (list, dict)):
            yield from _walk_catalog(data[key], None)
            return
    yield from _walk_catalog(data, None)


def _first(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _name(fallback: Optional[str], record: Dict[str, Any]) -> Optional[str]:
    explicit = _explicit_name(record)
    if explicit:
        return explicit
    return fallback.strip() if isinstance(fallback, str) and fallback.strip() and not _looks_container_key(fallback) else None


def _preference(record: Dict[str, Any]) -> str:
    for container in (record, record.get("ctbrec") if isinstance(record.get("ctbrec"), dict) else {}):
        for key in PRIORITY_KEYS:
            if key in container:
                return priority_to_label(container.get(key))
    return "unsorted"


def _normalize_per_drive(record: Dict[str, Any]) -> Any:
    value = _first(record, "per_drive", "perDrive", "drive_sizes", "driveSizes", "bytes_by_root", "bytesByRoot")
    if value is not None:
        return value
    roots = record.get("roots")
    if isinstance(roots, list):
        out = {}
        for row in roots:
            if not isinstance(row, dict):
                continue
            name = _first(row, "drive", "root", "path", "name")
            size = _first(row, "size_bytes", "sizeBytes", "bytes", "total_bytes", "totalBytes")
            if name is not None and size is not None:
                out[str(name)] = size
        return out or None
    return None


def _metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    keep: Dict[str, Any] = {}
    mapping = {
        "tags": ("tags", "hashtags"),
        "language": ("language", "lang"),
        "gender": ("gender",),
        "active_hours": ("active_hours", "activeHours", "hours"),
        "size_bytes": ("size_bytes", "sizeBytes", "total_bytes", "totalBytes", "total_size_bytes", "totalSizeBytes", "bytes"),
        "last_active": ("last_active", "lastActive"),
        "last_seen": ("last_seen", "lastSeen"),
        "first_seen": ("first_seen", "firstSeen"),
        "profile_text": ("profile_text", "profileText", "bio"),
        "room_subject": ("room_subject", "roomSubject", "subject"),
        "aliases": ("aliases", "alias"),
        "site": ("site",),
        "platform": ("platform",),
        "online": ("online", "is_online", "isOnline"),
        "recording": ("recording", "is_recording", "isRecording"),
        "ctbrec_state": ("ctbrec_state", "ctbrecState", "state"),
        "hidden": ("hidden",),
        "ez_sort": ("ez_sort", "ezSort"),
        "file_count": ("file_count", "fileCount"),
        "folder_count": ("folder_count", "folderCount"),
    }
    for out_key, keys in mapping.items():
        value = _first(record, *keys)
        if value is not None:
            keep[out_key] = value
    per_drive = _normalize_per_drive(record)
    if per_drive is not None:
        keep["per_drive"] = per_drive
    if isinstance(record.get("ctbrec"), dict):
        keep["ctbrec"] = record["ctbrec"]
    return keep


def _merge_metadata(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(existing)
    for key, value in incoming.items():
        if key in {"size_bytes", "file_count", "folder_count"}:
            out[key] = _safe_int(out.get(key)) + _safe_int(value)
        elif key == "per_drive" and isinstance(value, dict):
            merged = dict(out.get(key) or {}) if isinstance(out.get(key), dict) else {}
            for drive, size in value.items():
                merged[str(drive)] = _safe_int(merged.get(str(drive))) + _safe_int(size)
            out[key] = merged
        elif key == "tags" and isinstance(value, list):
            old = out.get(key) if isinstance(out.get(key), list) else []
            out[key] = list(dict.fromkeys([str(x) for x in old + value if str(x).strip()]))
        elif value not in (None, "", [], {}):
            out[key] = value
    return out


def import_catalog(path: str | Path, store: DiscoveryStore, default_platform: str = "chaturbate") -> Dict[str, int]:
    p = Path(path)
    if not p.exists():
        store.set_source_state("mobile_reviewer_catalog", status="missing", detail=str(p))
        return {"seen": 0, "imported": 0, "errors": 0}
    st = p.stat()
    # Bump parser generation so a v0.4.0 3/3 false-success is automatically reparsed once.
    signature = f"v46:{st.st_mtime_ns}:{st.st_size}"
    previous = store.source_state("mobile_reviewer_catalog")
    if previous and previous.get("cursor") == signature:
        store.set_source_state("mobile_reviewer_catalog", status="unchanged", detail=f"unchanged; {p.name}", cursor=signature, success=True)
        return {"seen": 0, "imported": 0, "errors": 0, "unchanged": 1}

    data = json.loads(p.read_text(encoding="utf-8-sig"))
    raw_records = list(_iter_records(data))
    seen = len(raw_records)
    errors = 0

    # De-duplicate repeated Original/Review/deletion/root rows into one account per platform+username.
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for fallback, record in raw_records:
        try:
            username = _name(fallback, record)
            if not username:
                continue
            platform = str(_first(record, "platform", "site") or default_platform).strip().casefold()
            norm = normalize_username(username)
            if not norm:
                continue
            key = (platform, norm)
            item = aggregated.setdefault(key, {
                "platform": platform, "username": username, "preference": "unsorted",
                "first_seen": None, "last_seen": None, "metadata": {},
            })
            pref = _preference(record)
            if pref != "unsorted":
                # Let a non-neutral explicit label win over neutral catalog rows; existing DB user
                # feedback still remains authoritative because upsert never overwrites with unsorted.
                item["preference"] = pref
            first_seen = _first(record, "first_seen", "firstSeen")
            last_seen = _first(record, "last_seen", "lastSeen", "last_active", "lastActive")
            if first_seen and (item["first_seen"] is None or str(first_seen) < str(item["first_seen"])):
                item["first_seen"] = first_seen
            if last_seen and (item["last_seen"] is None or str(last_seen) > str(item["last_seen"])):
                item["last_seen"] = last_seen
            item["metadata"] = _merge_metadata(item["metadata"], _metadata(record))
        except Exception:
            errors += 1

    imported = 0
    for item in aggregated.values():
        try:
            store.upsert_identity_account(
                item["platform"], item["username"], preference_label=item["preference"],
                first_seen=str(item["first_seen"]) if item["first_seen"] else None,
                last_seen=str(item["last_seen"]) if item["last_seen"] else None,
                source="mobile_reviewer_catalog", metadata=item["metadata"],
            )
            imported += 1
        except Exception:
            errors += 1

    # Remove only the exact harmless placeholders v0.4.0 could have created from the outer
    # catalog buckets. Never touch an account with feedback/evidence/outbox/link history.
    cleaned = 0
    catalog = data.get("catalog") if isinstance(data, dict) else None
    if isinstance(catalog, dict):
        for outer_key, outer_value in catalog.items():
            if isinstance(outer_value, (dict, list)) and _looks_container_key(outer_key):
                if store.remove_safe_import_artifact_account(default_platform, str(outer_key), source="mobile_reviewer_catalog"):
                    cleaned += 1

    detail = f"{imported} unique models imported from {seen} catalog row/group records; {errors} errors; cleaned {cleaned} v0.4 bucket artifact(s); {p.name}"
    store.set_source_state(
        "mobile_reviewer_catalog", status="ok" if errors == 0 else "partial", detail=detail,
        cursor=signature, success=True,
    )
    return {"seen": seen, "imported": imported, "errors": errors, "cleaned_artifacts": cleaned}


def import_legacy_list(path: str | Path, store: DiscoveryStore, label: str, platform: str = "chaturbate") -> Dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {"seen": 0, "imported": 0, "errors": 0}
    seen = imported = errors = 0
    for raw in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        username = raw.strip()
        if not username or username.startswith("#"):
            continue
        seen += 1
        try:
            store.upsert_identity_account(platform, username, preference_label=label, source=p.name)
            imported += 1
        except Exception:
            errors += 1
    store.set_source_state(p.name, status="ok" if errors == 0 else "partial", detail=f"{imported}/{seen} imported", success=True)
    return {"seen": seen, "imported": imported, "errors": errors}
