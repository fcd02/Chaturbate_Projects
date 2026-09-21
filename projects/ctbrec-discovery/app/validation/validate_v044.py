from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
bridge_path = root / 'tools' / 'git_bridge' / 'bridge.py'
spec = importlib.util.spec_from_file_location('ctbrec_discovery_git_bridge_v044', bridge_path)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert mod.BRIDGE_VERSION == '1.0.2'
cfg = json.loads((root / 'tools' / 'git_bridge' / 'git_bridge.config.example.json').read_text(encoding='utf-8'))
assert cfg['bridge_version'] == mod.BRIDGE_VERSION

# Core regression: the package gate must validate the CURRENT release, not require every
# historical validator that may or may not exist in an incrementally PATCH-upgraded runtime.
pkg = (root / 'validation' / 'validate_package.py').read_text(encoding='utf-8')
assert "'validation/validate_v044.py'" in pkg
for old in ['validate_v041.py', 'validate_v042.py', 'validate_v043.py']:
    required_form = f"'validation/{old}'"
    assert required_form not in pkg, f'historical validator is incorrectly mandatory: {old}'

# User-facing validation must discover whatever release validators are present instead of
# spelling out a brittle historical sequence.
validate_bat = (root / 'scripts' / 'VALIDATE.bat').read_text(encoding='utf-8', errors='replace').casefold()
assert 'for %%v in (validation\\validate_v*.py)' in validate_bat
assert 'validation\\validate_v041.py' not in validate_bat
assert 'validation\\validate_v042.py' not in validate_bat

bridge_src = bridge_path.read_text(encoding='utf-8')
assert 'Validation scripts present:' in bridge_src

# Reproduce the real field-upgrade shape: a public-safe runtime can be current while missing
# historical validators. The staging/package validator MUST still pass.
with tempfile.TemporaryDirectory() as td:
    staging = Path(td) / 'app'
    copied, skipped = mod.copy_public_source(root, staging)
    assert copied > 0
    for old in ['validate_v041.py', 'validate_v042.py', 'validate_v043.py']:
        p = staging / 'validation' / old
        if p.exists():
            p.unlink()
    assert (staging / 'validation' / 'validate_v044.py').exists()
    cp = subprocess.run(
        [sys.executable, str(staging / 'validation' / 'validate_package.py')],
        cwd=str(staging), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    assert cp.returncode == 0, cp.stdout

# Historical validators are intentionally OPTIONAL at runtime/bootstrap. The repair PATCH
# carries them to self-heal current installations, but current validation may not depend on them.
assert (root / 'validation' / 'validate_v044.py').exists()

print('PASS v0.4.4 current-release validation invariant, incremental-upgrade bootstrap regression, dynamic validator discovery, and Git Bridge v1.0.2')
