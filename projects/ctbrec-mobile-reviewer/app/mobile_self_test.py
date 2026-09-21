#!/usr/bin/env python3
from __future__ import annotations
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def main() -> int:
    print("\nCTBRec Mobile Reviewer self-test")
    print("Python:", sys.version.split()[0])
    try:
        from PIL import Image  # noqa: F401
        print("Pillow: installed")
    except Exception as exc:
        print("Pillow: MISSING -", exc)
        return 1
    try:
        import websocket  # noqa: F401
        print("websocket-client: installed")
    except Exception as exc:
        print("websocket-client: MISSING -", exc)
        print("Run install_mobile_reviewer.bat again to enable Recu browser-session capture.")
        return 1
    try:
        from nudenet import NudeDetector  # noqa: F401
        print("NudeNet: installed (Non-NSFW cleanup available)")
    except Exception as exc:
        print("NudeNet: MISSING/BROKEN -", exc)
        print("Run install_mobile_reviewer.bat again to enable Non-NSFW cleanup.")
        return 1
    config = json.loads((APP_DIR / "mobile_config.json").read_text(encoding="utf-8"))
    roots = APP_DIR / str(config.get("roots_file", "recording_roots.txt"))
    print("Roots file:", roots, "OK" if roots.is_file() else "MISSING")
    mosaic = load("ctbrec_mobile_selftest_mosaic", APP_DIR / "ctbrec_mosaic_sort_lite.py")
    review = load("ctbrec_mobile_selftest_review", APP_DIR / "ctbrec_review_folder_sort_lite.py")
    cleanup = load("ctbrec_mobile_selftest_nsfw", APP_DIR / "ctbrec_nsfw_cleanup.py")
    bridge = load("ctbrec_mobile_selftest_bridge", APP_DIR / "ctbrec_live_bridge_client.py")
    admin = load("ctbrec_mobile_selftest_admin", APP_DIR / "ctbrec_mobile_model_admin.py")
    print("Mosaic engine:", mosaic.APP_VERSION)
    print("Review engine:", review.APP_VERSION)
    print("Non-NSFW cleanup engine: present; explicit classes:", ", ".join(cleanup.DEFAULT_EXPLICIT_CLASSES))
    print("CTBRec live bridge client: present; default port:", bridge.LiveCTBRecBridgeClient.defaults()["bridge_port"])
    print("CTBRec mobile model admin: present; priority tiers:", ", ".join(admin.PRIORITY_TIERS))
    configured = str(config.get("ffmpeg_path", ""))
    ffmpeg, ffprobe = mosaic.resolve_ffmpeg(configured)
    print("ffmpeg:", ffmpeg or "NOT FOUND")
    print("ffprobe:", ffprobe or "not found (ffmpeg fallback can still work)")
    tailscale = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if not tailscale and os.name == "nt":
        candidate = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        tailscale = str(candidate) if candidate.is_file() else None
    print("Tailscale CLI:", tailscale or "not installed yet")
    chrome = None
    for base in (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe" if base else None
        if candidate and candidate.is_file():
            chrome = candidate
            break
    print("Google Chrome:", chrome or "not auto-detected (only required for Recu verification)")
    for web_name in ("static/service-worker.js", "static/manifest.webmanifest", "static/rapid.js", "ctbrec_nsfw_cleanup.py", "ctbrec_mobile_model_admin.py", "ctbrec_live_bridge_client.py"):
        web_path = APP_DIR / web_name
        print(web_name + ":", "present" if web_path.is_file() else "MISSING")
        if not web_path.is_file():
            return 1
    index_text = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8", errors="replace")
    server_text = (APP_DIR / "ctbrec_mobile_server.py").read_text(encoding="utf-8", errors="replace")
    review_text = (APP_DIR / "ctbrec_review_folder_sort_lite.py").read_text(encoding="utf-8", errors="replace")
    keep_last_text = (APP_DIR / "ctbrec_keep_last.py").read_text(encoding="utf-8", errors="replace")
    app_text = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
    service_worker_text = (APP_DIR / "static" / "service-worker.js").read_text(encoding="utf-8", errors="replace")
    feature_checks = {
        "PC queue UI": 'id="queue-view"' in index_text,
        "custom offline pack UI": 'id="offline-pack-modal"' in index_text,
        "phone video preview UI": 'id="preview-modal"' in index_text,
        "video Range/FFmpeg endpoint": '/api/video/' in server_text,
        "durable queue reorder API": '/api/action-queue/reorder' in server_text,
        "deletion recovery action": 'deletion_restore' in server_text,
        "CTBRec model admin API": '/api/model-admin' in server_text,
        "identity-verified live bridge": 'LiveCTBRecBridgeClient' in server_text and 'live_bridge' in server_text,
        "client-disconnect crash guard": 'ResilientThreadingHTTPServer' in server_text and '_is_client_disconnect' in server_text,
        "background liveness queue lease": 'active_sort_queue_id' in server_text and '_whole_library_blocker_reason' in server_text,
        "bounded model retry/skip": 'mosaic_retry_attempts' in server_text and 'last_skipped_model' in server_text,
        "ffmpeg frame hard timeout": 'frame_extract_timeout_seconds' in (APP_DIR / "ctbrec_mosaic_sort_lite.py").read_text(encoding="utf-8", errors="replace") and 'frame_extract_timeout_seconds' in (APP_DIR / "ctbrec_review_folder_sort_lite.py").read_text(encoding="utf-8", errors="replace"),
        "precise Review frame-cut UI": 'data-granularity="frames"' in index_text and 'frameDecisions' in app_text,
        "timestamped Review mosaic sidecars": 'interval_end_seconds' in review_text and '"version": 3' in review_text,
        "durable idle frame-cut queue": 'review_frame_cuts' in server_text and '_frame_cut_idle_ready' in server_text,
        "verify-before-original-deletion": 'clips_verified' in server_text and 'MARKED_FOR_DELETION' in server_text,
        "v2.13 paired-device auth healing": 'device_token_value' in server_text and 'paired_token_supported' in server_text and 'ctbrec_device_token' in app_text,
        "v2.13.3 stale-tab response sequencing without catalog aborts": 'modelLoadSeq' in app_text and 'noAbort: true' in app_text and 'modelLoadController' not in app_text,
        "v2.13 bounded progressive model DOM": 'modelRenderLimit' in app_text and 'IntersectionObserver' in app_text,
        "v2.13 lightweight sorting status": 'def queue_status_payload' in server_text and '/status' in app_text,
        "v2.13.3 iOS/Tailscale-compatible HTTP/1.0 + plain JSON": 'protocol_version = "HTTP/1.0"' in server_text and 'gzip.compress' not in server_text,
        "v2.13.3 settings optimistic-lock guard": 'settings_revision_token' in server_text and 'settings_revision' in app_text,
        "v2.13.3 corrupted-settings field repair": '_looks_like_v213_settings_form_corruption' in server_text and 'pre_v2_13_3_settings_repair' in server_text,
        "v2.13.3 durable EZ Sort import": 'easy_sort_imported' in (APP_DIR / "ctbrec_mobile_model_admin.py").read_text(encoding="utf-8", errors="replace"),
        "v2.13.3 Recu Originals + Review": 'queue_data.get("mode") not in {"original", "review"}' in server_text,
        "v2.14 incremental Recu session memory": 'scan_memory' in (APP_DIR / "ctbrec_mosaic_sort_lite.py").read_text(encoding="utf-8", errors="replace") and 'last_new_sessions' in (APP_DIR / "ctbrec_mosaic_sort_lite.py").read_text(encoding="utf-8", errors="replace"),
        "v2.14 verified-Chrome concurrent Recu loading": 'Network.loadNetworkResource' in server_text and 'includeCredentials' in server_text and 'fetch_many_background' in server_text,
        "v2.14 Recu navigation fallback": 'Network.setBlockedURLs' in server_text and 'Page.stopLoading' in server_text,
        "v2.14 desktop direct-disk preview": 'local_disk_url' in server_text and 'isDesktopPreviewClient' in app_text,
        "v2.14.3 Recu explicit-confirmation capture": 'explicit user confirmation' in server_text and 'no DOM/login heuristic or navigation was run during capture' in server_text,
        "v2.14.3 Recu proven challenge tokens": 'RECU_CHALLENGE_TOKENS' in server_text and 'checking your browser' in server_text,
        "v2.14.3 Recu browser fallback remains secondary": 'Persistent-cookie Recu fetch looked auth-related; confirming only as fallback' in server_text,
        "v2.14.1 browser-correct disk Range + remux fallback": '_parse_http_byte_range' in server_text and 'remux_url' in server_text and 'previewFailover' in app_text and 'item.remux_url' in app_text and 'item.compatibility_url' in app_text,
        "v2.13 bounded open-fast warm set": 'open_fast_initial_chunks' in server_text,
        "v2.13 batched Original/Review/NSFW frame extraction": all(
            'frame_extract_batch_size' in (APP_DIR / name).read_text(encoding="utf-8", errors="replace")
            for name in ("ctbrec_mosaic_sort_lite.py", "ctbrec_review_folder_sort_lite.py", "ctbrec_nsfw_cleanup.py")
        ),
        "v2.14.4 durable whole-library traversal plan": 'mobile_library_mosaic_plan.json' in server_text and '_persist_library_plan' in server_text,
        "v2.14.4 live model-size tile refresh": 'lastCatalogUpdatedAt' in app_text and 'resetWindow: false' in app_text,
        "v2.14.4 reconcile chunk + mosaic action": 'rebuild-chunk-button' in index_text and '/rebuild-chunk' in server_text,
        "v2.14.4 strict mosaic hit-map validation": 'expected_seconds' in server_text and 'actual_seconds' in server_text and '_purge_mosaic_artifacts_for_chunk_sources' in server_text,
        "v2.14.4 end-relative kept-file naming": '_end_relative_tag' in review_text and 'position_from_end_tag' in server_text,
        "v2.14.5 Keep Last tagged timestamp parsing": 'COMPACT_RE' in keep_last_text and 'END_MINUS_RE' in keep_last_text and 'SEGMENT_RE' in keep_last_text,
        "v2.14.6 Keep Last per-session cross-drive retention": 'split the chronological footage into sessions' in keep_last_text and '_session_blocks' in keep_last_text,
        "v2.14.7 stale ready-index sidecar recovery": '_existing_mosaic_sidecar_count' in server_text and 'sidecar-reindex' in server_text and 'recovered_existing_mosaics' in server_text,
        "v2.14.8 exact-model pending open without 12s abort": 'recover_sidecars=False' in server_text and 'preparing_model' in server_text and 'timeoutMs: 0' in app_text,
        "v2.14.8 demand-only mosaic generation": 'policy": "current-only' in server_text and '_deactivate_queue_work_locked' in server_text and 'Generate Ahead stopped because this model is no longer open' in server_text,
        "v2.14.8 preview fallback ladder": 'previewFailover' in app_text and 'default_base_moof' in server_text and '+genpts+discardcorrupt' in server_text,
        "v2.14.9 scoped sidecar reindex": 'sidecar_count > loaded_ready' in server_text and 'no sidecar reindex needed' in server_text and 'Recovered {len(additions)} additional valid existing mosaic(s)' in server_text,
        "v2.15.0 model sort controls": 'MODEL_SORT_PREF_KEY' in app_text and 'deterministicRandomRank' in app_text and 'avg_chunk_bytes' in server_text and 'random-min-size-gb' in index_text,
        "v2.15.1 Shift-click range selection": 'lastFileSelectionIndex' in app_text and 'lastFrameSelectionIndex' in app_text and 'event?.shiftKey' in app_text and 'inclusiveRangeBetween' in app_text,
        "v2.15.2 active-model look-ahead refill": 'self.schedule_prefetch(target)' in server_text and 'self.schedule_prefetch(queue_id)' in server_text and 'queue_data["preparing_model"] = True' in server_text and 'ctbrec-shell-v2157' in service_worker_text,
        "v2.15.3 pending model-open self-heal": 'def _sync_pending_model_task' in server_text and 'targets.difference_update(processed_targets)' in server_text and 'Recovering model-open indexing' in server_text,
        "v2.15.4 visible-queue lease renewal": 'def _touch_sort_queue_from_poll' in server_text and 'queue_data["phone_open"] = False' in server_text and 'poll_active = self._touch_sort_queue_from_poll(queue_id)' in server_text,
    }
    for label, ok in feature_checks.items():
        print(label + ":", "present" if ok else "MISSING")
        if not ok:
            return 1
    frame_cut = config.get("action_queue", {})
    print("Precise Review frame cuts:", f"idle-only ({frame_cut.get('frame_cut_idle_minutes', 10)} min), priority above background mosaics/Non-NSFW once eligible")
    library = config.get("library_background", {})
    print(
        "Whole-library generation:",
        "enabled" if library.get("enabled", True) else "disabled",
        f"(idle {library.get('idle_minutes', 10)} min; Originals={library.get('original_enabled', True)}; Review={library.get('review_enabled', True)})",
    )
    for runtime_name in ("mobile_action_queue.json", "mobile_library_mosaic_state.json", "mobile_ready_work_index.json", "mobile_offline_sync_history.json", "mobile_non_nsfw_index.json", "mobile_non_nsfw_state.json"):
        runtime_path = APP_DIR / runtime_name
        try:
            probe = runtime_path.with_suffix(runtime_path.suffix + ".selftest.tmp")
            probe.write_text("{}", encoding="utf-8")
            probe.unlink()
            print(runtime_name + ": writable")
        except Exception as exc:
            print(runtime_name + ": NOT WRITABLE -", exc)
            return 1
    if ffmpeg is None:
        print("\nACTION NEEDED: Put this folder inside your CTBRec folder, copy ffmpeg.exe")
        print("beside the scripts, add ffmpeg to PATH, or set ffmpeg_path in mobile_config.json.")
        return 1
    print("\nCore setup looks good.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
