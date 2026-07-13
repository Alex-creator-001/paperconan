from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading

import pytest

from paperconan.fetch import _http


class _StubResponse(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        final_url: str = "https://api.example.org/result",
        headers=None,
    ):
        super().__init__(body)
        self.final_url = final_url
        self.headers = headers or {}
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self):
        return self.final_url

    def info(self):
        return self.headers

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class _TextResponse(io.BytesIO):
    def __init__(self, body: bytes, final_url: str):
        super().__init__(body)
        self.final_url = final_url
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self):
        return self.final_url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _StubOpener:
    def __init__(self, response):
        self.response = response

    def open(self, req, timeout=None):
        return self.response


@contextmanager
def _serve(routes):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, headers, body = routes[self.path]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_get_json_builds_query_and_parses(monkeypatch):
    seen = {}

    def stub_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _StubResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(_http, "_open_http", stub_urlopen)
    out = _http.get_json("https://api.example.org/x", params={"q": "a b", "size": 3})
    assert out == {"ok": True}
    assert seen["url"].startswith("https://api.example.org/x?")
    assert "q=a+b" in seen["url"] and "size=3" in seen["url"]
    assert seen["headers"].get("accept") == "application/json"


def test_post_json_sends_body(monkeypatch):
    seen = {}

    def stub_urlopen(req, timeout=None):
        seen["data"] = req.data
        seen["method"] = req.get_method()
        return _StubResponse(json.dumps([{"id": 1}]).encode())

    monkeypatch.setattr(_http, "_open_http", stub_urlopen)
    out = _http.post_json("https://api.example.org/search", {"search_for": "x"})
    assert out == [{"id": 1}]
    assert seen["method"] == "POST"
    assert json.loads(seen["data"]) == {"search_for": "x"}


def _call_json_helper(method):
    if method == "GET":
        return _http.get_json("https://api.example.org/data")
    return _http.post_json("https://api.example.org/data", {"query": "x"})


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_json_helpers_reject_declared_body_above_fixed_ceiling(
    monkeypatch,
    method,
):
    response = _StubResponse(
        b"{}",
        headers={"Content-Length": "6"},
    )

    def stub_open(req, timeout=None):
        return response

    monkeypatch.setattr(
        _http,
        "_JSON_RESPONSE_MAX_BYTES",
        5,
        raising=False,
    )
    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_open)
    monkeypatch.setattr(_http, "_open_http", stub_open, raising=False)

    with pytest.raises(ValueError) as exc:
        _call_json_helper(method)

    assert str(exc.value) == "JSON response exceeds byte limit"
    assert response.read_sizes == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_json_helpers_bound_actual_body_read(monkeypatch, method):
    response = _StubResponse(b'{"x":1}')

    def stub_open(req, timeout=None):
        return response

    monkeypatch.setattr(
        _http,
        "_JSON_RESPONSE_MAX_BYTES",
        5,
        raising=False,
    )
    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_open)
    monkeypatch.setattr(_http, "_open_http", stub_open, raising=False)

    with pytest.raises(ValueError) as exc:
        _call_json_helper(method)

    assert str(exc.value) == "JSON response exceeds byte limit"
    assert response.read_sizes == [6]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.example.org/data",
        "https://user:secret@api.example.org/data",
        "https:///missing-host",
        "https://api.example.org:not-a-port/data",
        "https://[2001:db8::1/data",
    ],
)
def test_get_json_rejects_invalid_initial_url_before_open(monkeypatch, url):
    def reject_open(*args, **kwargs):
        raise AssertionError("invalid initial URL must not be opened")

    monkeypatch.setattr(_http.urllib.request, "urlopen", reject_open)
    monkeypatch.setattr(_http, "_open_http", reject_open, raising=False)

    with pytest.raises(ValueError) as exc:
        _http.get_json(url)

    assert str(exc.value) == "HTTP request URL is invalid"


@pytest.mark.parametrize(
    "final_url",
    [
        "ftp://cdn.example.org/data",
        "https://user:secret@cdn.example.org/data",
        "https:///missing-host",
        "https://cdn.example.org:not-a-port/data",
        "https://[2001:db8::1/data",
    ],
)
def test_get_json_rejects_invalid_final_url(monkeypatch, final_url):
    response = _StubResponse(b"{}", final_url=final_url)

    def stub_open(req, timeout=None):
        return response

    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_open)
    monkeypatch.setattr(_http, "_open_http", stub_open, raising=False)

    with pytest.raises(ValueError) as exc:
        _http.get_json("https://api.example.org/data")

    assert str(exc.value) == "HTTP response URL is invalid"
    assert response.read_sizes == []


@pytest.mark.parametrize(
    "target",
    [
        "ftp://cdn.example.org/data",
        "https://user:secret@cdn.example.org/data",
        "https:///missing-host",
        "https://cdn.example.org:not-a-port/data",
        "https://[2001:db8::1/data",
    ],
)
def test_http_redirect_handler_rejects_invalid_target(target):
    handler = _http._ValidatedHTTPRedirectHandler()
    request = _http.urllib.request.Request(
        "https://repository.example.org/data",
    )

    with pytest.raises(ValueError) as exc:
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            target,
        )

    assert str(exc.value) == "HTTP redirect URL is invalid"


def test_http_redirect_handler_allows_https_cdn_host():
    handler = _http._ValidatedHTTPRedirectHandler()
    request = _http.urllib.request.Request(
        "https://repository.example.org/data",
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://cdn.example.net/files/data.csv",
    )

    assert redirected.full_url == "https://cdn.example.net/files/data.csv"


def test_get_text_bounded_reader_rejects_oversized_body(monkeypatch):
    response = _TextResponse(
        b"abcdef",
        "https://www.nature.com/articles/sample",
    )
    monkeypatch.setattr(
        _http.urllib.request,
        "urlopen",
        lambda req, timeout=None: response,
    )

    with pytest.raises(ValueError) as exc:
        _http.get_text(
            "https://www.nature.com/articles/sample",
            max_bytes=5,
        )

    assert str(exc.value) == "text response exceeds byte limit"
    assert response.read_sizes == [6]


def test_get_text_allowed_origins_checks_redirect_destination(monkeypatch):
    response = _TextResponse(
        b"<html></html>",
        "https://external.example/articles/sample",
    )
    monkeypatch.setattr(
        _http.urllib.request,
        "urlopen",
        lambda req, timeout=None: response,
    )
    monkeypatch.setattr(
        _http.urllib.request,
        "build_opener",
        lambda *handlers: _StubOpener(response),
    )

    with pytest.raises(ValueError) as exc:
        _http.get_text(
            "https://www.nature.com/articles/sample",
            max_bytes=1024,
            allowed_origins={"https://www.nature.com"},
        )

    assert str(exc.value) == "text response origin is not allowed"


@pytest.mark.parametrize(
    "target_url",
    [
        lambda target: target.replace("127.0.0.1", "localhost") + "/sink",
        lambda target: target + "/sink",
        lambda target: target.replace("http://", "http://user@") + "/sink",
    ],
    ids=["disallowed-host", "unexpected-port", "credentials"],
)
def test_get_text_rejects_redirect_before_disallowed_target_contact(target_url):
    with _serve({"/sink": (200, {}, b"unexpected")}) as (
        target,
        target_requests,
    ):
        location = target_url(target)
        with _serve({
            "/start": (302, {"Location": location}, b""),
        }) as (source, source_requests):
            with pytest.raises(ValueError) as exc:
                _http.get_text(
                    source + "/start",
                    max_bytes=1024,
                    allowed_origins={source},
                )

    assert str(exc.value) == "text response origin is not allowed"
    assert source_requests == ["/start"]
    assert target_requests == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:not-a-port/sink",
        "http://[::1/sink",
    ],
    ids=["invalid-port", "invalid-bracket"],
)
def test_get_text_rejects_malformed_redirect_authority_with_fixed_message(
    location,
):
    with _serve({
        "/start": (
            302,
            {"Location": location},
            b"",
        ),
    }) as (source, source_requests):
        with pytest.raises(ValueError) as exc:
            _http.get_text(
                source + "/start",
                max_bytes=1024,
                allowed_origins={source},
            )

    assert str(exc.value) == "text response origin is not allowed"
    assert source_requests == ["/start"]


def test_get_text_allows_relative_redirect_within_allowed_origin():
    with _serve({
        "/start": (302, {"Location": "/final"}, b""),
        "/final": (200, {}, b"<html>ok</html>"),
    }) as (source, requests):
        text = _http.get_text(
            source + "/start",
            max_bytes=1024,
            allowed_origins={source},
        )

    assert text == "<html>ok</html>"
    assert requests == ["/start", "/final"]


def test_get_text_generic_call_remains_backward_compatible(monkeypatch):
    monkeypatch.setattr(
        _http.urllib.request,
        "urlopen",
        lambda req, timeout=None: _StubResponse(b"<html>ok</html>"),
    )

    assert _http.get_text("https://example.org/article") == "<html>ok</html>"
