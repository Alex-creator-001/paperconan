import io
import json
import pytest
from paperconan.fetch import _http


class _StubResponse(io.BytesIO):
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


def test_get_json_builds_query_and_parses(monkeypatch):
    seen = {}

    def stub_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _StubResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_urlopen)
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

    monkeypatch.setattr(_http.urllib.request, "urlopen", stub_urlopen)
    out = _http.post_json("https://api.example.org/search", {"search_for": "x"})
    assert out == [{"id": 1}]
    assert seen["method"] == "POST"
    assert json.loads(seen["data"]) == {"search_for": "x"}


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

    with pytest.raises(ValueError) as exc:
        _http.get_text(
            "https://www.nature.com/articles/sample",
            max_bytes=1024,
            allowed_origins={"https://www.nature.com"},
        )

    assert str(exc.value) == "text response origin is not allowed"


def test_get_text_generic_call_remains_backward_compatible(monkeypatch):
    monkeypatch.setattr(
        _http.urllib.request,
        "urlopen",
        lambda req, timeout=None: _StubResponse(b"<html>ok</html>"),
    )

    assert _http.get_text("https://example.org/article") == "<html>ok</html>"
