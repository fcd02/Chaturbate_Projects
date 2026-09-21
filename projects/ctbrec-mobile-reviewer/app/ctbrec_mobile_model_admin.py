#!/usr/bin/env python3
"""Live-first CTBRec model administration for Mobile Reviewer v2.11.

CTBRec-native mutations go only through the identity-verified live JVM bridge.
Legacy models.json files are READ ONLY and are used solely to preserve old
Sorter notes/alias metadata for display and migration. Reviewer-only state is
stored in sidecar JSON files beside Mobile Reviewer.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

PRIORITY_TIERS: Dict[str, Dict[str, Any]] = {
    "favorite": {"label": "Favorite", "value": 1000},
    "likely_favorite": {"label": "Likely Favorite", "value": 800},
    "continue": {"label": "Continue", "value": 500},
    "test": {"label": "Test", "value": 250},
    "unsorted": {"label": "Unsorted", "value": 50},
    "low": {"label": "Low Priority", "value": 10},
}
PRIORITY_BY_VALUE = {int(v["value"]): key for key, v in PRIORITY_TIERS.items()}
PRIORITY_NOTE_RE = re.compile(r"^\s*CTBRec Sorter Priority:\s*(.*?)\s*(?:\(([-+]?\d+)\))?\s*$", re.I)
EZ_NOTE_RE = re.compile(r"^\s*CTBRec Sorter EZ Sort:\s*(?:yes|true|1|on)\s*$", re.I)
ALIASES_RE = re.compile(r"^\s*Aliases:\s*(.*?)\s*$", re.I)
PRIMARY_RE = re.compile(r"^\s*Primary Alias:\s*(.*?)\s*$", re.I)
DUP_SUFFIX_RE = re.compile(r"_dup\d+$", re.I)

SITE_INFO: Dict[str, Dict[str, str]] = {
    "cb": {"url": "https://chaturbate.com/{name}/"},
    "sc": {"url": "https://stripchat.com/{name}"},
    "cs": {"url": "https://www.camsoda.com/{name}"},
    "c4": {"url": "https://www.cam4.com/{name}"},
    "f4f": {"url": "https://www.flirt4free.com/rooms/{name}/"},
    "bc": {"url": "https://bongacams.com/{name}"},
    "sm": {"url": "https://www.streamate.com/cam/{name}"},
}
SITE_ALIASES = {
    "chaturbate": "cb", "cb": "cb",
    "stripchat": "sc", "sc": "sc",
    "camsoda": "cs", "cs": "cs",
    "cam4": "c4", "c4": "c4",
    "flirt4free": "f4f", "f4f": "f4f",
    "bongacams": "bc", "bc": "bc",
    "streamate": "sm", "streammate": "sm", "sm": "sm",
}


def norm_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def base_name(value: Any) -> str:
    return DUP_SUFFIX_RE.sub("", str(value or "").strip()).casefold()


def norm_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
        host = parsed.netloc.casefold()
        path = parsed.path.rstrip("/").casefold()
        return f"{host}{path}"
    except Exception:
        return raw.rstrip("/").casefold()


def site_key_from_url(url: str) -> str:
    host = urllib.parse.urlparse(str(url or "")).netloc.casefold()
    if "chaturbate.com" in host: return "cb"
    if "stripchat.com" in host: return "sc"
    if "camsoda.com" in host: return "cs"
    if "cam4.com" in host: return "c4"
    if "flirt4free.com" in host: return "f4f"
    if "bongacams.com" in host: return "bc"
    if "streamate.com" in host: return "sm"
    return ""


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            try: os.fsync(handle.fileno())
            except OSError: pass
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists(): tmp.unlink()
        except OSError:
            pass


def _note_lines(text: Any) -> List[str]:
    return [line.rstrip() for line in str(text or "").splitlines() if line.strip()]


def _alias_tokens_from_note(text: Any) -> List[Tuple[str, str]]:
    for line in _note_lines(text):
        match = ALIASES_RE.match(line)
        if not match:
            continue
        out: List[Tuple[str, str]] = []
        for token in match.group(1).split(","):
            raw = token.strip()
            if not raw: continue
            site_match = re.match(r"^(.*?)\s*\[([A-Za-z0-9]+)\]\s*$", raw)
            if site_match:
                name = site_match.group(1).strip(); site = SITE_ALIASES.get(site_match.group(2).casefold(), "cb")
            else:
                name = raw; site = "cb"
            if name: out.append((name, site))
        return out
    return []


def _primary_from_note(text: Any) -> str:
    for line in _note_lines(text):
        match = PRIMARY_RE.match(line)
        if match: return match.group(1).strip()
    return ""


def _priority_from_model(model: Dict[str, Any], note: str) -> Tuple[str, int, str]:
    value = int(model.get("priority", 50) or 50)
    for line in _note_lines(note):
        match = PRIORITY_NOTE_RE.match(line)
        if match:
            try: value = int(match.group(2)) if match.group(2) else value
            except Exception: pass
            label = match.group(1).strip()
            tier = next((key for key, rec in PRIORITY_TIERS.items() if rec["label"].casefold() == label.casefold()), PRIORITY_BY_VALUE.get(value, "unsorted"))
            return tier, value, PRIORITY_TIERS.get(tier, {"label": label}).get("label", label)
    tier = PRIORITY_BY_VALUE.get(value, "unsorted")
    return tier, value, PRIORITY_TIERS.get(tier, {"label": str(value)})["label"]


class MobileModelAdmin:
    def __init__(self, app_dir: Path, config: Optional[Dict[str, Any]] = None, live_bridge: Any = None) -> None:
        self.app_dir = Path(app_dir)
        self.hidden_path = self.app_dir / "mobile_hidden_models.json"
        self.meta_path = self.app_dir / "mobile_model_metadata.json"
        self.lock = threading.RLock()
        self.live_bridge = live_bridge
        self.config: Dict[str, Any] = {}
        self._paths_cache: Tuple[float, List[Path]] = (0.0, [])
        self._filter_cache_signature: Optional[Tuple[Any, ...]] = None
        self._filter_cache_sets: Dict[str, Set[str]] = {"hidden": set(), "ignored": set(), "easy": set()}
        self._filter_cache_built_at = 0.0
        self._filter_cache_builds = 0
        self.configure(config or {})

    def configure(self, config: Dict[str, Any]) -> None:
        with self.lock:
            self.config = {
                "enabled": bool(config.get("enabled", True)),
                "auto_discover": bool(config.get("auto_discover", True)),
                "models_json_paths": config.get("models_json_paths", []),
            }
            self._paths_cache = (0.0, [])
            self._invalidate_filter_cache()

    def _invalidate_filter_cache(self) -> None:
        self._filter_cache_signature = None
        self._filter_cache_built_at = 0.0

    @staticmethod
    def _file_signature(path: Path) -> Tuple[str, int, int]:
        try:
            stat = path.stat()
            return (os.path.normcase(str(path)), int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            return (os.path.normcase(str(path)), -1, -1)

    def _filter_signature(self) -> Tuple[Any, ...]:
        return tuple([
            self._file_signature(self.hidden_path),
            self._file_signature(self.meta_path),
            *[self._file_signature(path) for path in self.paths(include_when_disabled=True)],
        ])

    def _cached_filter_sets(self) -> Dict[str, Set[str]]:
        signature = self._filter_signature()
        if signature == self._filter_cache_signature:
            return {key: set(value) for key, value in self._filter_cache_sets.items()}
        hidden = self._hidden()
        meta = self._meta()
        ignored = {base_name(v) for v in meta.get("ignored_models", []) if str(v).strip()}
        # EZ Sort is now Reviewer-durable. Import any names discovered from
        # legacy read-only models.json notes, but never let a transient controller
        # restart/path disappearance erase names already imported into Reviewer.
        discovered_easy = self._legacy_easy_sort_names()
        imported_easy = {base_name(v) for v in meta.get("easy_sort_imported", []) if str(v).strip()}
        newly_imported = discovered_easy - imported_easy
        if newly_imported:
            imported_easy.update(newly_imported)
            meta["easy_sort_imported"] = sorted(imported_easy)
            self._save_meta(meta)
            # _save_meta changes the file signature and invalidates the cache.
            # Capture the post-write signature so the very next EZ Sort request
            # can reuse this build instead of reparsing every legacy models.json.
            signature = self._filter_signature()
        easy = set(imported_easy) | set(discovered_easy)
        overrides = meta.get("easy_sort_overrides", {})
        if isinstance(overrides, dict):
            for key, enabled in overrides.items():
                if bool(enabled): easy.add(base_name(key))
                else: easy.discard(base_name(key))
        self._filter_cache_signature = signature
        self._filter_cache_sets = {"hidden": set(hidden), "ignored": set(ignored), "easy": set(easy)}
        self._filter_cache_built_at = time.time()
        self._filter_cache_builds += 1
        return {key: set(value) for key, value in self._filter_cache_sets.items()}

    def filter_cache_token(self) -> Tuple[Any, ...]:
        with self.lock:
            return self._filter_signature()

    def filter_cache_debug(self) -> Dict[str, Any]:
        with self.lock:
            return {"builds": self._filter_cache_builds, "built_at": self._filter_cache_built_at}

    # ---------- read-only legacy models.json discovery ----------
    def _explicit_paths(self) -> List[Path]:
        raw = self.config.get("models_json_paths", [])
        if isinstance(raw, str):
            values = [line.strip() for line in raw.replace(";", "\n").splitlines() if line.strip()]
        elif isinstance(raw, list):
            values = [str(v).strip() for v in raw if str(v).strip()]
        else:
            values = []
        out: List[Path] = []
        for value in values:
            p = Path(os.path.expandvars(os.path.expanduser(value.strip('"'))))
            if p.is_dir(): p = p / "models.json"
            if p.is_file() and p.name.casefold() == "models.json": out.append(p.resolve())
        return out

    def _discover_paths(self) -> List[Path]:
        now = time.time(); cached_at, cached = self._paths_cache
        if now - cached_at < 60: return list(cached)
        found: Dict[str, Path] = {}
        for p in self._explicit_paths(): found[os.path.normcase(str(p))] = p
        if self.config.get("auto_discover", True):
            bases: List[Path] = [self.app_dir]; bases.extend(list(self.app_dir.parents)[:5])
            patterns = ("models.json", "config/*/models.json", "ctbrec/config/*/models.json", "ctbrec*/config/*/models.json", "*/ctbrec/config/*/models.json", "*/config/*/models.json")
            for root in bases:
                for pattern in patterns:
                    try:
                        for candidate in root.glob(pattern):
                            if not candidate.is_file() or candidate.name.casefold() != "models.json": continue
                            parts = " ".join(part.casefold() for part in candidate.parts)
                            if any(token in parts for token in ("ctbrec_paused", "ctbrec_female", "lastseen6months")): continue
                            probe = _read_json(candidate, {})
                            if isinstance(probe, dict) and isinstance(probe.get("models"), list):
                                found[os.path.normcase(str(candidate.resolve()))] = candidate.resolve()
                    except (OSError, PermissionError):
                        continue
        paths = sorted(found.values(), key=lambda p: (-p.stat().st_mtime if p.exists() else 0, str(p).casefold()))
        self._paths_cache = (now, paths)
        return list(paths)

    def paths(self, include_when_disabled: bool = False) -> List[Path]:
        # Reviewer-owned metadata filters (especially EZ Sort) must not disappear
        # merely because native/live model controls are disabled.  `enabled`
        # governs the controller UI/mutations, not read-only legacy metadata.
        if not include_when_disabled and not self.config.get("enabled", True):
            return []
        return self._discover_paths()

    def _load(self, path: Path) -> Dict[str, Any]:
        data = _read_json(path, {})
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise RuntimeError(f"Not a valid CTBRec models.json: {path}")
        data.setdefault("modelNotes", {})
        return data

    def _matches(self, data: Dict[str, Any], model_name: str) -> List[Tuple[int, Dict[str, Any], str]]:
        target = base_name(model_name)
        notes: Dict[str, Any] = data.get("modelNotes", {}) if isinstance(data.get("modelNotes"), dict) else {}
        matches: List[Tuple[int, Dict[str, Any], str]] = []
        for index, model in enumerate(data.get("models", [])):
            if not isinstance(model, dict): continue
            note = str(notes.get(str(model.get("url", "")), ""))
            alias_names = {base_name(model.get("name", ""))} | {base_name(alias) for alias, _site in _alias_tokens_from_note(note)}
            primary = _primary_from_note(note)
            if primary: alias_names.add(base_name(primary))
            if target in alias_names: matches.append((index, model, note))
        return matches

    def _legacy_group(self, model_name: str) -> Dict[str, Any]:
        names: Dict[str, Dict[str, str]] = {}
        urls: Set[str] = set()
        primary = ""
        first: Optional[Tuple[Path, Dict[str, Any], str]] = None
        for path in self.paths(include_when_disabled=True):
            try: data = self._load(path)
            except Exception: continue
            matches = self._matches(data, model_name)
            for _idx, model, note in matches:
                if first is None: first = (path, model, note)
                raw_name = str(model.get("name", model_name)); url = str(model.get("url", ""))
                skey = site_key_from_url(url) or "cb"
                names[f"{norm_name(raw_name)}|{skey}"] = {"name": raw_name, "site": skey, "url": url}
                if url: urls.add(url)
                for alias, site in _alias_tokens_from_note(note):
                    key = f"{norm_name(alias)}|{site}"
                    names.setdefault(key, {"name": alias, "site": site, "url": SITE_INFO.get(site, SITE_INFO["cb"])["url"].format(name=alias)})
                primary = primary or _primary_from_note(note)
        return {"identities": list(names.values()), "urls": urls, "primary": primary, "first": first}

    # ---------- Reviewer sidecars ----------
    def _meta(self) -> Dict[str, Any]:
        raw = _read_json(self.meta_path, {})
        if not isinstance(raw, dict): raw = {}
        raw.setdefault("version", 2)
        raw.setdefault("easy_sort_overrides", {})
        raw.setdefault("easy_sort_imported", [])
        raw.setdefault("alias_groups", {})
        raw.setdefault("ignored_models", [])
        raw.setdefault("ignored_urls", [])
        return raw

    def _save_meta(self, data: Dict[str, Any]) -> None:
        data["version"] = 2; data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_json(self.meta_path, data)

    def _hidden(self) -> Set[str]:
        raw = _read_json(self.hidden_path, {"models": []})
        values = raw.get("models", []) if isinstance(raw, dict) else []
        return {norm_name(v) for v in values if str(v).strip()}

    def hidden_names(self) -> Set[str]:
        with self.lock: return set(self._cached_filter_sets()["hidden"])

    def ignored_names(self) -> Set[str]:
        with self.lock:
            return set(self._cached_filter_sets()["ignored"])

    def set_hidden(self, model_name: str, hidden: bool) -> Dict[str, Any]:
        key = norm_name(model_name)
        with self.lock:
            values = self._hidden()
            if hidden: values.add(key)
            else: values.discard(key)
            _atomic_json(self.hidden_path, {"version": 1, "models": sorted(values), "updated_at": datetime.now().isoformat(timespec="seconds")})
            self._invalidate_filter_cache()
        return {"hidden": bool(hidden), "storage": "reviewer_sidecar"}

    def _legacy_easy_sort_names(self) -> Set[str]:
        names: Set[str] = set()
        for path in self.paths(include_when_disabled=True):
            try: data = self._load(path)
            except Exception: continue
            notes = data.get("modelNotes", {}) if isinstance(data.get("modelNotes"), dict) else {}
            for model in data.get("models", []):
                if not isinstance(model, dict): continue
                note = str(notes.get(str(model.get("url", "")), ""))
                if any(EZ_NOTE_RE.match(line) for line in _note_lines(note)): names.add(base_name(model.get("name", "")))
        return names

    def easy_sort_names(self) -> Set[str]:
        with self.lock:
            return set(self._cached_filter_sets()["easy"])

    def set_easy_sort(self, model_name: str, enabled: bool) -> Dict[str, Any]:
        with self.lock:
            meta = self._meta(); overrides = meta.setdefault("easy_sort_overrides", {})
            overrides[base_name(model_name)] = bool(enabled); self._save_meta(meta)
            self._invalidate_filter_cache()
        return {"easy_sort": bool(enabled), "storage": "reviewer_sidecar", "models_json_unchanged": True}

    def _sidecar_aliases(self, model_name: str) -> List[Dict[str, str]]:
        meta = self._meta(); groups = meta.get("alias_groups", {})
        if not isinstance(groups, dict): return []
        target = base_name(model_name)
        direct = groups.get(target, [])
        if isinstance(direct, list): return [dict(x) for x in direct if isinstance(x, dict)]
        # Allow lookup from an alias itself.
        for rows in groups.values():
            if not isinstance(rows, list): continue
            if any(base_name(row.get("name", "")) == target for row in rows if isinstance(row, dict)):
                return [dict(x) for x in rows if isinstance(x, dict)]
        return []

    def _identity_group(self, model_name: str) -> Dict[str, Any]:
        legacy = self._legacy_group(model_name)
        identities: Dict[str, Dict[str, str]] = {}
        for row in legacy.get("identities", []):
            if not isinstance(row, dict): continue
            name = str(row.get("name", "")); site = str(row.get("site", "") or "cb"); url = str(row.get("url", ""))
            identities[f"{norm_name(name)}|{site}"] = {"name": name, "site": site, "url": url}
        for row in self._sidecar_aliases(model_name):
            name = str(row.get("name", "")); site = str(row.get("site", "") or "cb"); url = str(row.get("url", ""))
            if name: identities[f"{norm_name(name)}|{site}"] = {"name": name, "site": site, "url": url}
        if not identities:
            identities[f"{norm_name(model_name)}|cb"] = {"name": str(model_name), "site": "cb", "url": ""}
        return {"identities": list(identities.values()), "primary": legacy.get("primary", "") or str(model_name), "first": legacy.get("first")}

    def _resolve_live_rows(self, model_name: str, refresh_if_empty: bool = True) -> List[Dict[str, Any]]:
        if self.live_bridge is None: return []
        group = self._identity_group(model_name)
        names = [row.get("name", "") for row in group["identities"]]
        urls = [row.get("url", "") for row in group["identities"] if row.get("url")]
        rows = self.live_bridge.find_models(names=names + [model_name], urls=urls)
        if not rows and refresh_if_empty:
            try: self.live_bridge.refresh_now(include_list=True)
            except Exception: pass
            rows = self.live_bridge.find_models(names=names + [model_name], urls=urls)
        return rows

    def _require_live_rows(self, model_name: str) -> List[Dict[str, Any]]:
        if self.live_bridge is None: raise RuntimeError("Live CTBRec bridge client is unavailable.")
        self.live_bridge.verify_identity()
        rows = self._resolve_live_rows(model_name, refresh_if_empty=True)
        if not rows: raise RuntimeError(f"{model_name} is not currently tracked by the identity-verified running CTBRec instance.")
        return rows

    # ---------- payload/status ----------
    def discovery_payload(self) -> Dict[str, Any]:
        paths = self.paths()
        bridge = self.live_bridge.discovery_payload() if self.live_bridge is not None else {"ok": False, "message": "Live bridge client unavailable."}
        return {
            "enabled": bool(self.config.get("enabled", True)),
            "auto_discover": bool(self.config.get("auto_discover", True)),
            "models_json_paths": [str(p) for p in self._explicit_paths()],
            "discovered_paths": [str(p) for p in paths],
            "found": bool(paths),
            "read_only": True,
            "message": "Legacy models.json is read-only in v2.11; native CTBRec changes use the live bridge.",
            "bridge": bridge,
        }

    def model_payload(self, model_name: str, keep_last_minutes: Optional[float] = None) -> Dict[str, Any]:
        group = self._identity_group(model_name); first = group.get("first")
        hidden = norm_name(model_name) in self._hidden()
        ignored = base_name(model_name) in self.ignored_names()
        easy = base_name(model_name) in self.easy_sort_names()
        rows = self._resolve_live_rows(model_name, refresh_if_empty=True)
        bridge = self.live_bridge.discovery_payload() if self.live_bridge is not None else {"ok": False, "message": "Live bridge client unavailable."}

        legacy_model: Dict[str, Any] = {}; legacy_note = ""; legacy_path = ""
        if first:
            path, legacy_model, legacy_note = first; legacy_path = str(path)
        legacy_tier, legacy_value, legacy_label = _priority_from_model(legacy_model or {}, legacy_note)

        priority_value = int(rows[0].get("priority", legacy_value) or legacy_value) if rows else legacy_value
        priority_tier = PRIORITY_BY_VALUE.get(priority_value, legacy_tier if not rows else "unsorted")
        priority_label = PRIORITY_TIERS.get(priority_tier, {"label": str(priority_value)})["label"]
        paused_values = [bool(row.get("paused")) for row in rows]
        later_values = [bool(row.get("markedLater")) for row in rows]
        force_values = [bool(row.get("forcePriority")) for row in rows]
        suspended = bool(paused_values and all(paused_values))
        marked_later = bool(later_values and all(later_values))
        force_priority = bool(force_values and all(force_values))

        identities = group["identities"]
        primary_row = rows[0] if rows else {}
        return {
            "found": bool(rows or first or identities),
            "live_found": bool(rows),
            "native_controls_available": bool(bridge.get("ok") and rows),
            "model": str(primary_row.get("modelName") or primary_row.get("name") or model_name),
            "requested_model": model_name,
            "url": str(primary_row.get("url", "") or (identities[0].get("url", "") if identities else "")),
            "legacy_models_json": legacy_path,
            "models_json_read_only": True,
            "priority_tier": priority_tier,
            "priority_value": priority_value,
            "priority_label": priority_label,
            "legacy_priority_label": legacy_label,
            "priority_tiers": PRIORITY_TIERS,
            "easy_sort": easy,
            "hidden": hidden,
            "ignored": ignored,
            "suspended": suspended,
            "suspended_mixed": bool(paused_values and len(set(paused_values)) > 1),
            "marked_later": marked_later,
            "marked_later_mixed": bool(later_values and len(set(later_values)) > 1),
            "force_priority": force_priority,
            "force_priority_mixed": bool(force_values and len(set(force_values)) > 1),
            "recording": any(bool(row.get("recording")) for row in rows),
            "online": any(bool(row.get("online")) for row in rows),
            "aliases": [row for row in identities if base_name(row.get("name", "")) != base_name(model_name)],
            "primary_alias": group.get("primary") or str(model_name),
            "live_identities": rows,
            "keep_last_minutes": keep_last_minutes or 0,
            "search_links": self.search_links(model_name),
            "bridge": bridge,
            "message": bridge.get("message") or "CTBRec live controls loaded.",
        }

    # ---------- live CTBRec mutations ----------
    def set_priority(self, model_name: str, tier: str) -> Dict[str, Any]:
        tier = str(tier or "").casefold()
        if tier not in PRIORITY_TIERS: raise RuntimeError("Unknown CTBRec priority tier.")
        rec = PRIORITY_TIERS[tier]; rows = self._require_live_rows(model_name)
        result = self.live_bridge.mutation_for_rows("PRIORITY", rows, int(rec["value"]))
        return {**result, "priority_tier": tier, "priority_value": rec["value"], "priority_label": rec["label"], "models_json_unchanged": True}

    def set_suspended(self, model_name: str, suspended: bool) -> Dict[str, Any]:
        rows = self._require_live_rows(model_name); command = "PAUSE" if suspended else "RESUME"
        result = self.live_bridge.mutation_for_rows(command, rows)
        return {**result, "suspended": bool(suspended), "models_json_unchanged": True}

    def set_mark_later(self, model_name: str, enabled: bool) -> Dict[str, Any]:
        rows = self._require_live_rows(model_name)
        result = self.live_bridge.mutation_for_rows("MARK_LATER", rows, str(bool(enabled)).lower())
        return {**result, "marked_later": bool(enabled), "models_json_unchanged": True}

    def set_force_priority(self, model_name: str, enabled: bool) -> Dict[str, Any]:
        rows = self._require_live_rows(model_name); command = "FORCE_PRIORITY" if enabled else "NORMAL_PRIORITY"
        result = self.live_bridge.mutation_for_rows(command, rows)
        return {**result, "force_priority": bool(enabled), "models_json_unchanged": True}

    def _parse_alias_spec(self, spec: str) -> Tuple[str, str, str]:
        tokens = [t for t in str(spec or "").strip().split() if t]
        if not tokens: raise RuntimeError("Enter an alias name, optionally prefixed with site and suffixed with c/f/p/i.")
        site = "cb"; action = "c"
        if tokens and tokens[0].casefold() in SITE_ALIASES: site = SITE_ALIASES[tokens.pop(0).casefold()]
        if tokens and tokens[-1].casefold() in {"c", "f", "p", "i"}: action = tokens.pop(-1).casefold()
        if len(tokens) != 1: raise RuntimeError("Alias format: newname, sc newname p, cb newname f, etc.")
        name = tokens[0].strip()
        if not name: raise RuntimeError("Alias name cannot be blank.")
        return site, name, action

    def add_alias(self, model_name: str, spec: str) -> Dict[str, Any]:
        site, alias_name, action = self._parse_alias_spec(spec)
        alias_url = SITE_INFO[site]["url"].format(name=alias_name)
        # i = remember as reviewer-ignored identity; the current bridge has no
        # native IGNORE command, so do not add a model just to immediately remove it.
        bridge_response: Dict[str, Any] = {"ok": True, "skipped_live_add": action == "i"}
        if action != "i":
            if self.live_bridge is None: raise RuntimeError("Live CTBRec bridge client is unavailable.")
            bridge_response = self.live_bridge.mutate("ADD_URL", alias_url)
            result_row = bridge_response.get("result", {}) if isinstance(bridge_response.get("result"), dict) else {}
            live_url = str(result_row.get("url", alias_url) or alias_url)
            if action == "f": self.live_bridge.mutate("PRIORITY", live_url, PRIORITY_TIERS["favorite"]["value"])
            elif action == "p": self.live_bridge.mutate("PAUSE", live_url)

        with self.lock:
            meta = self._meta(); groups = meta.setdefault("alias_groups", {}); key = base_name(model_name)
            rows = groups.get(key, []) if isinstance(groups.get(key, []), list) else []
            group = self._identity_group(model_name)
            for identity in group.get("identities", []):
                if isinstance(identity, dict): rows.append(dict(identity))
            rows.append({"name": alias_name, "site": site, "url": alias_url})
            unique: Dict[str, Dict[str, str]] = {}
            for row in rows:
                if not isinstance(row, dict): continue
                name = str(row.get("name", "")); skey = str(row.get("site", "") or "cb"); url = str(row.get("url", ""))
                if name: unique[f"{norm_name(name)}|{skey}"] = {"name": name, "site": skey, "url": url}
            groups[key] = list(unique.values())
            if action == "i":
                ignored_urls = {norm_url(v): str(v) for v in meta.get("ignored_urls", []) if str(v).strip()}
                ignored_urls[norm_url(alias_url)] = alias_url; meta["ignored_urls"] = sorted(ignored_urls.values(), key=str.casefold)
            self._save_meta(meta)
            self._invalidate_filter_cache()
        return {"site": site, "name": alias_name, "action": action, "url": alias_url, "bridge": bridge_response, "storage": "reviewer_sidecar", "models_json_unchanged": True}

    def ignore_model(self, model_name: str) -> Dict[str, Any]:
        rows = self._require_live_rows(model_name)
        group = self._identity_group(model_name)
        live_result = self.live_bridge.mutation_for_rows("REMOVE", rows)
        names = {base_name(model_name)}
        urls: Set[str] = set()
        for identity in group.get("identities", []):
            if not isinstance(identity, dict): continue
            if identity.get("name"): names.add(base_name(identity.get("name")))
            if identity.get("url"): urls.add(str(identity.get("url")))
        for row in rows:
            if row.get("modelName") or row.get("name"): names.add(base_name(row.get("modelName") or row.get("name")))
            if row.get("url"): urls.add(str(row.get("url")))
        with self.lock:
            meta = self._meta()
            ignored_names = {base_name(v) for v in meta.get("ignored_models", []) if str(v).strip()} | names
            ignored_urls = {norm_url(v): str(v) for v in meta.get("ignored_urls", []) if str(v).strip()}
            for url in urls: ignored_urls[norm_url(url)] = url
            meta["ignored_models"] = sorted(ignored_names)
            meta["ignored_urls"] = sorted(ignored_urls.values(), key=str.casefold)
            self._save_meta(meta)
            self._invalidate_filter_cache()
        return {
            "live_remove": live_result, "model_names": sorted(names), "urls": sorted(urls),
            "ignore_mode": "live_remove_plus_reviewer_sidecar",
            "message": "Removed from the running CTBRec instance now; Reviewer will keep it ignored locally and move its recording folders to deletion. The existing bridge has no native persistent IGNORE command.",
            "models_json_unchanged": True,
        }

    def search_links(self, model_name: str) -> List[Dict[str, str]]:
        q = urllib.parse.quote_plus(str(model_name or "").strip()); name_path = urllib.parse.quote(str(model_name or "").strip())
        return [
            {"label": "CamGirlFinder", "url": f"https://camgirlfinder.net/models?model={q}&platform=&gender="},
            {"label": "Recu", "url": f"https://recu.me/{name_path}"},
            {"label": "Google", "url": f"https://www.google.com/search?q={q}"},
            {"label": "Chaturbate", "url": f"https://chaturbate.com/{name_path}/"},
            {"label": "Stripchat", "url": f"https://stripchat.com/{name_path}"},
        ]
