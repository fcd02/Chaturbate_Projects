from __future__ import annotations
import importlib.util
import json
import tempfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
bridge_path = root / 'tools' / 'git_bridge' / 'bridge.py'
spec = importlib.util.spec_from_file_location('ctbrec_discovery_git_bridge', bridge_path)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert mod.REPO_FULL_NAME == 'fcd02/Chaturbate_Projects'
assert mod.PROJECT_SUBDIR.replace('\\','/') == 'projects/ctbrec-discovery'
assert mod.APP_SUBDIR.replace('\\','/') == 'projects/ctbrec-discovery/app'

# Protected/private/runtime filters.
for rel in [
    'discovery_config.json', 'state/discovery.sqlite3', 'logs/x.log', '.env',
    'runtime/recu_local_archive.json', 'secret_api_key.txt', 'recording.mp4',
    'release.zip', 'CTBRec_Discovery_v0_4_2_PATCH.zip.base85.txt'
]:
    assert mod.is_protected_relative_path(rel), rel
for rel in [
    'discovery_config.example.json', 'discovery/server.py', 'static/app.js',
    'tools/git_bridge/bridge.py', 'CTBRec_Discovery_v0_4_2_HANDOFF.md'
]:
    assert not mod.is_protected_relative_path(rel), rel

# Public snapshot must exclude active config/state while retaining safe source.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    src = td / 'src'; dst = td / 'dst'
    (src/'discovery').mkdir(parents=True)
    (src/'state').mkdir()
    (src/'tools'/'git_bridge').mkdir(parents=True)
    (src/'discovery'/'server.py').write_text('print(1)')
    (src/'discovery_config.example.json').write_text('{}')
    (src/'discovery_config.json').write_text('{"wm":"SECRET"}')
    (src/'state'/'discovery.sqlite3').write_bytes(b'private')
    (src/'tools'/'git_bridge'/'bridge.py').write_text('safe')
    copied, skipped = mod.copy_public_source(src, dst)
    assert (dst/'discovery'/'server.py').exists()
    assert (dst/'discovery_config.example.json').exists()
    assert (dst/'tools'/'git_bridge'/'bridge.py').exists()
    assert not (dst/'discovery_config.json').exists()
    assert not (dst/'state'/'discovery.sqlite3').exists()
    assert copied == 3
    assert any(x == 'discovery_config.json' for x in skipped)

# Patch ZIP validator must reject protected payloads and accept safe payloads.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    good = td/'good.zip'
    with zipfile.ZipFile(good,'w') as z:
        z.writestr('discovery/server.py','x')
        z.writestr('CTBRec_Discovery_v0_4_2_HANDOFF.md','# handoff')
    names = mod.validate_zip_entries(good)
    assert 'discovery/server.py' in names

    bad = td/'bad.zip'
    with zipfile.ZipFile(bad,'w') as z:
        z.writestr('discovery_config.json','{"wm":"SECRET"}')
    try:
        mod.validate_zip_entries(bad)
        raise AssertionError('private patch unexpectedly accepted')
    except RuntimeError:
        pass

    traversal = td/'traversal.zip'
    with zipfile.ZipFile(traversal,'w') as z:
        z.writestr('../escape.txt','x')
    try:
        mod.validate_zip_entries(traversal)
        raise AssertionError('path traversal unexpectedly accepted')
    except RuntimeError:
        pass

# Version inference must use latest versioned handoff.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    (td/'CTBRec_Discovery_v0_4_1_HANDOFF.md').write_text('old')
    (td/'CTBRec_Discovery_v0_4_2_HANDOFF.md').write_text('new')
    assert mod.infer_version_from_payload(td) == '0.4.2'

cfg = json.loads((root/'tools'/'git_bridge'/'git_bridge.config.example.json').read_text())
assert cfg['repository_full_name'] == 'fcd02/Chaturbate_Projects'
assert cfg['project_subdir'].replace('\\','/') == 'projects/ctbrec-discovery'
assert cfg['app_subdir'].replace('\\','/') == 'projects/ctbrec-discovery/app'
assert cfg['publish_mode'] == 'pull-request'
assert cfg['auto_merge_pull_request'] is True
assert cfg['create_version_tag'] is True
assert cfg['run_full_validation'] is True
assert cfg['deploy_runtime'] is True
assert cfg['restart_after_update'] is True

bridge_src=(root/'tools'/'git_bridge'/'bridge.py').read_text(encoding='utf-8')
assert 'def run_runtime_validation' in bridge_src
assert 'include_package_validator=False' in bridge_src
assert 'discovery_config.json and state/' in bridge_src
assert 'package-safety gate' in bridge_src
assert 'shutil.rmtree(cache' in bridge_src

readme=(root/'tools'/'git_bridge'/'README.md').read_text(encoding='utf-8')
assert '%LOCALAPPDATA%\\CTBRecDiscoveryGitBridge\\config.json' in readme
assert 'Apply-ChatGPT-Update.cmd' in readme
assert 'projects\\ctbrec-discovery\\app' in readme
assert 'discovery_config.json' in readme

print('PASS v0.4.2 Git Bridge constants, protected-path filter, public-safe snapshot, patch guard, version inference, config and documentation')
