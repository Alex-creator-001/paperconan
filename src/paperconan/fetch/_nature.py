"""nature.com / Springer ESM source: a paper's own article page links its
supplementary / Source Data files on the open static-content.springer.com CDN —
reachable for both OA and paywalled articles without a login."""
from __future__ import annotations

import re
import urllib.parse

from . import _http
from ._files import make_fileref
from ._sources import _candidate

_ESM_HREF = re.compile(
    r'href="(https://static-content\.springer\.com/esm/[^"]+)"', re.I)
_FIGURE_HREF = re.compile(r'href="([^"]+/figures/\d+)"', re.I)
_FULL_IMAGE_SRC = re.compile(
    r'(https://media\.springernature\.com/full/[^"\']+\.(?:png|jpe?g|tiff?))',
    re.I,
)


def parse_nature_esm_links(html: str) -> list[dict]:
    """Extract ESM file refs from a Nature article page. Returns make_fileref dicts,
    deduped by URL, with ext derived from the URL path."""
    seen, refs = set(), []
    for url in _ESM_HREF.findall(html or ""):
        url = url.replace("&amp;", "&")
        if url in seen:
            continue
        seen.add(url)
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        refs.append(make_fileref(name, None, url))
    return refs


def parse_nature_figure_links(html: str, article_url: str) -> list[str]:
    return sorted({
        urllib.parse.urljoin(article_url, href.replace("&amp;", "&"))
        for href in _FIGURE_HREF.findall(html or "")
    })


def parse_nature_full_image(html: str) -> dict | None:
    match = _FULL_IMAGE_SRC.search(html or "")
    if not match:
        return None
    url = match.group(1).replace("&amp;", "&")
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    return make_fileref(name, None, url)


def search_nature_esm(query, size=5):
    """If `query` is a DOI, fetch its nature.com page and return one candidate
    carrying its ESM files. Non-DOI queries return [] (this source is DOI-keyed)."""
    doi = str(query).strip()
    if not doi.startswith("10.1038/"):
        return []
    suffix = doi[len("10.1038/"):]
    url = f"https://www.nature.com/articles/{suffix}"
    try:
        html = _http.get_text(url, timeout=60)
    except Exception:
        return []
    all_files = parse_nature_esm_links(html)
    for figure_url in parse_nature_figure_links(html, url):
        try:
            ref = parse_nature_full_image(_http.get_text(figure_url, timeout=60))
        except Exception:
            ref = None
        if ref is not None:
            all_files.append(ref)
    if not all_files:
        return []
    c = _candidate("nature_esm", suffix, doi, None, [], None, all_files, [doi])
    c["match_signals"] = {"doi_in_related": True}
    return [c]
