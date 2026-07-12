"""Thin stdlib HTTP helpers returning parsed JSON. No third-party deps."""
from __future__ import annotations
import json
import urllib.parse
import urllib.request

_UA = "paperconan-fetch/0.6 (+https://github.com/zixixr/paperconan)"


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    if (
        not parts.scheme
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return (scheme, parts.hostname.lower(), port)


def _allowed_origin_keys(allowed_origins):
    if allowed_origins is None:
        return None
    values = (
        [allowed_origins]
        if isinstance(allowed_origins, str)
        else allowed_origins
    )
    keys = {_origin(value) for value in values}
    if None in keys:
        raise ValueError("text response origin configuration is invalid")
    return keys


def _require_allowed_origin(url, allowed_origin_keys):
    if allowed_origin_keys is not None and _origin(url) not in allowed_origin_keys:
        raise ValueError("text response origin is not allowed")


def get_json(url, params=None, headers=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    h = {"Accept": "application/json", "User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def get_text(
    url,
    params=None,
    headers=None,
    timeout=30,
    *,
    max_bytes=None,
    allowed_origins=None,
):
    """GET a text resource (HTML/XML) and return the decoded body as str."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    if max_bytes is not None and (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        raise ValueError("text response byte limit must be a non-negative integer")
    allowed_origin_keys = _allowed_origin_keys(allowed_origins)
    _require_allowed_origin(url, allowed_origin_keys)
    h = {"Accept": "text/html,application/xml;q=0.9,*/*;q=0.8", "User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if allowed_origin_keys is not None:
            geturl = getattr(resp, "geturl", None)
            if not callable(geturl):
                raise ValueError("text response origin is not allowed")
            _require_allowed_origin(geturl(), allowed_origin_keys)
        body = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
        if max_bytes is not None and len(body) > max_bytes:
            raise ValueError("text response exceeds byte limit")
        return body.decode("utf-8", "replace")


def post_json(url, payload, headers=None, timeout=15):
    body = json.dumps(payload).encode("utf-8")
    h = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))
