from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
server = (ROOT / 'ctbrec_mobile_server.py').read_text(encoding='utf-8')
sw = (ROOT / 'static' / 'service-worker.js').read_text(encoding='utf-8')
html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')

assert 'CTBRecMobile/2.15.10' in server
assert 'ctbrec-shell-v21510' in sw
assert 'app.js?v=21510' in html and 'rapid.js?v=21510' in html and 'styles.css?v=21510' in html

bridge = ROOT / 'tools' / 'git_bridge'
required = [
    'Setup-Git-Bridge.cmd',
    'Setup-Git-Bridge.ps1',
    'Apply-ChatGPT-Update.cmd',
    'Apply-ChatGPT-Update.ps1',
    'Install-Codex-CLI.cmd',
    'README.md',
    'CODEX_WINDOWS_SETUP.md',
    'git_bridge.config.example.json',
    'BRIDGE_VERSION.txt',
]
for name in required:
    assert (bridge / name).is_file(), name

setup_cmd = (bridge / 'Setup-Git-Bridge.cmd').read_text(encoding='utf-8')
apply_cmd = (bridge / 'Apply-ChatGPT-Update.cmd').read_text(encoding='utf-8')
codex_cmd = (bridge / 'Install-Codex-CLI.cmd').read_text(encoding='utf-8')
setup = (bridge / 'Setup-Git-Bridge.ps1').read_text(encoding='utf-8')
apply = (bridge / 'Apply-ChatGPT-Update.ps1').read_text(encoding='utf-8')
readme = (bridge / 'README.md').read_text(encoding='utf-8')

# The bridge itself must remain runnable even on a machine whose normal
# PowerShell execution policy blocks npm.ps1/unsigned .ps1 files.
assert '-ExecutionPolicy Bypass' in setup_cmd
assert '-ExecutionPolicy Bypass' in apply_cmd
assert 'npm.cmd install -g @openai/codex@latest' in codex_cmd
assert 'npm install -g @openai/codex@latest' not in codex_cmd

# Fixed umbrella-repo layout and local-only config.
for token in [
    'fcd02/Chaturbate_Projects',
    'projects\\ctbrec-mobile-reviewer',
    'projects\\ctbrec-mobile-reviewer\\app',
    'CTBRecGitBridge',
    'config.json',
]:
    assert token in setup, token

# Public-source filtering must explicitly protect the state most dangerous to
# accidentally commit or overwrite.
for token in [
    'mobile_config.json',
    'recording_roots.txt',
    'mobile_recu_session.json',
    'mobile_action_queue.json',
    'mobile_catalog_cache.json',
    'mobile_ready_work_index.json',
    'mobile_library_mosaic_state.json',
    'mobile_hidden_models.json',
    'recu_browser_profile',
    'models',
    'credential',
    'cookie',
]:
    assert token in setup.lower(), token
    assert token in apply.lower(), token

# Future update contract: validate -> PR/version -> live stop/backup/apply/test/
# restart/health-check, with rollback support.
for token in [
    'Run-FullValidation',
    'gh pr create',
    'gh pr merge',
    'git -C $repo tag',
    'Stop-Reviewer',
    'Backup-RuntimeFiles',
    'Restore-RuntimeFiles',
    'mobile_self_test.py',
    'Start-Reviewer',
    'Wait-Reviewer',
    '/api/auth',
    'UPDATE FAILED',
]:
    assert token in apply, token

assert 'runtime deployment is skipped so live code never outruns Git main' in apply
assert 'Patch contains protected/private/runtime paths and was rejected' in apply
assert 'all configured' in apply
assert 'drag it onto `Apply-ChatGPT-Update.cmd`' in readme

# Crude but useful packaging-time PowerShell sanity check: here-string markers
# must be paired, and the major function declarations must have bodies.
assert setup.count("@'") == setup.count("'@")
assert apply.count('@"') == apply.count('"@')
for func in [
    'Test-ProtectedRelativePath', 'Stop-Reviewer', 'Start-Reviewer',
    'Wait-Reviewer', 'Copy-Payload', 'Backup-RuntimeFiles',
    'Restore-RuntimeFiles', 'Run-FullValidation', 'Run-RuntimeSelfTest',
]:
    assert re.search(rf'function\s+{re.escape(func)}\b[^{{]*{{', apply), func

print('PASS: v2.15.10 Git Bridge — guarded public bootstrap, validated PR/tag publishing, live stop/deploy/restart/rollback, Codex npm.cmd helper')
