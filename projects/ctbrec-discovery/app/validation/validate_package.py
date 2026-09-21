from __future__ import annotations

import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
required = [
    'README.md',
    'discovery/__init__.py', 'discovery/server.py', 'discovery/store.py', 'discovery/scoring.py',
    'discovery/continuity.py', 'discovery/integration.py', 'discovery/collectors.py', 'discovery/importers.py',
    'discovery/check_config.py', 'scripts/CHECK_CONFIG.bat', 'scripts/START_DISCOVERY.bat', 'scripts/VALIDATE.bat',
    'static/index.html', 'static/app.js', 'static/styles.css', 'discovery_config.example.json',
    'CTBRec_Discovery_v0_4_6_HANDOFF.md', 'CHATGPT_HANDOFF.md',
    'docs/ARCHITECTURE.md', 'docs/INTEGRATION_CONTRACT.md', 'docs/RELEASE_ENGINEERING_PROMPT.md',
    'validation/release_manifest.json', 'validation/source_manifest.json', 'validation/runner.py', 'validation/validate_current.py', 'validation/validate_package.py',
    'tools/git_bridge/bridge.py', 'tools/git_bridge/README.md', 'tools/git_bridge/Setup-Git-Bridge.cmd',
    'tools/git_bridge/Apply-ChatGPT-Update.cmd', 'tools/git_bridge/Bridge-Status.cmd',
    'tools/git_bridge/git_bridge.config.example.json', 'tools/release/build_release.py', 'PATCH_MANIFEST.json'
]
missing = [x for x in required if not (root / x).exists()]
assert not missing, f'missing: {missing}'

# Release manifest is the ONLY authority for which release-specific validators execute.
manifest = json.loads((root / 'validation' / 'release_manifest.json').read_text(encoding='utf-8'))
assert manifest.get('schema_version') == 1, manifest
assert manifest.get('release_version') == '0.4.6', manifest
assert manifest.get('current_suites') == ['validate_current.py'], manifest
assert manifest.get('package_suite') == 'validate_package.py', manifest

init = (root / 'discovery' / '__init__.py').read_text(encoding='utf-8')
m = re.search(r'__version__\s*=\s*["\']([^"\']+)', init)
assert m and m.group(1) == manifest['release_version'], (m.group(1) if m else None, manifest)

source_manifest = json.loads((root / 'validation' / 'source_manifest.json').read_text(encoding='utf-8'))
assert source_manifest.get('schema_version') == 1, source_manifest
assert source_manifest.get('release_version') == '0.4.6', source_manifest
assert source_manifest.get('algorithm') == 'sha256', source_manifest
assert isinstance(source_manifest.get('files'), dict) and source_manifest['files'], source_manifest
import hashlib
for rel, expected in sorted(source_manifest['files'].items()):
    fp = root / rel
    assert fp.is_file(), f'canonical source file missing: {rel}'
    actual = hashlib.sha256(fp.read_bytes()).hexdigest()
    assert actual == expected, f'canonical source hash mismatch: {rel} expected={expected} actual={actual}'

patch_manifest = json.loads((root / 'PATCH_MANIFEST.json').read_text(encoding='utf-8'))
assert patch_manifest.get('schema_version') == 1, patch_manifest
assert patch_manifest.get('release_version') == '0.4.6', patch_manifest
assert patch_manifest.get('patch_type') == 'cumulative_public_source_overlay', patch_manifest
assert patch_manifest.get('primary_artifact') is True, patch_manifest
assert patch_manifest.get('source_manifest') == 'validation/source_manifest.json', patch_manifest
assert patch_manifest.get('min_supported_version') == '0.3.0', patch_manifest

bridge = (root / 'tools' / 'git_bridge' / 'bridge.py').read_text(encoding='utf-8')
assert 'BRIDGE_VERSION = "1.2.0"' in bridge
assert 'validation/runner.py + release_manifest.json define the current' in bridge
assert 'glob("validate_*.py")' not in bridge, 'Git Bridge must not auto-run historical validators'
assert 'Current-release validation passed' in bridge
assert 'cumulative_public_source_overlay' in bridge, 'bridge must enforce cumulative patch contract'
assert 'PATCH_MANIFEST.json' in bridge, 'bridge must require patch manifest'

validate_bat = (root / 'scripts' / 'VALIDATE.bat').read_text(encoding='utf-8', errors='replace').casefold()
assert 'validation\\runner.py --mode auto' in validate_bat
assert 'validate_v*.py' not in validate_bat

runner_src = (root / 'validation' / 'runner.py').read_text(encoding='utf-8')
assert 'validation_compile = [ROOT / "validation" / "runner.py", package_suite(ROOT), *current]' in runner_src
assert 'sorted((ROOT / "validation").glob("*.py"))' not in runner_src, 'historical validators must not enter the compile gate'

scan_files = ['discovery/server.py','discovery/store.py','discovery/integration.py','discovery/collectors.py','README.md']
text = '\n'.join((root / x).read_text(encoding='utf-8') for x in scan_files).casefold()
for forbidden in ['face_recognition','deepface']:
    assert forbidden not in text, f'forbidden marker found: {forbidden}'

integration = (root / 'discovery/integration.py').read_text(encoding='utf-8').casefold()
assert 'models.json' not in integration, 'integration adapter must not write models.json'
assert '/api/auth' in integration, 'Reviewer health probe must prefer lightweight /api/auth'
assert 'get-fallback' not in integration, 'whole-shell GET fallback must not return'

collectors = (root / 'discovery/collectors.py').read_text(encoding='utf-8')
assert 'https://chaturbate.com/api/public/affiliates/onlinerooms/' in collectors
assert 'affiliates/promotools/api_usersonline' not in collectors.casefold()
assert 'recu.me' not in collectors.casefold(), 'collector module must not directly crawl Recu'
assert 'live_control_models' in collectors
assert 'self._locks' in collectors and 'self._lock.acquire' not in collectors, 'collector locking must be per source'
assert 'v46|' in collectors, 'Recu local cursor generation must force one field reparse'

importers = (root / 'discovery/importers.py').read_text(encoding='utf-8')
assert 'signature = f"v46:' in importers, 'catalog parser generation must force v0.4 false-success reparse'
assert '_walk_catalog' in importers

cfg = json.loads((root / 'discovery_config.example.json').read_text())
assert cfg['listen_port'] == 8793
assert cfg['reviewer_base_url'] == 'http://127.0.0.1:8787'
assert float(cfg['reviewer_probe_timeout_seconds']) >= 2.0
assert cfg['live_control_base_url'] == 'http://127.0.0.1:8792'
assert cfg['collectors']['chaturbate_affiliate']['wm'] == ''
assert cfg['collectors']['chaturbate_affiliate']['enabled'] is False
assert cfg['collectors']['live_control_models']['enabled'] is True
for name, c in cfg['collectors'].items():
    if name != 'live_control_models':
        assert c.get('enabled') is False, f'shipping external/file collector must default disabled: {name}'
assert cfg['recommendation_filters']['allowed_genders'] == ['m']
assert cfg['recommendation_filters']['include_unknown_gender'] is True
assert cfg['recommendation_filters']['include_couples'] is False

assert not (root / 'discovery_config.json').exists(), 'active config must not ship'
assert not (root / 'state' / 'discovery.sqlite3').exists(), 'runtime DB must not ship'
for p in root.rglob('*'):
    if p.is_file():
        low = str(p).casefold()
        assert '__pycache__' not in low and not low.endswith('.pyc'), f'cache file shipped: {p}'
        assert not low.endswith('.log'), f'log file shipped: {p}'
        assert 'mobile_recu_session' not in low and 'cookie' not in p.name.casefold(), f'private/session file shipped: {p}'
assert not (root / 'tools' / 'git_bridge' / 'config.json').exists(), 'active Git Bridge config must not ship'

print('PASS v0.4.6 package structure, manifest-based current-release validation, Git Bridge v1.2.0, local-first defaults, recommendation filters, integration boundary, and safety gates')
