from __future__ import annotations

import math
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Set

from .models import PREFERENCE_WEIGHTS
from .store import DiscoveryStore


def _tokens(value: str) -> Set[str]:
    return {x for x in re.findall(r"[a-z0-9]{3,}", (value or "").casefold())}


def _jaccard(a: Set[Any], b: Set[Any]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_time(v: Any):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_set(raw: Any) -> Set[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\s,#]+", raw)
    if isinstance(raw, dict):
        raw = [k for k, v in raw.items() if v]
    return {str(x).strip().casefold() for x in (raw or []) if str(x).strip()}


def _official_redirect_pairs(store: DiscoveryStore):
    pairs = set()
    for e in store.all_evidence():
        if e["kind"] == "official_redirect" and e.get("related_account_id"):
            pairs.add((e["subject_account_id"], e["related_account_id"]))
            pairs.add((e["related_account_id"], e["subject_account_id"]))
    return pairs


def _rejected_identity_pairs(store: DiscoveryStore):
    pairs = set()
    for row in store.link_decisions("rejected"):
        pairs.add((row["left_identity_id"], row["right_identity_id"]))
        pairs.add((row["right_identity_id"], row["left_identity_id"]))
    return pairs


def suggest_continuations(store: DiscoveryStore, limit: int = 100) -> List[Dict[str, Any]]:
    accounts = store.list_accounts()
    official = _official_redirect_pairs(store)
    rejected = _rejected_identity_pairs(store)
    preferred = [a for a in accounts if PREFERENCE_WEIGHTS.get(a["preference_label"], 0.0) >= 0.25]
    candidates = [a for a in accounts if a["preference_label"] in {"unsorted", "test"}]
    results = []

    for old in preferred:
        om = old.get("metadata", {})
        old_tags = _as_set(om.get("tags"))
        old_hours = _as_set(om.get("active_hours"))
        old_text = _tokens(str(om.get("profile_text", "")))
        old_last = _parse_time(old.get("last_seen") or om.get("last_seen"))
        for new in candidates:
            if old["identity_id"] == new["identity_id"] or old["platform"] != new["platform"]:
                continue
            if (old["identity_id"], new["identity_id"]) in rejected:
                continue
            reasons=[]
            if (old["id"], new["id"]) in official:
                score=0.995
                reasons.append("Official old-profile redirect/link evidence")
            else:
                nm = new.get("metadata", {})
                uname = SequenceMatcher(None, old["normalized_username"], new["normalized_username"]).ratio()
                tags = _jaccard(old_tags, _as_set(nm.get("tags")))
                hours = _jaccard(old_hours, _as_set(nm.get("active_hours")))
                text = _jaccard(old_text, _tokens(str(nm.get("profile_text", ""))))
                language = 1.0 if om.get("language") and om.get("language") == nm.get("language") else 0.0
                temporal = 0.0
                new_first = _parse_time(new.get("first_seen") or nm.get("first_seen"))
                if old_last and new_first:
                    gap = abs((new_first - old_last).total_seconds()) / 86400.0
                    temporal = math.exp(-gap / 30.0) if gap <= 180 else 0.0
                score = 0.35*uname + 0.20*tags + 0.10*hours + 0.10*text + 0.05*language + 0.20*temporal
                if uname >= 0.75: reasons.append("Username morphology is similar")
                if tags >= 0.45: reasons.append("Strong tag overlap")
                if hours >= 0.45: reasons.append("Streaming-hour pattern overlaps")
                if text >= 0.35: reasons.append("Public profile-text terms overlap")
                if language: reasons.append("Language matches")
                if temporal >= 0.5: reasons.append("Old/new timing is consistent with a transition")
            if score < 0.50:
                continue
            band = "A" if score >= 0.95 else "B" if score >= 0.80 else "C" if score >= 0.65 else "D"
            results.append({
                "old_identity_id": old["identity_id"], "old_account_id": old["id"], "old_username": old["username"],
                "new_identity_id": new["identity_id"], "new_account_id": new["id"], "new_username": new["username"],
                "platform": old["platform"], "score": round(score, 4), "confidence_band": band,
                "reasons": reasons or ["Weak multi-signal similarity; manual review only"],
            })
    results.sort(key=lambda x: (x["score"], x["new_username"].casefold()), reverse=True)
    return results[:max(1, min(1000, int(limit)))]
