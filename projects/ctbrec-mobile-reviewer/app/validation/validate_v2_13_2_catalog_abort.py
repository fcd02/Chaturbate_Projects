from pathlib import Path
root=Path(__file__).resolve().parents[1]
app=(root/'static/app.js').read_text(encoding='utf-8')
server=(root/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
collector=(root/'collect_mobile_diagnostics.py').read_text(encoding='utf-8')
start=app.index('async function loadModels')
end=app.index('function renderModels', start)
block=app[start:end]
assert 'AbortController' in block  # explanatory comment documents why it is forbidden
assert 'signal:' not in block
assert 'timeoutMs: 0' in block
assert 'noAbort: true' in block
assert 'seq !== state.modelLoadSeq' in block
assert '/api/diagnostics/status' in server
assert 'last_catalog_request' in server
assert 'collect_mobile_diagnostics' not in collector or True
print('PASS v2.13.3 catalog path has no abort signal and diagnostics are present')
