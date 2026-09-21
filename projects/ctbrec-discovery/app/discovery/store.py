from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import Evidence, evidence_dedupe_key, normalize_username, priority_to_label, utc_now_iso

SCHEMA_VERSION = 2


class DiscoveryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def conn(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _columns(c: sqlite3.Connection, table: str) -> set[str]:
        return {str(r[1]) for r in c.execute(f"PRAGMA table_info({table})").fetchall()}

    def _init_db(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identities(
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    preference_label TEXT NOT NULL DEFAULT 'unsorted',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    username TEXT NOT NULL,
                    normalized_username TEXT NOT NULL,
                    profile_url TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    source TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, normalized_username)
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_identity ON accounts(identity_id);
                CREATE INDEX IF NOT EXISTS idx_accounts_seen ON accounts(last_seen, first_seen);
                CREATE TABLE IF NOT EXISTS evidence(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    subject_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    related_account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
                    observed_at TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    dedupe_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_subject ON evidence(subject_account_id, kind);
                CREATE INDEX IF NOT EXISTS idx_evidence_related ON evidence(related_account_id, kind);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_dedupe ON evidence(dedupe_key) WHERE dedupe_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS feedback(
                    id TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_identity ON feedback(identity_id, created_at);
                CREATE TABLE IF NOT EXISTS identity_links(
                    id TEXT PRIMARY KEY,
                    left_identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                    right_identity_id TEXT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(left_identity_id, right_identity_id)
                );
                CREATE TABLE IF NOT EXISTS source_state(
                    source TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_attempt_at TEXT,
                    cursor TEXT,
                    status TEXT,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS action_outbox(
                    id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    identity_id TEXT REFERENCES identities(id) ON DELETE SET NULL,
                    account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_action_outbox_state ON action_outbox(state, created_at);
                """
            )
            # Upgrade databases first created by v0.1.0 without deleting any user state.
            if "dedupe_key" not in self._columns(c, "evidence"):
                c.execute("ALTER TABLE evidence ADD COLUMN dedupe_key TEXT")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_dedupe ON evidence(dedupe_key) WHERE dedupe_key IS NOT NULL")
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))

    @staticmethod
    def _best_seen(existing: Optional[str], incoming: Optional[str], *, earliest: bool) -> Optional[str]:
        if not incoming:
            return existing
        if not existing:
            return incoming
        def parse(value: str):
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                return None
        a, b = parse(existing), parse(incoming)
        if a is not None and b is not None:
            return incoming if (b < a if earliest else b > a) else existing
        # If timestamps are not parseable, preserve the existing value rather than allowing
        # a lower-quality enrichment source to overwrite it.
        return existing

    def find_account(self, platform: str, username: str) -> Optional[Dict[str, Any]]:
        platform = (platform or "chaturbate").strip().casefold()
        norm = normalize_username(username)
        if not norm:
            return None
        with self.conn() as c:
            r = c.execute(
                """SELECT a.*, i.display_name, i.preference_label FROM accounts a
                   JOIN identities i ON i.id=a.identity_id
                   WHERE a.platform=? AND a.normalized_username=?""",
                (platform, norm),
            ).fetchone()
        return self._account_row(r) if r else None

    def upsert_identity_account(
        self,
        platform: str,
        username: str,
        *,
        preference_label: str = "unsorted",
        display_name: Optional[str] = None,
        profile_url: Optional[str] = None,
        first_seen: Optional[str] = None,
        last_seen: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        platform = (platform or "chaturbate").strip().casefold()
        username = (username or "").strip()
        norm = normalize_username(username)
        if not norm:
            raise ValueError("username is required")
        now = utc_now_iso()
        preference_label = priority_to_label(preference_label)
        with self.conn() as c:
            row = c.execute(
                "SELECT id, identity_id, metadata_json, first_seen, last_seen FROM accounts WHERE platform=? AND normalized_username=?",
                (platform, norm),
            ).fetchone()
            if row:
                account_id = row["id"]
                identity_id = row["identity_id"]
                old_meta = json.loads(row["metadata_json"] or "{}")
                if metadata:
                    old_meta.update(metadata)
                best_first = self._best_seen(row["first_seen"], first_seen, earliest=True)
                best_last = self._best_seen(row["last_seen"], last_seen, earliest=False)
                c.execute(
                    """UPDATE accounts SET username=?, profile_url=COALESCE(?, profile_url),
                       first_seen=?, last_seen=?, source=COALESCE(source, ?),
                       metadata_json=?, updated_at=? WHERE id=?""",
                    (username, profile_url, best_first, best_last, source, json.dumps(old_meta, sort_keys=True), now, account_id),
                )
                if preference_label != "unsorted":
                    c.execute(
                        "UPDATE identities SET preference_label=?, updated_at=? WHERE id=?",
                        (preference_label, now, identity_id),
                    )
                return identity_id, account_id

            identity_id = str(uuid.uuid4())
            account_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO identities(id,display_name,preference_label,created_at,updated_at) VALUES(?,?,?,?,?)",
                (identity_id, display_name or username, preference_label, now, now),
            )
            c.execute(
                """INSERT INTO accounts(id,identity_id,platform,username,normalized_username,profile_url,first_seen,last_seen,source,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, identity_id, platform, username, norm, profile_url, first_seen, last_seen, source,
                 json.dumps(metadata or {}, sort_keys=True), now, now),
            )
            return identity_id, account_id

    def remove_safe_import_artifact_account(self, platform: str, username: str, *, source: str) -> bool:
        """Remove a clearly importer-created placeholder account only when it has no user/evidence state.

        v0.4.0 briefly misread Mobile Reviewer's outer catalog buckets as usernames. This
        cleanup is intentionally conservative: it only deletes a single-account unsorted
        identity created by the named importer when there is no evidence, feedback, outbox
        action, or identity-link history attached to it.
        """
        platform = (platform or "chaturbate").strip().casefold()
        norm = normalize_username(username)
        if not norm:
            return False
        with self.conn() as c:
            row = c.execute(
                """SELECT a.id account_id, a.identity_id, a.source, i.preference_label
                   FROM accounts a JOIN identities i ON i.id=a.identity_id
                   WHERE a.platform=? AND a.normalized_username=?""",
                (platform, norm),
            ).fetchone()
            if not row or str(row["source"] or "") != str(source) or str(row["preference_label"] or "unsorted") != "unsorted":
                return False
            aid, iid = str(row["account_id"]), str(row["identity_id"])
            checks = [
                ("SELECT COUNT(*) FROM accounts WHERE identity_id=?", (iid,)),
                ("SELECT COUNT(*) FROM evidence WHERE subject_account_id=? OR related_account_id=?", (aid, aid)),
                ("SELECT COUNT(*) FROM feedback WHERE identity_id=?", (iid,)),
                ("SELECT COUNT(*) FROM action_outbox WHERE identity_id=? OR account_id=?", (iid, aid)),
                ("SELECT COUNT(*) FROM identity_links WHERE left_identity_id=? OR right_identity_id=?", (iid, iid)),
            ]
            counts = [int(c.execute(sql, args).fetchone()[0]) for sql, args in checks]
            if counts[0] != 1 or any(counts[1:]):
                return False
            c.execute("DELETE FROM identities WHERE id=?", (iid,))
            return True

    def add_evidence(self, ev: Evidence) -> str:
        key = evidence_dedupe_key(ev)
        ev_id = str(uuid.uuid4())
        with self.conn() as c:
            existing = c.execute("SELECT id FROM evidence WHERE dedupe_key=?", (key,)).fetchone()
            if existing:
                ev_id = existing["id"]
                c.execute(
                    """UPDATE evidence SET observed_at=?, confidence=?, payload_json=? WHERE id=?""",
                    (ev.observed_at, max(0.0, min(1.0, float(ev.confidence))), json.dumps(ev.payload or {}, sort_keys=True), ev_id),
                )
            else:
                c.execute(
                    """INSERT INTO evidence(id,kind,source,subject_account_id,related_account_id,observed_at,confidence,payload_json,dedupe_key)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (ev_id, ev.kind, ev.source, ev.subject_account_id, ev.related_account_id, ev.observed_at,
                     max(0.0, min(1.0, float(ev.confidence))), json.dumps(ev.payload or {}, sort_keys=True), key),
                )
        return ev_id

    def set_preference(self, identity_id: str, label: str, source: str = "user", metadata: Optional[Dict[str, Any]] = None) -> str:
        label = priority_to_label(label)
        now = utc_now_iso()
        with self.conn() as c:
            current = c.execute("SELECT preference_label FROM identities WHERE id=?", (identity_id,)).fetchone()
            if not current:
                raise KeyError(identity_id)
            previous = str(current["preference_label"])
            c.execute("UPDATE identities SET preference_label=?, updated_at=? WHERE id=?", (label, now, identity_id))
            event_id = str(uuid.uuid4())
            meta = dict(metadata or {})
            meta.setdefault("previous_label", previous)
            c.execute(
                "INSERT INTO feedback(id,identity_id,label,source,created_at,metadata_json) VALUES(?,?,?,?,?,?)",
                (event_id, identity_id, label, source, now, json.dumps(meta, sort_keys=True)),
            )
            return event_id

    def undo_last_feedback(self) -> Dict[str, Any]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM feedback ORDER BY created_at DESC, rowid DESC LIMIT 100").fetchall()
            chosen = None
            meta = None
            for row in rows:
                candidate_meta = json.loads(row["metadata_json"] or "{}")
                if not candidate_meta.get("undone_at"):
                    chosen = row
                    meta = candidate_meta
                    break
            if chosen is None or meta is None:
                raise KeyError("no feedback to undo")
            current = c.execute("SELECT preference_label FROM identities WHERE id=?", (chosen["identity_id"],)).fetchone()
            if not current:
                raise KeyError(chosen["identity_id"])
            if str(current["preference_label"]) != str(chosen["label"]):
                raise ValueError("latest feedback no longer matches current preference; refusing ambiguous undo")
            previous = priority_to_label(meta.get("previous_label", "unsorted"))
            now = utc_now_iso()
            c.execute("UPDATE identities SET preference_label=?, updated_at=? WHERE id=?", (previous, now, chosen["identity_id"]))
            meta["undone_at"] = now
            c.execute("UPDATE feedback SET metadata_json=? WHERE id=?", (json.dumps(meta, sort_keys=True), chosen["id"]))
            return {"feedback_id": chosen["id"], "identity_id": chosen["identity_id"], "restored_label": previous, "reverted_label": chosen["label"]}

    def list_accounts(self) -> List[Dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                """SELECT a.*, i.display_name, i.preference_label FROM accounts a
                   JOIN identities i ON i.id=a.identity_id ORDER BY a.username COLLATE NOCASE"""
            ).fetchall()
        return [self._account_row(r) for r in rows]

    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self.conn() as c:
            r = c.execute(
                """SELECT a.*, i.display_name, i.preference_label FROM accounts a
                   JOIN identities i ON i.id=a.identity_id WHERE a.id=?""",
                (account_id,),
            ).fetchone()
        return self._account_row(r) if r else None

    def accounts_for_identity(self, identity_id: str) -> List[Dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                """SELECT a.*, i.display_name, i.preference_label FROM accounts a
                   JOIN identities i ON i.id=a.identity_id WHERE a.identity_id=? ORDER BY a.username COLLATE NOCASE""",
                (identity_id,),
            ).fetchall()
        return [self._account_row(r) for r in rows]

    def _account_row(self, r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        return d

    @staticmethod
    def _evidence_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        return d

    def evidence_for_accounts(self, account_ids: Iterable[str]) -> List[Dict[str, Any]]:
        ids = list(dict.fromkeys(account_ids))
        if not ids:
            return []
        q = ",".join("?" for _ in ids)
        with self.conn() as c:
            rows = c.execute(
                f"SELECT * FROM evidence WHERE subject_account_id IN ({q}) OR related_account_id IN ({q})",
                ids + ids,
            ).fetchall()
        return [self._evidence_row(r) for r in rows]

    def all_evidence(self) -> List[Dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM evidence ORDER BY observed_at DESC").fetchall()
        return [self._evidence_row(r) for r in rows]

    def evidence_count(self) -> int:
        with self.conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])

    def merge_identities(self, keep_identity_id: str, merge_identity_id: str, reason: str = "user_confirmed") -> None:
        if keep_identity_id == merge_identity_id:
            return
        now = utc_now_iso()
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("UPDATE accounts SET identity_id=?, updated_at=? WHERE identity_id=?", (keep_identity_id, now, merge_identity_id))
                c.execute("UPDATE feedback SET identity_id=? WHERE identity_id=?", (keep_identity_id, merge_identity_id))
                c.execute("DELETE FROM identities WHERE id=?", (merge_identity_id,))
                # Merged relationship is represented by account membership after the move; log a self-link audit marker.
                c.execute(
                    "INSERT OR REPLACE INTO identity_links(id,left_identity_id,right_identity_id,status,confidence,reason_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), keep_identity_id, keep_identity_id, "merged", 1.0, json.dumps([reason]), now, now),
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def set_identity_link_decision(self, left_identity_id: str, right_identity_id: str, status: str, confidence: float = 0.0, reasons: Optional[List[str]] = None) -> None:
        if not left_identity_id or not right_identity_id or left_identity_id == right_identity_id:
            raise ValueError("two distinct identity ids are required")
        status = status.strip().casefold()
        if status not in {"rejected", "candidate", "confirmed"}:
            raise ValueError("unsupported link status")
        now = utc_now_iso()
        with self.conn() as c:
            existing = c.execute(
                "SELECT id FROM identity_links WHERE left_identity_id=? AND right_identity_id=?",
                (left_identity_id, right_identity_id),
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE identity_links SET status=?, confidence=?, reason_json=?, updated_at=? WHERE id=?",
                    (status, float(confidence), json.dumps(reasons or []), now, existing["id"]),
                )
            else:
                c.execute(
                    "INSERT INTO identity_links(id,left_identity_id,right_identity_id,status,confidence,reason_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), left_identity_id, right_identity_id, status, float(confidence), json.dumps(reasons or []), now, now),
                )

    def link_decisions(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.conn() as c:
            if status:
                rows = c.execute("SELECT * FROM identity_links WHERE status=?", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM identity_links").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d.pop("reason_json") or "[]")
            out.append(d)
        return out

    def enqueue_action(self, action_type: str, *, identity_id: Optional[str] = None, account_id: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> str:
        now = utc_now_iso()
        action_id = str(uuid.uuid4())
        with self.conn() as c:
            c.execute(
                "INSERT INTO action_outbox(id,action_type,identity_id,account_id,payload_json,state,attempts,created_at,updated_at) VALUES(?,?,?,?,?,'pending',0,?,?)",
                (action_id, action_type, identity_id, account_id, json.dumps(payload or {}, sort_keys=True), now, now),
            )
        return action_id

    def list_actions(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        with self.conn() as c:
            if state:
                rows = c.execute("SELECT * FROM action_outbox WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM action_outbox ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def action_counts(self) -> Dict[str, int]:
        with self.conn() as c:
            rows = c.execute("SELECT state, COUNT(*) n FROM action_outbox GROUP BY state").fetchall()
        out = {str(r["state"]): int(r["n"]) for r in rows}
        out["total"] = sum(out.values())
        return out

    def set_source_state(self, source: str, *, status: str, detail: str = "", cursor: Optional[str] = None, success: bool = False) -> None:
        now = utc_now_iso()
        with self.conn() as c:
            c.execute(
                """INSERT INTO source_state(source,last_success_at,last_attempt_at,cursor,status,detail)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                     last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL THEN excluded.last_success_at ELSE source_state.last_success_at END,
                     last_attempt_at=excluded.last_attempt_at,
                     cursor=COALESCE(excluded.cursor, source_state.cursor), status=excluded.status, detail=excluded.detail""",
                (source, now if success else None, now, cursor, status, detail),
            )

    def source_state(self, source: str) -> Optional[Dict[str, Any]]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM source_state WHERE source=?", (source,)).fetchone()
        return dict(row) if row else None

    def source_states(self) -> List[Dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM source_state ORDER BY source").fetchall()]
