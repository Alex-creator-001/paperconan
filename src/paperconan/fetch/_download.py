"""Defensive file download: redirects (urllib default), timeout, size cap,
content-type sniffing so an HTML error page is never saved as data."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import io
import json
import os
import secrets
import stat
import tarfile
import tempfile
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


class _UnstableRegularFileError(OSError):
    pass


class _PinnedOutputDirectory:
    def __init__(self, path: str, fd: int):
        self.path = os.path.abspath(path)
        self.fd = fd
        self._opened = os.fstat(fd)

    def verify(self) -> None:
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(
                "fetch output directory changed during publication"
            ) from exc
        if (
            not stat.S_ISDIR(self._opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or self._opened.st_dev != current.st_dev
            or self._opened.st_ino != current.st_ino
        ):
            raise ValueError("fetch output directory changed during publication")


@contextmanager
def _pinned_output_directory(path: str):
    absolute = os.path.abspath(path)
    os.makedirs(absolute, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ValueError("secure fetch output publication is unavailable")
    try:
        fd = os.open(absolute, os.O_RDONLY | directory | nofollow)
    except OSError as exc:
        raise ValueError(
            "fetch output directory is not a stable no-follow directory"
        ) from exc
    try:
        output = _PinnedOutputDirectory(absolute, fd)
        output.verify()
        yield output
    finally:
        os.close(fd)


def _output_path(output: str | _PinnedOutputDirectory) -> str:
    return output.path if isinstance(output, _PinnedOutputDirectory) else output


@contextmanager
def _open_stable_regular(path: str):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _UnstableRegularFileError("no-follow file opening is unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise _UnstableRegularFileError(f"cannot open without following links: {exc}") from exc
    try:
        opened = os.fstat(fd)
        try:
            current = os.lstat(path)
        except OSError as exc:
            raise _UnstableRegularFileError(f"path entry is unavailable: {exc}") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise _UnstableRegularFileError("opened file does not match the current path entry")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            yield fh
    finally:
        if fd >= 0:
            os.close(fd)


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
        fd = -1
        temp_path = None
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
                dest_dir = os.path.realpath(
                    os.path.dirname(os.path.abspath(dest_path)) or "."
                )
                os.makedirs(dest_dir, exist_ok=True)
                resolved_dest = os.path.join(dest_dir, os.path.basename(dest_path))
                fd, temp_path = tempfile.mkstemp(
                    prefix=".paperconan-download-body-",
                    dir=dest_dir,
                )
                total = 0
                with os.fdopen(fd, "wb") as fh:
                    fd = -1
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            return {"ok": False, "path": dest_path,
                                    "skipped_reason": f"file exceeds max_bytes ({max_bytes})"}
                        fh.write(chunk)
                os.replace(temp_path, resolved_dest)
                temp_path = None
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
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    return {"ok": False, "path": dest_path, "skipped_reason": last_reason}


def _write_collision_safe(
    out_dir: str | _PinnedOutputDirectory,
    name: str,
    data: bytes,
) -> str:
    if not isinstance(out_dir, _PinnedOutputDirectory):
        with _pinned_output_directory(out_dir) as output:
            return _write_collision_safe(output, name, data)

    def regular_file_matches(filename: str) -> bool:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return False
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | nofollow,
                dir_fd=out_dir.fd,
            )
        except OSError:
            return False
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                return False
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                matches = fh.read() == data
            if not matches:
                return False
            try:
                current = os.stat(
                    filename,
                    dir_fd=out_dir.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            return (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == opened.st_dev
                and current.st_ino == opened.st_ino
            )
        finally:
            if fd >= 0:
                os.close(fd)

    out_dir.verify()
    stem, suffix = os.path.splitext(os.path.basename(name))
    digest = hashlib.sha256(data).hexdigest()[:10]
    temp_name = None
    temp_fd = -1
    try:
        for _ in range(128):
            temp_name = f".paperconan-publish-{secrets.token_hex(8)}"
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=out_dir.fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("could not allocate fetch publication staging file")
        with os.fdopen(temp_fd, "wb") as fh:
            temp_fd = -1
            fh.write(data)
        collision_index = 0
        while True:
            if collision_index == 0:
                filename = stem + suffix
            elif collision_index == 1:
                filename = f"{stem}-{digest}{suffix}"
            else:
                filename = f"{stem}-{digest}-{collision_index}{suffix}"
            try:
                os.link(
                    temp_name,
                    filename,
                    src_dir_fd=out_dir.fd,
                    dst_dir_fd=out_dir.fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if regular_file_matches(filename):
                    out_dir.verify()
                    return os.path.join(out_dir.path, filename)
                collision_index += 1
                continue
            try:
                out_dir.verify()
            except Exception:
                os.unlink(filename, dir_fd=out_dir.fd)
                raise
            return os.path.join(out_dir.path, filename)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=out_dir.fd)
            except FileNotFoundError:
                pass


def _archive_staging_path(
    out_dir: str | _PinnedOutputDirectory,
    suffix: str,
) -> str:
    resolved_out = _output_path(out_dir)
    os.makedirs(resolved_out, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix=".paperconan-archive-",
        suffix=suffix,
        dir=resolved_out,
    )
    os.close(fd)
    return path


def _extract_selected_zip(
    zip_bytes,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
):
    extracted = []
    written = _dir_size(_output_path(out_dir))
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
    tar_source,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
):
    extracted = []
    written = _dir_size(_output_path(out_dir))
    allowed = {"tabular"}
    if include_images:
        allowed.update({"image", "document"})
    if hasattr(tar_source, "read"):
        archive = tarfile.open(fileobj=tar_source, mode="r:gz")
    else:
        archive = tarfile.open(tar_source, "r:gz")
    with archive as tf:
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
    tmp = _archive_staging_path(out_dir, ".tar.gz")
    try:
        res = download_file(pkg["url"], tmp, max_bytes=_ARCHIVE_MAX)
        if not res.get("ok"):
            skipped.append({"name": pkg.get("name"), "reason": res.get("skipped_reason")})
            return []
        try:
            with _open_stable_regular(tmp) as archive_fh:
                extracted = _extract_selected_tar(
                    archive_fh,
                    out_dir,
                    include_images=include_images,
                    max_member_bytes=max_bytes,
                )
        except _UnstableRegularFileError as e:
            skipped.append({
                "name": pkg.get("name"),
                "reason": f"downloaded archive is not a stable regular file: {e}",
            })
            return []
        downloaded.extend(extracted)
        return extracted
    except (tarfile.TarError, OSError) as e:
        skipped.append({"name": pkg.get("name"), "reason": f"bad tar.gz: {e}"})
        return []
    finally:
        try:
            os.remove(tmp)
        except (OSError, ValueError):
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
    tmp_zip = _archive_staging_path(out_dir, ".zip")
    try:
        res = download_file(arch["url"], tmp_zip, max_bytes=archive_max)
        if not res.get("ok"):
            skipped.append({"name": arch.get("name"), "reason": res.get("skipped_reason")})
            return []
        try:
            with _open_stable_regular(tmp_zip) as archive_fh:
                extracted = _extract_selected_zip(
                    archive_fh.read(),
                    out_dir,
                    include_images=include_images,
                    max_member_bytes=max_bytes,
                )
        except _UnstableRegularFileError as e:
            skipped.append({
                "name": arch.get("name"),
                "reason": f"downloaded archive is not a stable regular file: {e}",
            })
            return []
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
    if not isinstance(out_dir, _PinnedOutputDirectory):
        try:
            with _pinned_output_directory(out_dir) as output:
                _write_source_sidecar(cand, output, downloads=downloads)
        except OSError:
            pass
        return
    temp_name = None
    temp_fd = -1
    try:
        out_dir.verify()
        try:
            current = os.stat(
                SOURCE_SIDECAR,
                dir_fd=out_dir.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            return
        temp_name = f".paperconan-sidecar-{secrets.token_hex(8)}"
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=out_dir.fd,
        )
        with os.fdopen(temp_fd, "w", encoding="utf-8") as fh:
            temp_fd = -1
            json.dump(prov, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        out_dir.verify()
        os.replace(
            temp_name,
            SOURCE_SIDECAR,
            src_dir_fd=out_dir.fd,
            dst_dir_fd=out_dir.fd,
        )
        temp_name = None
        out_dir.verify()
    except (OSError, ValueError):
        pass  # provenance is best-effort; never fail a download over it
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=out_dir.fd)
            except FileNotFoundError:
                pass


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
    with _pinned_output_directory(out_dir) as output:
        downloaded, skipped = [], []
        provenance_files = []
        direct_asset_types = set()
        for f in files:
            if _dir_size(output.path) > _MAX_PAPER_BYTES:
                skipped.append({
                    "name": f["name"],
                    "reason": "paper data exceeds per-paper cap",
                })
                continue
            suffix = os.path.splitext(os.path.basename(f["name"]))[1]
            fd, temp_path = tempfile.mkstemp(
                prefix=".paperconan-download-",
                suffix=suffix,
                dir=output.path,
            )
            os.close(fd)
            try:
                res = download_file(f["download_url"], temp_path, max_bytes=max_bytes)
                if res.get("ok"):
                    try:
                        with _open_stable_regular(temp_path) as fh:
                            data = fh.read()
                    except _UnstableRegularFileError as e:
                        skipped.append({
                            "name": f["name"],
                            "reason": (
                                "downloaded file is not a stable regular file: "
                                f"{e}"
                            ),
                        })
                        continue
                    try:
                        published = _write_collision_safe(output, f["name"], data)
                    except (OSError, ValueError) as exc:
                        skipped.append({
                            "name": f["name"],
                            "reason": f"secure publication failed: {exc}",
                        })
                        continue
                    downloaded.append(published)
                    direct_asset_types.add(asset_type(f.get("name") or ""))
                    provenance_files.append(_provenance_entry(
                        published,
                        res.get("source_url") or f.get("download_url"),
                        content_type=res.get("content_type"),
                        size=res.get("size"),
                    ))
                else:
                    skipped.append({
                        "name": f["name"],
                        "reason": res.get("skipped_reason"),
                    })
            finally:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass
        pkg = cand.get("oa_package")
        if pkg and pkg.get("url"):
            extracted = _download_oa_package(
                pkg,
                output,
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
                output,
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
        _write_source_sidecar(cand, output, downloads=list(by_file.values()))
        return {
            "cand_id": cand.get("cand_id"),
            "out_dir": output.path,
            "downloaded": downloaded,
            "skipped": skipped,
        }
