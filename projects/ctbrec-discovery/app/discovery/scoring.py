from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Set, Tuple

from .models import PREFERENCE_WEIGHTS, Recommendation
from .store import DiscoveryStore

DEFAULT_WEIGHTS = {
    "preference_match": 0.45,
    "neighbor": 0.25,
    "recu_momentum": 0.15,
    "context": 0.05,
    "exploration": 0.10,
}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _tags(account: Dict[str, Any]) -> Set[str]:
    raw = account.get("metadata", {}).get("tags", [])
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace("#", " ").replace(",", " ").split()]
    if isinstance(raw, dict):
        raw = [k for k, v in raw.items() if v]
    return {str(x).strip().casefold() for x in (raw or []) if str(x).strip()}


def _active_hours(account: Dict[str, Any]) -> Set[int]:
    raw = account.get("metadata", {}).get("active_hours", [])
    out = set()
    if isinstance(raw, dict):
        raw = [k for k, v in raw.items() if v]
    for x in raw or []:
        try:
            out.add(int(x) % 24)
        except (TypeError, ValueError):
            pass
    return out


def _jaccard(a: Set[Any], b: Set[Any]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _exploration_score(account_id: str) -> float:
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    digest = hashlib.sha256(f"{account_id}:{year}:{week}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _freshness(ev: Dict[str, Any], half_life_days: float) -> float:
    dt = _parse_dt(ev.get("observed_at"))
    if not dt:
        return 0.7
    age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(0.25, half_life_days))


def _recu_signal(evidence: Iterable[Dict[str, Any]]) -> Tuple[float, List[str], int]:
    values = []
    reasons = []
    sources = set()
    period_weight = {"today": 0.8, "week": 1.0, "month": 0.7, "all_time": 0.25, "alltime": 0.25}
    period_half_life = {"today": 2.0, "week": 10.0, "month": 45.0, "all_time": 365.0, "alltime": 365.0}
    for e in evidence:
        if e["kind"] not in {"recu_bookmark_rank", "recu_clip_velocity", "recu_bookmark_momentum"}:
            continue
        p = e.get("payload", {})
        conf = float(e.get("confidence", 1.0))
        sources.add(e.get("source", "recu"))
        if e["kind"] == "recu_bookmark_rank":
            rank = max(1, int(p.get("rank", 999999)))
            total = max(rank, int(p.get("total", rank)))
            percentile = 1.0 - ((rank - 1) / max(1, total - 1))
            period = str(p.get("period", "week")).casefold()
            freshness = _freshness(e, period_half_life.get(period, 30.0))
            values.append(percentile * period_weight.get(period, 0.5) * conf * freshness)
            reasons.append(f"Recu {period} bookmark rank #{rank}")
        elif e["kind"] == "recu_clip_velocity":
            clips = max(0.0, float(p.get("clips_7d", p.get("clips", 0))))
            values.append((1.0 - math.exp(-clips / 8.0)) * 0.7 * conf * _freshness(e, 7.0))
            if clips:
                reasons.append(f"{int(clips)} recent Recu clip(s)")
        else:
            delta = float(p.get("delta_percentile", p.get("momentum", 0.0)))
            values.append(_clamp(0.5 + delta / 2.0) * conf * _freshness(e, 10.0))
            reasons.append("Recu bookmark momentum rising")
    if not values:
        return 0.0, [], 0
    values.sort(reverse=True)
    score = values[0] + sum(values[1:3]) * 0.2
    return _clamp(score), reasons[:3], len(sources)




def _new_account_signal(evidence: Iterable[Dict[str, Any]]) -> float:
    best = 0.0
    for e in evidence:
        if e.get("kind") != "new_account_seen":
            continue
        best = max(best, float(e.get("confidence", 1.0)) * _freshness(e, 14.0))
    return _clamp(best)

def _gender_code(account: Dict[str, Any]) -> str:
    raw = str(account.get("metadata", {}).get("gender", "") or "").strip().casefold()
    aliases = {
        "male": "m", "man": "m", "men": "m",
        "female": "f", "woman": "f", "women": "f",
        "couple": "c", "couples": "c",
        "trans": "t", "transgender": "t",
    }
    return aliases.get(raw, raw[:1] if raw else "")


def _eligible(account: Dict[str, Any], filters: Dict[str, Any] | None) -> bool:
    filters = filters or {}
    gender = _gender_code(account)
    if not gender:
        return bool(filters.get("include_unknown_gender", True))
    if gender == "c":
        return bool(filters.get("include_couples", False))
    allowed = {str(x).strip().casefold() for x in (filters.get("allowed_genders") or []) if str(x).strip()}
    allowed = {({"male":"m","female":"f","trans":"t"}.get(x, x)) for x in allowed}
    return gender in allowed


def rank_recommendations(store: DiscoveryStore, limit: int = 100, include_known: bool = False, weights: Dict[str, float] | None = None, filters: Dict[str, Any] | None = None) -> List[Recommendation]:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    accounts = store.list_accounts()
    if not accounts:
        return []
    by_id = {a["id"]: a for a in accounts}
    positives = [a for a in accounts if PREFERENCE_WEIGHTS.get(a["preference_label"], 0.0) >= 0.25]
    negatives = [a for a in accounts if PREFERENCE_WEIGHTS.get(a["preference_label"], 0.0) <= -0.5]
    all_ev = store.all_evidence()
    ev_by_subject = defaultdict(list)
    neighbor_to = defaultdict(list)
    for e in all_ev:
        ev_by_subject[e["subject_account_id"]].append(e)
        if e["kind"] in {"neighbor", "rooms_like_this", "similar_model"} and e.get("related_account_id"):
            neighbor_to[e["related_account_id"]].append(e)

    tag_df = Counter()
    for a in accounts:
        for tag in _tags(a):
            tag_df[tag] += 1
    n_accounts = max(1, len(accounts))

    out: List[Recommendation] = []
    for cand in accounts:
        label = cand["preference_label"]
        if not _eligible(cand, filters):
            continue
        if not include_known and label not in {"unsorted", "test"}:
            continue

        cand_tags = _tags(cand)
        weighted_pos = 0.0
        pos_weight_sum = 0.0
        for seed in positives:
            sw = max(0.0, PREFERENCE_WEIGHTS.get(seed["preference_label"], 0.0))
            if seed["identity_id"] == cand["identity_id"]:
                continue
            seed_tags = _tags(seed)
            shared = cand_tags & seed_tags
            if shared:
                rarity = sum(math.log((n_accounts + 1) / (tag_df[t] + 1)) + 1.0 for t in shared)
                union = cand_tags | seed_tags
                denom = sum(math.log((n_accounts + 1) / (tag_df[t] + 1)) + 1.0 for t in union) or 1.0
                sim = rarity / denom
            else:
                sim = 0.0
            weighted_pos += sim * sw
            pos_weight_sum += sw
        pos_sim = weighted_pos / pos_weight_sum if pos_weight_sum else 0.0

        weighted_neg = 0.0
        neg_weight_sum = 0.0
        for seed in negatives:
            nw = abs(PREFERENCE_WEIGHTS.get(seed["preference_label"], 0.0))
            sim = _jaccard(cand_tags, _tags(seed))
            weighted_neg += sim * nw
            neg_weight_sum += nw
        neg_sim = weighted_neg / neg_weight_sum if neg_weight_sum else 0.0
        preference_match = _clamp(0.5 + 0.75 * pos_sim - 0.85 * neg_sim)

        neighbor_raw = 0.0
        neighbor_reasons = []
        neighbor_sources = set()
        neighbor_count = 0
        for e in neighbor_to.get(cand["id"], []):
            seed = by_id.get(e["subject_account_id"])
            if not seed:
                continue
            sw = PREFERENCE_WEIGHTS.get(seed["preference_label"], 0.0)
            if sw <= 0:
                continue
            p = e.get("payload", {})
            appearances = max(0.0, float(p.get("appearances", p.get("count", 1.0))))
            opportunities = max(appearances, float(p.get("opportunities", max(appearances, 1.0))))
            edge = (appearances + 1.0) / (opportunities + 4.0)  # Beta(1,3) shrinkage.
            freshness = _freshness(e, 60.0 if e["kind"] == "rooms_like_this" else 90.0)
            strength = edge * float(e.get("confidence", 1.0)) * sw * freshness
            neighbor_raw += strength
            neighbor_count += 1
            neighbor_sources.add(e.get("source", "unknown"))
            if len(neighbor_reasons) < 3:
                neighbor_reasons.append(f"Neighbor of {seed['username']} ({e.get('source','source')})")
        neighbor = _clamp(1.0 - math.exp(-neighbor_raw))

        recu, recu_reasons, recu_sources = _recu_signal(ev_by_subject.get(cand["id"], []))
        cand_hours = _active_hours(cand)
        context_vals = [_jaccard(cand_hours, _active_hours(seed)) for seed in positives if _active_hours(seed)]
        hour_context = max(context_vals, default=0.0)
        new_account = _new_account_signal(ev_by_subject.get(cand["id"], []))
        # Newness is useful as a discovery reason, but intentionally remains a small contextual
        # nudge rather than a popularity substitute. The entire context component is only 5%.
        context = max(hour_context, new_account * 0.60)
        exploration = _exploration_score(cand["id"])

        components = {
            "preference_match": preference_match,
            "neighbor": neighbor,
            "recu_momentum": recu,
            "context": context,
            "exploration": exploration,
        }
        weighted = sum(weights[k] * components[k] for k in DEFAULT_WEIGHTS)
        score = round(_clamp(weighted) * 100.0, 2)
        evidence_volume = neighbor_count + len(ev_by_subject.get(cand["id"], []))
        source_count = len(neighbor_sources) + recu_sources + (1 if cand.get("source") else 0)
        confidence = round(_clamp(1.0 - math.exp(-(evidence_volume + source_count) / 5.0)), 3)

        reasons = []
        if pos_sim >= 0.15:
            reasons.append("Matches tags seen in positive models")
        if neg_sim >= 0.20:
            reasons.append("Some overlap with low-priority models")
        reasons.extend(neighbor_reasons)
        reasons.extend(recu_reasons)
        if hour_context >= 0.25:
            reasons.append("Active hours overlap with positive models")
        if new_account >= 0.25:
            reasons.append("Recently surfaced as a new account in the official affiliate feed")
        if exploration >= 0.90:
            reasons.append("Wildcard exploration candidate")
        if not reasons:
            reasons.append("Low-evidence candidate; needs more signals")

        derived_meta = dict(cand.get("metadata", {}))
        derived_meta["recommendation_evidence_level"] = "high" if confidence >= 0.70 else ("medium" if confidence >= 0.40 else "low")
        derived_meta["score_is_probability"] = False
        out.append(Recommendation(
            account_id=cand["id"], identity_id=cand["identity_id"], username=cand["username"], platform=cand["platform"],
            score=score, confidence=confidence, components={k: round(v, 4) for k, v in components.items()},
            reasons=reasons[:6], source_count=source_count, metadata=derived_meta,
        ))
    out.sort(key=lambda r: (r.score, r.confidence, r.username.casefold()), reverse=True)
    return out[:max(1, min(1000, int(limit)))]
