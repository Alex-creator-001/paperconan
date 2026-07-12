"""Defensive file download: redirects (urllib default), timeout, size cap,
content-type sniffing so an HTML error page is never saved as data."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
import re
import secrets
import stat
import struct
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
_MAX_PUBLISHED_FILES_PER_CANDIDATE = 1000
_MAX_ARCHIVE_MEMBERS_PER_CANDIDATE = 1000
_MAX_RAW_ZIP_ENTRIES_PER_ARCHIVE = 4096
_MAX_SOURCE_SIDECAR_BYTES = 8 * 1024 * 1024
_FILE_COPY_CHUNK_BYTES = 64 * 1024
_URL_IN_ERROR = re.compile(r"https?://[^\s]+")
_ZIP_EOCD = struct.Struct("<4s4H2IH")
_ZIP64_LOCATOR = struct.Struct("<4sIQI")
_ZIP64_EOCD = struct.Struct("<4sQ2H2I4Q")
_ZIP_CENTRAL_FILE_HEADER = struct.Struct("<4s6H3I5H2I")
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_ZIP_MAX_COMMENT_BYTES = 0xFFFF
_ZIP16_SENTINEL = 0xFFFF
_ZIP32_SENTINEL = 0xFFFFFFFF


class _UnstableRegularFileError(OSError):
    pass


class _SourceSidecarLimitError(ValueError):
    pass


@dataclass(frozen=True)
class _PublishedOutputFile:
    filename: str
    size: int
    identity: tuple[int, int]
    sha256: str
    created: bool

    def display_path(self, output: _PinnedOutputDirectory) -> str:
        return os.path.join(output.path, self.filename)


@dataclass
class _CandidateCardinality:
    max_published_files: int
    max_archive_members: int
    published_files: int = 0
    archive_members: int = 0

    def can_publish(self) -> bool:
        return self.published_files < self.max_published_files

    def record_publication(self) -> None:
        self.published_files += 1

    def claim_archive_member(self) -> bool:
        if self.archive_members >= self.max_archive_members:
            return False
        self.archive_members += 1
        return True


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


@dataclass
class _SidecarPublication:
    output: _PinnedOutputDirectory
    backup_name: str | None
    active: bool = True

    def commit(self) -> None:
        if not self.active:
            return
        if self.backup_name is not None:
            os.unlink(self.backup_name, dir_fd=self.output.fd)
            self.backup_name = None
        self.active = False

    def rollback(self) -> None:
        if not self.active:
            return
        if self.backup_name is None:
            try:
                os.unlink(SOURCE_SIDECAR, dir_fd=self.output.fd)
            except FileNotFoundError:
                pass
        else:
            os.replace(
                self.backup_name,
                SOURCE_SIDECAR,
                src_dir_fd=self.output.fd,
                dst_dir_fd=self.output.fd,
            )
            self.backup_name = None
        self.active = False


class _DownloadStagingFile:
    def __init__(
        self,
        output: _PinnedOutputDirectory,
        name: str,
        fd: int,
    ):
        self.output = output
        self.name = name
        self.fd = fd

    @property
    def display_path(self) -> str:
        return os.path.join(self.output.path, self.name)

    def __fspath__(self) -> str:
        if os.path.isdir("/dev/fd"):
            return f"/dev/fd/{self.fd}"
        return f"/proc/self/fd/{self.fd}"


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


def _verify_staging_file(staging: _DownloadStagingFile) -> None:
    opened = os.fstat(staging.fd)
    try:
        current = os.stat(
            staging.name,
            dir_fd=staging.output.fd,
            follow_symlinks=False,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise _UnstableRegularFileError(
            "download staging entry is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
    ):
        raise _UnstableRegularFileError(
            "download staging entry is not a stable regular file"
        )


def _download_staging_file(
    output: _PinnedOutputDirectory,
    *,
    prefix: str,
    suffix: str,
) -> _DownloadStagingFile:
    output.verify()
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(8)}{suffix}"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=output.fd)
        except FileExistsError:
            continue
        staging = _DownloadStagingFile(output, name, fd)
        try:
            _verify_staging_file(staging)
            output.verify()
            return staging
        except Exception:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=output.fd)
            except FileNotFoundError:
                pass
            raise
    raise FileExistsError("could not allocate fetch download staging file")


def _cleanup_download_staging(
    staging: _DownloadStagingFile | None,
) -> None:
    if staging is None:
        return
    try:
        os.unlink(staging.name, dir_fd=staging.output.fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(staging.fd)


@contextmanager
def _open_download_staging(staging: _DownloadStagingFile):
    try:
        staging.output.verify()
        _verify_staging_file(staging)
    except ValueError as exc:
        raise _UnstableRegularFileError(str(exc)) from exc
    with os.fdopen(os.dup(staging.fd), "rb") as fh:
        fh.seek(0)
        yield fh
        _verify_staging_file(staging)
        try:
            staging.output.verify()
        except ValueError as exc:
            raise _UnstableRegularFileError(str(exc)) from exc


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


def _remove_new_publication_if_same(
    output: _PinnedOutputDirectory,
    entry: _PublishedOutputFile,
) -> str:
    if not entry.created:
        return "reused"
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return "unavailable"
    fd = -1
    try:
        try:
            fd = os.open(
                entry.filename,
                os.O_RDONLY | nofollow,
                dir_fd=output.fd,
            )
        except FileNotFoundError:
            return "absent"
        except (OSError, TypeError, NotImplementedError):
            return "unavailable"
        try:
            opened = os.fstat(fd)
        except OSError:
            return "unavailable"
        try:
            current = os.stat(
                entry.filename,
                dir_fd=output.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent"
        except (OSError, TypeError, NotImplementedError):
            return "unavailable"
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != entry.identity
            or (current.st_dev, current.st_ino) != entry.identity
        ):
            return "replaced"
        try:
            final = os.stat(
                entry.filename,
                dir_fd=output.fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent"
        except (OSError, TypeError, NotImplementedError):
            return "unavailable"
        if (
            not stat.S_ISREG(final.st_mode)
            or (final.st_dev, final.st_ino) != entry.identity
        ):
            return "replaced"
        try:
            os.unlink(entry.filename, dir_fd=output.fd)
        except FileNotFoundError:
            return "absent"
        except (OSError, TypeError, NotImplementedError):
            return "unavailable"
        return "removed"
    finally:
        if fd >= 0:
            os.close(fd)


def _dir_size_fd(directory_fd: int) -> int:
    total = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for name in os.listdir(directory_fd):
        try:
            current = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISREG(current.st_mode):
                total += current.st_size
            elif stat.S_ISDIR(current.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    total += _dir_size_fd(child_fd)
                finally:
                    os.close(child_fd)
        except OSError:
            pass
    return total


def _dir_size(path):
    if isinstance(path, _PinnedOutputDirectory):
        return _dir_size_fd(path.fd)
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
    staging = (
        dest_path
        if isinstance(dest_path, _DownloadStagingFile)
        else None
    )
    result_path = staging.display_path if staging is not None else dest_path
    if not url.lower().startswith(("https://", "http://")):
        return {"ok": False, "path": result_path,
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
                    return {"ok": False, "path": result_path,
                            "skipped_reason": f"server returned HTML ({ctype}), not a data file"}
                clen = resp.info().get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    return {"ok": False, "path": result_path,
                            "skipped_reason": f"file exceeds max_bytes ({max_bytes})"}
                if staging is None:
                    dest_dir = os.path.realpath(
                        os.path.dirname(os.path.abspath(dest_path)) or "."
                    )
                    os.makedirs(dest_dir, exist_ok=True)
                    resolved_dest = os.path.join(
                        dest_dir,
                        os.path.basename(dest_path),
                    )
                    fd, temp_path = tempfile.mkstemp(
                        prefix=".paperconan-download-body-",
                        dir=dest_dir,
                    )
                    destination_fd = fd
                else:
                    _verify_staging_file(staging)
                    os.ftruncate(staging.fd, 0)
                    os.lseek(staging.fd, 0, os.SEEK_SET)
                    destination_fd = os.dup(staging.fd)
                total = 0
                with os.fdopen(destination_fd, "wb") as fh:
                    if staging is None:
                        fd = -1
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            return {"ok": False, "path": result_path,
                                    "skipped_reason": f"file exceeds max_bytes ({max_bytes})"}
                        fh.write(chunk)
                    fh.flush()
                if staging is None:
                    os.replace(temp_path, resolved_dest)
                    temp_path = None
                else:
                    _verify_staging_file(staging)
                    staging.output.verify()
                return {
                    "ok": True,
                    "path": result_path,
                    "size": total,
                    "content_type": ctype.split(";", 1)[0].strip(),
                    "source_url": url,
                }
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"ok": False, "path": result_path,
                        "skipped_reason": (f"requires authentication (HTTP {e.code}); "
                                           "download this file manually from the dataset page")}
            last_reason = f"HTTP {e.code}: {e.reason}"
            if not (500 <= e.code < 600):
                return {"ok": False, "path": result_path, "skipped_reason": last_reason}
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
    return {"ok": False, "path": result_path, "skipped_reason": last_reason}


def _write_collision_safe(
    out_dir: str | _PinnedOutputDirectory,
    name: str,
    data: bytes,
    *,
    _return_entry: bool = False,
) -> str | _PublishedOutputFile:
    if not isinstance(out_dir, _PinnedOutputDirectory):
        with _pinned_output_directory(out_dir) as output:
            return _write_collision_safe(
                output,
                name,
                data,
                _return_entry=_return_entry,
            )

    def regular_file_matches(filename: str) -> os.stat_result | None:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return None
        try:
            fd = os.open(
                filename,
                os.O_RDONLY | nofollow,
                dir_fd=out_dir.fd,
            )
        except OSError:
            return None
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != len(data)
            ):
                return None
            offset = 0
            with os.fdopen(os.dup(fd), "rb") as fh:
                while offset < len(data):
                    chunk = fh.read(min(1024 * 1024, len(data) - offset))
                    if not chunk or chunk != data[offset:offset + len(chunk)]:
                        return None
                    offset += len(chunk)
            final_opened = os.fstat(fd)
            try:
                current = os.stat(
                    filename,
                    dir_fd=out_dir.fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            if (
                stat.S_ISREG(current.st_mode)
                and stat.S_ISREG(final_opened.st_mode)
                and current.st_size == len(data)
                and final_opened.st_size == len(data)
                and current.st_dev == opened.st_dev
                and current.st_ino == opened.st_ino
                and final_opened.st_dev == opened.st_dev
                and final_opened.st_ino == opened.st_ino
            ):
                return current
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    def publication(
        filename: str,
        current: os.stat_result,
        *,
        created: bool,
    ) -> _PublishedOutputFile:
        return _PublishedOutputFile(
            filename=filename,
            size=current.st_size,
            identity=(current.st_dev, current.st_ino),
            sha256=content_sha256,
            created=created,
        )

    def result(entry: _PublishedOutputFile) -> str | _PublishedOutputFile:
        if _return_entry:
            return entry
        return entry.display_path(out_dir)

    out_dir.verify()
    stem, suffix = os.path.splitext(os.path.basename(name))
    content_sha256 = hashlib.sha256(data).hexdigest()
    digest = content_sha256[:10]
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
                matched = regular_file_matches(filename)
                if matched is not None:
                    out_dir.verify()
                    return result(
                        publication(filename, matched, created=False)
                    )
                collision_index += 1
                continue
            try:
                current = os.stat(
                    filename,
                    dir_fd=out_dir.fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_size != len(data)
                ):
                    raise _UnstableRegularFileError(
                        "published output entry is not a stable regular file"
                    )
                out_dir.verify()
            except Exception:
                entry = publication(filename, current, created=True)
                _remove_new_publication_if_same(out_dir, entry)
                raise
            return result(publication(filename, current, created=True))
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=out_dir.fd)
            except FileNotFoundError:
                pass


def _archive_staging_file(
    out_dir: _PinnedOutputDirectory,
    suffix: str,
) -> _DownloadStagingFile:
    return _download_staging_file(
        out_dir,
        prefix=".paperconan-archive-",
        suffix=suffix,
    )


def _published_file_limit_reason(cardinality: _CandidateCardinality) -> str:
    return (
        "published file cardinality ceiling reached "
        f"({cardinality.max_published_files}); remaining files were skipped"
    )


def _archive_member_limit_reason(cardinality: _CandidateCardinality) -> str:
    return (
        "archive member cardinality ceiling reached "
        f"({cardinality.max_archive_members}); remaining eligible members were skipped"
    )


def _append_limit_reason(reasons: list[str] | None, reason: str) -> None:
    if reasons is not None and reason not in reasons:
        reasons.append(reason)


def _archive_blocking_reason(
    cardinality: _CandidateCardinality | None,
) -> str | None:
    if cardinality is None:
        return None
    if not cardinality.can_publish():
        return _published_file_limit_reason(cardinality)
    if cardinality.archive_members >= cardinality.max_archive_members:
        return _archive_member_limit_reason(cardinality)
    return None


def _read_exact_zip_range(source, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0:
        raise ValueError(f"ZIP {label} position is invalid")
    source.seek(offset, os.SEEK_SET)
    data = source.read(size)
    if len(data) != size:
        raise ValueError(f"ZIP {label} is truncated")
    return data


def _validate_zip_central_directory(
    source,
    *,
    entry_count: int,
    directory_size: int,
    directory_offset: int,
    record_position: int,
    max_entries: int,
    prefix_adjustment: int | None = None,
) -> int:
    if directory_size > record_position:
        raise ValueError("ZIP central directory position is invalid")
    actual_offset = record_position - directory_size
    if actual_offset < directory_offset or actual_offset < 0:
        raise ValueError("ZIP central directory position is invalid")
    if (
        prefix_adjustment is not None
        and actual_offset - directory_offset != prefix_adjustment
    ):
        raise ValueError("ZIP central directory position is inconsistent")
    observed = 0
    position = actual_offset
    while position < record_position:
        remaining = record_position - position
        if remaining < _ZIP_CENTRAL_FILE_HEADER.size:
            raise ValueError("ZIP central directory fixed header is truncated")
        fixed = _read_exact_zip_range(
            source,
            position,
            _ZIP_CENTRAL_FILE_HEADER.size,
            "central directory fixed header",
        )
        fields = _ZIP_CENTRAL_FILE_HEADER.unpack(fixed)
        if fields[0] != _ZIP_CENTRAL_DIRECTORY_SIGNATURE:
            raise ValueError("ZIP central directory signature is invalid")
        filename_size, extra_size, comment_size = fields[10:13]
        disk_number = fields[13]
        if disk_number != 0:
            raise ValueError("multi-disk ZIP archives are unavailable")
        variable_size = filename_size + extra_size + comment_size
        record_size = _ZIP_CENTRAL_FILE_HEADER.size + variable_size
        record_end = position + record_size
        if (
            record_size < _ZIP_CENTRAL_FILE_HEADER.size
            or record_end <= position
            or record_end > record_position
        ):
            raise ValueError("ZIP central directory record is truncated")
        observed += 1
        if observed > max_entries:
            raise ValueError(
                f"observed ZIP entry count {observed} exceeds "
                f"preflight ceiling {max_entries}"
            )
        position = record_end
        source.seek(position, os.SEEK_SET)
    if position != record_position:
        raise ValueError("ZIP central directory end is inconsistent")
    if observed != entry_count:
        raise ValueError("ZIP entry counts are inconsistent")
    return observed


def _preflight_zip_entry_count(source, *, max_entries: int) -> int:
    if not isinstance(max_entries, int) or max_entries < 0:
        raise ValueError("ZIP entry ceiling is invalid")
    try:
        source.seek(0, os.SEEK_END)
        file_size = source.tell()
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("ZIP source is not seekable") from exc
    if file_size < _ZIP_EOCD.size:
        raise ValueError("ZIP EOCD record is missing or truncated")

    tail_size = min(
        file_size,
        _ZIP_EOCD.size + _ZIP_MAX_COMMENT_BYTES,
    )
    tail_offset = file_size - tail_size
    tail = _read_exact_zip_range(source, tail_offset, tail_size, "EOCD tail")
    candidates = []
    for relative_offset in range(tail_size - _ZIP_EOCD.size, -1, -1):
        fields = _ZIP_EOCD.unpack_from(tail, relative_offset)
        if fields[0] != _ZIP_EOCD_SIGNATURE:
            continue
        comment_size = fields[-1]
        record_position = tail_offset + relative_offset
        if record_position + _ZIP_EOCD.size + comment_size == file_size:
            candidates.append((record_position, fields))
    if not candidates:
        raise ValueError("ZIP EOCD record is missing or truncated")
    if len(candidates) != 1:
        raise ValueError("ZIP EOCD metadata is ambiguous")

    record_position, fields = candidates[0]
    (
        _,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        total_entries,
        directory_size,
        directory_offset,
        _,
    ) = fields
    needs_zip64 = (
        disk_number == _ZIP16_SENTINEL
        or central_directory_disk == _ZIP16_SENTINEL
        or entries_on_disk == _ZIP16_SENTINEL
        or total_entries == _ZIP16_SENTINEL
        or directory_size == _ZIP32_SENTINEL
        or directory_offset == _ZIP32_SENTINEL
    )

    if not needs_zip64:
        if disk_number != 0 or central_directory_disk != 0:
            raise ValueError("multi-disk ZIP archives are unavailable")
        if entries_on_disk != total_entries:
            raise ValueError("ZIP entry counts are inconsistent")
        if total_entries > max_entries:
            raise ValueError(
                f"raw ZIP entry count {total_entries} exceeds "
                f"preflight ceiling {max_entries}"
            )
        observed_entries = _validate_zip_central_directory(
            source,
            entry_count=total_entries,
            directory_size=directory_size,
            directory_offset=directory_offset,
            record_position=record_position,
            max_entries=max_entries,
        )
        return observed_entries

    locator_position = record_position - _ZIP64_LOCATOR.size
    locator_data = _read_exact_zip_range(
        source,
        locator_position,
        _ZIP64_LOCATOR.size,
        "ZIP64 locator",
    )
    (
        locator_signature,
        zip64_disk,
        declared_zip64_offset,
        disk_count,
    ) = _ZIP64_LOCATOR.unpack(locator_data)
    if locator_signature != _ZIP64_LOCATOR_SIGNATURE:
        raise ValueError("ZIP64 locator signature is invalid")
    if zip64_disk != 0 or disk_count != 1:
        raise ValueError("multi-disk ZIP64 archives are unavailable")

    candidate_positions = [declared_zip64_offset]
    inferred_position = locator_position - _ZIP64_EOCD.size
    if inferred_position != declared_zip64_offset:
        candidate_positions.append(inferred_position)
    zip64_records = []
    for candidate_position in candidate_positions:
        if (
            candidate_position < 0
            or candidate_position + _ZIP64_EOCD.size > locator_position
        ):
            continue
        data = _read_exact_zip_range(
            source,
            candidate_position,
            _ZIP64_EOCD.size,
            "ZIP64 EOCD record",
        )
        values = _ZIP64_EOCD.unpack(data)
        if values[0] != _ZIP64_EOCD_SIGNATURE:
            continue
        record_size = values[1]
        if (
            record_size < _ZIP64_EOCD.size - 12
            or candidate_position + 12 + record_size != locator_position
        ):
            continue
        zip64_records.append((candidate_position, values))
    if len(zip64_records) != 1:
        raise ValueError("ZIP64 EOCD record position or length is invalid")

    zip64_position, values = zip64_records[0]
    (
        _,
        _,
        _,
        _,
        zip64_disk_number,
        zip64_directory_disk,
        zip64_entries_on_disk,
        zip64_total_entries,
        zip64_directory_size,
        zip64_directory_offset,
    ) = values
    if zip64_disk_number != 0 or zip64_directory_disk != 0:
        raise ValueError("multi-disk ZIP64 archives are unavailable")
    if zip64_entries_on_disk != zip64_total_entries:
        raise ValueError("ZIP64 entry counts are inconsistent")

    classic_pairs = (
        (disk_number, _ZIP16_SENTINEL, zip64_disk_number),
        (
            central_directory_disk,
            _ZIP16_SENTINEL,
            zip64_directory_disk,
        ),
        (entries_on_disk, _ZIP16_SENTINEL, zip64_entries_on_disk),
        (total_entries, _ZIP16_SENTINEL, zip64_total_entries),
        (directory_size, _ZIP32_SENTINEL, zip64_directory_size),
        (directory_offset, _ZIP32_SENTINEL, zip64_directory_offset),
    )
    if any(
        classic_value != sentinel and classic_value != zip64_value
        for classic_value, sentinel, zip64_value in classic_pairs
    ):
        raise ValueError("classic and ZIP64 metadata are inconsistent")
    if zip64_total_entries > max_entries:
        raise ValueError(
            f"raw ZIP entry count {zip64_total_entries} exceeds "
            f"preflight ceiling {max_entries}"
        )
    prefix_adjustment = zip64_position - declared_zip64_offset
    if prefix_adjustment < 0:
        raise ValueError("ZIP64 EOCD position is invalid")
    observed_entries = _validate_zip_central_directory(
        source,
        entry_count=zip64_total_entries,
        directory_size=zip64_directory_size,
        directory_offset=zip64_directory_offset,
        record_position=zip64_position,
        max_entries=max_entries,
        prefix_adjustment=prefix_adjustment,
    )
    return observed_entries


def _extract_selected_zip(
    zip_source,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
    return_entries=False,
    cardinality=None,
    limit_reasons=None,
    published_entries=None,
    pending_entries=None,
):
    extracted = published_entries if published_entries is not None else []
    pending = pending_entries if pending_entries is not None else []
    written = _dir_size(out_dir)
    allowed = {"tabular"}
    if include_images:
        allowed.update({"image", "document"})
    if isinstance(zip_source, (bytes, bytearray, memoryview)):
        source = io.BytesIO(bytes(zip_source))
    elif all(hasattr(zip_source, name) for name in ("read", "seek", "tell")):
        source = zip_source
    else:
        raise ValueError("ZIP source is not seekable")
    _preflight_zip_entry_count(
        source,
        max_entries=_MAX_RAW_ZIP_ENTRIES_PER_ARCHIVE,
    )
    source.seek(0, os.SEEK_SET)
    with zipfile.ZipFile(source) as zf:
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
            if cardinality is not None:
                if not cardinality.can_publish():
                    _append_limit_reason(
                        limit_reasons,
                        _published_file_limit_reason(cardinality),
                    )
                    break
                if not cardinality.claim_archive_member():
                    _append_limit_reason(
                        limit_reasons,
                        _archive_member_limit_reason(cardinality),
                    )
                    break
            with zf.open(info) as src:
                data = src.read(max_member_bytes + 1)
                if len(data) > max_member_bytes:
                    continue
            dest = _write_collision_safe(
                out_dir,
                name,
                data,
                _return_entry=return_entries,
            )
            if return_entries:
                pending.append(dest)
            if cardinality is not None:
                cardinality.record_publication()
            written += len(data)
            if return_entries:
                _verify_published_output_file(out_dir, dest)
                out_dir.verify()
                pending.remove(dest)
            extracted.append(dest)
    return extracted


def _extract_selected_tar(
    tar_source,
    out_dir,
    *,
    include_images=False,
    max_member_bytes=_DEFAULT_MAX,
    return_entries=False,
    cardinality=None,
    limit_reasons=None,
    published_entries=None,
    pending_entries=None,
):
    extracted = published_entries if published_entries is not None else []
    pending = pending_entries if pending_entries is not None else []
    written = _dir_size(out_dir)
    allowed = {"tabular"}
    if include_images:
        allowed.update({"image", "document"})
    if hasattr(tar_source, "read"):
        archive = tarfile.open(fileobj=tar_source, mode="r:gz")
    else:
        archive = tarfile.open(tar_source, "r:gz")
    with archive as tf:
        for member in tf:
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
            if cardinality is not None:
                if not cardinality.can_publish():
                    _append_limit_reason(
                        limit_reasons,
                        _published_file_limit_reason(cardinality),
                    )
                    break
                if not cardinality.claim_archive_member():
                    _append_limit_reason(
                        limit_reasons,
                        _archive_member_limit_reason(cardinality),
                    )
                    break
            src = tf.extractfile(member)
            if src is None:
                continue
            data = src.read(max_member_bytes + 1)
            if len(data) > max_member_bytes:
                continue
            dest = _write_collision_safe(
                out_dir,
                name,
                data,
                _return_entry=return_entries,
            )
            if return_entries:
                pending.append(dest)
            if cardinality is not None:
                cardinality.record_publication()
            written += len(data)
            if return_entries:
                _verify_published_output_file(out_dir, dest)
                out_dir.verify()
                pending.remove(dest)
            extracted.append(dest)
    return extracted


def _extract_tabular_tar(tar_path, out_dir, max_member_bytes=_DEFAULT_MAX):
    """Backward-compatible tabular-only tar extraction."""
    return _extract_selected_tar(
        tar_path,
        out_dir,
        max_member_bytes=max_member_bytes,
    )


def _verify_published_output_file(
    output: _PinnedOutputDirectory,
    entry: _PublishedOutputFile,
) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _UnstableRegularFileError(
            "published output verification is unavailable"
        )
    try:
        fd = os.open(
            entry.filename,
            os.O_RDONLY | nofollow,
            dir_fd=output.fd,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise _UnstableRegularFileError(
            "published output entry is unavailable"
        ) from exc
    try:
        opened = os.fstat(fd)
        try:
            current = os.stat(
                entry.filename,
                dir_fd=output.fd,
                follow_symlinks=False,
            )
        except (OSError, TypeError, NotImplementedError) as exc:
            raise _UnstableRegularFileError(
                "published output entry is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_size != entry.size
            or current.st_size != entry.size
            or (opened.st_dev, opened.st_ino) != entry.identity
            or (current.st_dev, current.st_ino) != entry.identity
        ):
            raise _UnstableRegularFileError(
                "published output entry is not a stable regular file"
            )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        try:
            final = os.stat(
                entry.filename,
                dir_fd=output.fd,
                follow_symlinks=False,
            )
        except (OSError, TypeError, NotImplementedError) as exc:
            raise _UnstableRegularFileError(
                "published output entry is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_size != entry.size
            or (final.st_dev, final.st_ino) != entry.identity
        ):
            raise _UnstableRegularFileError(
                "published output entry is not a stable regular file"
            )
        if digest.hexdigest() != entry.sha256:
            raise _UnstableRegularFileError(
                "published output entry content changed during publication"
            )
    finally:
        os.close(fd)


def _reconcile_archive_publications(
    output: _PinnedOutputDirectory,
    accepted: list[_PublishedOutputFile],
    pending: list[_PublishedOutputFile],
) -> tuple[list[_PublishedOutputFile], list[str], BaseException | None]:
    reconciled = []
    outcomes = []
    first_error = None
    seen = set()
    for entry in [*accepted, *pending]:
        key = (entry.filename, entry.identity)
        if key in seen:
            continue
        seen.add(key)
        try:
            _verify_published_output_file(output, entry)
            output.verify()
        except (OSError, ValueError) as exc:
            if first_error is None:
                first_error = exc
            if not entry.created:
                outcomes.append(
                    "retained collision-reused output without reporting it: "
                    f"{entry.filename}"
                )
                continue
            removal = _remove_new_publication_if_same(output, entry)
            if removal in {"removed", "absent"}:
                outcomes.append(
                    f"removed unverified new output: {entry.filename}"
                )
            elif removal == "replaced":
                outcomes.append(
                    f"retained replacement path for recovery: {entry.filename}"
                )
            else:
                outcomes.append(
                    f"retained new output for recovery: {entry.filename}"
                )
            continue
        reconciled.append(entry)
        outcomes.append(f"retained verified output: {entry.filename}")
    accepted[:] = reconciled
    pending.clear()
    return reconciled, outcomes, first_error


def _download_oa_package(
    pkg,
    out_dir,
    published_outputs,
    skipped,
    max_bytes,
    *,
    include_images=False,
    cardinality=None,
):
    """Download the static PMC OA tar.gz, extract selected members, drop the tarball."""
    blocking_reason = _archive_blocking_reason(cardinality)
    if blocking_reason is not None:
        skipped.append({"name": pkg.get("name"), "reason": blocking_reason})
        return []
    tmp = None
    try:
        tmp = _archive_staging_file(out_dir, ".tar.gz")
        res = download_file(pkg["url"], tmp, max_bytes=_ARCHIVE_MAX)
        if not res.get("ok"):
            skipped.append({"name": pkg.get("name"), "reason": res.get("skipped_reason")})
            return []
        try:
            limit_reasons = []
            extracted = []
            pending = []
            processing_error = None
            staging_error = None
            try:
                with _open_download_staging(tmp) as archive_fh:
                    try:
                        _extract_selected_tar(
                            archive_fh,
                            out_dir,
                            include_images=include_images,
                            max_member_bytes=max_bytes,
                            return_entries=True,
                            cardinality=cardinality,
                            limit_reasons=limit_reasons,
                            published_entries=extracted,
                            pending_entries=pending,
                        )
                    except (tarfile.TarError, OSError, ValueError) as exc:
                        processing_error = exc
            except _UnstableRegularFileError as exc:
                staging_error = exc
            reconciled, outcomes, reconciliation_error = (
                _reconcile_archive_publications(
                    out_dir,
                    extracted,
                    pending,
                )
            )
            published_outputs.extend(reconciled)
            failure = staging_error or processing_error or reconciliation_error
            if failure is not None:
                if staging_error is not None:
                    reason = (
                        "downloaded archive is not a stable regular file: "
                        f"{staging_error}"
                    )
                else:
                    reason = (
                        f"archive publication unavailable: {failure}"
                        if isinstance(failure, OSError)
                        else f"archive processing unavailable: {failure}"
                    )
                if outcomes:
                    reason += "; " + "; ".join(outcomes)
                skipped.append({"name": pkg.get("name"), "reason": reason})
                return reconciled
        except (tarfile.TarError, OSError, ValueError) as e:
            reason = (
                f"archive publication unavailable: {e}"
                if isinstance(e, OSError)
                else f"archive processing unavailable: {e}"
            )
            skipped.append({"name": pkg.get("name"), "reason": reason})
            return []
        skipped.extend(
            {"name": pkg.get("name"), "reason": reason}
            for reason in limit_reasons
        )
        return reconciled
    except (tarfile.TarError, OSError, ValueError) as e:
        skipped.append({"name": pkg.get("name"), "reason": f"bad tar.gz: {e}"})
        return []
    finally:
        _cleanup_download_staging(tmp)


def _download_supplementary_archive(
    arch,
    out_dir,
    published_outputs,
    skipped,
    max_bytes,
    archive_max=_ARCHIVE_MAX,
    *,
    include_images=False,
    cardinality=None,
):
    """Fetch a supplementary zip, extract selected members, and drop the zip.

    The archive downloads with the larger ``archive_max`` cap; each extracted member is
    still capped at the per-file ``max_bytes``."""
    blocking_reason = _archive_blocking_reason(cardinality)
    if blocking_reason is not None:
        skipped.append({"name": arch.get("name"), "reason": blocking_reason})
        return []
    tmp_zip = None
    try:
        tmp_zip = _archive_staging_file(out_dir, ".zip")
        res = download_file(arch["url"], tmp_zip, max_bytes=archive_max)
        if not res.get("ok"):
            skipped.append({"name": arch.get("name"), "reason": res.get("skipped_reason")})
            return []
        try:
            limit_reasons = []
            extracted = []
            pending = []
            processing_error = None
            staging_error = None
            try:
                with _open_download_staging(tmp_zip) as archive_fh:
                    try:
                        _extract_selected_zip(
                            archive_fh,
                            out_dir,
                            include_images=include_images,
                            max_member_bytes=max_bytes,
                            return_entries=True,
                            cardinality=cardinality,
                            limit_reasons=limit_reasons,
                            published_entries=extracted,
                            pending_entries=pending,
                        )
                    except (zipfile.BadZipFile, OSError, ValueError) as exc:
                        processing_error = exc
            except _UnstableRegularFileError as exc:
                staging_error = exc
            reconciled, outcomes, reconciliation_error = (
                _reconcile_archive_publications(
                    out_dir,
                    extracted,
                    pending,
                )
            )
            published_outputs.extend(reconciled)
            failure = staging_error or processing_error or reconciliation_error
            if failure is not None:
                if staging_error is not None:
                    reason = (
                        "downloaded archive is not a stable regular file: "
                        f"{staging_error}"
                    )
                else:
                    reason = (
                        "not a valid zip archive"
                        if isinstance(failure, zipfile.BadZipFile)
                        else (
                            f"archive publication unavailable: {failure}"
                            if isinstance(failure, OSError)
                            else f"archive processing unavailable: {failure}"
                        )
                    )
                if outcomes:
                    reason += "; " + "; ".join(outcomes)
                skipped.append({"name": arch.get("name"), "reason": reason})
                return reconciled
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            reason = (
                "not a valid zip archive"
                if isinstance(exc, zipfile.BadZipFile)
                else (
                    f"archive publication unavailable: {exc}"
                    if isinstance(exc, OSError)
                    else f"archive processing unavailable: {exc}"
                )
            )
            skipped.append({"name": arch.get("name"), "reason": reason})
            return []
        skipped.extend(
            {"name": arch.get("name"), "reason": reason}
            for reason in limit_reasons
        )
        return reconciled
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        reason = (
            "not a valid zip archive"
            if isinstance(exc, zipfile.BadZipFile)
            else (
                f"archive publication unavailable: {exc}"
                if isinstance(exc, OSError)
                else f"archive processing unavailable: {exc}"
            )
        )
        skipped.append({"name": arch.get("name"), "reason": reason})
        return []
    finally:
        _cleanup_download_staging(tmp_zip)


def _safe_source_url(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _safe_failure_context(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__

    def redact_url(match: re.Match) -> str:
        parsed = urllib.parse.urlsplit(match.group(0))
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port is not None else hostname
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, "", "")
        )

    return _URL_IN_ERROR.sub(redact_url, text)


def _hash_exact_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = size
    while remaining:
        chunk = os.read(fd, min(_FILE_COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise _UnstableRegularFileError(
                "regular file changed during bounded read"
            )
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise _UnstableRegularFileError(
            "regular file changed during bounded read"
        )
    return digest.hexdigest()


def _copy_prior_sidecar_backup(output: _PinnedOutputDirectory) -> str:
    source_fd = backup_fd = -1
    backup_name = None
    try:
        source_fd = os.open(
            SOURCE_SIDECAR,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=output.fd,
        )
        opened = os.fstat(source_fd)
        current = os.stat(
            SOURCE_SIDECAR,
            dir_fd=output.fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_size > _MAX_SOURCE_SIDECAR_BYTES
            or opened.st_size != current.st_size
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise _UnstableRegularFileError(
                "prior provenance sidecar is not a stable bounded regular file"
            )
        for _ in range(128):
            backup_name = f".paperconan-sidecar-backup-{secrets.token_hex(8)}"
            try:
                backup_fd = os.open(
                    backup_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=output.fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(
                "could not retain prior provenance sidecar"
            )
        os.lseek(source_fd, 0, os.SEEK_SET)
        remaining = opened.st_size
        copied_digest = hashlib.sha256()
        while remaining:
            chunk = os.read(
                source_fd,
                min(_FILE_COPY_CHUNK_BYTES, remaining),
            )
            if not chunk:
                raise _UnstableRegularFileError(
                    "prior provenance sidecar changed during backup"
                )
            copied_digest.update(chunk)
            remaining -= len(chunk)
            pending = memoryview(chunk)
            while pending:
                written = os.write(backup_fd, pending)
                if written <= 0:
                    raise OSError("provenance sidecar backup write failed")
                pending = pending[written:]
        if os.read(source_fd, 1):
            raise _UnstableRegularFileError(
                "prior provenance sidecar changed during backup"
            )
        os.fsync(backup_fd)
        final_opened = os.fstat(source_fd)
        final_current = os.stat(
            SOURCE_SIDECAR,
            dir_fd=output.fd,
            follow_symlinks=False,
        )
        backup = os.fstat(backup_fd)
        if (
            not stat.S_ISREG(final_opened.st_mode)
            or not stat.S_ISREG(final_current.st_mode)
            or not stat.S_ISREG(backup.st_mode)
            or final_opened.st_size != opened.st_size
            or final_current.st_size != opened.st_size
            or backup.st_size != opened.st_size
            or final_opened.st_dev != opened.st_dev
            or final_opened.st_ino != opened.st_ino
            or final_current.st_dev != opened.st_dev
            or final_current.st_ino != opened.st_ino
            or final_opened.st_mtime_ns != opened.st_mtime_ns
            or final_opened.st_ctime_ns != opened.st_ctime_ns
            or (backup.st_dev, backup.st_ino)
            == (opened.st_dev, opened.st_ino)
        ):
            raise _UnstableRegularFileError(
                "prior provenance sidecar changed during backup"
            )
        copied_sha256 = copied_digest.hexdigest()
        if (
            _hash_exact_fd(source_fd, opened.st_size) != copied_sha256
            or _hash_exact_fd(backup_fd, opened.st_size) != copied_sha256
        ):
            raise _UnstableRegularFileError(
                "prior provenance sidecar changed during backup"
            )
        verified_opened = os.fstat(source_fd)
        verified_current = os.stat(
            SOURCE_SIDECAR,
            dir_fd=output.fd,
            follow_symlinks=False,
        )
        if (
            verified_opened.st_size != opened.st_size
            or verified_current.st_size != opened.st_size
            or verified_opened.st_dev != opened.st_dev
            or verified_opened.st_ino != opened.st_ino
            or verified_current.st_dev != opened.st_dev
            or verified_current.st_ino != opened.st_ino
            or verified_opened.st_mtime_ns != opened.st_mtime_ns
            or verified_opened.st_ctime_ns != opened.st_ctime_ns
        ):
            raise _UnstableRegularFileError(
                "prior provenance sidecar changed during backup"
            )
        output.verify()
        retained_name = backup_name
        backup_name = None
        return retained_name
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if backup_fd >= 0:
            os.close(backup_fd)
        if backup_name is not None:
            try:
                os.unlink(backup_name, dir_fd=output.fd)
            except FileNotFoundError:
                pass


def _write_source_sidecar(cand, out_dir, downloads=None):
    """Record which paper/dataset these downloads came from, for scan.json provenance."""
    prov = {"doi": cand.get("doi"), "title": cand.get("title"),
            "source": cand.get("source"), "cand_id": cand.get("cand_id"),
            "related_dois": cand.get("related_dois") or []}
    prov["downloads"] = sorted(downloads or [], key=lambda x: x["file"])
    if not isinstance(out_dir, _PinnedOutputDirectory):
        try:
            with _pinned_output_directory(out_dir) as output:
                publication = _write_source_sidecar(
                    cand,
                    output,
                    downloads=downloads,
                )
                if publication is not None:
                    publication.commit()
        except (OSError, _SourceSidecarLimitError):
            pass
        return
    temp_name = None
    backup_name = None
    temp_fd = -1
    publication = None
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
            return None
        encoded = json.dumps(prov, indent=2, default=str).encode("utf-8")
        if len(encoded) > _MAX_SOURCE_SIDECAR_BYTES:
            raise _SourceSidecarLimitError(
                "new provenance sidecar exceeds "
                f"{_MAX_SOURCE_SIDECAR_BYTES}-byte limit"
            )
        temp_name = f".paperconan-sidecar-{secrets.token_hex(8)}"
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=out_dir.fd,
        )
        with os.fdopen(temp_fd, "wb") as fh:
            temp_fd = -1
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        out_dir.verify()
        if current is not None:
            backup_name = _copy_prior_sidecar_backup(out_dir)
        os.replace(
            temp_name,
            SOURCE_SIDECAR,
            src_dir_fd=out_dir.fd,
            dst_dir_fd=out_dir.fd,
        )
        temp_name = None
        publication = _SidecarPublication(
            output=out_dir,
            backup_name=backup_name,
        )
        backup_name = None
        out_dir.verify()
        return publication
    except _SourceSidecarLimitError:
        raise
    except (OSError, ValueError):
        if publication is not None:
            try:
                publication.rollback()
            except (OSError, ValueError) as exc:
                raise _UnstableRegularFileError(
                    "provenance sidecar rollback unavailable"
                ) from exc
        return None  # provenance is best-effort when no state was replaced
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=out_dir.fd)
            except FileNotFoundError:
                pass
        if backup_name is not None:
            try:
                os.unlink(backup_name, dir_fd=out_dir.fd)
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
        published_outputs, skipped = [], []
        provenance_files = []
        direct_asset_types = set()
        cardinality = _CandidateCardinality(
            max_published_files=_MAX_PUBLISHED_FILES_PER_CANDIDATE,
            max_archive_members=_MAX_ARCHIVE_MEMBERS_PER_CANDIDATE,
        )
        for f in files:
            if not cardinality.can_publish():
                skipped.append({
                    "name": f["name"],
                    "reason": _published_file_limit_reason(cardinality),
                })
                break
            if _dir_size(output) > _MAX_PAPER_BYTES:
                skipped.append({
                    "name": f["name"],
                    "reason": "paper data exceeds per-paper cap",
                })
                continue
            suffix = os.path.splitext(os.path.basename(f["name"]))[1]
            try:
                staging = _download_staging_file(
                    output,
                    prefix=".paperconan-download-",
                    suffix=suffix,
                )
            except (OSError, ValueError) as exc:
                skipped.append({
                    "name": f["name"],
                    "reason": f"secure download staging failed: {exc}",
                })
                continue
            try:
                res = download_file(
                    f["download_url"],
                    staging,
                    max_bytes=max_bytes,
                )
                if res.get("ok"):
                    try:
                        with _open_download_staging(staging) as fh:
                            data = fh.read()
                    except (_UnstableRegularFileError, ValueError) as e:
                        skipped.append({
                            "name": f["name"],
                            "reason": (
                                "downloaded file is not a stable regular file: "
                                f"{e}"
                            ),
                        })
                        continue
                    try:
                        published = _write_collision_safe(
                            output,
                            f["name"],
                            data,
                            _return_entry=True,
                        )
                    except (OSError, ValueError) as exc:
                        skipped.append({
                            "name": f["name"],
                            "reason": f"secure publication failed: {exc}",
                        })
                        continue
                    cardinality.record_publication()
                    published_outputs.append(published)
                    direct_asset_types.add(asset_type(f.get("name") or ""))
                    provenance_files.append(_provenance_entry(
                        published.filename,
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
                _cleanup_download_staging(staging)
        pkg = cand.get("oa_package")
        if pkg and pkg.get("url"):
            extracted = _download_oa_package(
                pkg,
                output,
                published_outputs,
                skipped,
                max_bytes,
                include_images=include_images,
                cardinality=cardinality,
            )
            provenance_files.extend(
                _provenance_entry(
                    entry.filename,
                    pkg.get("url"),
                    size=entry.size,
                )
                for entry in extracted
            )
        arch = cand.get("supplementary_archive")
        needs_archive = not published_outputs
        if include_images:
            needs_archive = needs_archive or bool(
                {"tabular", "image", "document"} - direct_asset_types
            )
        if needs_archive and arch and arch.get("url"):
            extracted = _download_supplementary_archive(
                arch,
                output,
                published_outputs,
                skipped,
                max_bytes,
                archive_max=archive_max,
                include_images=include_images,
                cardinality=cardinality,
            )
            provenance_files.extend(
                _provenance_entry(
                    entry.filename,
                    arch.get("url"),
                    size=entry.size,
                )
                for entry in extracted
            )
        by_file = {}
        for entry in provenance_files:
            by_file.setdefault(entry["file"], entry)
        sidecar_publication = None
        try:
            for entry in published_outputs:
                _verify_published_output_file(output, entry)
            output.verify()
            try:
                sidecar_publication = _write_source_sidecar(
                    cand,
                    output,
                    downloads=list(by_file.values()),
                )
            except _SourceSidecarLimitError as exc:
                skipped.append({
                    "name": SOURCE_SIDECAR,
                    "reason": str(exc),
                })
            for entry in published_outputs:
                _verify_published_output_file(output, entry)
            output.verify()
        except (OSError, ValueError) as exc:
            if sidecar_publication is not None:
                try:
                    sidecar_publication.rollback()
                except (OSError, ValueError) as rollback_exc:
                    exc = _UnstableRegularFileError(
                        "provenance sidecar rollback unavailable: "
                        f"{rollback_exc}"
                    )
            if not any(
                str(exc) in str(item.get("reason") or "")
                for item in skipped
            ):
                skipped.append({
                    "name": cand.get("cand_id"),
                    "reason": f"published output verification failed: {exc}",
                })
            downloaded = []
        else:
            try:
                if sidecar_publication is not None:
                    sidecar_publication.commit()
            except (OSError, ValueError) as exc:
                reason = (
                    "provenance sidecar commit failed: "
                    f"{_safe_failure_context(exc)}"
                )
                if sidecar_publication is not None:
                    try:
                        sidecar_publication.rollback()
                    except (OSError, ValueError) as rollback_exc:
                        reason += (
                            "; provenance sidecar rollback failed: "
                            f"{_safe_failure_context(rollback_exc)}"
                        )
                        if sidecar_publication.backup_name is not None:
                            reason += (
                                "; retained recovery backup: "
                                f"{os.path.basename(sidecar_publication.backup_name)}"
                            )
                skipped.append({
                    "name": cand.get("cand_id"),
                    "reason": reason,
                })
                downloaded = []
            else:
                unique_outputs = list({
                    entry.filename: entry
                    for entry in published_outputs
                }.values())
                downloaded = [
                    entry.display_path(output)
                    for entry in unique_outputs
                ]
        return {
            "cand_id": cand.get("cand_id"),
            "out_dir": output.path,
            "downloaded": downloaded,
            "skipped": skipped,
        }
