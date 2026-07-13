"""Thin stdlib HTTP helpers returning parsed JSON. No third-party deps."""
from __future__ import annotations
import ipaddress
import json
import re
import urllib.parse
import urllib.request

_UA = "paperconan-fetch/0.6 (+https://github.com/zixixr/paperconan)"
_JSON_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _valid_hostname(hostname):
    if (
        not hostname
        or any(character.isspace() or ord(character) < 32 for character in hostname)
    ):
        return False
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return False
        return True
    try:
        ipaddress.IPv4Address(hostname)
    except ValueError:
        pass
    else:
        return True
    if hostname.replace(".", "").isdigit():
        return False
    try:
        ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return False
    if not ascii_hostname or len(ascii_hostname) > 253:
        return False
    return all(
        _HOST_LABEL.fullmatch(label) is not None
        for label in ascii_hostname.split(".")
    )


def _is_valid_http_url(url):
    if (
        not isinstance(url, str)
        or not url
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return False
    if (
        scheme not in {"http", "https"}
        or not parts.netloc
        or username is not None
        or password is not None
        or not _valid_hostname(hostname)
        or parts.netloc.endswith(":")
    ):
        return False
    return port is None or 0 <= port <= 65535


def _require_valid_http_url(url, message):
    if not _is_valid_http_url(url):
        raise ValueError(message)
    return url


class _ValidatedHTTPRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            raw_parts = urllib.parse.urlsplit(newurl)
            if raw_parts.scheme or raw_parts.netloc:
                _require_valid_http_url(
                    newurl,
                    "HTTP redirect URL is invalid",
                )
            target = urllib.parse.urljoin(req.full_url, newurl)
        except (TypeError, ValueError):
            raise ValueError("HTTP redirect URL is invalid") from None
        _require_valid_http_url(target, "HTTP redirect URL is invalid")
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            target,
        )

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            try:
                raw_parts = urllib.parse.urlsplit(location)
                if raw_parts.scheme or raw_parts.netloc:
                    _require_valid_http_url(
                        location,
                        "HTTP redirect URL is invalid",
                    )
                target = urllib.parse.urljoin(req.full_url, location)
            except (TypeError, ValueError):
                raise ValueError("HTTP redirect URL is invalid") from None
            _require_valid_http_url(target, "HTTP redirect URL is invalid")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = (
        http_error_302
    )


def _open_http(req, timeout):
    opener = urllib.request.build_opener(_ValidatedHTTPRedirectHandler())
    return opener.open(req, timeout=timeout)


def _require_valid_response_url(resp):
    geturl = getattr(resp, "geturl", None)
    if not callable(geturl):
        raise ValueError("HTTP response URL is invalid")
    try:
        final_url = geturl()
    except (TypeError, ValueError):
        raise ValueError("HTTP response URL is invalid") from None
    return _require_valid_http_url(final_url, "HTTP response URL is invalid")


def _read_json_response(resp):
    info = getattr(resp, "info", None)
    headers = info() if callable(info) else getattr(resp, "headers", {})
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is not None:
        try:
            declared_length = int(str(content_length).strip())
        except ValueError:
            declared_length = None
        if (
            declared_length is not None
            and declared_length > _JSON_RESPONSE_MAX_BYTES
        ):
            raise ValueError("JSON response exceeds byte limit")
    body = resp.read(_JSON_RESPONSE_MAX_BYTES + 1)
    if len(body) > _JSON_RESPONSE_MAX_BYTES:
        raise ValueError("JSON response exceeds byte limit")
    return json.loads(body.decode("utf-8", "replace"))


def _origin(url):
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        scheme not in {"http", "https"}
        or not hostname
        or username is not None
        or password is not None
    ):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, hostname.lower(), port)


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


class _AllowedOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_origin_keys):
        super().__init__()
        self.allowed_origin_keys = allowed_origin_keys

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _require_allowed_origin(target, self.allowed_origin_keys)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            target,
        )

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            try:
                target = urllib.parse.urljoin(req.full_url, location)
            except ValueError:
                raise ValueError("text response origin is not allowed") from None
            _require_allowed_origin(target, self.allowed_origin_keys)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = (
        http_error_302
    )


def get_json(url, params=None, headers=None, timeout=15):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    _require_valid_http_url(url, "HTTP request URL is invalid")
    h = {"Accept": "application/json", "User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with _open_http(req, timeout=timeout) as resp:
        _require_valid_response_url(resp)
        return _read_json_response(resp)


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
    if allowed_origin_keys is None:
        response = urllib.request.urlopen(req, timeout=timeout)
    else:
        opener = urllib.request.build_opener(
            _AllowedOriginRedirectHandler(allowed_origin_keys)
        )
        response = opener.open(req, timeout=timeout)
    with response as resp:
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
    _require_valid_http_url(url, "HTTP request URL is invalid")
    body = json.dumps(payload).encode("utf-8")
    h = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": _UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with _open_http(req, timeout=timeout) as resp:
        _require_valid_response_url(resp)
        return _read_json_response(resp)
