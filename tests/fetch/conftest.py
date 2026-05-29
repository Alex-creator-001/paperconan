# tests/fetch/conftest.py
import json
import os
import pytest

_FX = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(_FX, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixture():
    return load_fixture


def make_fake_get_json(routes):
    """routes: list of (url_substring, fixture_object). First match wins."""
    def _fake(url, params=None, headers=None, timeout=15):
        for sub, obj in routes:
            if sub in url:
                return obj
        raise AssertionError(f"no fake route for GET {url}")
    return _fake


def make_fake_post_json(routes):
    def _fake(url, payload, headers=None, timeout=15):
        for sub, obj in routes:
            if sub in url:
                return obj
        raise AssertionError(f"no fake route for POST {url}")
    return _fake


@pytest.fixture
def fake_http():
    return {"get": make_fake_get_json, "post": make_fake_post_json}
