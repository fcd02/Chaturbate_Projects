#!/usr/bin/env python3
"""Identity-verified client for the existing CTBRec Live Mobile Control bridge.

This client NEVER edits CTBRec configuration files. Native CTBRec model mutations
are sent to the already-running JVM bridge on loopback (normally 127.0.0.1:8791)
after an exact instance-identity PING.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _norm_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/").casefold()
    return raw


def _norm_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _b64(value: Any) -> str:
    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")


def expected_instance_for_dir(ctbrec_dir: Path | str) -> str:
    target = Path(os.path.expandvars(str(ctbrec_dir or ""))).expanduser().resolve()
    return hashlib.sha256(os.path.normcase(str(target)).encode("utf-8", "replace")).hexdigest()[:20]


class LiveBridgeError(RuntimeError):
    pass


class LiveBridgeIdentityError(LiveBridgeError):
    pass


class LiveBridgeUnavailable(LiveBridgeError):
    pass


class LiveCTBRecBridgeClient:
    """Small monitor/cache + mutation client for the user's existing 8791 bridge."""

    def __init__(self, app_dir: Path, config: Optional[Dict[str, Any]] = None) -> None:
        self.app_dir = Path(app_dir).resolve()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.monitor: Optional[threading.Thread] = None
        self.config: Dict[str, Any] = {}
        self._selected_config_path: Optional[Path] = None
        self._ctbrec_dir: Optional[Path] = None
        self._expected_instance = ""
        self._bridge_port = 8791
        self._cache: Dict[str, Any] = {}
        self.configure(config or {}, restart=False)
        self.start()

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {
            "enabled": True,
            "auto_discover": True,
            "controller_config_path": "",
            "ctbrec_dir": "",
            "bridge_port": 8791,
            "heartbeat_seconds": 2.0,
            "snapshot_poll_seconds": 5.0,
            "offline_grace_seconds": 10.0,
        }

    def configure(self, config: Dict[str, Any], restart: bool = True) -> None:
        merged = self.defaults()
        if isinstance(config, dict):
            merged.update(config)
        with self.lock:
            self.config = merged
            self._reset_cache_locked("Waiting for CTBRec live bridge discovery.")
            self._resolve_target_locked()
        self.refresh_event.set()
        if restart and self.monitor is None:
            self.start()

    def _reset_cache_locked(self, message: str = "") -> None:
        self._cache = {
            "last_ping_ok": 0.0,
            "last_ping_attempt": 0.0,
            "last_list_ok": 0.0,
            "last_error": message,
            "ping": {},
            "models": [],
            "status": {},
            "snapshot_generation": 0,
        }

    def _candidate_controller_configs(self) -> List[Path]:
        candidates: Dict[str, Path] = {}
        explicit = str(self.config.get("controller_config_path", "") or "").strip().strip('"')
        if explicit:
            p = Path(os.path.expandvars(os.path.expanduser(explicit)))
            if p.is_dir():
                p = p / "controller_config.json"
            if p.is_file():
                candidates[os.path.normcase(str(p.resolve()))] = p.resolve()
            return list(candidates.values())
        if not bool(self.config.get("auto_discover", True)):
            return []

        env = str(os.environ.get("CTBREC_LIVE_CONTROL_CONFIG", "") or "").strip()
        if env:
            p = Path(os.path.expandvars(os.path.expanduser(env)))
            if p.is_dir(): p = p / "controller_config.json"
            if p.is_file(): candidates[os.path.normcase(str(p.resolve()))] = p.resolve()

        bases: List[Path] = [self.app_dir]
        bases.extend(list(self.app_dir.parents)[:4])
        # Also cover the ordinary Windows locations where a user may keep the
        # standalone Live Control package even when Reviewer lives elsewhere.
        home = Path.home()
        for common in (home, home / "Desktop", home / "Downloads", home / "Documents"):
            try:
                if common.exists():
                    bases.append(common.resolve())
            except OSError:
                pass
        # Preserve order while avoiding duplicate scans of the same directory.
        unique_bases: List[Path] = []
        seen_bases: set[str] = set()
        for base in bases:
            key = os.path.normcase(str(base))
            if key not in seen_bases:
                seen_bases.add(key); unique_bases.append(base)
        bases = unique_bases
        patterns = (
            "controller_config.json",
            "CTBRec_Live_Mobile_Control*/controller_config.json",
            "*Live*Mobile*Control*/controller_config.json",
            "*/CTBRec_Live_Mobile_Control*/controller_config.json",
        )
        for base in bases:
            for pattern in patterns:
                try:
                    for p in base.glob(pattern):
                        if p.is_file():
                            candidates[os.path.normcase(str(p.resolve()))] = p.resolve()
                except (OSError, PermissionError):
                    continue
        return sorted(candidates.values(), key=lambda p: str(p).casefold())

    def _resolve_target_locked(self) -> None:
        self._selected_config_path = None
        self._ctbrec_dir = None
        self._expected_instance = ""
        self._bridge_port = int(self.config.get("bridge_port", 8791) or 8791)

        direct_dir = str(self.config.get("ctbrec_dir", "") or "").strip().strip('"')
        if direct_dir:
            target = Path(os.path.expandvars(os.path.expanduser(direct_dir))).resolve()
            self._ctbrec_dir = target
            self._expected_instance = expected_instance_for_dir(target)

        candidates = self._candidate_controller_configs()
        parsed: List[Tuple[Path, Path, int]] = []
        for path in candidates:
            raw = _read_json(path, {})
            if not isinstance(raw, dict):
                continue
            cdir = str(raw.get("ctbrec_dir", "") or "").strip()
            if not cdir:
                continue
            try:
                target = Path(os.path.expandvars(os.path.expanduser(cdir))).resolve()
                port = int(raw.get("bridge_port", self._bridge_port) or self._bridge_port)
                parsed.append((path, target, port))
            except Exception:
                continue

        # An explicit direct ctbrec_dir is also authoritative when no explicit
        # controller_config path was supplied. This lets advanced users pin the
        # intended installation without relying on discovery.
        explicit_path = str(self.config.get("controller_config_path", "") or "").strip()
        if direct_dir and not explicit_path:
            return

        # An explicit controller_config path is authoritative. For auto-discovery,
        # prefer a sole candidate. If there are several, probe each candidate's
        # port and select only if exactly one PING identity matches its own dir hash.
        if explicit_path and parsed:
            path, target, port = parsed[0]
            self._selected_config_path, self._ctbrec_dir, self._bridge_port = path, target, port
            self._expected_instance = expected_instance_for_dir(target)
            return
        if len(parsed) == 1:
            path, target, port = parsed[0]
            self._selected_config_path, self._ctbrec_dir, self._bridge_port = path, target, port
            self._expected_instance = expected_instance_for_dir(target)
            return
        if len(parsed) > 1 and not self._ctbrec_dir:
            matching: List[Tuple[Path, Path, int]] = []
            for path, target, port in parsed:
                try:
                    ping = self._exchange_on_port(port, "PING", timeout=0.55)
                    if str(ping.get("instance", "")) == expected_instance_for_dir(target):
                        matching.append((path, target, port))
                except Exception:
                    pass
            if len(matching) == 1:
                path, target, port = matching[0]
                self._selected_config_path, self._ctbrec_dir, self._bridge_port = path, target, port
                self._expected_instance = expected_instance_for_dir(target)
            elif len(matching) > 1:
                self._cache["last_error"] = "Multiple live CTBRec bridge configurations are active. Choose the intended controller_config.json in Mobile Settings."
            else:
                self._cache["last_error"] = "Multiple Live Control configurations were found, but none could be uniquely matched to a healthy bridge. Choose the intended controller_config.json in Mobile Settings."

    def start(self) -> None:
        with self.lock:
            if self.monitor and self.monitor.is_alive():
                return
            self.stop_event.clear()
            self.monitor = threading.Thread(target=self._monitor_loop, name="ctbrec-reviewer-live-bridge", daemon=True)
            self.monitor.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.refresh_event.set()
        thread = self.monitor
        if thread and thread.is_alive():
            thread.join(timeout=2.5)

    def _exchange_on_port(self, port: int, command: str, *args: Any, timeout: float = 12.0) -> Dict[str, Any]:
        fields = [str(command).upper()] + [_b64(value) for value in args]
        data = ("\t".join(fields) + "\n").encode("utf-8")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=min(timeout, 2.5)) as sock:
                sock.settimeout(timeout)
                sock.sendall(data)
                reader = sock.makefile("rb")
                raw = reader.readline(8 * 1024 * 1024)
        except socket.timeout as exc:
            raise LiveBridgeUnavailable(f"CTBRec live bridge timed out answering {str(command).upper()}.") from exc
        except ConnectionRefusedError as exc:
            raise LiveBridgeUnavailable(f"CTBRec live bridge is not listening on 127.0.0.1:{int(port)}.") from exc
        except OSError as exc:
            raise LiveBridgeUnavailable(f"CTBRec live bridge on 127.0.0.1:{int(port)} is unavailable ({exc}).") from exc
        if not raw:
            raise LiveBridgeUnavailable("CTBRec live bridge closed the request without a response.")
        try:
            response = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            raise LiveBridgeError("CTBRec live bridge returned malformed JSON.") from exc
        if not isinstance(response, dict):
            raise LiveBridgeError("CTBRec live bridge returned an unexpected response.")
        return response

    def exchange(self, command: str, *args: Any, timeout: float = 12.0) -> Dict[str, Any]:
        with self.lock:
            port = self._bridge_port
        return self._exchange_on_port(port, command, *args, timeout=timeout)

    def verify_identity(self) -> Dict[str, Any]:
        with self.lock:
            enabled = bool(self.config.get("enabled", True))
            expected = self._expected_instance
            port = self._bridge_port
            target = str(self._ctbrec_dir or "")
        if not enabled:
            raise LiveBridgeUnavailable("Live CTBRec bridge controls are disabled in Mobile Settings.")
        if not expected:
            raise LiveBridgeIdentityError("No intended CTBRec installation is configured. Choose the existing Live Control controller_config.json in Mobile Settings.")
        try:
            ping = self._exchange_on_port(port, "PING", timeout=2.0)
        except Exception as exc:
            with self.lock:
                self._cache["last_ping_attempt"] = time.monotonic()
                self._cache["last_error"] = str(exc)
            raise
        actual = str(ping.get("instance", ""))
        now = time.monotonic()
        with self.lock:
            self._cache["last_ping_attempt"] = now
            self._cache["ping"] = ping
        if actual != expected:
            message = f"Bridge identity mismatch: 127.0.0.1:{port} belongs to a different CTBRec installation. No mutation was sent."
            with self.lock:
                self._cache["last_error"] = message
            raise LiveBridgeIdentityError(message)
        with self.lock:
            self._cache["last_ping_ok"] = now
            self._cache["last_error"] = ""
        if not bool(ping.get("registered")):
            raise LiveBridgeUnavailable("The correct CTBRec bridge is alive, but its recorder is still initializing.")
        return ping

    def mutate(self, command: str, *args: Any, timeout: float = 65.0) -> Dict[str, Any]:
        """Verify identity immediately before every mutation; never auto-retry."""
        self.verify_identity()
        response = self.exchange(command, *args, timeout=timeout)
        if not bool(response.get("ok")):
            raise LiveBridgeError(str(response.get("error") or response.get("message") or f"CTBRec rejected {command}."))
        result = response.get("result")
        if isinstance(result, dict) and str(result.get("url", "")).strip():
            url_key = _norm_url(result.get("url"))
            with self.lock:
                rows = [dict(row) for row in self._cache.get("models", []) if isinstance(row, dict)]
                if str(command).upper() == "REMOVE":
                    rows = [row for row in rows if _norm_url(row.get("url")) != url_key]
                else:
                    replaced = False
                    for index, row in enumerate(rows):
                        if _norm_url(row.get("url")) == url_key:
                            rows[index] = dict(result); replaced = True; break
                    if not replaced:
                        rows.append(dict(result))
                self._cache["models"] = rows
                self._cache["last_list_ok"] = time.monotonic()
        self.refresh_event.set()
        return response

    def _poll_once(self, include_list: bool = True) -> None:
        with self.lock:
            if not bool(self.config.get("enabled", True)):
                return
            if not self._expected_instance:
                # Re-run discovery occasionally; the user may have just installed
                # or moved Live Control without restarting Mobile Reviewer.
                self._resolve_target_locked()
                if not self._expected_instance:
                    return
            port = self._bridge_port
            expected = self._expected_instance
        now = time.monotonic()
        try:
            ping = self._exchange_on_port(port, "PING", timeout=1.5)
            actual = str(ping.get("instance", ""))
            if actual != expected:
                raise LiveBridgeIdentityError("Bridge port belongs to a different CTBRec installation; live mutations are blocked.")
            with self.lock:
                self._cache["last_ping_attempt"] = now
                self._cache["last_ping_ok"] = now
                self._cache["last_error"] = ""
                self._cache["ping"] = ping
            if include_list and bool(ping.get("registered")):
                listing = self._exchange_on_port(port, "LIST", timeout=4.0)
                if bool(listing.get("ok")):
                    loading = bool(listing.get("loading"))
                    rows = listing.get("models", [])
                    status = listing.get("status") if isinstance(listing.get("status"), dict) else None
                    # Exact Live Control behavior: during a Java snapshot warm-up,
                    # do not replace a previously good model cache with an empty
                    # loading response. If LIST omits status, STATUS is a safe
                    # read-only fallback and is cached separately.
                    if status is None:
                        try:
                            status_reply = self._exchange_on_port(port, "STATUS", timeout=2.5)
                            status = status_reply.get("status") if bool(status_reply.get("ok")) and isinstance(status_reply.get("status"), dict) else None
                        except Exception:
                            status = None
                    with self.lock:
                        if not loading or not self._cache.get("models"):
                            if isinstance(rows, list):
                                self._cache["models"] = rows
                            self._cache["snapshot_generation"] = int(listing.get("snapshotGeneration", 0) or 0)
                            if not loading:
                                self._cache["last_list_ok"] = time.monotonic()
                        if isinstance(status, dict):
                            self._cache["status"] = status
        except Exception as exc:
            with self.lock:
                self._cache["last_ping_attempt"] = time.monotonic()
                self._cache["last_error"] = str(exc)

    def refresh_now(self, include_list: bool = True) -> Dict[str, Any]:
        self._poll_once(include_list=include_list)
        return self.health()

    def _monitor_loop(self) -> None:
        next_list = 0.0
        while not self.stop_event.is_set():
            with self.lock:
                heartbeat = max(1.0, float(self.config.get("heartbeat_seconds", 2.0) or 2.0))
                list_every = max(2.0, float(self.config.get("snapshot_poll_seconds", 5.0) or 5.0))
            now = time.monotonic()
            forced = self.refresh_event.is_set()
            if forced:
                self.refresh_event.clear()
            include_list = forced or now >= next_list
            self._poll_once(include_list=include_list)
            if include_list:
                next_list = time.monotonic() + list_every
            self.stop_event.wait(heartbeat)

    def health(self) -> Dict[str, Any]:
        with self.lock:
            cache = dict(self._cache)
            cfg = dict(self.config)
            expected = self._expected_instance
            target = str(self._ctbrec_dir or "")
            path = str(self._selected_config_path or "")
            port = self._bridge_port
        now = time.monotonic()
        last_ok = float(cache.get("last_ping_ok", 0.0) or 0.0)
        grace = max(3.0, float(cfg.get("offline_grace_seconds", 10.0) or 10.0))
        age = now - last_ok if last_ok else float("inf")
        ping = cache.get("ping", {}) if isinstance(cache.get("ping"), dict) else {}
        connected = bool(last_ok and age <= grace and str(ping.get("instance", "")) == expected)
        registered = bool(connected and ping.get("registered"))
        latest_attempt_failed = bool(float(cache.get("last_ping_attempt", 0.0) or 0.0) > last_ok)
        error = str(cache.get("last_error", "") or "")
        if connected and registered and not latest_attempt_failed:
            error = ""
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "connected": connected,
            "ok": connected and registered and not latest_attempt_failed,
            "registered": registered,
            "initializing": connected and not registered,
            "reconnecting": connected and latest_attempt_failed,
            "error": error,
            "port": port,
            "instance": str(ping.get("instance", "")),
            "expected_instance": expected,
            "ctbrec_dir": target,
            "controller_config_path": path,
            "last_heartbeat_seconds": None if not last_ok else round(max(0.0, age), 1),
            "snapshot_generation": int(cache.get("snapshot_generation", 0) or 0),
            "snapshot_age_seconds": None if not float(cache.get("last_list_ok", 0.0) or 0.0) else round(max(0.0, now - float(cache.get("last_list_ok", 0.0))), 1),
        }

    def discovery_payload(self) -> Dict[str, Any]:
        health = self.health()
        with self.lock:
            candidates = [str(p) for p in self._candidate_controller_configs()]
            models_count = len(self._cache.get("models", [])) if isinstance(self._cache.get("models"), list) else 0
        health["candidate_controller_configs"] = candidates
        health["cached_models"] = models_count
        if health.get("ok"):
            health["message"] = "CTBRec LIVE — changes apply immediately to the identity-verified running instance."
        elif health.get("initializing"):
            health["message"] = "Correct CTBRec bridge found; recorder is still initializing. Sorting remains available."
        elif health.get("error"):
            health["message"] = str(health["error"])
        else:
            health["message"] = "Live CTBRec bridge is not connected. Sorting remains available; native model controls are blocked."
        return health

    def models(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self._cache.get("models", [])
            return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def status(self) -> Dict[str, Any]:
        with self.lock:
            value = self._cache.get("status", {})
            return dict(value) if isinstance(value, dict) else {}

    def find_models(self, names: Iterable[str] = (), urls: Iterable[str] = ()) -> List[Dict[str, Any]]:
        wanted_names = {_norm_name(v) for v in names if str(v or "").strip()}
        wanted_urls = {_norm_url(v) for v in urls if str(v or "").strip()}
        out: List[Dict[str, Any]] = []
        for row in self.models():
            row_names = {_norm_name(row.get("name")), _norm_name(row.get("modelName"))}
            row_url = _norm_url(row.get("url"))
            if (wanted_names and row_names & wanted_names) or (wanted_urls and row_url in wanted_urls):
                out.append(row)
        return out

    def mutation_for_rows(self, command: str, rows: Iterable[Dict[str, Any]], *extra_args: Any) -> Dict[str, Any]:
        """Apply one command to each resolved row without retrying any command.

        This can be partially successful if CTBRec changes between rows. The
        caller gets exact per-row outcomes rather than a false all-or-nothing claim.
        """
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            url = str(row.get("url", "") or "").strip()
            key = _norm_url(url)
            if not url or key in seen:
                continue
            seen.add(key)
            try:
                response = self.mutate(command, url, *extra_args)
                results.append({"url": url, "response": response})
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)})
        if errors and not results:
            raise LiveBridgeError(errors[0]["error"])
        return {"ok": not errors, "applied": len(results), "failed": len(errors), "results": results, "errors": errors}
