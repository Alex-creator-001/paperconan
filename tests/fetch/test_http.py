from contextlib import contextmanager
import email.message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import urllib.response

import pytest

from paperconan.fetch import _download, _http


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


class _UnreadableRedirectBody(io.BytesIO):
    def __init__(self, *, close_error=False):
        super().__init__(b"x" * (2 * 1024 * 1024))
        self.close_error = close_error
        self.read_sizes = []
        self.was_closed = False

    def read(self, size=-1):
        self.read_sizes.append(size)
        raise AssertionError("redirect response body must not be read")

    def close(self):
        self.was_closed = True
        if self.close_error:
            self.close_error = False
            raise OSError("redirect response close failed")
        super().close()


class _RedirectTransport(_http.urllib.request.BaseHandler):
    handler_order = 100

    def __init__(
        self,
        final_body,
        final_content_type,
        location="//cdn.example.net/final",
        redirect_close_error=False,
    ):
        self.final_body = final_body
        self.final_content_type = final_content_type
        self.location = location
        self.redirect_body = _UnreadableRedirectBody(
            close_error=redirect_close_error,
        )
        self.redirect_response = None
        self.requests = []

    def https_open(self, req):
        self.requests.append(req.full_url)
        headers = email.message.Message()
        if len(self.requests) == 1:
            headers["Location"] = self.location
            headers["Content-Length"] = str(2 * 1024 * 1024)
            response = urllib.response.addinfourl(
                self.redirect_body,
                headers,
                req.full_url,
                302,
            )
            response.msg = "Found"
            self.redirect_response = response
            return response
        assert self.redirect_body.was_closed
        headers["Content-Type"] = self.final_content_type
        response = urllib.response.addinfourl(
            io.BytesIO(self.final_body),
            headers,
            req.full_url,
            200,
        )
        response.msg = "OK"
        return response


def _install_redirect_transport(
    monkeypatch,
    *,
    final_body,
    final_content_type,
    location="//cdn.example.net/final",
    redirect_close_error=False,
):
    transport = _RedirectTransport(
        final_body,
        final_content_type,
        location=location,
        redirect_close_error=redirect_close_error,
    )
    real_build_opener = _http.urllib.request.build_opener

    def build_opener(*handlers):
        return real_build_opener(transport, *handlers)

    monkeypatch.setattr(
        _http.urllib.request,
        "build_opener",
        build_opener,
    )
    return transport


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

    monkeypatch.setattr(_http, "open_http", stub_urlopen)
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

    monkeypatch.setattr(_http, "open_http", stub_urlopen)
    out = _http.post_json("https://api.example.org/search", {"search_for": "x"})
    assert out == [{"id": 1}]
    assert seen["method"] == "POST"
    assert json.loads(seen["data"]) == {"search_for": "x"}


def _call_json_helper(method):
    if method == "GET":
        return _http.get_json("https://api.example.org/data")
    return _http.post_json("https://api.example.org/data", {"query": "x"})


def test_public_url_policy_helpers_expose_typed_failures():
    assert (
        _http.validate_http_url("https://repository.example.org/data")
        == "https://repository.example.org/data"
    )
    assert _http.resolve_http_url(
        "https://repository.example.org/data",
        "//cdn.example.net/files/data.csv",
    ) == "https://cdn.example.net/files/data.csv"
    assert callable(_http.open_http)

    with pytest.raises(_http.URLPolicyError):
        _http.validate_http_url("ftp://repository.example.org/data")


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
    monkeypatch.setattr(_http, "open_http", stub_open)

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
    monkeypatch.setattr(_http, "open_http", stub_open)

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
    monkeypatch.setattr(_http, "open_http", reject_open)

    with pytest.raises(_http.URLPolicyError) as exc:
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
        "//user:secret@cdn.example.org/data",
        "//cdn.example.org:not-a-port/data",
        "//",
    ],
)
def test_get_json_rejects_invalid_final_url(monkeypatch, final_url):
    response = _StubResponse(b"{}", final_url=final_url)

    def stub_open(req, timeout=None):
        return response

    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_open)
    monkeypatch.setattr(_http, "open_http", stub_open)

    with pytest.raises(_http.URLPolicyError) as exc:
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
    handler = _http.ValidatedHTTPRedirectHandler()
    request = _http.urllib.request.Request(
        "https://repository.example.org/data",
    )

    with pytest.raises(_http.URLPolicyError) as exc:
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
    handler = _http.ValidatedHTTPRedirectHandler()
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


def test_http_redirect_handler_allows_scheme_relative_https_cdn_host():
    handler = _http.ValidatedHTTPRedirectHandler()
    request = _http.urllib.request.Request(
        "https://repository.example.org/data",
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "//cdn.example.net/files/data.csv",
    )

    assert redirected.full_url == "https://cdn.example.net/files/data.csv"


@pytest.mark.parametrize("limit", ["repeat", "total"])
def test_http_redirect_handler_preserves_redirect_limits(limit):
    class Parent:
        def __init__(self):
            self.open_calls = []

        def open(self, req, timeout=None):
            self.open_calls.append(req.full_url)
            raise AssertionError("redirect limit must stop before opening")

    handler = _http.ValidatedHTTPRedirectHandler()
    handler.parent = Parent()
    request = _http.urllib.request.Request(
        "https://repository.example.org/start",
    )
    target = "https://cdn.example.net/final"
    if limit == "repeat":
        request.redirect_dict = {target: handler.max_repeats}
    else:
        request.redirect_dict = {
            f"https://cdn.example.net/{index}": 1
            for index in range(handler.max_redirections)
        }
    headers = email.message.Message()
    headers["Location"] = target
    redirect_body = _UnreadableRedirectBody()

    with pytest.raises(_http.urllib.error.HTTPError) as exc:
        handler.http_error_302(
            request,
            redirect_body,
            302,
            "Found",
            headers,
        )

    assert "infinite loop" in str(exc.value).lower()
    assert handler.parent.open_calls == []
    assert redirect_body.read_sizes == []


@pytest.mark.parametrize("client", ["json", "download"])
def test_redirect_following_does_not_read_large_30x_body(
    monkeypatch,
    tmp_path,
    client,
):
    if client == "json":
        final_body = b'{"ok":true}'
        final_content_type = "application/json"
    else:
        final_body = b"a,b\n1,2\n"
        final_content_type = "text/csv"
    transport = _install_redirect_transport(
        monkeypatch,
        final_body=final_body,
        final_content_type=final_content_type,
    )

    if client == "json":
        assert _http.get_json("https://repository.example.org/start") == {
            "ok": True,
        }
    else:
        destination = tmp_path / "data.csv"
        result = _download.download_file(
            "https://repository.example.org/start",
            str(destination),
            retries=1,
        )
        assert result["ok"] is True
        assert destination.read_bytes() == final_body

    assert transport.requests == [
        "https://repository.example.org/start",
        "https://cdn.example.net/final",
    ]
    assert transport.redirect_body.read_sizes == []
    assert transport.redirect_body.was_closed


def test_download_invalid_redirect_is_terminal_with_default_retries(
    monkeypatch,
    tmp_path,
):
    sleep_calls = []
    transport = _install_redirect_transport(
        monkeypatch,
        final_body=b"unexpected",
        final_content_type="text/csv",
        location="ftp://cdn.example.net/final",
    )
    monkeypatch.setattr(
        _download.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    result = _download.download_file(
        "https://repository.example.org/start",
        str(tmp_path / "data.csv"),
    )

    assert result["ok"] is False
    assert result["skipped_reason"] == (
        "download URL rejected by HTTP(S) policy"
    )
    assert transport.requests == [
        "https://repository.example.org/start",
    ]
    assert transport.redirect_body.read_sizes == []
    assert transport.redirect_body.was_closed
    assert sleep_calls == []


def test_download_invalid_redirect_preserves_policy_error_when_close_fails(
    monkeypatch,
    tmp_path,
):
    sleep_calls = []
    transport = _install_redirect_transport(
        monkeypatch,
        final_body=b"unexpected",
        final_content_type="text/csv",
        location="ftp://cdn.example.net/final",
        redirect_close_error=True,
    )
    monkeypatch.setattr(
        _download.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    result = _download.download_file(
        "https://repository.example.org/start",
        str(tmp_path / "data.csv"),
    )

    assert result["ok"] is False
    assert result["skipped_reason"] == (
        "download URL rejected by HTTP(S) policy"
    )
    assert transport.requests == [
        "https://repository.example.org/start",
    ]
    assert transport.redirect_body.read_sizes == []
    assert transport.redirect_body.was_closed
    assert sleep_calls == []
    transport.redirect_response.close()


def test_redirect_normalizes_non_ascii_location_before_following(monkeypatch):
    transport = _install_redirect_transport(
        monkeypatch,
        final_body=b'{"ok":true}',
        final_content_type="application/json",
        location="/caf\xe9 data.csv",
    )

    assert _http.get_json("https://repository.example.org/start") == {
        "ok": True,
    }
    assert transport.requests == [
        "https://repository.example.org/start",
        "https://repository.example.org/caf%E9%20data.csv",
    ]
    assert transport.redirect_body.read_sizes == []
    assert transport.redirect_body.was_closed


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/data\x7f.csv",
        "https://example.org/data?token=\x7f",
        "https://example.org/data#\x7f",
        "https://exa\x7fmple.org/data",
        "https://example.org../data",
    ],
)
def test_http_url_validation_rejects_del_and_multiple_trailing_dots(url):
    assert _http.is_valid_http_url(url) is False


def test_http_url_validation_allows_one_trailing_hostname_dot():
    assert _http.is_valid_http_url("https://example.org./data") is True


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
