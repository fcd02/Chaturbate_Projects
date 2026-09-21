#!/usr/bin/env python3
"""v2.14.2/v2.14.3 compatibility regression: capture must not re-navigate; proven cookie HTTP is primary."""
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
s = load('ctbrec_server_v21423_test', ROOT/'ctbrec_mobile_server.py')

class DummyWs:
    def close(self): pass
class FakeWebsocket:
    @staticmethod
    def create_connection(*args, **kwargs): return DummyWs()
s.websocket = FakeWebsocket

tmp = Path(tempfile.mkdtemp(prefix='recu21423-'))
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
bridge._page_target=lambda: {'webSocketDebuggerUrl':'ws://page','url':'https://recu.me/performer/x'}

def call_ws(_ws, method, params=None):
    if method=='Browser.getVersion': return {'userAgent':'Mozilla/5.0 Chrome/152.0','product':'Chrome/152'}
    if method=='Storage.getCookies': return {'cookies':[{'domain':'.recu.me','name':'cf_clearance','value':'abc'},{'domain':'recu.me','name':'session','value':'xyz'}]}
    raise AssertionError(method)
bridge._call_ws=call_ws

auth_html='<html><a href="/account/signout">Sign Out</a><form><input name="password"></form><div class="header-main__logo">The Ultimate Chaturbate Archive</div></html>'
bridge._call_target=lambda _ws, method, params=None, timeout=30: {'result':{'value':json.dumps({'html':auth_html,'href':'https://recu.me/performer/x'})}} if method=='Runtime.evaluate' else {}
bridge.fetch_html_navigation=lambda *a, **k: (_ for _ in ()).throw(AssertionError('capture must not navigate'))
bridge.fetch_html_cookie_http=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic probe failure'))
result=bridge.capture_session()
assert result['captured'] is True
assert result['preferred_transport']=='cookie_http'
assert result['verified'] is True, result
saved=json.loads(s.RECU_SESSION_PATH.read_text(encoding='utf-8'))
assert saved['schema_version']==2143
assert saved['preferred_transport']=='cookie_http'
assert 'cf_clearance=abc' in saved['cookie'] and 'session=xyz' in saved['cookie']
assert state.applied, 'captured session must be applied even if probe fails'

# Authenticated shell must beat dormant password/login markup.
assert bridge._validate_recu_html('https://recu.me/x', auth_html)==auth_html

# Old v2.14.1 session preferences migrate back to proven cookie_http.
class PrefBrowser:
    def __init__(self): self.value=None
    def _set_transport(self, name, reason): self.value=(name,reason)
class FakeApply:
    mosaic_settings={}
    recu_browser=PrefBrowser()
    mosaic=Mosaic()
old={'cookie':'a=b','user_agent':'ua','preferred_transport':'navigation','transport_reason':'old'}
s.MobileReviewerState.apply_recu_session(FakeApply(), old)
assert FakeApply.recu_browser.value if False else True
# instantiate properly so we can inspect
fa=FakeApply(); s.MobileReviewerState.apply_recu_session(fa, old)
assert fa.recu_browser.value[0]=='cookie_http', fa.recu_browser.value

text=(ROOT/'ctbrec_mobile_server.py').read_text(encoding='utf-8')
assert 'persistent Cookie/User-Agent HTTP' in text
assert 'does **not** inspect the DOM' in text or 'no DOM/login heuristic or navigation was run during capture' in text
assert 'Persistent-cookie Recu fetch looked auth-related; confirming only as fallback' in text
print('PASS v2.14.2 compatibility + v2.14.3 capture regression suite')
