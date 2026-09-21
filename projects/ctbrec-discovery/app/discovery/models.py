from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PREFERENCE_WEIGHTS = {
    "favorite": 1.00,
    "likely_favorite": 0.80,
    "continue": 0.55,
    "test": 0.00,
    "unsorted": 0.00,
    "low_priority": -0.60,
    "ignore": -1.00,
}

NUMERIC_PRIORITY_TO_LABEL = (
    (1000, "favorite"),
    (800, "likely_favorite"),
    (500, "continue"),
    (250, "test"),
    (50, "unsorted"),
    (10, "low_priority"),
)

LABEL_TO_NUMERIC_PRIORITY = {
    "favorite": 1000,
    "likely_favorite": 800,
    "continue": 500,
    "test": 250,
    "unsorted": 50,
    "low_priority": 10,
    "ignore": 0,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_username(value: str) -> str:
    return "".join(ch for ch in (value or "").strip().casefold() if ch.isalnum() or ch == "_")


def priority_to_label(value: Any) -> str:
    if value is None:
        return "unsorted"
    if isinstance(value, str):
        raw = value.strip().casefold().replace(" ", "_").replace("-", "_")
        aliases = {
            "likelyfavorite": "likely_favorite",
            "lowpriority": "low_priority",
            "not_interested": "ignore",
            "notinterested": "ignore",
            "hidden": "unsorted",  # operational state, not taste evidence
            "paused": "unsorted",
            "suspended": "unsorted",
            "active": "unsorted",
        }
        raw = aliases.get(raw, raw)
        if raw in PREFERENCE_WEIGHTS:
            return raw
        try:
            value = float(raw)
        except ValueError:
            return "unsorted"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "unsorted"
    for threshold, label in NUMERIC_PRIORITY_TO_LABEL:
        if n >= threshold:
            return label
    return "low_priority" if n > 0 else "unsorted"


@dataclass
class Evidence:
    kind: str
    source: str
    subject_account_id: str
    related_account_id: Optional[str] = None
    observed_at: str = field(default_factory=utc_now_iso)
    confidence: float = 1.0
    payload: Dict[str, Any] = field(default_factory=dict)


def evidence_dedupe_key(ev: Evidence) -> str:
    """Return a stable signal key so recurring collectors update rather than inflate evidence volume."""
    kind = (ev.kind or "unknown").strip().casefold()
    source = (ev.source or "unknown").strip().casefold()
    payload = ev.payload or {}
    explicit = payload.get("dedupe_key") or payload.get("signal_key")
    if explicit:
        dimension = str(explicit)
    elif kind == "recu_bookmark_rank":
        dimension = f"period={str(payload.get('period', 'week')).casefold()}"
    elif kind in {"recu_bookmark_momentum", "recu_clip_velocity", "official_redirect", "neighbor", "rooms_like_this", "similar_model"}:
        dimension = "current"
    else:
        # Unknown observation kinds may legitimately vary. Deduplicate only within one UTC day unless
        # the collector supplies a more specific signal key.
        dimension = str(ev.observed_at or "")[:10] or "undated"
    material = {
        "kind": kind,
        "source": source,
        "subject": ev.subject_account_id,
        "related": ev.related_account_id or "",
        "dimension": dimension,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass
class Recommendation:
    account_id: str
    identity_id: str
    username: str
    platform: str
    score: float
    confidence: float
    components: Dict[str, float]
    reasons: List[str]
    source_count: int
    metadata: Dict[str, Any]
