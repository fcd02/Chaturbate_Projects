from __future__ import annotations

import csv
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .models import Evidence, normalize_username, priority_to_label, utc_now_iso
from .store import DiscoveryStore

OFFICIAL_AFFILIATE_ENDPOINT = "https://chaturbate.com/api/public/affiliates/onlinerooms/"
PUBLIC_STATES = {"public", "free", "online", "open"}
NONPUBLIC_STATES = {"private", "away", "group", "hidden", "offline", "password", "spy"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
        return [x.strip() for x in raw if x.strip()]
    if isinstance(value, dict):
        return [str(k).strip() for k, v in value.items() if v and str(k).strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _file_signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _parse_json_or_csv(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.casefold() == ".csv":
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    data = json.loads(text)
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "rows", "records", "rankings", "data", "models"):
            value = data.get(key)
            if isinstance(value, list):
                return [dict(x) for x in value if isinstance(x, dict)]
        # username -> payload maps are common for local caches.
        if data and all(isinstance(v, dict) for v in data.values()):
            out = []
            for key, value in data.items():
                row = dict(value)
                row.setdefault("username", key)
                out.append(row)
            return out
        return [dict(data)]
    return []


def _looks_public(row: Mapping[str, Any]) -> bool:
    raw = _first(row, "current_show", "room_status", "status", "show")
    if raw is None:
        return True
    state = str(raw).strip().casefold().replace("_", " ")
    if any(token in state for token in NONPUBLIC_STATES):
        return False
    return state in PUBLIC_STATES or not state


def _affiliate_metadata(row: Mapping[str, Any], observed_at: str) -> Dict[str, Any]:
    tags = _as_list(_first(row, "tags", "room_tags"))
    langs = _as_list(_first(row, "spoken_languages", "languages", "language"))
    meta: Dict[str, Any] = {
        "online": True,
        "affiliate_observed_at": observed_at,
    }
    fields = {
        "room_subject": ("room_subject", "room_title"),
        "seconds_online": ("seconds_online", "time_online"),
        "num_users": ("num_users", "num_viewers"),
        "num_followers": ("num_followers", "followers"),
        "age": ("age",),
        "gender": ("gender",),
        "location": ("location",),
        "country": ("country",),
        "is_hd": ("is_hd", "hd"),
        "is_new": ("is_new", "new"),
        "image_url": ("image_url_360x270", "image_url", "image_url_320x240"),
        "current_show": ("current_show", "room_status", "status"),
    }
    for out_key, keys in fields.items():
        value = _first(row, *keys)
        if value not in (None, ""):
            meta[out_key] = value
    if tags:
        meta["tags"] = tags
    if langs:
        meta["spoken_languages"] = langs
        if len(langs) == 1:
            meta["language"] = langs[0]
    return meta


@dataclass
class CollectorResult:
    source: str
    status: str
    detail: Dict[str, Any]
    success: bool = True
    cursor: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "status": self.status, **self.detail}


class ChaturbateAffiliateCollector:
    """Collect online-room metadata only from Chaturbate's official affiliate API.

    This deliberately does not fetch or scrape performer HTML pages. A user-owned affiliate
    WM ID must be configured before the collector becomes active.
    """

    source = "chaturbate_affiliate_v2"

    def __init__(
        self,
        store: DiscoveryStore,
        config: Mapping[str, Any],
        *,
        fetch_json: Optional[Callable[[str, float], Any]] = None,
    ) -> None:
        self.store = store
        self.config = dict(config)
        self.fetch_json = fetch_json or self._http_json

    @staticmethod
    def _http_json(url: str, timeout: float) -> Any:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "CTBRec-Discovery/0.4 (+official-affiliate-api)",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(20_000_000)
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            suffix = f" retry_after={retry_after}" if retry_after else ""
            raise RuntimeError(f"affiliate API HTTP {exc.code}{suffix}") from exc
        return json.loads(raw.decode("utf-8"))

    def enabled(self) -> bool:
        return bool(self.config.get("enabled")) and bool(str(self.config.get("wm", "")).strip())

    def _url(self, offset: int, limit: int) -> str:
        params = {
            "wm": str(self.config.get("wm", "")).strip(),
            "client_ip": "request_ip",
            "limit": str(limit),
            "offset": str(offset),
        }
        return OFFICIAL_AFFILIATE_ENDPOINT + "?" + urllib.parse.urlencode(params)

    @staticmethod
    def _rows_and_next(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[bool]]:
        if isinstance(payload, list):
            return [dict(x) for x in payload if isinstance(x, dict)], None
        if not isinstance(payload, dict):
            raise ValueError("affiliate API response must be an object or list")
        raw = payload.get("results")
        if raw is None and isinstance(payload.get("data"), list):
            raw = payload.get("data")
        rows = [dict(x) for x in (raw or []) if isinstance(x, dict)]
        if "next" in payload:
            return rows, bool(payload.get("next"))
        return rows, None

    def run(self) -> CollectorResult:
        if not self.enabled():
            reason = "disabled" if not self.config.get("enabled") else "missing_wm"
            return CollectorResult(self.source, reason, {"enabled": False}, success=False)

        limit = max(1, min(500, _safe_int(self.config.get("page_size"), 500)))
        max_pages = max(1, min(500, _safe_int(self.config.get("max_pages"), 100)))
        timeout = max(2.0, min(60.0, _safe_float(self.config.get("timeout_seconds"), 12.0)))
        mode = str(self.config.get("candidate_mode", "new_and_known")).strip().casefold()
        if mode not in {"new_only", "new_and_known", "all"}:
            mode = "new_and_known"
        allowed_genders = {x.casefold() for x in _as_list(self.config.get("allowed_genders"))}

        observed_at = utc_now_iso()
        rows_seen = imported = new_candidates = known_refreshed = skipped = 0
        pages = 0
        offset = 0
        for _ in range(max_pages):
            payload = self.fetch_json(self._url(offset, limit), timeout)
            rows, has_next = self._rows_and_next(payload)
            pages += 1
            rows_seen += len(rows)
            for row in rows:
                username = str(_first(row, "username", "room", "model") or "").strip()
                if not normalize_username(username):
                    skipped += 1
                    continue
                if not _looks_public(row):
                    skipped += 1
                    continue
                gender = str(_first(row, "gender") or "").strip().casefold()
                if allowed_genders and gender not in allowed_genders:
                    skipped += 1
                    continue
                existing = self.store.find_account("chaturbate", username)
                is_new = _as_bool(_first(row, "is_new", "new"))
                should_import = mode == "all" or (mode == "new_only" and is_new) or (mode == "new_and_known" and (is_new or existing is not None))
                if not should_import:
                    skipped += 1
                    continue
                meta = _affiliate_metadata(row, observed_at)
                identity_id, account_id = self.store.upsert_identity_account(
                    "chaturbate",
                    username,
                    profile_url=f"https://chaturbate.com/{username}/",
                    first_seen=observed_at if existing is None else None,
                    last_seen=observed_at,
                    source=self.source,
                    metadata=meta,
                )
                imported += 1
                if existing is None and is_new:
                    new_candidates += 1
                    self.store.add_evidence(Evidence(
                        kind="new_account_seen",
                        source=self.source,
                        subject_account_id=account_id,
                        observed_at=observed_at,
                        confidence=0.75,
                        payload={
                            "signal_key": "official-affiliate-first-seen",
                            "is_new": True,
                            "gender": gender,
                        },
                    ))
                elif existing is not None:
                    known_refreshed += 1
            offset += len(rows)
            if not rows:
                break
            if has_next is False:
                break
            if has_next is None and len(rows) < limit:
                break
            if len(rows) < limit:
                break

        detail = {
            "pages": pages,
            "rows_seen": rows_seen,
            "imported": imported,
            "new_candidates": new_candidates,
            "known_refreshed": known_refreshed,
            "skipped": skipped,
            "candidate_mode": mode,
            "observed_at": observed_at,
        }
        return CollectorResult(self.source, "ok", detail, success=True, cursor=observed_at)


class LiveControlModelsCollector:
    """Read the already-running Live Control model snapshot over loopback.

    This is deliberately local-only enrichment. It reuses Live Control's tracked-model
    state and affiliateRoomInfo instead of issuing another Chaturbate request. It does
    NOT yet replace the global new-account affiliate feed, because Live Control's public
    /api/models payload intentionally contains tracked models rather than every online room.
    """

    source = "live_control_models"

    def __init__(self, store: DiscoveryStore, config: Mapping[str, Any], *, fetch_json: Optional[Callable[[str, float], Any]] = None):
        self.store = store
        self.config = dict(config)
        self.fetch_json = fetch_json or self._http_json

    @staticmethod
    def _http_json(url: str, timeout: float) -> Any:
        req = urllib.request.Request(url, headers={"Accept":"application/json", "User-Agent":"CTBRec-Discovery/0.4-local-integration"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(20_000_000).decode("utf-8"))

    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and bool(str(self.config.get("base_url", "")).strip())

    @staticmethod
    def _rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(x) for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("models", "items", "rows", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [dict(x) for x in value if isinstance(x, dict)]
                if isinstance(value, dict):
                    out=[]
                    for name,row in value.items():
                        if isinstance(row, dict):
                            item=dict(row); item.setdefault("username", name); out.append(item)
                    return out
        return []

    @staticmethod
    def _username(row: Mapping[str, Any]) -> str:
        name = str(_first(row, "username", "model", "model_name", "name", "room") or "").strip()
        if name:
            return name
        url = str(_first(row, "url", "model_url", "profile_url") or "").strip()
        if url:
            try:
                parts=[x for x in urllib.parse.urlparse(url).path.split("/") if x]
                if parts:
                    return parts[-1]
            except Exception:
                pass
        return ""

    def run(self) -> CollectorResult:
        if not self.enabled():
            return CollectorResult(self.source, "disabled", {"enabled": False}, success=False)
        base=str(self.config.get("base_url", "http://127.0.0.1:8792")).rstrip("/")
        timeout=max(0.5, min(10.0, _safe_float(self.config.get("timeout_seconds"), 2.0)))
        payload=self.fetch_json(base+"/api/models", timeout)
        rows=self._rows(payload)
        imported=errors=with_room_info=0
        for row in rows:
            try:
                username=self._username(row)
                if not normalize_username(username):
                    continue
                info=row.get("affiliateRoomInfo") if isinstance(row.get("affiliateRoomInfo"), dict) else {}
                meta: Dict[str, Any]={"live_control_present": True}
                for out_key, keys in {
                    "ctbrec_state": ("state","status","recording_state"),
                    "online": ("online","is_online","public","is_public"),
                    "recording": ("recording","is_recording"),
                    "priority": ("priority","numeric_priority"),
                    "paused": ("paused","is_paused"),
                }.items():
                    value=_first(row,*keys)
                    if value not in (None,""):
                        meta[out_key]=value
                if info:
                    with_room_info += 1
                    observed=str(info.get("snapshot_ms") or utc_now_iso())
                    affiliate=_affiliate_metadata(info, observed)
                    meta.update(affiliate)
                    meta["live_control_affiliate_info"] = True
                pref="favorite" if _as_bool(_first(row,"favorite","is_favorite")) else priority_to_label(_first(row,"priority","numeric_priority","sortPriority"))
                last_seen = utc_now_iso() if _as_bool(meta.get("online")) else None
                self.store.upsert_identity_account(
                    "chaturbate", username, preference_label=pref, last_seen=last_seen,
                    source=self.source, metadata=meta,
                )
                imported += 1
            except Exception:
                errors += 1
        return CollectorResult(self.source, "ok" if not errors else "partial", {
            "base_url": base, "rows": len(rows), "imported": imported,
            "with_affiliate_room_info": with_room_info, "errors": errors,
            "note": "local loopback only; no extra Chaturbate request",
        }, success=True, cursor=utc_now_iso())


class FileCollectorBase:
    source = "file"

    def __init__(self, store: DiscoveryStore, base_dir: Path, config: Mapping[str, Any]):
        self.store = store
        self.base_dir = base_dir
        self.config = dict(config)

    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    def resolve(self, raw: Any) -> Path:
        p = Path(str(raw or ""))
        return p if p.is_absolute() else self.base_dir / p

    def unchanged(self, path: Path) -> Tuple[bool, str]:
        sig = _file_signature(path)
        state = self.store.source_state(self.source)
        return bool(state and state.get("cursor") == sig), sig


class RecuDiscoveryExportCollector(FileCollectorBase):
    """Read a user-authorized/local Recu ranking export; never logs into or scrapes Recu itself."""

    source = "recu_discovery_export"

    def run(self) -> CollectorResult:
        if not self.enabled():
            return CollectorResult(self.source, "disabled", {"enabled": False}, success=False)
        path = self.resolve(self.config.get("path"))
        if not path.exists() or not path.is_file():
            return CollectorResult(self.source, "missing", {"path": str(path)}, success=False)
        same, sig = self.unchanged(path)
        if same:
            return CollectorResult(self.source, "unchanged", {"path": str(path), "records": 0}, success=True, cursor=sig)

        rows = _parse_json_or_csv(path)
        imported = evidence = errors = 0
        for row in rows:
            try:
                username = str(_first(row, "username", "model", "performer") or "").strip()
                if not normalize_username(username):
                    continue
                observed_at = str(_first(row, "observed_at", "snapshot_at", "date") or utc_now_iso())
                _, account_id = self.store.upsert_identity_account(
                    str(_first(row, "platform", "site") or "chaturbate"),
                    username,
                    source=self.source,
                    metadata={"recu_discovery_observed_at": observed_at},
                )
                imported += 1
                period = str(_first(row, "period", "window") or "week").strip().casefold().replace(" ", "_")
                rank = _safe_int(_first(row, "bookmark_rank", "rank", "bookmarked_rank"), 0)
                total = _safe_int(_first(row, "bookmark_total", "total", "rank_total"), 0)
                if rank > 0:
                    self.store.add_evidence(Evidence(
                        kind="recu_bookmark_rank", source="recu", subject_account_id=account_id,
                        observed_at=observed_at, confidence=0.9,
                        payload={"period": period, "rank": rank, "total": max(rank, total or rank), "signal_key": f"bookmark-rank:{period}"},
                    )); evidence += 1
                clips_7d = _safe_int(_first(row, "clips_7d", "recent_clips", "clip_velocity_7d"), -1)
                if clips_7d >= 0:
                    self.store.add_evidence(Evidence(
                        kind="recu_clip_velocity", source="recu", subject_account_id=account_id,
                        observed_at=observed_at, confidence=0.8,
                        payload={"clips_7d": clips_7d, "signal_key": "clips-7d"},
                    )); evidence += 1
                momentum = _first(row, "delta_percentile", "bookmark_momentum", "momentum")
                if momentum not in (None, ""):
                    self.store.add_evidence(Evidence(
                        kind="recu_bookmark_momentum", source="recu", subject_account_id=account_id,
                        observed_at=observed_at, confidence=0.8,
                        payload={"delta_percentile": _safe_float(momentum), "signal_key": "bookmark-momentum"},
                    )); evidence += 1
            except Exception:
                errors += 1
        return CollectorResult(self.source, "ok" if not errors else "partial", {"path": str(path), "rows": len(rows), "imported": imported, "evidence": evidence, "errors": errors}, success=True, cursor=sig)


class NeighborExportCollector(FileCollectorBase):
    """Import source-attributed model-neighbor observations from an authorized/local export."""

    source = "neighbor_export"

    def run(self) -> CollectorResult:
        if not self.enabled():
            return CollectorResult(self.source, "disabled", {"enabled": False}, success=False)
        path = self.resolve(self.config.get("path"))
        if not path.exists() or not path.is_file():
            return CollectorResult(self.source, "missing", {"path": str(path)}, success=False)
        same, sig = self.unchanged(path)
        if same:
            return CollectorResult(self.source, "unchanged", {"path": str(path), "records": 0}, success=True, cursor=sig)
        rows = _parse_json_or_csv(path)
        imported = errors = 0
        for row in rows:
            try:
                seed = str(_first(row, "seed_username", "seed", "source_username", "from_username") or "").strip()
                candidate = str(_first(row, "candidate_username", "candidate", "related_username", "to_username") or "").strip()
                if not normalize_username(seed) or not normalize_username(candidate):
                    continue
                platform = str(_first(row, "platform", "site") or "chaturbate")
                source = str(_first(row, "source", "evidence_source") or "neighbor_export")
                kind = str(_first(row, "kind") or "neighbor").strip().casefold()
                if kind not in {"neighbor", "rooms_like_this", "similar_model"}:
                    kind = "neighbor"
                observed_at = str(_first(row, "observed_at", "snapshot_at", "date") or utc_now_iso())
                _, seed_id = self.store.upsert_identity_account(platform, seed, source=source)
                _, cand_id = self.store.upsert_identity_account(platform, candidate, source=source)
                appearances = max(0, _safe_int(_first(row, "appearances", "count", "hits"), 1))
                opportunities = max(appearances, _safe_int(_first(row, "opportunities", "co_online", "eligible_observations"), appearances))
                confidence = max(0.0, min(1.0, _safe_float(_first(row, "confidence"), 1.0)))
                self.store.add_evidence(Evidence(
                    kind=kind, source=source, subject_account_id=seed_id, related_account_id=cand_id,
                    observed_at=observed_at, confidence=confidence,
                    payload={"appearances": appearances, "opportunities": opportunities, "signal_key": f"neighbor:{normalize_username(seed)}:{normalize_username(candidate)}"},
                ))
                imported += 1
            except Exception:
                errors += 1
        return CollectorResult(self.source, "ok" if not errors else "partial", {"path": str(path), "rows": len(rows), "imported": imported, "errors": errors}, success=True, cursor=sig)


class ContinuityExportCollector(FileCollectorBase):
    """Import explicit public account-continuity links (e.g. official old-URL redirect)."""

    source = "continuity_export"

    def run(self) -> CollectorResult:
        if not self.enabled():
            return CollectorResult(self.source, "disabled", {"enabled": False}, success=False)
        path = self.resolve(self.config.get("path"))
        if not path.exists() or not path.is_file():
            return CollectorResult(self.source, "missing", {"path": str(path)}, success=False)
        same, sig = self.unchanged(path)
        if same:
            return CollectorResult(self.source, "unchanged", {"path": str(path), "records": 0}, success=True, cursor=sig)
        rows = _parse_json_or_csv(path)
        imported = errors = 0
        for row in rows:
            try:
                old = str(_first(row, "old_username", "old", "from_username") or "").strip()
                new = str(_first(row, "new_username", "new", "to_username") or "").strip()
                if not normalize_username(old) or not normalize_username(new):
                    continue
                platform = str(_first(row, "platform", "site") or "chaturbate")
                source = str(_first(row, "source", "evidence_source") or "official_redirect")
                observed_at = str(_first(row, "observed_at", "date") or utc_now_iso())
                _, old_id = self.store.upsert_identity_account(platform, old, source=source)
                _, new_id = self.store.upsert_identity_account(platform, new, source=source)
                self.store.add_evidence(Evidence(
                    kind="official_redirect", source=source, subject_account_id=old_id, related_account_id=new_id,
                    observed_at=observed_at, confidence=max(0.0, min(1.0, _safe_float(_first(row, "confidence"), 1.0))),
                    payload={"signal_key": f"official-redirect:{normalize_username(old)}:{normalize_username(new)}"},
                ))
                imported += 1
            except Exception:
                errors += 1
        return CollectorResult(self.source, "ok" if not errors else "partial", {"path": str(path), "rows": len(rows), "imported": imported, "errors": errors}, success=True, cursor=sig)


def _walk_recu_records(data: Any) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield likely performer records from several known local-cache shapes without network access."""
    if isinstance(data, dict):
        for container_key in ("models", "performers", "items", "data", "cache", "records"):
            value = data.get(container_key)
            if isinstance(value, dict):
                for name, payload in value.items():
                    if isinstance(payload, dict):
                        yield str(name), dict(payload)
                return
            if isinstance(value, list):
                for payload in value:
                    if isinstance(payload, dict):
                        name = str(_first(payload, "username", "model", "performer", "name") or "")
                        if name:
                            yield name, dict(payload)
                return
        # Common Recu cache shape is username -> record plus metadata keys such as version/updated_at.
        for name, payload in data.items():
            if name in {"version", "updated_at", "meta", "metadata"}:
                continue
            if isinstance(payload, dict) and normalize_username(str(name)):
                yield str(name), dict(payload)
    elif isinstance(data, list):
        for payload in data:
            if isinstance(payload, dict):
                name = str(_first(payload, "username", "model", "performer", "name") or "")
                if name:
                    yield name, dict(payload)


class RecuLocalArchivesCollector(FileCollectorBase):
    source = "recu_local_archives"

    def run(self) -> CollectorResult:
        if not self.enabled():
            return CollectorResult(self.source, "disabled", {"enabled": False}, success=False)
        raw_paths = self.config.get("paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths = [self.resolve(x) for x in raw_paths if str(x or "").strip()]
        existing = [p for p in paths if p.exists() and p.is_file()]
        if not existing:
            return CollectorResult(self.source, "missing", {"paths": [str(p) for p in paths]}, success=False)
        composite_sig = "v46|" + "|".join(f"{p}:{_file_signature(p)}" for p in existing)
        state = self.store.source_state(self.source)
        if state and state.get("cursor") == composite_sig:
            return CollectorResult(self.source, "unchanged", {"files": len(existing), "imported": 0}, success=True, cursor=composite_sig)
        imported = errors = parsed_records = matched_known = skipped_unknown = 0
        per_file: Dict[str, Any] = {}
        for path in existing:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
                file_parsed = file_matched = file_skipped = 0
                for username, record in _walk_recu_records(data):
                    parsed_records += 1; file_parsed += 1
                    try:
                        existing_account = self.store.find_account("chaturbate", username)
                        # Local Recu archives are enrichment, not a source for creating an unlimited candidate universe.
                        if existing_account is None:
                            skipped_unknown += 1; file_skipped += 1
                            continue
                        matched_known += 1; file_matched += 1
                        last_broadcast = _first(record, "last_broadcast", "lastBroadcast", "last_seen", "lastSeen", "latest_recording")
                        meta: Dict[str, Any] = {"recu_archive_source": path.name}
                        for out_key, keys in {
                            "recu_recordings": ("recordings", "recording_count", "video_count", "videos", "video_meta"),
                            "recu_moments": ("moments", "moment_count", "clips", "clip_count"),
                            "recu_cached_at": ("updated_at", "fetched_at", "checked_at", "last_success_at"),
                        }.items():
                            value = _first(record, *keys)
                            if value in (None, ""):
                                continue
                            if isinstance(value, (list, dict)) and out_key in {"recu_recordings", "recu_moments"}:
                                meta[out_key] = len(value)
                            elif not isinstance(value, (dict, list)):
                                meta[out_key] = value
                        self.store.upsert_identity_account("chaturbate", username, last_seen=str(last_broadcast) if last_broadcast else None, source=self.source, metadata=meta)
                        imported += 1
                    except Exception:
                        errors += 1
                per_file[path.name] = {"parsed": file_parsed, "matched_known": file_matched, "skipped_unknown": file_skipped}
            except Exception:
                errors += 1
                per_file[path.name] = {"error": True}
        return CollectorResult(self.source, "ok" if not errors else "partial", {"files": len(existing), "parsed_records": parsed_records, "matched_known": matched_known, "skipped_unknown": skipped_unknown, "imported": imported, "errors": errors, "per_file": per_file}, success=True, cursor=composite_sig)


class CollectorManager:
    def __init__(self, store: DiscoveryStore, base_dir: Path, config: Mapping[str, Any]):
        self.store = store
        self.base_dir = base_dir
        self.config = dict(config)
        collectors = self.config.get("collectors") if isinstance(self.config.get("collectors"), dict) else {}
        self.collector_configs: Dict[str, Dict[str, Any]] = {str(k): dict(v) for k, v in collectors.items() if isinstance(v, dict)}
        # v0.4.0 used one global lock for every source. A long 50+ MB local Recu parse could
        # therefore make an unrelated loopback Live Control click report merely "busy". Keep
        # a lock per collector instead: duplicate runs of the SAME source are coalesced, while
        # independent local sources can proceed concurrently.
        self._locks: Dict[str, threading.Lock] = {name: threading.Lock() for name in self.names()}

    def _make(self, name: str):
        cfg = self.collector_configs.get(name, {})
        if name == "live_control_models":
            merged = dict(cfg)
            merged.setdefault("base_url", str(self.config.get("live_control_base_url", "http://127.0.0.1:8792")))
            merged.setdefault("timeout_seconds", self.config.get("live_control_probe_timeout_seconds", 2.0))
            return LiveControlModelsCollector(self.store, merged)
        if name == "chaturbate_affiliate":
            return ChaturbateAffiliateCollector(self.store, cfg)
        if name == "recu_discovery_export":
            return RecuDiscoveryExportCollector(self.store, self.base_dir, cfg)
        if name == "neighbor_export":
            return NeighborExportCollector(self.store, self.base_dir, cfg)
        if name == "continuity_export":
            return ContinuityExportCollector(self.store, self.base_dir, cfg)
        if name == "recu_local_archives":
            return RecuLocalArchivesCollector(self.store, self.base_dir, cfg)
        raise KeyError(name)

    def names(self) -> List[str]:
        return ["live_control_models", "chaturbate_affiliate", "recu_discovery_export", "neighbor_export", "continuity_export", "recu_local_archives"]

    def _due(self, name: str) -> bool:
        cfg = self.collector_configs.get(name, {})
        if not cfg.get("enabled"):
            return False
        state = self.store.source_state(getattr(self._make(name), "source", name))
        if not state or not state.get("last_attempt_at"):
            return True
        try:
            last = datetime.fromisoformat(str(state["last_attempt_at"]).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
        except ValueError:
            return True
        return age >= max(30, _safe_int(cfg.get("poll_seconds"), 300))

    def run(self, name: str, force: bool = False) -> Dict[str, Any]:
        if name not in self.names():
            raise KeyError(name)
        cfg = self.collector_configs.get(name, {})
        collector = self._make(name)
        # Direct affiliate collection is a fallback in v0.4. When it is configured but
        # disabled for background polling, an explicit user "Run now" may still perform
        # a one-shot official-API sweep without changing the persisted schedule.
        if force and name == "chaturbate_affiliate" and not cfg.get("enabled") and str(cfg.get("wm", "")).strip():
            one_shot = dict(cfg); one_shot["enabled"] = True
            collector = ChaturbateAffiliateCollector(self.store, one_shot)
        source = collector.source
        if not force and not self._due(name):
            return {"source": source, "status": "not_due", "enabled": bool(cfg.get("enabled"))}
        lock = self._locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"source": source, "status": "busy", "busy_source": name, "detail": "this source already has a run in progress"}
        try:
            result: CollectorResult = collector.run()
            self.store.set_source_state(
                source,
                status=result.status,
                detail=json.dumps(result.detail, sort_keys=True),
                cursor=result.cursor,
                success=result.success,
            )
            return result.as_dict()
        except Exception as exc:
            self.store.set_source_state(source, status="error", detail=f"{type(exc).__name__}: {exc}")
            return {"source": source, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.release()

    def run_due(self) -> Dict[str, Any]:
        out = {}
        for name in self.names():
            if self._due(name):
                out[name] = self.run(name, force=True)
        return out

    def status(self) -> List[Dict[str, Any]]:
        states = {x["source"]: x for x in self.store.source_states()}
        out: List[Dict[str, Any]] = []
        for name in self.names():
            cfg = self.collector_configs.get(name, {})
            source = getattr(self._make(name), "source", name)
            state = states.get(source, {})
            entry = {
                "name": name,
                "source": source,
                "enabled": bool(cfg.get("enabled")),
                "poll_seconds": max(30, _safe_int(cfg.get("poll_seconds"), 300)),
                "due": self._due(name) if cfg.get("enabled") else False,
                "status": state.get("status", "never_run"),
                "last_success_at": state.get("last_success_at"),
                "last_attempt_at": state.get("last_attempt_at"),
                "detail": state.get("detail", ""),
                "running": self._locks.get(name).locked() if name in self._locks else False,
            }
            if name == "live_control_models":
                entry["base_url"] = str(self.config.get("live_control_base_url", "http://127.0.0.1:8792"))
                entry["network_scope"] = "loopback_only"
            elif name == "chaturbate_affiliate":
                entry["configured"] = bool(str(cfg.get("wm", "")).strip())
                entry["endpoint"] = OFFICIAL_AFFILIATE_ENDPOINT
                # Never expose the WM ID through the phone/API status payload.
            else:
                if "path" in cfg:
                    entry["path"] = str(cfg.get("path") or "")
                if "paths" in cfg:
                    entry["paths"] = [str(x) for x in (cfg.get("paths") or [])]
            out.append(entry)
        return out
