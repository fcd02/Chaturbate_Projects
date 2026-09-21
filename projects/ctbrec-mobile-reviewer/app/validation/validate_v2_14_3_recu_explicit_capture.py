#!/usr/bin/env python3
"""v2.14.3 regression: explicit capture + exact field-proven challenge semantics."""
from __future__ import annotations
import importlib.util, json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

s = load('ctbrec_server_v2143_test', ROOT/'ctbrec_mobile_server.py')

# The field screenshot showed a normal accessible Recu homepage with Sign In UI.
# Normal pages may also contain generic Cloudflare helper/script strings. Those
# must NOT be treated as a human-verification challenge.
normal_public_html = '''
<html><head><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head>
<body>
<div>Cloudflare Ray ID helper footer</div>
<a href="/login">Sign In</a>
<form style="display:none"><input name="password"></form>
<h1>The Biggest Chaturbate Archive</h1>
<div>Most Bookmarked Recordings</div>
</body></html>
'''
assert not s.RecuBrowserBridge._looks_like_challenge(normal_public_html), 'generic CF script/footer caused false challenge'
assert not s.RecuBrowserBridge._looks_like_login(normal_public_html, 'https://recu.me/'), 'Sign In UI caused false login'
assert s.RecuBrowserBridge._has_normal_recu_shell(normal_public_html)
assert s.RecuBrowserBridge._looks_like_challenge('<html><div id="cf-chl-widget">Just a moment</div></html>')
assert s.RecuBrowserBridge._looks_like_login('', 'https://recu.me/login')

class DummyWs:
    def close(self): pass
class FakeWebsocket:
    @staticmethod
    def create_connection(*args, **kwargs): return DummyWs()
s.websocket = FakeWebsocket

tmp = Path(tempfile.mkdtemp(prefix='recu2143-'))
s.RECU_SESSION_PATH = tmp/'mobile_recu_session.json'

class Mosaic:
    def reset_recu_http_sessions(self): self.reset = True
class State:
    def __init__(self):
        self.config={'recu': {'base_url':'https://recu.me'}}
        self.mosaic_settings={}
        self.mosaic=Mosaic()
        self.applied=[]
    def apply_recu_session(self, payload):
        self.applied.append(dict(payload))
        if payload.get('cookie'): self.mosaic_settings['recu_cookie']=payload['cookie']
        if payload.get('user_agent'): self.mosaic_settings['recu_user_agent']=payload['user_agent']

state=State(); bridge=s.RecuBrowserBridge(state)
bridge.is_running=lambda: True
bridge._browser_ws_url=lambda: 'ws://browser'
# Capture must never inspect or navigate a page. If it does, this test fails.
bridge._page_target=lambda: (_ for _ in ()).throw(AssertionError('capture must not inspect page DOM'))
bridge.fetch_html_navigation=lambda *a, **k: (_ for _ in ()).throw(AssertionError('capture must not navigate'))
bridge.fetch_html_cookie_http=lambda *a, **k: (_ for _ in ()).throw(AssertionError('capture must not probe HTTP'))

def call_ws(_ws, method, params=None):
    if method=='Browser.getVersion': return {'userAgent':'Mozilla/5.0 Chrome/152.0','product':'Chrome/152'}
    if method=='Storage.getCookies': return {'cookies':[{'domain':'.recu.me','name':'cf_clearance','value':'abc'},{'domain':'recu.me','name':'session','value':'xyz'}]}
    raise AssertionError(method)
bridge._call_ws=call_ws

result=bridge.capture_session()
assert result['captured'] is True and result['verified'] is True
assert result['preferred_transport']=='cookie_http'
saved=json.loads(s.RECU_SESSION_PATH.read_text(encoding='utf-8'))
assert saved['schema_version']==2143
assert saved['validation_transport']=='explicit_user_confirmation'
assert saved['preferred_transport']=='cookie_http'
assert 'cf_clearance=abc' in saved['cookie'] and 'session=xyz' in saved['cookie']
assert state.applied

# No-cookie capture is still allowed: the verified browser itself remains a
# valid fallback rather than trapping the user in another capture loop.
state2=State(); bridge2=s.RecuBrowserBridge(state2)
bridge2.is_running=lambda: True
bridge2._browser_ws_url=lambda: 'ws://browser'
def call_ws2(_ws, method, params=None):
    if method=='Browser.getVersion': return {'userAgent':'Mozilla/5.0 Chrome/152.0','product':'Chrome/152'}
    if method=='Storage.getCookies': return {'cookies':[]}
    raise AssertionError(method)
bridge2._call_ws=call_ws2
result2=bridge2.capture_session()
assert result2['captured'] and result2['verified'] and result2['preferred_transport']=='navigation'

# Valid public empty shell is not an auth failure solely because it has no tiles.
assert bridge._validate_recu_html('https://recu.me/performer/x/kinks/y', normal_public_html, expect_listing=True)==normal_public_html

text=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
assert 'RECU_CHALLENGE_TOKENS' in text
assert 'explicit user confirmation' in text
assert 'no DOM/login heuristic or navigation was run during capture' in text
print('PASS v2.14.3 explicit Recu capture/challenge regression suite')
