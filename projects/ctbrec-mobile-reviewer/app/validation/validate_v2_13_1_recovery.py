from pathlib import Path
import re
root=Path(__file__).resolve().parents[1]
app=(root/'static/app.js').read_text(encoding='utf-8')
server=(root/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
sw=(root/'static/service-worker.js').read_text(encoding='utf-8')
html=(root/'static/index.html').read_text(encoding='utf-8')
assert 'protocol_version = "HTTP/1.0"' in server
assert 'gzip.compress' not in server
assert 'Content-Encoding' not in server
assert 'headers.Authorization' not in app and 'Authorization = ' not in app
assert '/api/auth/recover' in app and '/api/auth/recover' in server
assert 'Model list will continue independently' in app
assert 'Model list temporarily unavailable — retrying automatically' in app
assert 'state.modelRetryTimer' in app
assert 'ctbrec-shell-v21510' in sw
assert 'app.js?v=21510' in html and 'rapid.js?v=21510' in html
assert 'server_version = "CTBRecMobile/2.15.10"' in server
assert 'timeoutMs: 0' in app and 'noAbort: true' in app
assert '/api/diagnostics/status' in server
print('PASS current static transport/recovery invariants')
