from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
bridge_path = root / 'tools' / 'git_bridge' / 'bridge.py'
spec = importlib.util.spec_from_file_location('ctbrec_discovery_git_bridge_v043', bridge_path)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert tuple(map(int, mod.BRIDGE_VERSION.split('.'))) >= (1, 0, 1)

import json
cfg = json.loads((root / 'tools' / 'git_bridge' / 'git_bridge.config.example.json').read_text(encoding='utf-8'))
assert cfg['bridge_version'] == mod.BRIDGE_VERSION

# The historical v0.4 startup validation must still detect blocking construction, but
# a sub-second threshold is too flaky on a real Windows/Defender workstation.
v040 = (root / 'validation' / 'validate_v040.py').read_text(encoding='utf-8')
assert "CTBREC_VALIDATION_STARTUP_MAX_SECONDS" in v040
assert "'5.0'" in v040
assert 'elapsed < 0.5' not in v040

bridge_src = bridge_path.read_text(encoding='utf-8')
assert 'Preparing public-safe Discovery baseline for validation' in bridge_src
assert bridge_src.index('run_full_validation(staging_app, log)') < bridge_src.index('Writing validated public-safe Discovery baseline into umbrella repository')
assert 'Recovering files left by an interrupted Discovery Git Bridge setup' in bridge_src
assert 'validation output (tail)' in bridge_src
assert 'bridge_version": BRIDGE_VERSION' in bridge_src

# Launcher fallback must select a Python executable, not rerun the bridge after an actual
# bridge/validation failure. v1.0 retried with `python` on any non-zero exit and caused the
# second dirty-tree failure seen in the user's field log.
validate_cmd = (root / 'scripts' / 'VALIDATE.bat').read_text(encoding='utf-8', errors='replace').lower()
assert ('validate_v043.py' in validate_cmd) or ('validation\\validate_v*.py' in validate_cmd)
assert 'if errorlevel 1 python' not in validate_cmd

for name in ['Setup-Git-Bridge.cmd', 'Apply-ChatGPT-Update.cmd', 'Bridge-Status.cmd']:
    text = (root / 'tools' / 'git_bridge' / name).read_text(encoding='utf-8', errors='replace').lower()
    assert 'where py' in text, name
    assert 'if errorlevel 1 python' not in text, name


def git(*args, cwd: Path):
    return subprocess.run(['git', *args], cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout

# Interrupted-bootstrap recovery is narrow and safe: only untracked files carrying this
# bridge's markers/project path are removed, while tracked repo content remains intact.
with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / 'repo'
    repo.mkdir()
    git('init', '-b', 'main', cwd=repo)
    git('config', 'user.email', 'test@example.com', cwd=repo)
    git('config', 'user.name', 'Test', cwd=repo)
    (repo / 'test.md').write_text('keep me\n', encoding='utf-8')
    git('add', 'test.md', cwd=repo)
    git('commit', '-m', 'baseline', cwd=repo)

    (repo / '.gitignore').write_text('# BEGIN CTBREC DISCOVERY GIT BRIDGE\nx\n# END CTBREC DISCOVERY GIT BRIDGE\n', encoding='utf-8')
    (repo / 'PROJECTS.md').write_text(f'{mod.PROJECT_MARKER_START}\nx\n{mod.PROJECT_MARKER_END}\n', encoding='utf-8')
    project = repo / 'projects' / 'ctbrec-discovery'
    project.mkdir(parents=True)
    (project / 'README.md').write_text('generated\n', encoding='utf-8')

    assert mod.recover_interrupted_setup(repo) is True
    assert (repo / 'test.md').read_text(encoding='utf-8') == 'keep me\n'
    assert not (repo / '.gitignore').exists()
    assert not (repo / 'PROJECTS.md').exists()
    assert not project.exists()
    assert mod.git_status_lines(repo) == []

# Unrelated user work must not be auto-cleaned.
with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / 'repo'
    repo.mkdir()
    git('init', '-b', 'main', cwd=repo)
    git('config', 'user.email', 'test@example.com', cwd=repo)
    git('config', 'user.name', 'Test', cwd=repo)
    (repo / 'test.md').write_text('base\n', encoding='utf-8')
    git('add', 'test.md', cwd=repo)
    git('commit', '-m', 'baseline', cwd=repo)
    (repo / 'my-notes.txt').write_text('do not delete\n', encoding='utf-8')
    assert mod.recover_interrupted_setup(repo) is False
    assert (repo / 'my-notes.txt').exists()

print('PASS v0.4.3 Git Bridge transactional setup, interrupted-bootstrap recovery, non-retrying launchers, surfaced validation diagnostics, and Windows-tolerant startup timing gate')
