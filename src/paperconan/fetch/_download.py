"""Defensive file download: redirects (urllib default), timeout, size cap,
content-type sniffing so an HTML error page is never saved as data."""
from __future__ import annotations
import hashlib
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from ._files import asset_type

# Provenance sidecar written next to downloads; read back by scan_dir to stamp scan.json.
SOURCE_SIDECAR = "paperconan_source.json"

_UA = "paperconan-fetch/0.6 (+https://github.com/zixixr/paperconan)"
_DEFAULT_MAX = 50 * 1024 * 1024     # 50 MB — per individual file / per extracted table
# A supplementary archive bundles ALL supplementary material (often 100MB+ of video/
# imaging) but we only keep its small tabular members, so it needs a much larger cap
# than a single file — otherwise big-but-tabular Europe PMC zips truncate and are lost.
_ARCHIVE_MAX = 250 * 1024 * 1024    # 250 MB — whole supplementary zip
# Per-PAPER total cap: a genomics supplement can hold hundreds of tabular files that extract
# to many GB and fill the worker disk before audit cleans up. Stop extracting/downloading once
# a paper's out_dir reaches this. Default 1.5 GB; raise PAPERCONAN_MAX_PAPER_MB on big disks.
_MAX_PAPER_MB = float(os.environ.get("PAPERCONAN_MAX_PAPER_MB", "1500"))
_MAX_PAPER_BYTES = int(_MAX_PAPER_MB * 1024 * 1024)


def _dir_size(path):
    total = 0
    for dp, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def download_file(url, dest_path, timeout=180, max_bytes=_DEFAULT_MAX,
                  retries=3, backoff=2.0):
    """Download to disk with redirects, size cap, HTML sniffing, and retry/backoff.
    Streams the body in chunks (no whole-file buffering). Retries on timeout and
    HTTP 5xx; auth errors (401/403) and size/HTML rejections are terminal."""
    if not url.lower().startswith(("https://", "http://")):
        return {"ok": False, "path": dest_path,
                "skipped_reason": f"unsupported URL scheme: {url!r}"}
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last_reason = "unknown error"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = (resp.info().get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    return {"ok": False, "path": dest_path,
                            "skipped_reason": f"server returned HTML ({ctype}), not a data file"}
                clen = resp.info().get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    return {"ok": False, "path": dest_path,
                            "skipped_reason": f"file exceeds max_bytes ({max_bytes})"}
                os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
                total = 0
                with open(dest_path, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            fh.close()
                            try:
                                os.remove(dest_path)
                            except OSError:
                                pass
                            return {"ok": False, "path": dest_path,
                                    "skipped_reason": f"file exceeds max_bytes ({max_bytes})"}
                        fh.write(chunk)
                return {
                    "ok": True,
                    "path": dest_path,
                    "size": total,
                    "content_type": ctype.split(";", 1)[0].strip(),
                    "source_url": url,
                }
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"ok": False, "path": dest_path,
                        "skipped_reason": (f"requires authentication (HTTP {e.code}); "
                                           "download this file manually from the dataset page")}
            last_reason = f"HTTP {e.code}: {e.reason}"
            if not (500 <= e.code < 600):
                return {"ok": False, "path": dest_path, "skipped_reason": last_reason}
        except Exception as e:
            last_reason = f"download error: {e}"
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    return {"ok": False, "path": dest_path, "skipped_reason": last_reason}


def _write_collision_safe(out_dir: str, name: str, data: bytes) -> str:
    stem, suffix = os.path.splitext(os.path.basename(name))
    candidate = os.path.join(out_dir, stem + suffix)
    if os.path.exists(candidate):
        with open(candidate, "rb") as fh:
            if fh.read() == data:
                return candidate
        digest = hashlib.sha256(data).hexdigest()[:10]
        candidate = os.path.join(out_dir, f"{stem}-{digest}{suffix}")
    with open(candidate, "wb") as fh:
        fh.write(data)
    return candidate


def _extract_selected_zip(
    zip_bytes,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
):
    extracted = []
    written = _dir_size(out_dir)
    allowed = {"tabular"}
    if include_images:
        allowed.update({"image", "document"})
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if written >= _MAX_PAPER_BYTES:
                break
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if (
                not name
                or asset_type(name) not in allowed
                or info.file_size > max_member_bytes
                or written + info.file_size > _MAX_PAPER_BYTES
            ):
                continue
            with zf.open(info) as src:
                data = src.read(max_member_bytes + 1)
                if len(data) > max_member_bytes:
                    continue
            dest = _write_collision_safe(out_dir, name, data)
            written += len(data)
            extracted.append(dest)
    return extracted


def _extract_selected_tar(
    tar_path,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
):
    extracted = []
    written = _dir_size(out_dir)
    allowed = {"tabular"}
    if include_images:
        allowed.update({"image", "document"})
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if written >= _MAX_PAPER_BYTES:
                break
            if not member.isfile():
                continue
            name = os.path.basename(member.name)
            if (
                not name
                or asset_type(name) not in allowed
                or member.size > max_member_bytes
                or written + member.size > _MAX_PAPER_BYTES
            ):
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            data = src.read(max_member_bytes + 1)
            if len(data) > max_member_bytes:
                continue
            dest = _write_collision_safe(out_dir, name, data)
            written += len(data)
            extracted.append(dest)
    return extracted


def _extract_tabular_tar(tar_path, out_dir, max_member_bytes=_DEFAULT_MAX):
    """Backward-compatible tabular-only tar extraction."""
    return _extract_selected_tar(
        tar_path,
        out_dir,
        max_member_bytes=max_member_bytes,
    )


def _download_oa_package(
    pkg,
    out_dir,
    downloaded,
    skipped,
    max_bytes,
    *,
    include_images=False,
):
    """Download the static PMC OA tar.gz, extract selected members, drop the tarball."""
    tmp = os.path.join(out_dir, pkg.get("name") or "oa_package.tar.gz")
    res = download_file(pkg["url"], tmp, max_bytes=_ARCHIVE_MAX)
    if not res.get("ok"):
        skipped.append({"name": pkg.get("name"), "reason": res.get("skipped_reason")})
        return []
    try:
        extracted = _extract_selected_tar(
            tmp,
            out_dir,
            include_images=include_images,
            max_member_bytes=max_bytes,
        )
        downloaded.extend(extracted)
        return extracted
    except (tarfile.TarError, OSError) as e:
        skipped.append({"name": pkg.get("name"), "reason": f"bad tar.gz: {e}"})
        return []
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _download_supplementary_archive(
    arch,
    out_dir,
    downloaded,
    skipped,
    max_bytes,
    archive_max=_ARCHIVE_MAX,
    *,
    include_images=False,
):
    """Fetch a supplementary zip, extract selected members, and drop the zip.

    The archive downloads with the larger ``archive_max`` cap; each extracted member is
    still capped at the per-file ``max_bytes``."""
    tmp_zip = os.path.join(out_dir, arch.get("name") or "supplementary.zip")
    res = download_file(arch["url"], tmp_zip, max_bytes=archive_max)
    if not res.get("ok"):
        skipped.append({"name": arch.get("name"), "reason": res.get("skipped_reason")})
        return []
    try:
        with open(tmp_zip, "rb") as fh:
            extracted = _extract_selected_zip(
                fh.read(),
                out_dir,
                include_images=include_images,
                max_member_bytes=max_bytes,
            )
        downloaded.extend(extracted)
        return extracted
    except zipfile.BadZipFile:
        skipped.append({"name": arch.get("name"), "reason": "not a valid zip archive"})
        return []
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass


def _safe_source_url(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _write_source_sidecar(cand, out_dir, downloads=None):
    """Record which paper/dataset these downloads came from, for scan.json provenance."""
    prov = {"doi": cand.get("doi"), "title": cand.get("title"),
            "source": cand.get("source"), "cand_id": cand.get("cand_id"),
            "related_dois": cand.get("related_dois") or []}
    prov["downloads"] = sorted(downloads or [], key=lambda x: x["file"])
    try:
        with open(os.path.join(out_dir, SOURCE_SIDECAR), "w", encoding="utf-8") as fh:
            json.dump(prov, fh, indent=2, default=str)
    except OSError:
        pass  # provenance is best-effort; never fail a download over it


def _selected_files(cand, *, tabular_only: bool, include_images: bool) -> list[dict]:
    if not tabular_only:
        return list(cand.get("all_files") or cand.get("tabular_files") or [])
    selected = list(cand.get("tabular_files") or [])
    if include_images:
        selected.extend(cand.get("image_files") or [])
        selected.extend(
            f for f in cand.get("all_files") or []
            if asset_type(f.get("name") or "") == "document"
        )
    out, seen = [], set()
    for ref in selected:
        key = (ref.get("download_url"), ref.get("name"))
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def _provenance_entry(path, source_url, content_type=None, size=None):
    return {
        "file": os.path.basename(path),
        "source_url": _safe_source_url(source_url),
        "content_type": content_type,
        "asset_type": asset_type(os.path.basename(path)),
        "size": size,
    }


def download_candidate(
    cand,
    out_dir,
    tabular_only=True,
    max_bytes=_DEFAULT_MAX,
    archive_max=_ARCHIVE_MAX,
    include_images=False,
):
    files = _selected_files(
        cand,
        tabular_only=tabular_only,
        include_images=include_images,
    )
    os.makedirs(out_dir, exist_ok=True)
    downloaded, skipped = [], []
    provenance_files = []
    direct_asset_types = set()
    for f in files:
        if _dir_size(out_dir) > _MAX_PAPER_BYTES:   # per-paper budget reached; stop downloading
            skipped.append({"name": f["name"], "reason": "paper data exceeds per-paper cap"})
            continue
        dest = os.path.join(out_dir, os.path.basename(f["name"]))
        res = download_file(f["download_url"], dest, max_bytes=max_bytes)
        if res.get("ok"):
            downloaded.append(res["path"])
            direct_asset_types.add(asset_type(f.get("name") or ""))
            provenance_files.append(_provenance_entry(
                res["path"],
                res.get("source_url") or f.get("download_url"),
                content_type=res.get("content_type"),
                size=res.get("size"),
            ))
        else:
            skipped.append({"name": f["name"], "reason": res.get("skipped_reason")})
    pkg = cand.get("oa_package")
    if pkg and pkg.get("url"):
        extracted = _download_oa_package(
            pkg,
            out_dir,
            downloaded,
            skipped,
            max_bytes,
            include_images=include_images,
        )
        provenance_files.extend(
            _provenance_entry(path, pkg.get("url"), size=os.path.getsize(path))
            for path in extracted
        )
    arch = cand.get("supplementary_archive")
    needs_archive = not downloaded
    if include_images:
        needs_archive = needs_archive or bool(
            {"tabular", "image", "document"} - direct_asset_types
        )
    if needs_archive and arch and arch.get("url"):
        extracted = _download_supplementary_archive(
            arch,
            out_dir,
            downloaded,
            skipped,
            max_bytes,
            archive_max=archive_max,
            include_images=include_images,
        )
        provenance_files.extend(
            _provenance_entry(path, arch.get("url"), size=os.path.getsize(path))
            for path in extracted
        )
    downloaded = list(dict.fromkeys(downloaded))
    by_file = {}
    for entry in provenance_files:
        by_file.setdefault(entry["file"], entry)
    _write_source_sidecar(cand, out_dir, downloads=list(by_file.values()))
    return {"cand_id": cand.get("cand_id"), "out_dir": out_dir,
            "downloaded": downloaded, "skipped": skipped}
