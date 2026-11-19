"""app.fetch_image_bytes / download_urls_for_sku: browser-impersonation retry
and loud (never silent) download failures. All network is mocked.

image.haier.com resets every python-requests TLS handshake but answers
curl_cffi impersonate='chrome' with HTTP 200, so a ConnectionError or a
403/429 gets ONE curl_cffi retry; any other failure is recorded in the row's
_image_flags as 'image download failed: N of M'.
"""
import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")

from PIL import Image                      # noqa: E402
from curl_cffi import requests as creq     # noqa: E402

import app                                 # noqa: E402


def _png(size=(1200, 1200)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


class Resp:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise app.requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def net(monkeypatch, tmp_path):
    """Record calls to both clients; tests set the behaviour per URL."""
    calls = {"requests": [], "curl": []}
    behaviour = {"requests": {}, "curl": {}}

    def fake_requests_get(url, **kw):
        calls["requests"].append(url)
        b = behaviour["requests"].get(url, Resp(_png()))
        if isinstance(b, Exception):
            raise b
        return b

    def fake_curl_get(url, **kw):
        calls["curl"].append((url, kw))
        b = behaviour["curl"].get(url, Resp(_png()))
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(app.requests, "get", fake_requests_get)
    monkeypatch.setattr(creq, "get", fake_curl_get)
    monkeypatch.setattr(app, "IMAGE_DIR", tmp_path)
    warnings = []
    ui = type("UI", (), {"warning": staticmethod(warnings.append)})()
    monkeypatch.setattr(app, "_ui", lambda: ui)
    return calls, behaviour, warnings, tmp_path


def test_fallback_on_connection_error(net):
    calls, behaviour, _, _ = net
    url = "https://image.haier.com/i/a.png"
    behaviour["requests"][url] = app.requests.ConnectionError("reset by peer")
    data = app.fetch_image_bytes(url)
    assert data == _png()
    assert calls["requests"] == [url]
    assert len(calls["curl"]) == 1
    _, kw = calls["curl"][0]
    assert kw["impersonate"] == "chrome"
    assert kw["timeout"] == 30
    assert kw["headers"]["Accept"].startswith("image/avif,image/webp")


@pytest.mark.parametrize("code", [403, 429])
def test_fallback_on_refusal_status(net, code):
    calls, behaviour, _, _ = net
    url = "https://cdn.example.com/i/b.png"
    behaviour["requests"][url] = Resp(b"denied", code)
    assert app.fetch_image_bytes(url) == _png()
    assert len(calls["curl"]) == 1


def test_no_fallback_on_404(net):
    calls, behaviour, _, _ = net
    url = "https://cdn.example.com/i/gone.png"
    behaviour["requests"][url] = Resp(b"", 404)
    with pytest.raises(app.requests.HTTPError):
        app.fetch_image_bytes(url)
    assert calls["curl"] == []


def test_fallback_failure_raises(net):
    _, behaviour, _, _ = net
    url = "https://image.haier.com/i/c.png"
    behaviour["requests"][url] = app.requests.ConnectionError("reset")
    behaviour["curl"][url] = Resp(b"", 403)
    with pytest.raises(app.requests.HTTPError):
        app.fetch_image_bytes(url)


def test_success_path_unchanged(net):
    calls, _, warnings, tmp = net
    urls = [f"https://cdn.example.com/i/{k}.png" for k in range(3)]
    flags = []
    saved = app.download_urls_for_sku("AP-OK", urls, flags)
    assert saved == [1, 2, 3]
    assert calls["curl"] == []            # plain requests was enough
    assert flags == []
    assert warnings == []
    for n in saved:
        with Image.open(tmp / f"AP-OK_{n}.webp") as im:
            assert im.size == (app.CANVAS_EDGE, app.CANVAS_EDGE)


def test_failure_is_flagged_not_silent(net):
    _, behaviour, warnings, _ = net
    urls = [f"https://cdn.example.com/i/{k}.png" for k in range(3)]
    behaviour["requests"][urls[1]] = Resp(b"", 404)
    flags = []
    saved = app.download_urls_for_sku("AP-BAD", urls, flags)
    assert saved == [1, 2]
    assert flags == ["image download failed: 1 of 3"]
    assert len(warnings) == 1
    assert "AP-BAD" in warnings[0] and "1 of 3" in warnings[0]


def test_resolution_reject_is_not_a_download_failure(net):
    _, behaviour, warnings, _ = net
    urls = [f"https://cdn.example.com/i/{k}.png" for k in range(2)]
    behaviour["requests"][urls[0]] = Resp(_png((300, 300)))
    flags = []
    saved = app.download_urls_for_sku("AP-SMALL", urls, flags)
    assert saved == [1]
    assert flags == []                    # existing behaviour: warning only
    assert len(warnings) == 1 and "below" in warnings[0]


def test_haier_reset_recovered_by_fallback_in_download(net):
    calls, behaviour, warnings, _ = net
    urls = ["https://image.haier.com/i/1.png", "https://image.haier.com/i/2.png"]
    for u in urls:
        behaviour["requests"][u] = app.requests.ConnectionError("reset")
    flags = []
    saved = app.download_urls_for_sku("AP-HAIER", urls, flags)
    assert saved == [1, 2]
    assert flags == [] and warnings == []
    assert len(calls["curl"]) == 2
