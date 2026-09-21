from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 8793,
    "database_path": "state/discovery.sqlite3",
    "mobile_catalog_cache": "",
    "favorite_models_file": "",
    "continue_models_file": "",
    "sync_interval_seconds": 60,
    "default_platform": "chaturbate",
    "external_evidence_inbox": "state/inbox",
    "reviewer_base_url": "http://127.0.0.1:8787",
    "reviewer_probe_timeout_seconds": 2.5,
    "queue_preference_actions": True,
    "live_control_base_url": "http://127.0.0.1:8792",
    "live_control_probe_timeout_seconds": 2.0,
    "recommendation_filters": {
        "allowed_genders": ["m"],
        "include_unknown_gender": True,
        "include_couples": False,
    },
    "collectors": {
        "live_control_models": {
            "enabled": True,
            "poll_seconds": 60,
        },
        "chaturbate_affiliate": {
            "enabled": False,
            "wm": "",
            "poll_seconds": 900,
            "timeout_seconds": 12,
            "page_size": 500,
            "max_pages": 100,
            "candidate_mode": "new_and_known",
            "allowed_genders": [],
        },
        "recu_discovery_export": {
            "enabled": False,
            "path": "",
            "poll_seconds": 300,
        },
        "neighbor_export": {
            "enabled": False,
            "path": "",
            "poll_seconds": 300,
        },
        "continuity_export": {
            "enabled": False,
            "path": "",
            "poll_seconds": 300,
        },
        "recu_local_archives": {
            "enabled": False,
            "paths": [],
            "poll_seconds": 600,
        },
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = value
    return out


def load_config(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    # Deep-copy defaults so runtime Settings updates cannot mutate module globals.
    cfg = _deep_merge({}, DEFAULTS)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        cfg = _deep_merge(cfg, data)
    return cfg
