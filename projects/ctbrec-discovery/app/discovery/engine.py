from __future__ import annotations
import json
import threading
from pathlib import Path
from typing import Any, Dict

from .continuity import suggest_continuations
from .collectors import CollectorManager
from .importers import import_catalog, import_legacy_list
from .integration import ReviewerIntegration
from .models import Evidence, LABEL_TO_NUMERIC_PRIORITY, priority_to_label
from .scoring import rank_recommendations
from .store import DiscoveryStore


class DiscoveryEngine:
    def __init__(self, config: Dict[str, Any], base_dir: Path):
        self.config = config
        self.base_dir = base_dir
        db_path = Path(config["database_path"])
        if not db_path.is_absolute():
            db_path = base_dir / db_path
        self.store = DiscoveryStore(db_path)
        self.integration = ReviewerIntegration(config, base_dir)
        self.collectors = CollectorManager(self.store, base_dir, config)
        self.last_integration_status: Dict[str, Any] = {"not_checked": True}
        self.stop_event = threading.Event()
        self.thread = None
        self._sync_lock = threading.Lock()
        self._sync_state_lock = threading.Lock()
        self._sync_state: Dict[str, Any] = {"running": False, "last_result": None, "last_error": None}

    def _resolve(self, raw: str) -> Path | None:
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else self.base_dir / p

    def sync_local_sources(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        catalog = self._resolve(str(self.config.get("mobile_catalog_cache", "")))
        if catalog:
            try:
                results["catalog"] = import_catalog(catalog, self.store, self.config.get("default_platform", "chaturbate"))
            except Exception as exc:
                self.store.set_source_state("mobile_reviewer_catalog", status="error", detail=str(exc))
                results["catalog"] = {"error": str(exc)}
        for key, label in (("favorite_models_file", "favorite"), ("continue_models_file", "continue")):
            p = self._resolve(str(self.config.get(key, "")))
            if p:
                try:
                    results[key] = import_legacy_list(p, self.store, label, self.config.get("default_platform", "chaturbate"))
                except Exception as exc:
                    self.store.set_source_state(p.name, status="error", detail=str(exc))
                    results[key] = {"error": str(exc)}
        results["inbox"] = self.ingest_inbox()
        try:
            self.last_integration_status = self.integration.status()
            reviewer = self.last_integration_status.get("reviewer", {})
            status = "reachable" if reviewer.get("reachable") else "offline"
            if reviewer.get("auth_required"):
                status = "reachable_auth_required"
            self.store.set_source_state("mobile_reviewer_service", status=status, detail=json.dumps(reviewer, sort_keys=True), success=bool(reviewer.get("reachable")))
        except Exception as exc:
            self.last_integration_status = {"error": f"{type(exc).__name__}: {exc}"}
            self.store.set_source_state("mobile_reviewer_service", status="error", detail=str(exc))
        results["integration"] = self.last_integration_status
        results["collectors"] = self.collectors.run_due()
        return results

    def ingest_inbox(self) -> Dict[str, int]:
        inbox = self._resolve(str(self.config.get("external_evidence_inbox", "state/inbox")))
        if inbox is None:
            return {"files": 0, "records": 0, "errors": 0}
        inbox.mkdir(parents=True, exist_ok=True)
        done = inbox / "processed"
        done.mkdir(exist_ok=True)
        files = records = errors = 0
        for path in sorted(inbox.glob("*.jsonl")):
            files += 1
            ok = True
            for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    self.ingest_payload(payload)
                    records += 1
                except Exception:
                    errors += 1
                    ok = False
            if ok:
                target = done / path.name
                if target.exists():
                    target = done / f"{path.stem}_{path.stat().st_mtime_ns}{path.suffix}"
                path.replace(target)
        return {"files": files, "records": records, "errors": errors}

    def ingest_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(payload.get("kind", "")).strip()
        if not kind:
            raise ValueError("kind is required")
        source = str(payload.get("source", "external")).strip() or "external"
        subject = payload.get("subject", {})
        if not isinstance(subject, dict):
            raise ValueError("subject must be object")
        sid, sa = self.store.upsert_identity_account(
            subject.get("platform", self.config.get("default_platform", "chaturbate")), subject.get("username", ""),
            first_seen=subject.get("first_seen"), last_seen=subject.get("last_seen"), source=source,
            metadata=subject.get("metadata") if isinstance(subject.get("metadata"), dict) else {},
        )
        related_account = None
        related_identity = None
        related = payload.get("related")
        if isinstance(related, dict) and related.get("username"):
            related_identity, related_account = self.store.upsert_identity_account(
                related.get("platform", subject.get("platform", self.config.get("default_platform", "chaturbate"))),
                related.get("username", ""), first_seen=related.get("first_seen"), last_seen=related.get("last_seen"), source=source,
                metadata=related.get("metadata") if isinstance(related.get("metadata"), dict) else {},
            )
        ev = Evidence(
            kind=kind, source=source, subject_account_id=sa, related_account_id=related_account,
            confidence=float(payload.get("confidence", 1.0)), payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
        )
        if payload.get("observed_at"):
            ev.observed_at = str(payload["observed_at"])
        ev_id = self.store.add_evidence(ev)
        return {"evidence_id": ev_id, "subject_identity_id": sid, "subject_account_id": sa,
                "related_identity_id": related_identity, "related_account_id": related_account}

    def set_feedback(self, identity_id: str, label: str, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        normalized = priority_to_label(label)
        event_id = self.store.set_preference(identity_id, normalized, source="discovery_ui", metadata=metadata)
        action_id = None
        if bool(self.config.get("queue_preference_actions", True)):
            accounts = self.store.accounts_for_identity(identity_id)
            action_id = self.store.enqueue_action(
                "preference_sync_request",
                identity_id=identity_id,
                account_id=accounts[0]["id"] if len(accounts) == 1 else None,
                payload={
                    "label": normalized,
                    "numeric_priority": LABEL_TO_NUMERIC_PRIORITY[normalized],
                    "accounts": [{"id": a["id"], "platform": a["platform"], "username": a["username"]} for a in accounts],
                    "target": "verified_mobile_reviewer_live_bridge_adapter",
                    "execute": False,
                },
            )
        return {"ok": True, "feedback_id": event_id, "identity_id": identity_id, "label": normalized, "outbox_action_id": action_id}

    def undo_last_feedback(self) -> Dict[str, Any]:
        result = self.store.undo_last_feedback()
        action_id = None
        if bool(self.config.get("queue_preference_actions", True)):
            accounts = self.store.accounts_for_identity(result["identity_id"])
            restored = result["restored_label"]
            action_id = self.store.enqueue_action(
                "preference_sync_request",
                identity_id=result["identity_id"],
                account_id=accounts[0]["id"] if len(accounts) == 1 else None,
                payload={
                    "label": restored,
                    "numeric_priority": LABEL_TO_NUMERIC_PRIORITY[restored],
                    "accounts": [{"id": a["id"], "platform": a["platform"], "username": a["username"]} for a in accounts],
                    "target": "verified_mobile_reviewer_live_bridge_adapter",
                    "execute": False,
                    "reason": "undo",
                },
            )
        result.update({"ok": True, "outbox_action_id": action_id})
        return result

    def reject_continuation(self, old_identity_id: str, new_identity_id: str) -> Dict[str, Any]:
        self.store.set_identity_link_decision(old_identity_id, new_identity_id, "rejected", confidence=0.0, reasons=["user_rejected_in_discovery_ui"])
        return {"ok": True, "status": "rejected"}

    def recommendations(self, limit: int = 100):
        return rank_recommendations(
            self.store, limit=limit,
            filters=self.config.get("recommendation_filters") if isinstance(self.config.get("recommendation_filters"), dict) else None,
        )

    def continuations(self, limit: int = 100):
        return suggest_continuations(self.store, limit=limit)

    def integration_status(self, refresh: bool = False) -> Dict[str, Any]:
        if refresh or self.last_integration_status.get("not_checked"):
            self.last_integration_status = self.integration.status()
        return {**self.last_integration_status, "outbox": self.store.action_counts()}

    def diagnostics(self) -> Dict[str, Any]:
        from collections import Counter
        accounts = self.store.list_accounts()
        prefs = Counter(str(a.get("preference_label") or "unsorted") for a in accounts)
        genders = Counter(str((a.get("metadata") or {}).get("gender") or "unknown").casefold() for a in accounts)
        evidence = Counter(str(e.get("kind") or "unknown") for e in self.store.all_evidence())
        filters = self.config.get("recommendation_filters") if isinstance(self.config.get("recommendation_filters"), dict) else {}
        return {
            "accounts_total": len(accounts),
            "preference_counts": dict(prefs),
            "gender_counts": dict(genders),
            "evidence_counts": dict(evidence),
            "recommendation_filters": filters,
            "sync": self.sync_status(),
            "notes": {
                "score": "ranking index 0-100, not a probability",
                "test_label": "workflow-uncertain; neutral in taste learning as of v0.4",
            },
        }

    def collector_status(self) -> Dict[str, Any]:
        return {"items": self.collectors.status()}

    def run_collector(self, name: str) -> Dict[str, Any]:
        return self.collectors.run(name, force=True)

    def sync_status(self) -> Dict[str, Any]:
        with self._sync_state_lock:
            return dict(self._sync_state)

    def request_sync(self) -> Dict[str, Any]:
        if self._sync_lock.locked():
            return {"ok": True, "started": False, "status": "already_running", **self.sync_status()}

        def worker():
            if not self._sync_lock.acquire(blocking=False):
                return
            try:
                with self._sync_state_lock:
                    self._sync_state.update({"running": True, "last_error": None})
                result = self.sync_local_sources()
                with self._sync_state_lock:
                    self._sync_state.update({"running": False, "last_result": result, "last_error": None})
            except Exception as exc:
                with self._sync_state_lock:
                    self._sync_state.update({"running": False, "last_error": f"{type(exc).__name__}: {exc}"})
                self.store.set_source_state("manual_sync", status="error", detail=str(exc))
            finally:
                self._sync_lock.release()

        threading.Thread(target=worker, name="DiscoveryManualSync", daemon=True).start()
        return {"ok": True, "started": True, "status": "running"}

    def start_background(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="DiscoverySync", daemon=True)
        self.thread.start()

    def _loop(self):
        interval = max(60, int(self.config.get("sync_interval_seconds", 300)))
        while not self.stop_event.is_set():
            if self._sync_lock.acquire(blocking=False):
                try:
                    with self._sync_state_lock:
                        self._sync_state.update({"running": True, "last_error": None})
                    result = self.sync_local_sources()
                    with self._sync_state_lock:
                        self._sync_state.update({"running": False, "last_result": result, "last_error": None})
                except Exception as exc:
                    with self._sync_state_lock:
                        self._sync_state.update({"running": False, "last_error": f"{type(exc).__name__}: {exc}"})
                    self.store.set_source_state("background_sync", status="error", detail=str(exc))
                finally:
                    self._sync_lock.release()
            self.stop_event.wait(interval)
