from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "validation" / "release_manifest.json"


def _run(cmd: list[str], cwd: Path = ROOT) -> None:
    print("$ " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.stdout:
        print(p.stdout, end="" if p.stdout.endswith("\n") else "\n")
    if p.returncode != 0:
        raise SystemExit(p.returncode)


def load_manifest(root: Path = ROOT) -> dict:
    path = root / "validation" / "release_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise RuntimeError(f"Unsupported validation manifest schema: {data.get('schema_version')!r}")
    release = str(data.get("release_version") or "").strip()
    if not release:
        raise RuntimeError("Validation manifest release_version is required")
    suites = data.get("current_suites")
    if not isinstance(suites, list) or not suites:
        raise RuntimeError("Validation manifest current_suites must be a non-empty list")
    for item in suites:
        if not isinstance(item, str) or not item.endswith(".py") or "/" in item or "\\" in item or item in {".", ".."}:
            raise RuntimeError(f"Unsafe validation suite entry: {item!r}")
    package = data.get("package_suite")
    if not isinstance(package, str) or not package.endswith(".py") or "/" in package or "\\" in package:
        raise RuntimeError(f"Unsafe package_suite entry: {package!r}")
    return data


def selected_release_suites(root: Path = ROOT) -> list[Path]:
    data = load_manifest(root)
    out: list[Path] = []
    for name in data["current_suites"]:
        p = root / "validation" / name
        if not p.is_file():
            raise RuntimeError(f"Current release validator missing: {p}")
        out.append(p)
    return out


def package_suite(root: Path = ROOT) -> Path:
    data = load_manifest(root)
    p = root / "validation" / data["package_suite"]
    if not p.is_file():
        raise RuntimeError(f"Package validator missing: {p}")
    return p


def verify_source_manifest(root: Path = ROOT) -> dict:
    path = root / "validation" / "source_manifest.json"
    if not path.is_file():
        raise RuntimeError(f"Current source manifest missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("algorithm") != "sha256":
        raise RuntimeError(f"Unsupported source manifest: {data}")
    release = load_manifest(root)["release_version"]
    if data.get("release_version") != release:
        raise RuntimeError(f"Source manifest release mismatch: {data.get('release_version')!r} != {release!r}")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("Source manifest files mapping is empty")
    import hashlib
    missing = []
    mismatched = []
    for rel, expected in sorted(files.items()):
        fp = root / Path(rel)
        if not fp.is_file():
            missing.append(rel)
            continue
        actual = hashlib.sha256(fp.read_bytes()).hexdigest()
        if actual != expected:
            mismatched.append((rel, expected, actual))
    if missing or mismatched:
        lines = ["Current source tree does not match this release's canonical source manifest."]
        if missing:
            lines.append("Missing: " + ", ".join(missing))
        if mismatched:
            lines.extend(f"Hash mismatch: {rel} expected={exp} actual={act}" for rel, exp, act in mismatched[:20])
        lines.append("Apply the CURRENT cumulative PATCH ZIP over the live folder, then rerun setup. Do not skip intermediate source files manually.")
        raise RuntimeError("\n".join(lines))
    return data


def _clear_caches(root: Path = ROOT) -> None:
    for cache in list(root.rglob("__pycache__")):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
    for pyc in list(root.rglob("*.pyc")):
        try:
            pyc.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CTBRec Discovery authoritative current-release validation runner")
    ap.add_argument("--mode", choices=["package", "runtime", "auto"], default="auto")
    args = ap.parse_args(argv)

    manifest = load_manifest(ROOT)
    mode = args.mode
    if mode == "auto":
        mode = "runtime" if (ROOT / "discovery_config.json").exists() or (ROOT / "state" / "discovery.sqlite3").exists() else "package"

    print(f"Validation release: {manifest['release_version']}")
    print(f"Validation mode: {mode}")
    current = selected_release_suites(ROOT)
    historical = sorted(
        p.name for p in (ROOT / "validation").glob("validate_v*.py")
        if p.name not in {x.name for x in current}
    )
    print("Authoritative current suites: " + ", ".join(x.name for x in current))
    if historical:
        print("Historical validators present but NOT auto-executed: " + ", ".join(historical))

    source_manifest = verify_source_manifest(ROOT)
    print(f"Canonical source manifest verified: {len(source_manifest['files'])} files")

    py = sys.executable
    py_files = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "discovery").glob("*.py"))]
    py_files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests").glob("*.py"))]
    # Compile only the authoritative validation pipeline. Historical validate_vXYZ.py files
    # are deliberately outside the current release contract, even at syntax-compile time.
    validation_compile = [ROOT / "validation" / "runner.py", package_suite(ROOT), *current]
    py_files += [str(p.relative_to(ROOT)) for p in validation_compile]
    bridge = ROOT / "tools" / "git_bridge" / "bridge.py"
    if bridge.exists():
        py_files.append(str(bridge.relative_to(ROOT)))
    if py_files:
        _run([py, "-m", "py_compile", *py_files])

    _run([py, "-m", "unittest", "discover", "-s", "tests", "-v"])

    node = shutil.which("node")
    app_js = ROOT / "static" / "app.js"
    if node and app_js.exists():
        _run([node, "--check", str(app_js)])

    for suite in current:
        _run([py, str(suite)])

    _clear_caches(ROOT)
    if mode == "package":
        _run([py, str(package_suite(ROOT))])

    print(f"ALL CURRENT-RELEASE VALIDATION PASSED ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
