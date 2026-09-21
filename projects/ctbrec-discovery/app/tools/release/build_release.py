from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PROTECTED_NAMES = {
    "discovery_config.json", "discovery.sqlite3", "discovery.sqlite", "discovery.db",
    "mobile_catalog_cache.json", "mosaic_lite_recu_cache.json", "recu_local_archive.json",
    "mobile_recu_session.json",
}
PROTECTED_DIRS = {"state", "runtime", "logs", "log", "tmp", "temp", "cookies", "sessions", "__pycache__"}
PROTECTED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".log", ".pyc", ".pyo", ".zip", ".7z", ".rar", ".pem", ".key", ".p12", ".pfx"}
PUBLIC_DIRS = {"discovery", "docs", "examples", "scripts", "static", "tests", "tools", "validation"}
PUBLIC_ROOT = {".gitignore", "README.md", "CHATGPT_HANDOFF.md", "PATCH_MANIFEST.json", "discovery_config.example.json"}
VERSION_HANDOFF = re.compile(r"CTBRec_Discovery_v\d+_\d+_\d+_HANDOFF\.md$", re.I)
TEST_REPORT = re.compile(r"TEST_REPORT_v\d+_\d+_\d+\.txt$", re.I)


def version() -> str:
    text = (ROOT / "discovery" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
    if not m:
        raise RuntimeError("Could not read Discovery version")
    return m.group(1)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def protected(r: str) -> bool:
    pp = PurePosixPath(r)
    parts = {x.casefold() for x in pp.parts}
    name = pp.name.casefold()
    if name in PROTECTED_NAMES or any(x in PROTECTED_DIRS for x in parts):
        return True
    if pp.suffix.casefold() in PROTECTED_SUFFIXES:
        return True
    if name.startswith(".env") or "cookie" in name or "session_token" in name or "credential" in name:
        return True
    if name.endswith(".base64.txt") or name.endswith(".base85.txt"):
        return True
    return False


def public_file(p: Path) -> bool:
    if not p.is_file():
        return False
    r = rel(p)
    if protected(r):
        return False
    pp = PurePosixPath(r)
    if len(pp.parts) == 1:
        return pp.name in PUBLIC_ROOT or bool(VERSION_HANDOFF.fullmatch(pp.name)) or bool(TEST_REPORT.fullmatch(pp.name))
    return pp.parts[0] in PUBLIC_DIRS


def canonical_paths() -> list[str]:
    # These files define executable behavior, validation, release tooling, and user-facing UI.
    roots = ["discovery", "static", "scripts", "tests", "tools/git_bridge", "tools/release"]
    files: set[str] = set()
    for r in roots:
        base = ROOT / r
        if base.exists():
            for p in base.rglob("*"):
                if public_file(p):
                    files.add(rel(p))
    for r in [
        "validation/release_manifest.json", "validation/runner.py", "validation/validate_current.py", "validation/validate_package.py",
        "discovery_config.example.json", "docs/RELEASE_ENGINEERING_PROMPT.md",
    ]:
        p = ROOT / r
        if p.is_file():
            files.add(r)
    return sorted(files)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifests() -> None:
    v = version()
    canonical = canonical_paths()
    src = {
        "schema_version": 1,
        "release_version": v,
        "algorithm": "sha256",
        "files": {r: sha256(ROOT / r) for r in canonical},
    }
    (ROOT / "validation" / "source_manifest.json").write_text(json.dumps(src, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    patch = {
        "schema_version": 1,
        "release_version": v,
        "patch_type": "cumulative_public_source_overlay",
        "primary_artifact": True,
        "min_supported_version": "0.3.0",
        "source_manifest": "validation/source_manifest.json",
        "contract": "Contains every canonical public source file required to self-heal a skipped/interrupted incremental upgrade. Never includes runtime/private state.",
    }
    (ROOT / "PATCH_MANIFEST.json").write_text(json.dumps(patch, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def all_public_files() -> list[Path]:
    return sorted((p for p in ROOT.rglob("*") if public_file(p)), key=lambda p: rel(p).casefold())


def patch_files() -> list[Path]:
    # PATCH is intentionally cumulative, not a diff. It contains ALL current public source/tooling.
    keep: list[Path] = []
    current = version().replace(".", "_")
    for p in all_public_files():
        r = rel(p)
        name = p.name
        if VERSION_HANDOFF.fullmatch(name) and name != f"CTBRec_Discovery_v{current}_HANDOFF.md":
            continue
        if TEST_REPORT.fullmatch(name) and name != f"TEST_REPORT_v{current}.txt":
            continue
        keep.append(p)
    return keep


def write_zip(out: Path, files: list[Path], top_dir: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            arc = rel(p)
            if top_dir:
                arc = f"{top_dir}/{arc}"
            z.write(p, arc)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build CTBRec Discovery FULL + cumulative PATCH artifacts")
    ap.add_argument("--out", default=str(ROOT.parent))
    args = ap.parse_args()
    write_manifests()
    v = version()
    tag = v.replace(".", "_")
    out = Path(args.out).resolve()
    full = out / f"CTBRec_Discovery_v{tag}_FULL_SOURCE.zip"
    patch = out / f"CTBRec_Discovery_v{tag}_PATCH.zip"
    # Manifest generation changes the public file set; enumerate only after manifests are written.
    write_zip(full, all_public_files(), top_dir=f"CTBRec_Discovery_v{tag}")
    write_zip(patch, patch_files(), top_dir=None)
    print(f"FULL={full}")
    print(f"PATCH={patch}")
    print(f"PATCH_MODE=cumulative_public_source_overlay")
    print(f"PATCH_FILES={len(patch_files())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
