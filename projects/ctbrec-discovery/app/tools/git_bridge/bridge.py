from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

REPO_FULL_NAME = "fcd02/Chaturbate_Projects"
DEFAULT_REPO_PATH = r"C:\GitHub\Chaturbate_Projects"
PROJECT_SUBDIR = r"projects\ctbrec-discovery"
APP_SUBDIR = r"projects\ctbrec-discovery\app"
BASE_BRANCH = "main"
BRIDGE_VERSION = "1.2.0"
PATCH_MANIFEST_NAME = "PATCH_MANIFEST.json"
PATCH_TYPE = "cumulative_public_source_overlay"
CONFIG_HOME = "CTBRecDiscoveryGitBridge"
PROJECT_MARKER_START = "<!-- CTBREC-DISCOVERY-START -->"
PROJECT_MARKER_END = "<!-- CTBREC-DISCOVERY-END -->"

PROTECTED_NAMES = {
    "discovery_config.json",
    "discovery.sqlite3",
    "discovery.sqlite",
    "discovery.db",
    "mobile_catalog_cache.json",
    "mosaic_lite_recu_cache.json",
    "recu_local_archive.json",
    "mobile_recu_session.json",
}

PROTECTED_DIRS = {
    "state", "runtime", "logs", "log", "tmp", "temp",
    "recu_browser_profile", "browser_profile", "cookies", "sessions",
}

BINARY_MEDIA_EXTS = {
    ".ts", ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".onnx",
}

ARCHIVE_EXTS = {".zip", ".7z", ".rar"}
SECRET_NAME_RE = re.compile(r"(secret|credential|cookie|session[-_]?token|auth[-_]?token|api[-_]?key|webmaster[-_]?id|wm[-_]?id)", re.I)
VERSION_HANDOFF_RE = re.compile(r"CTBRec_Discovery_v(\d+)_(\d+)_(\d+)_HANDOFF\.md$", re.I)
VERSION_INIT_RE = re.compile(r"__version__\s*=\s*['\"](\d+\.\d+\.\d+)['\"]")

PUBLIC_SOURCE_DIRS = {
    "discovery", "docs", "examples", "scripts", "static", "tests", "tools", "validation",
}
PUBLIC_ROOT_FILES = {
    ".gitignore", "README.md", "CHATGPT_HANDOFF.md", "PATCH_MANIFEST.json", "discovery_config.example.json",
    "requirements.txt", "pyproject.toml", "package.json", "package-lock.json", "LICENSE",
}
PUBLIC_ROOT_PATTERNS = (
    re.compile(r"CTBRec_Discovery_v\d+_\d+_\d+_HANDOFF\.md$", re.I),
    re.compile(r"TEST_REPORT_v\d+_\d+_\d+\.txt$", re.I),
)


def say(msg: str) -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"\n==> {msg}")


def die(msg: str, code: int = 1) -> None:
    raise RuntimeError(msg)


def run(cmd, cwd: Path | None = None, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    if capture:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    else:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if check and p.returncode != 0:
        out = (p.stdout or "").strip() if capture else ""
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(map(str, cmd))}" + (f"\n{out}" if out else ""))
    return p


def which_required(name: str, hint: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} was not found. {hint}")
    return p


def local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / CONFIG_HOME


def config_path() -> Path:
    return local_appdata() / "config.json"


def state_dir() -> Path:
    return local_appdata()


def normalize_rel(name: str) -> str:
    return str(PurePosixPath(name.replace("\\", "/"))).lstrip("/")


def repo_rel_path(value: str) -> Path:
    """Convert stored Windows-style repo subpaths into a native Path safely."""
    return Path(*PurePosixPath(normalize_rel(value)).parts)



def is_allowed_public_source_path(relative: str) -> bool:
    rel = normalize_rel(relative)
    if not rel or rel.startswith("../") or "/../" in f"/{rel}/":
        return False
    pp = PurePosixPath(rel)
    if pp.is_absolute():
        return False
    parts = pp.parts
    if len(parts) == 1:
        if pp.name in PUBLIC_ROOT_FILES:
            return True
        return any(rx.fullmatch(pp.name) for rx in PUBLIC_ROOT_PATTERNS)
    return parts[0].casefold() in PUBLIC_SOURCE_DIRS

def is_protected_relative_path(relative: str) -> bool:
    rel = normalize_rel(relative)
    low = rel.casefold()
    parts = [p.casefold() for p in PurePosixPath(rel).parts]
    name = PurePosixPath(rel).name.casefold()
    suffix = PurePosixPath(rel).suffix.casefold()

    if not rel or rel in {".", ".."}:
        return True
    if rel.startswith("../") or "/../" in f"/{rel}/" or PurePosixPath(rel).is_absolute():
        return True
    if any(part in PROTECTED_DIRS for part in parts):
        return True
    if name in PROTECTED_NAMES:
        return True
    if low.startswith(".env") or "/.env" in low:
        return True
    if SECRET_NAME_RE.search(low):
        return True
    if suffix in {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".log"}:
        return True
    if suffix in BINARY_MEDIA_EXTS or suffix in ARCHIVE_EXTS:
        return True
    if name.endswith(".base64.txt") or name.endswith(".base85.txt"):
        return True
    if "__pycache__" in parts or suffix in {".pyc", ".pyo"}:
        return True
    return False


def assert_runtime(runtime: Path) -> None:
    required = [runtime / "discovery" / "__init__.py", runtime / "discovery" / "server.py", runtime / "scripts" / "START_DISCOVERY.bat"]
    missing = [str(x) for x in required if not x.exists()]
    if missing:
        raise RuntimeError(f"RuntimePath does not look like CTBRec Discovery. Missing: {missing}")


def read_version(root: Path) -> str:
    for p in root.glob("CTBRec_Discovery_v*_HANDOFF.md"):
        m = VERSION_HANDOFF_RE.search(p.name)
        if m:
            pass
    matches = []
    for p in root.glob("CTBRec_Discovery_v*_HANDOFF.md"):
        m = VERSION_HANDOFF_RE.search(p.name)
        if m:
            matches.append((tuple(map(int, m.groups())), ".".join(m.groups())))
    if matches:
        matches.sort()
        return matches[-1][1]
    init = root / "discovery" / "__init__.py"
    if init.exists():
        m = VERSION_INIT_RE.search(init.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    raise RuntimeError("Could not determine Discovery version.")


def infer_version_from_payload(root: Path) -> str:
    patch_manifest = root / PATCH_MANIFEST_NAME
    if patch_manifest.is_file():
        try:
            data = json.loads(patch_manifest.read_text(encoding="utf-8"))
            version = str(data.get("release_version") or "").strip()
            if version:
                return version
        except Exception:
            pass
    versions = []
    for p in root.rglob("CTBRec_Discovery_v*_HANDOFF.md"):
        m = VERSION_HANDOFF_RE.search(p.name)
        if m:
            versions.append((tuple(map(int, m.groups())), ".".join(m.groups())))
    if versions:
        versions.sort()
        return versions[-1][1]
    init = root / "discovery" / "__init__.py"
    if init.exists():
        m = VERSION_INIT_RE.search(init.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    raise RuntimeError("Could not infer release version from patch. Include a versioned handoff or discovery/__init__.py.")


def copy_public_source(source: Path, dest: Path) -> tuple[int, list[str]]:
    copied = 0
    skipped: list[str] = []
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source).as_posix()
        if not is_allowed_public_source_path(rel) or is_protected_relative_path(rel):
            skipped.append(rel)
            continue
        target = dest / Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return copied, skipped


def append_marker_block(path: Path, start: str, end: str, body: str) -> None:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f"{start}\n{body.rstrip()}\n{end}"
    if start in current and end in current:
        current = re.sub(re.escape(start) + r".*?" + re.escape(end), block, current, flags=re.S)
    else:
        if current and not current.endswith("\n"):
            current += "\n"
        current += ("\n" if current else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(current, encoding="utf-8")


def ensure_root_repo_metadata(repo: Path) -> None:
    root_readme = repo / "README.md"
    if not root_readme.exists():
        root_readme.write_text(
            "# Chaturbate Projects\n\nUmbrella repository for independent Chaturbate/CTBRec-related software projects.\n\n"
            "Each project lives under `projects/<project-name>/`. See `PROJECTS.md` for the active project index.\n",
            encoding="utf-8",
        )
    security = repo / "SECURITY.md"
    if not security.exists():
        security.write_text(
            "# Security and private-data policy\n\nDo not commit passwords, auth tokens, cookies, browser/session profiles, API keys, "
            "private recordings, generated media, logs containing sensitive data, or machine-specific secret configuration.\n",
            encoding="utf-8",
        )
    # Historical placeholder from the empty umbrella-repo bootstrap.
    test_file = repo / "test.md"
    if test_file.exists():
        try:
            test_file.unlink()
        except OSError:
            pass


def refresh_repo_metadata(repo: Path, project_root: Path, app_root: Path, version: str) -> None:
    ensure_root_repo_metadata(repo)
    token = version.replace(".", "_")
    handoff = app_root / f"CTBRec_Discovery_v{token}_HANDOFF.md"
    if not handoff.exists():
        raise RuntimeError(f"Expected release handoff missing: {handoff}")
    shutil.copy2(handoff, project_root / "HANDOFF.md")
    release_dir = project_root / "docs" / "release-notes"
    release_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(handoff, release_dir / handoff.name)

    project_readme = f"""# CTBRec Discovery\n\nPersonalized model discovery, account-continuity and future session-opportunity companion for the CTBRec ecosystem.\n\n**Current stable baseline:** v{version}\n\n- Runnable source: [`app/`](app/)\n- Canonical continuation context: [`HANDOFF.md`](HANDOFF.md)\n- Release history: [`docs/release-notes/`](docs/release-notes/)\n- Git/deployment bridge: [`app/tools/git_bridge/`](app/tools/git_bridge/)\n\nPrivate runtime state and credentials are intentionally excluded from Git.\n"""
    (project_root / "README.md").write_text(project_readme, encoding="utf-8")

    agents = """# AGENTS.md — CTBRec Discovery\n\n## Source of truth\n\n- Runnable source: `app/`\n- Current handoff: `HANDOFF.md`\n- Regression tests: `app/tests/` + `app/validation/`\n\n## Required behavior\n\n1. Never commit `discovery_config.json`, state databases, logs, cookies/sessions, WM IDs, local recordings/media, or machine-specific secrets.\n2. Preserve the three-engine separation: model affinity, account continuity, session opportunity.\n3. Discovery must never block the Mobile Reviewer hot path or create a recurring whole-library scan.\n4. Prefer reuse of Live Control / Mobile Reviewer / Rapid Sorter state over duplicate polling or authentication.\n5. No direct `models.json` mutation and no guessed live-bridge operations.\n6. Keep external collection conservative, explainable and deduplicated.\n7. Add regression coverage for bugs and run all validation before publishing.\n8. Every release updates the versioned handoff and keeps `CHATGPT_HANDOFF.md` current.\n9. `app/tools/git_bridge/` is part of the supported release workflow and must remain in future FULL SOURCE and PATCH packages.\n10. Keep `main` stable; publish through a release branch/PR unless explicitly configured otherwise.\n"""
    (project_root / "AGENTS.md").write_text(agents, encoding="utf-8")

    projects_body = f"""## CTBRec Discovery\n\n**Path:** `projects/ctbrec-discovery/`  \n**Current stable baseline:** v{version}\n\nPersonalized model discovery and account-continuity companion for CTBRec. Source is maintained by the CTBRec Discovery Git Bridge.\n"""
    append_marker_block(repo / "PROJECTS.md", PROJECT_MARKER_START, PROJECT_MARKER_END, projects_body)

    ignore_body = """# CTBRec Discovery private/runtime state\n**/ctbrec-discovery/app/discovery_config.json\n**/ctbrec-discovery/app/state/\n**/ctbrec-discovery/app/**/*.sqlite\n**/ctbrec-discovery/app/**/*.sqlite3\n**/ctbrec-discovery/app/**/*.db\n**/ctbrec-discovery/app/**/*.log\n**/ctbrec-discovery/app/**/*.base64.txt\n**/ctbrec-discovery/app/**/*.base85.txt\n**/ctbrec-discovery/app/**/*.zip\n"""
    append_marker_block(repo / ".gitignore", "# BEGIN CTBREC DISCOVERY GIT BRIDGE", "# END CTBREC DISCOVERY GIT BRIDGE", ignore_body)


def load_bridge_config() -> dict:
    p = config_path()
    if not p.exists():
        raise RuntimeError(f"Bridge is not configured. Run Setup-Git-Bridge.cmd first. Missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError("Bridge config must be a JSON object.")
    return data


def save_bridge_config(cfg: dict) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def git_status_lines(repo: Path) -> list[str]:
    p = run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], capture=True)
    return [line for line in (p.stdout or "").splitlines() if line.strip()]


def git_clean(repo: Path) -> None:
    dirty = [line for line in git_status_lines(repo) if not line.startswith("?? .local/")]
    if dirty:
        raise RuntimeError("Repository has uncommitted/untracked changes:\n" + "\n".join(dirty))


def _untracked_path(line: str) -> str | None:
    if not line.startswith("?? "):
        return None
    raw = line[3:].strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return bytes(raw[1:-1], "utf-8").decode("unicode_escape")
        except Exception:
            return raw[1:-1]
    return raw


def recover_interrupted_setup(repo: Path) -> bool:
    """Remove only unmistakable untracked files left by a failed Discovery bootstrap.

    v1.0 wrote the baseline into the working tree before validation. If validation failed,
    the next launch saw those bridge-generated files as unrelated dirt and aborted. This
    recovery is deliberately narrow: tracked modifications are never touched, and root
    metadata is removed only when it contains this bridge's marker block.
    """
    lines = git_status_lines(repo)
    if not lines:
        return False
    tracked_dirty = [line for line in lines if not line.startswith("?? ")]
    if tracked_dirty:
        return False

    untracked = [_untracked_path(line) for line in lines]
    untracked = [x for x in untracked if x]
    allowed = []
    for rel in untracked:
        norm = normalize_rel(rel)
        if norm.startswith("projects/ctbrec-discovery/"):
            allowed.append(norm); continue
        if norm == ".gitignore":
            path = repo / ".gitignore"
            text_value = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if "# BEGIN CTBREC DISCOVERY GIT BRIDGE" in text_value:
                allowed.append(norm); continue
        if norm == "PROJECTS.md":
            path = repo / "PROJECTS.md"
            text_value = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            if PROJECT_MARKER_START in text_value:
                allowed.append(norm); continue
        # Any unrelated untracked file means we must not guess.
        return False

    if not allowed:
        return False
    step("Recovering files left by an interrupted Discovery Git Bridge setup")
    project = repo / repo_rel_path(PROJECT_SUBDIR)
    if project.exists():
        shutil.rmtree(project, ignore_errors=True)
    for rel in (".gitignore", "PROJECTS.md"):
        path = repo / rel
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    projects = repo / "projects"
    try:
        if projects.exists() and not any(projects.iterdir()):
            projects.rmdir()
    except OSError:
        pass
    say("Recovered the prior failed bootstrap; no tracked repository content was changed.")
    return True


def checkout_pull(repo: Path, branch: str) -> None:
    run(["git", "-C", str(repo), "checkout", branch])
    run(["git", "-C", str(repo), "pull", "--ff-only", "origin", branch])


def validate_zip_entries(zip_path: Path) -> list[str]:
    names: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        raw_names = [normalize_rel(info.filename) for info in zf.infolist() if not info.is_dir()]
        manifest_candidates = [n for n in raw_names if n == PATCH_MANIFEST_NAME or n.endswith("/" + PATCH_MANIFEST_NAME)]
        if len(manifest_candidates) != 1:
            raise RuntimeError(f"PATCH must contain exactly one {PATCH_MANIFEST_NAME}; found {manifest_candidates}")
        manifest_path = manifest_candidates[0]
        try:
            patch_manifest = json.loads(zf.read(manifest_path).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid {PATCH_MANIFEST_NAME}: {type(exc).__name__}: {exc}") from exc
        if patch_manifest.get("schema_version") != 1:
            raise RuntimeError(f"Unsupported PATCH manifest schema: {patch_manifest.get('schema_version')!r}")
        if patch_manifest.get("patch_type") != PATCH_TYPE:
            raise RuntimeError(f"PATCH is not cumulative/self-healing: patch_type={patch_manifest.get('patch_type')!r}")
        if patch_manifest.get("primary_artifact") is not True:
            raise RuntimeError("PATCH manifest must declare primary_artifact=true")
        source_manifest_rel = str(patch_manifest.get("source_manifest") or "").strip()
        if source_manifest_rel != "validation/source_manifest.json":
            raise RuntimeError(f"Unexpected source manifest path: {source_manifest_rel!r}")

        prefix = manifest_path[:-len(PATCH_MANIFEST_NAME)]
        source_manifest_zip = prefix + source_manifest_rel
        if source_manifest_zip not in raw_names:
            raise RuntimeError(f"Cumulative PATCH missing {source_manifest_rel}")
        try:
            source_manifest = json.loads(zf.read(source_manifest_zip).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Invalid source manifest in PATCH: {type(exc).__name__}: {exc}") from exc
        if source_manifest.get("release_version") != patch_manifest.get("release_version"):
            raise RuntimeError("PATCH/source manifest release versions disagree")
        canonical = source_manifest.get("files")
        if not isinstance(canonical, dict) or not canonical:
            raise RuntimeError("PATCH source manifest has no canonical files")
        missing_from_patch = [rel for rel in canonical if prefix + rel not in raw_names]
        if missing_from_patch:
            preview = ", ".join(missing_from_patch[:20])
            raise RuntimeError(f"Cumulative PATCH is missing canonical source files: {preview}")
        for rel, expected in canonical.items():
            actual = hashlib.sha256(zf.read(prefix + rel)).hexdigest()
            if actual != expected:
                raise RuntimeError(f"PATCH canonical source hash mismatch: {rel} expected={expected} actual={actual}")

        for info in zf.infolist():
            if info.is_dir():
                continue
            name = normalize_rel(info.filename)
            payload_name = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if name.startswith("../") or "/../" in f"/{name}/" or name.startswith("/"):
                raise RuntimeError(f"Patch contains path traversal: {info.filename}")
            if not is_allowed_public_source_path(payload_name):
                raise RuntimeError(f"Patch contains a path outside the public source allowlist: {payload_name}")
            if is_protected_relative_path(payload_name):
                raise RuntimeError(f"Patch contains protected/private/runtime content: {payload_name}")
            names.append(payload_name)
    if not names:
        raise RuntimeError("Patch ZIP is empty.")
    return names


def extract_patch(zip_path: Path, temp: Path) -> Path:
    validate_zip_entries(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(temp)
    entries = [p for p in temp.iterdir() if p.name not in {"__MACOSX"}]
    if len(entries) == 1 and entries[0].is_dir() and not (temp / "discovery").exists():
        candidate = entries[0]
        if (candidate / "discovery").exists() or list(candidate.glob("CTBRec_Discovery_v*_HANDOFF.md")):
            return candidate
    return temp


def copy_payload(payload_root: Path, dest: Path) -> list[str]:
    copied = []
    for src in payload_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(payload_root).as_posix()
        if rel == "GIT_BRIDGE_MANIFEST.json":
            continue
        if not is_allowed_public_source_path(rel):
            raise RuntimeError(f"Non-public path reached copy stage: {rel}")
        if is_protected_relative_path(rel):
            raise RuntimeError(f"Protected path reached copy stage: {rel}")
        target = dest / Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(rel)
    return copied


def run_logged(cmd, cwd: Path, log, check: bool = True) -> int:
    log.write("$ " + " ".join(map(str, cmd)) + "\n")
    log.flush()
    p = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write(p.stdout or "")
    log.write(f"\n[exit {p.returncode}]\n")
    log.flush()
    if check and p.returncode != 0:
        output = (p.stdout or "").strip().splitlines()
        tail = "\n".join(output[-80:])
        detail = f"\n--- validation output (tail) ---\n{tail}" if tail else ""
        raise RuntimeError(f"Validation command failed: {' '.join(map(str, cmd))}{detail}")
    return p.returncode


def _run_validation(app_root: Path, log_path: Path, *, include_package_validator: bool) -> int:
    """Run the single authoritative current-release validation pipeline.

    Historical validate_vXYZ.py files are archival regression artifacts. They may remain
    in PATCH-upgraded installations, and they are deliberately NOT auto-executed because
    their assertions describe older source behavior and can become incompatible with a
    later valid release. validation/runner.py + release_manifest.json define the current
    contract for both Git staging and live-runtime checks.
    """
    runner = app_root / "validation" / "runner.py"
    if not runner.is_file():
        raise RuntimeError(f"Authoritative validation runner missing: {runner}")
    mode = "package" if include_package_validator else "runtime"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        run_logged([sys.executable, str(runner), "--mode", mode], app_root, log)
    say(f"Current-release validation passed ({mode} mode).")
    return 1

def run_full_validation(app_root: Path, log_path: Path) -> int:
    step("Running Discovery release validation")
    return _run_validation(app_root, log_path, include_package_validator=True)


def run_runtime_validation(app_root: Path, log_path: Path) -> int:
    # The live installation intentionally contains discovery_config.json and state/,
    # so the release-package safety validator must not run against it.
    step("Running live-runtime validation (package validator intentionally skipped)")
    return _run_validation(app_root, log_path, include_package_validator=False)


def discovery_port(runtime: Path) -> int:
    cfg = runtime / "discovery_config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            return int(data.get("listen_port") or 8793)
        except Exception:
            pass
    return 8793


def listening_pids(port: int) -> list[int]:
    if os.name != "nt":
        return []
    p = subprocess.run(["netstat", "-ano", "-p", "tcp"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pids = set()
    needle = f":{port}"
    for line in (p.stdout or "").splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[0].upper() == "TCP" and needle in cols[1] and cols[3].upper() == "LISTENING":
            try:
                pids.add(int(cols[4]))
            except Exception:
                pass
    return sorted(pids)


def stop_discovery(runtime: Path) -> list[int]:
    pids = listening_pids(discovery_port(runtime))
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return pids


def start_discovery(runtime: Path, log_path: Path) -> subprocess.Popen | None:
    if os.name != "nt":
        return None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "ab", buffering=0)
    creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "discovery.server", "--config", "discovery_config.json"],
        cwd=str(runtime), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation,
    )


def wait_health(runtime: Path, seconds: int = 30) -> bool:
    port = discovery_port(runtime)
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8"))
                    if data.get("ok"):
                        return True
        except Exception:
            pass
        time.sleep(0.7)
    return False


def backup_runtime_files(payload_root: Path, runtime: Path, backup_root: Path) -> list[dict]:
    records = []
    for src in payload_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(payload_root).as_posix()
        if rel == "GIT_BRIDGE_MANIFEST.json":
            continue
        target = runtime / Path(rel)
        rec = {"relative": rel, "existed": target.exists()}
        if target.exists():
            backup = backup_root / Path(rel)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        records.append(rec)
    return records


def restore_runtime_files(records: list[dict], runtime: Path, backup_root: Path) -> None:
    for rec in records:
        target = runtime / Path(rec["relative"])
        if rec["existed"]:
            backup = backup_root / Path(rec["relative"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()


def create_pr_and_maybe_merge(repo_full: str, repo: Path, base: str, branch: str, version: str, comment: str, auto_merge: bool) -> str:
    body = f"""## CTBRec Discovery v{version}\n\n{comment or 'Validated automated release through CTBRec Discovery Git Bridge.'}\n\n### Automated gates\n- patch safety filter passed\n- Python compile passed\n- unit/regression suite passed\n- JavaScript syntax check passed when Node is available\n- authoritative current-release validation manifest passed\n\n_Automatically prepared by CTBRec Discovery Git Bridge._\n"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tf:
        tf.write(body)
        body_path = tf.name
    try:
        p = run(["gh", "pr", "create", "--repo", repo_full, "--base", base, "--head", branch,
                 "--title", f"CTBRec Discovery v{version}", "--body-file", body_path], capture=True)
        pr = (p.stdout or "").strip().splitlines()[-1]
        say(f"Pull request: {pr}")
        if auto_merge:
            run(["gh", "pr", "merge", pr, "--repo", repo_full, "--squash"])
            checkout_pull(repo, base)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", branch], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(repo), "push", "origin", "--delete", branch], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return pr
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


def setup(args) -> None:
    which_required("git", "Install Git first, e.g. winget install --id Git.Git -e")
    which_required("gh", "Install GitHub CLI first, e.g. winget install --id GitHub.cli -e")
    run(["gh", "auth", "status"])

    runtime = Path(args.runtime_path or Path(__file__).resolve().parents[2]).resolve()
    assert_runtime(runtime)
    repo = Path(args.repo_path)
    if not repo.exists():
        step(f"Cloning {args.repository}")
        repo.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", args.repository, str(repo)])
    if not (repo / ".git").exists():
        raise RuntimeError(f"RepoPath exists but is not a Git repository: {repo}")
    repo = repo.resolve()

    # v1.0 could leave its own untracked bootstrap files after validation failed.
    # Repair only that exact safe case; never clean arbitrary user work.
    recover_interrupted_setup(repo)
    git_clean(repo)
    checkout_pull(repo, args.base_branch)
    git_clean(repo)

    version = read_version(runtime)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = state_dir() / "logs" / f"setup-{stamp}.log"

    # Validate a public-safe staging copy BEFORE touching the Git working tree. This makes
    # setup transactional with respect to validation failures and prevents dirty-clone loops.
    with tempfile.TemporaryDirectory(prefix="ctbrec-disc-setup-") as td:
        staging_app = Path(td) / "app"
        step("Preparing public-safe Discovery baseline for validation")
        copied, skipped = copy_public_source(runtime, staging_app)
        run_full_validation(staging_app, log)

        project_root = repo / repo_rel_path(PROJECT_SUBDIR)
        app_root = repo / repo_rel_path(APP_SUBDIR)
        original_head = (run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture=True).stdout or "").strip()
        branch = ""
        try:
            step("Writing validated public-safe Discovery baseline into umbrella repository")
            if app_root.exists():
                shutil.rmtree(app_root)
            app_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging_app, app_root)
            project_root.mkdir(parents=True, exist_ok=True)
            refresh_repo_metadata(repo, project_root, app_root, version)

            run(["git", "-C", str(repo), "add", "-A"])
            diff = run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture=True).stdout or ""
            pr = ""
            if diff.strip():
                branch = f"bootstrap/ctbrec-discovery-v{version}"
                existing = run(["git", "-C", str(repo), "branch", "--list", branch], capture=True).stdout or ""
                if existing.strip():
                    branch += "-" + stamp
                # Create the branch after staging; the index/worktree follow it unchanged.
                run(["git", "-C", str(repo), "checkout", "-b", branch])
                run(["git", "-C", str(repo), "commit", "-m", f"Bootstrap CTBRec Discovery v{version}", "-m", "Public-safe baseline via CTBRec Discovery Git Bridge."])
                run(["git", "-C", str(repo), "push", "-u", "origin", branch])
                pr = create_pr_and_maybe_merge(args.repository, repo, args.base_branch, branch, version, "Initial public-safe CTBRec Discovery baseline.", True)
                tag = f"ctbrec-discovery-v{version}"
                if not (run(["git", "-C", str(repo), "tag", "--list", tag], capture=True).stdout or "").strip():
                    run(["git", "-C", str(repo), "tag", "-a", tag, "-m", f"CTBRec Discovery v{version}"])
                    run(["git", "-C", str(repo), "push", "origin", tag])
            else:
                say("Repository already matches this public-safe Discovery baseline; no commit needed.")

            cfg = {
                "repository_full_name": args.repository,
                "repo_path": str(repo),
                "project_subdir": PROJECT_SUBDIR,
                "app_subdir": APP_SUBDIR,
                "runtime_path": str(runtime),
                "base_branch": args.base_branch,
                "publish_mode": "pull-request",
                "auto_merge_pull_request": True,
                "create_version_tag": True,
                "run_full_validation": True,
                "deploy_runtime": True,
                "restart_after_update": True,
                "bridge_version": BRIDGE_VERSION,
            }
            save_bridge_config(cfg)

            step("Git Bridge setup complete")
            say(f"Repository: {repo}")
            say(f"Project:    {project_root}")
            say(f"Runtime:    {runtime}")
            say(f"Config:     {config_path()}")
            say(f"Bridge:     v{BRIDGE_VERSION}")
            say(f"Copied:     {copied} public-safe files")
            say(f"Excluded:   {len(skipped)} protected/runtime files")
            say(f"Validation log: {log}")
            if pr:
                say(f"PR:         {pr}")
        except Exception:
            # Keep a failed setup from poisoning the next run. Remote pushes/PRs are not
            # silently rewritten here, but the local checkout is restored to origin/main.
            try:
                run(["git", "-C", str(repo), "checkout", args.base_branch], check=False)
                run(["git", "-C", str(repo), "reset", "--hard", f"origin/{args.base_branch}"], check=False)
                if branch:
                    run(["git", "-C", str(repo), "branch", "-D", branch], check=False)
                project = repo / repo_rel_path(PROJECT_SUBDIR)
                if project.exists() and not (run(["git", "-C", str(repo), "ls-files", normalize_rel(PROJECT_SUBDIR)], capture=True, check=False).stdout or "").strip():
                    shutil.rmtree(project, ignore_errors=True)
            except Exception:
                pass
            raise


def apply_update(args) -> None:
    cfg = load_bridge_config()
    repo = Path(cfg["repo_path"]).resolve()
    runtime = Path(cfg["runtime_path"]).resolve()
    assert_runtime(runtime)
    patch = Path(args.patch).resolve()
    if not patch.exists():
        raise RuntimeError(f"Patch ZIP not found: {patch}")
    which_required("git", "Install Git first.")
    which_required("gh", "Install GitHub CLI first.")
    run(["gh", "auth", "status"])

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"update-{stamp}.log"
    backup_root = state_dir() / "backups" / stamp
    old_main = ""
    branch = ""
    pr = ""
    runtime_backup: list[dict] = []
    runtime_was_running = bool(listening_pids(discovery_port(runtime)))

    with tempfile.TemporaryDirectory(prefix="ctbrec-disc-gitbridge-") as td:
        temp = Path(td)
        payload = extract_patch(patch, temp / "payload")
        version = infer_version_from_payload(payload)
        base = cfg.get("base_branch") or BASE_BRANCH
        repo_full = cfg.get("repository_full_name") or REPO_FULL_NAME
        project_root = repo / repo_rel_path(cfg.get("project_subdir") or PROJECT_SUBDIR)
        app_root = repo / repo_rel_path(cfg.get("app_subdir") or APP_SUBDIR)
        branch = f"release/ctbrec-discovery-v{version}"
        try:
            git_clean(repo)
            checkout_pull(repo, base)
            old_main = (run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture=True).stdout or "").strip()
            if (run(["git", "-C", str(repo), "branch", "--list", branch], capture=True).stdout or "").strip():
                run(["git", "-C", str(repo), "branch", "-D", branch])
            if (run(["git", "-C", str(repo), "ls-remote", "--heads", "origin", branch], capture=True).stdout or "").strip():
                branch += "-" + stamp
            run(["git", "-C", str(repo), "checkout", "-b", branch])

            step("Applying patch to Git working copy")
            copy_payload(payload, app_root)
            refresh_repo_metadata(repo, project_root, app_root, version)
            if cfg.get("run_full_validation", True):
                run_full_validation(app_root, log_path)

            run(["git", "-C", str(repo), "add", "-A"])
            staged = run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture=True).stdout or ""
            if not staged.strip():
                raise RuntimeError("Patch produced no Git changes.")
            comment = args.comment or "Validated automated release through CTBRec Discovery Git Bridge."
            run(["git", "-C", str(repo), "commit", "-m", f"CTBRec Discovery v{version}", "-m", comment])

            if args.no_push:
                say("--no-push requested: Git commit created locally; runtime deployment skipped so live code cannot outrun main.")
                return

            run(["git", "-C", str(repo), "push", "-u", "origin", branch])
            if cfg.get("publish_mode", "pull-request") != "pull-request":
                raise RuntimeError("Only publish_mode=pull-request is supported.")
            pr = create_pr_and_maybe_merge(repo_full, repo, base, branch, version, comment, bool(cfg.get("auto_merge_pull_request", True)))
            if not cfg.get("auto_merge_pull_request", True):
                say("PR created but not merged; runtime deployment intentionally deferred until main contains the release.")
                return

            if cfg.get("create_version_tag", True):
                tag = f"ctbrec-discovery-v{version}"
                if not (run(["git", "-C", str(repo), "tag", "--list", tag], capture=True).stdout or "").strip():
                    run(["git", "-C", str(repo), "tag", "-a", tag, "-m", f"CTBRec Discovery v{version}"])
                    run(["git", "-C", str(repo), "push", "origin", tag])

            if not cfg.get("deploy_runtime", True):
                say("Runtime deployment disabled in bridge config.")
                return

            step("Backing up and deploying validated patch to live Discovery folder")
            if runtime_was_running:
                stop_discovery(runtime)
                time.sleep(0.8)
            runtime_backup = backup_runtime_files(payload, runtime, backup_root)
            copy_payload(payload, runtime)
            if cfg.get("run_full_validation", True):
                run_runtime_validation(runtime, logs / f"runtime-validation-{stamp}.log")

            if cfg.get("restart_after_update", True):
                server_log = logs / "discovery-server.log"
                start_discovery(runtime, server_log)
                if not wait_health(runtime, 30):
                    raise RuntimeError("Updated Discovery did not answer /api/health after restart.")
                say("Updated Discovery is running and healthy.")

            result = {
                "version": version,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "repository": repo_full,
                "branch": branch,
                "pull_request": pr,
                "runtime_path": str(runtime),
                "log": str(log_path),
            }
            state_dir().mkdir(parents=True, exist_ok=True)
            (state_dir() / "last-update.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            say(f"\nCTBRec Discovery v{version} published and deployed successfully.")
        except Exception:
            if runtime_backup:
                try:
                    say("Restoring previous live Discovery files...")
                    stop_discovery(runtime)
                    restore_runtime_files(runtime_backup, runtime, backup_root)
                    if runtime_was_running and cfg.get("restart_after_update", True):
                        start_discovery(runtime, logs / "discovery-server.log")
                        wait_health(runtime, 30)
                except Exception as rollback_exc:
                    say(f"Rollback encountered an error: {rollback_exc}")
            try:
                run(["git", "-C", str(repo), "checkout", base], check=False)
                if old_main:
                    run(["git", "-C", str(repo), "reset", "--hard", f"origin/{base}"], check=False)
                if branch:
                    run(["git", "-C", str(repo), "branch", "-D", branch], check=False)
            except Exception:
                pass
            raise


def status_cmd(args) -> None:
    cfg = load_bridge_config()
    repo = Path(cfg["repo_path"])
    runtime = Path(cfg["runtime_path"])
    say(json.dumps({
        "config_path": str(config_path()),
        "repository": cfg.get("repository_full_name"),
        "repo_path": str(repo),
        "project_subdir": cfg.get("project_subdir"),
        "runtime_path": str(runtime),
        "runtime_version": read_version(runtime) if runtime.exists() else None,
        "repo_exists": (repo / ".git").exists(),
        "runtime_health": wait_health(runtime, 2) if runtime.exists() else False,
        "bridge_version": BRIDGE_VERSION,
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CTBRec Discovery Git Bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="Bootstrap/sync current Discovery source into the umbrella GitHub repo")
    s.add_argument("--repository", default=REPO_FULL_NAME)
    s.add_argument("--repo-path", default=DEFAULT_REPO_PATH)
    s.add_argument("--runtime-path", default="")
    s.add_argument("--base-branch", default=BASE_BRANCH)
    s.set_defaults(func=setup)

    a = sub.add_parser("apply", help="Validate, commit/publish, and deploy a future ChatGPT PATCH ZIP")
    a.add_argument("patch")
    a.add_argument("--comment", default="")
    a.add_argument("--no-push", action="store_true")
    a.set_defaults(func=apply_update)

    st = sub.add_parser("status", help="Show bridge configuration and local runtime health")
    st.set_defaults(func=status_cmd)
    return p


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except KeyboardInterrupt:
        say("Cancelled.")
        return 130
    except Exception as exc:
        say(f"\nGIT BRIDGE FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
