#!/usr/bin/env python3
"""v2.14.1 regression checks for Recu verification/transport separation."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

s = load("ctbrec_server_v2141_test", ROOT / "ctbrec_mobile_server.py")

# Real saved Recu pages contain an authenticated shell even when a result set can
# legitimately be empty. That must NOT be treated as expired authentication.
auth_empty = '''
<html><body>
<div class="header-main__logo">The Ultimate Chaturbate Archive</div>
<a id="top-signin-signout-button" href="https://recu.me/account/signout">Sign Out</a>
<div id="dropdown-user-menu">My Account</div>
</body></html>
'''
dummy = type("Dummy", (), {"config": {"recu": {}}, "mosaic_settings": {}})()
bridge = s.RecuBrowserBridge(dummy)
assert bridge._validate_recu_html(
    "https://recu.me/performer/x/kinks/cumshot", auth_empty, expect_listing=True
) == auth_empty

# An unauthenticated/unknown empty shell remains auth-suspicious.
try:
    bridge._validate_recu_html(
        "https://recu.me/performer/x/kinks/cumshot", "<html>normal shell only</html>", expect_listing=True
    )
except s.RecuReauthRequired:
    pass
else:
    raise AssertionError("Unknown empty listing must remain auth-suspicious")

listing = auth_empty.replace("</body>", '<div class="video-thumb"><a href="/video/123/play">x</a></div></body>')

# The exact v2.14.0 field failure: experimental CDP says auth is bad, but the
# already-verified Chrome navigation works. The scanner must demote the transport
# and return the page instead of surfacing "verification needed".
bridge._preferred_transport = "cdp"
def bad_cdp(*args, **kwargs):
    raise s.RecuReauthRequired("synthetic CDP challenge")
def bad_http(*args, **kwargs):
    raise RuntimeError("synthetic copied-cookie rejection")
def good_nav(*args, **kwargs):
    return listing
bridge.fetch_html_background = bad_cdp
bridge.fetch_html_cookie_http = bad_http
bridge.fetch_html_navigation = good_nav
out = bridge.fetch_html("https://recu.me/performer/x/kinks/cumshot", expect_listing=True)
assert out == listing
assert bridge.transport_status()["preferred_transport"] == "navigation"

# Same rule for the 12-way listing batch. A single fast-transport auth-looking
# failure cannot skip the reliability fallback.
bridge._preferred_transport = "cdp"
def bad_many_cdp(*args, **kwargs):
    raise s.RecuReauthRequired("synthetic batch CDP challenge")
def bad_many_http(*args, **kwargs):
    raise RuntimeError("synthetic batch HTTP rejection")
bridge.fetch_many_background = bad_many_cdp
bridge.fetch_many_cookie_http = bad_many_http
bridge.fetch_html_navigation = good_nav
urls = [f"https://recu.me/performer/x/kinks/k{i}" for i in range(3)]
out_many = bridge.fetch_many(urls, expect_listing=True, max_workers=12)
assert set(out_many) == set(urls)
assert all(value == listing for value in out_many.values())
assert bridge.transport_status()["preferred_transport"] == "navigation"

# Conversely, if the authoritative browser navigation ALSO reports challenge/
# login, then and only then should re-authentication propagate.
bridge._preferred_transport = "cdp"
bridge.fetch_html_background = bad_cdp
bridge.fetch_html_cookie_http = bad_http
def bad_nav(*args, **kwargs):
    raise s.RecuReauthRequired("synthetic verified-navigation challenge")
bridge.fetch_html_navigation = bad_nav
try:
    bridge.fetch_html("https://recu.me/performer/x/kinks/cumshot", expect_listing=True)
except s.RecuReauthRequired as exc:
    assert "verified-navigation" in str(exc)
else:
    raise AssertionError("Authoritative navigation auth failure must request reauth")


# If Chrome was closed after capture and copied-cookie HTTP is rejected, the
# state-level bridge must launch/confirm the persisted browser profile before
# ever converting that rejection into another verification prompt.
class FakeMosaic:
    @staticmethod
    def canonicalize_recu_url(url, base):
        return url

class FakeBrowser:
    def is_running(self):
        return False
    def fetch_html_cookie_http(self, *args, **kwargs):
        raise RuntimeError("HTTP 403 synthetic copied-cookie rejection")

class FakeState:
    mosaic = FakeMosaic()
    mosaic_settings = {"recu_base_url": "https://recu.me"}
    recu_browser = FakeBrowser()
    _recu_error_looks_auth_related = staticmethod(s.MobileReviewerState._recu_error_looks_auth_related)
    def _original_recu_fetch(self, *args, **kwargs):
        raise RuntimeError("HTTP 403 synthetic copied-cookie rejection")
    def _ensure_recu_browser_navigation(self, url, timeout):
        self.browser_confirmed = (url, timeout)
        return listing

fake_state = FakeState()
out_closed = s.MobileReviewerState._recu_fetch_bridge(
    fake_state, "https://recu.me/performer/x/kinks/cumshot",
    {"recu_base_url": "https://recu.me", "recu_request_timeout_seconds": 20},
    __import__("threading").Event(), 1,
)
assert out_closed == listing
assert getattr(fake_state, "browser_confirmed", None), "raw HTTP auth rejection must be browser-confirmed"

text = (ROOT / "ctbrec_mobile_server.py").read_text(encoding="utf-8")
assert "preferred_transport" in text and "cookie_http" in text and "navigation" in text
assert "proven pre-v2.14 HTTP method" in text
assert "Persistent-cookie Recu fetch looked auth-related; confirming only as fallback" in text
assert "Recu authentication remained valid" in text
print("PASS v2.14.1 Recu auth/transport resilience regression suite")
