from __future__ import annotations

import hashlib
import inspect
import json
import math
import mimetypes
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import ImageDependencyError


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
_MAX_IMAGE_MB = float(os.environ.get("PAPERCONAN_MAX_IMAGE_MB", "100"))
_MAX_IMAGE_BYTES = int(_MAX_IMAGE_MB * 1024 * 1024)
_MAX_IMAGE_PIXELS = int(os.environ.get("PAPERCONAN_MAX_IMAGE_PIXELS", "100000000"))
_MAX_IMAGE_ASSETS = int(os.environ.get("PAPERCONAN_MAX_IMAGE_ASSETS", "1000"))
_PDF_DPI = 200


class _AssetPublicationRollbackError(RuntimeError):
    pass


def _load_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ImageDependencyError(
            'image support requires `pip install "paperconan[image]"`'
        ) from exc
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    return Image, ImageOps


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_id(digest: str) -> str:
    return f"img:{digest[:20]}"


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _source_provenance(in_dir: Path) -> dict[str, dict]:
    sidecar = in_dir / "paperconan_source.json"
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(item.get("file")): item
        for item in data.get("downloads", []) or []
        if item.get("file")
    }


def _provided_provenance(provenance: dict | None) -> dict[str, dict]:
    if not isinstance(provenance, dict):
        return {}
    downloads = provenance.get("downloads")
    if isinstance(downloads, list):
        return {
            str(item.get("file")): item
            for item in downloads
            if isinstance(item, dict) and item.get("file")
        }
    return {
        str(name): item
        for name, item in provenance.items()
        if isinstance(item, dict)
    }


def _write_preview(image, destination, max_side: int = 1400) -> None:
    preview = image.copy()
    try:
        preview.thumbnail((max_side, max_side))
        if preview.mode not in ("RGB", "L"):
            converted = preview.convert("RGB")
            preview.close()
            preview = converted
        if isinstance(destination, (str, os.PathLike)):
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            destination = path
        preview.save(destination, format="JPEG", quality=86, optimize=True)
    finally:
        preview.close()


def _secure_dirfd_capability_error() -> str | None:
    missing = []
    for name in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, name):
            missing.append(f"os.{name}")
    supports_dir_fd = getattr(os, "supports_dir_fd", frozenset())
    for name in ("open", "mkdir", "stat", "unlink"):
        function = getattr(os, name, None)
        if function is None or function not in supports_dir_fd:
            missing.append(f"os.{name}(dir_fd=...)")
    supports_follow_symlinks = getattr(
        os,
        "supports_follow_symlinks",
        frozenset(),
    )
    if getattr(os, "stat", None) not in supports_follow_symlinks:
        missing.append("os.stat(follow_symlinks=False)")
    try:
        replace_parameters = inspect.signature(os.replace).parameters
    except (AttributeError, TypeError, ValueError):
        replace_parameters = {}
    if not {"src_dir_fd", "dst_dir_fd"} <= set(replace_parameters):
        missing.append("os.replace(src_dir_fd=..., dst_dir_fd=...)")
    if not missing:
        return None
    return ", ".join(missing)


def _require_secure_dirfd_publication() -> None:
    missing = _secure_dirfd_capability_error()
    if missing is not None:
        raise ValueError(
            "secure image asset publication is unavailable on this platform; "
            f"required dir_fd/no-follow capabilities missing: {missing}"
        )


def _secure_dirfd_runtime_error(exc: Exception) -> ValueError:
    return ValueError(
        "secure image asset publication is unavailable on this platform; "
        "required dir_fd/no-follow operation failed"
    )


def _probe_replace_dirfd(directory_fd: int) -> None:
    token = secrets.token_hex(8)
    source_name = f".paperconan-dirfd-probe-{token}.src"
    destination_name = f".paperconan-dirfd-probe-{token}.dst"
    probe_fd = None
    try:
        probe_fd = os.open(
            source_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.close(probe_fd)
        probe_fd = None
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        current = os.stat(
            destination_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(current.st_mode):
            raise ValueError(
                "secure image asset publication dir_fd probe was not regular"
            )
    except (AttributeError, NotImplementedError, TypeError) as exc:
        raise _secure_dirfd_runtime_error(exc) from exc
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
        _unlink_at(source_name, directory_fd)
        _unlink_at(destination_name, directory_fd)


def _open_child_directory(parent_fd: int, name: str, display_path: Path) -> int:
    try:
        os.mkdir(name, dir_fd=parent_fd)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(
            f"{display_path}: resolves outside artifact root or is not a directory"
        ) from exc


@contextmanager
def _asset_output_directories(output_root: Path):
    _require_secure_dirfd_publication()
    flags = os.O_RDONLY | os.O_DIRECTORY
    try:
        root_fd = os.open(output_root, flags)
    except (AttributeError, NotImplementedError, TypeError) as exc:
        raise _secure_dirfd_runtime_error(exc) from exc
    images_fd = native_fd = preview_fd = None
    try:
        images_fd = _open_child_directory(
            root_fd,
            "images",
            output_root / "images",
        )
        _probe_replace_dirfd(images_fd)
        native_fd = _open_child_directory(
            images_fd,
            "native",
            output_root / "images" / "native",
        )
        preview_fd = _open_child_directory(
            images_fd,
            "preview",
            output_root / "images" / "preview",
        )
        yield root_fd, images_fd, native_fd, preview_fd
    finally:
        for fd in (preview_fd, native_fd, images_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _verify_directory_entry(
    parent_fd: int,
    name: str,
    child_fd: int,
    display_path: Path,
) -> None:
    opened = os.fstat(child_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(
            f"{display_path}: changed during asset publication"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise ValueError(f"{display_path}: changed during asset publication")


def _verify_asset_directories(
    output_root: Path,
    root_fd: int,
    images_fd: int,
    native_fd: int,
    preview_fd: int,
) -> None:
    images = output_root / "images"
    _verify_directory_entry(root_fd, "images", images_fd, images)
    _verify_directory_entry(images_fd, "native", native_fd, images / "native")
    _verify_directory_entry(images_fd, "preview", preview_fd, images / "preview")


def _verify_regular_file_entry(
    directory_fd: int,
    name: str,
    file_fd: int,
    display_path: Path,
) -> None:
    opened = os.fstat(file_fd)
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(
            f"{display_path}: changed during asset publication"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise ValueError(f"{display_path}: changed during asset publication")


def _asset_temp_path(directory_fd: int, *, suffix: str) -> tuple[str, int]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(128):
        name = f".paperconan-image-{secrets.token_hex(8)}{suffix}"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        return name, fd
    raise FileExistsError("could not allocate image staging file")


def _unlink_at(name: str | None, directory_fd: int) -> None:
    if name is None:
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _copy_source_to_fd(source: Path, destination_fd: int) -> None:
    os.lseek(destination_fd, 0, os.SEEK_SET)
    with source.open("rb") as source_fh:
        with os.fdopen(os.dup(destination_fd), "wb") as destination_fh:
            shutil.copyfileobj(source_fh, destination_fh, length=1024 * 1024)


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(fd), "rb") as fh:
        fh.seek(0)
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _cleanup_incomplete_pair(
    native_fd: int,
    native_name: str,
    preview_fd: int,
    preview_name: str,
) -> None:
    native_exists = _entry_exists(native_fd, native_name)
    preview_exists = _entry_exists(preview_fd, preview_name)
    if native_exists != preview_exists:
        if native_exists:
            _unlink_at(native_name, native_fd)
        if preview_exists:
            _unlink_at(preview_name, preview_fd)


def _move_existing_to_backup(directory_fd: int, final_name: str) -> str | None:
    backup_name, backup_fd = _asset_temp_path(directory_fd, suffix=".backup")
    os.close(backup_fd)
    try:
        os.replace(
            final_name,
            backup_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except FileNotFoundError:
        _unlink_at(backup_name, directory_fd)
        return None
    except Exception:
        _unlink_at(backup_name, directory_fd)
        raise
    return backup_name


def _publish_asset_pair(
    *,
    output_root: Path,
    root_fd: int,
    images_fd: int,
    native_fd: int,
    preview_fd: int,
    native_temp_name: str,
    preview_temp_name: str,
    native_temp_fd: int,
    preview_temp_fd: int,
    native_name: str,
    preview_name: str,
) -> None:
    native = output_root / "images" / "native" / native_name
    preview = output_root / "images" / "preview" / preview_name
    entries = [
        (native_fd, native_temp_name, native_temp_fd, native_name, native),
        (
            preview_fd,
            preview_temp_name,
            preview_temp_fd,
            preview_name,
            preview,
        ),
    ]
    backups: list[str | None] = [None, None]
    prepared = [False, False]
    installed = [False, False]
    publication_succeeded = False
    try:
        _verify_asset_directories(
            output_root,
            root_fd,
            images_fd,
            native_fd,
            preview_fd,
        )
        for index, (directory_fd, _, _, final_name, _) in enumerate(entries):
            backups[index] = _move_existing_to_backup(directory_fd, final_name)
            prepared[index] = True
        _verify_asset_directories(
            output_root,
            root_fd,
            images_fd,
            native_fd,
            preview_fd,
        )
        for index, entry in enumerate(entries):
            directory_fd, temp_name, temp_fd, final_name, final_path = entry
            _verify_regular_file_entry(
                directory_fd,
                temp_name,
                temp_fd,
                final_path,
            )
            os.replace(
                temp_name,
                final_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            installed[index] = True
            _verify_regular_file_entry(
                directory_fd,
                final_name,
                temp_fd,
                final_path,
            )
        _verify_asset_directories(
            output_root,
            root_fd,
            images_fd,
            native_fd,
            preview_fd,
        )
        publication_succeeded = True
    except Exception as publication_error:
        rollback_error = None
        for index, (directory_fd, _, _, final_name, _) in enumerate(entries):
            if installed[index]:
                try:
                    _unlink_at(final_name, directory_fd)
                except Exception as exc:
                    rollback_error = rollback_error or exc
        for index, (directory_fd, _, _, final_name, _) in enumerate(entries):
            backup_name = backups[index]
            if prepared[index] and backup_name is not None:
                try:
                    os.replace(
                        backup_name,
                        final_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    backups[index] = None
                except Exception as exc:
                    rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise _AssetPublicationRollbackError(
                "image asset publication rollback failed; "
                "recovery backup retained"
            ) from publication_error
        raise
    finally:
        if publication_succeeded:
            for index, (directory_fd, _, _, _, _) in enumerate(entries):
                _unlink_at(backups[index], directory_fd)


def _record_image(
    source: Path,
    output_root: Path,
    *,
    source_type: str,
    source_url: str | None,
    parent_file: str | None = None,
    page: int | None = None,
    render_dpi: int | None = None,
    digest: str | None = None,
) -> dict:
    Image, ImageOps = _load_pillow()
    if source.stat().st_size > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"{source.name}: exceeds PAPERCONAN_MAX_IMAGE_MB={_MAX_IMAGE_MB:g}"
        )
    digest = digest or _sha256(source)
    asset_id = _asset_id(digest)
    suffix = source.suffix.lower() or ".png"
    stem = asset_id.replace(":", "-")
    native = output_root / "images" / "native" / f"{stem}{suffix}"
    preview = output_root / "images" / "preview" / f"{stem}.jpg"
    native_name = native.name
    preview_name = preview.name
    with _asset_output_directories(output_root) as directory_fds:
        root_fd, images_fd, native_fd, preview_fd = directory_fds
        native_temp_name = preview_temp_name = None
        native_temp_fd = preview_temp_fd = None
        try:
            native_temp_name, native_temp_fd = _asset_temp_path(
                native_fd,
                suffix=suffix,
            )
            preview_temp_name, preview_temp_fd = _asset_temp_path(
                preview_fd,
                suffix=".jpg",
            )
            _copy_source_to_fd(source, native_temp_fd)
            if _sha256_fd(native_temp_fd) != digest:
                raise ValueError(
                    f"{source.name}: source changed while preparing image asset"
                )
            os.lseek(native_temp_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(native_temp_fd), "rb") as native_fh:
                with Image.open(native_fh) as image:
                    frame_count = int(getattr(image, "n_frames", 1))
                    if frame_count != 1:
                        raise ValueError(
                            f"{source.name}: multi-frame images are not silently "
                            "truncated; export each frame as a separate image"
                        )
                    exif_orientation = int(image.getexif().get(274, 1) or 1)
                    width, height = image.size
                    if width * height > _MAX_IMAGE_PIXELS:
                        raise ValueError(
                            f"{source.name}: exceeds "
                            f"PAPERCONAN_MAX_IMAGE_PIXELS={_MAX_IMAGE_PIXELS}"
                        )
                    display_image = ImageOps.exif_transpose(image)
                    try:
                        os.lseek(preview_temp_fd, 0, os.SEEK_SET)
                        with os.fdopen(os.dup(preview_temp_fd), "wb") as preview_fh:
                            _write_preview(display_image, preview_fh)
                    finally:
                        if display_image is not image:
                            display_image.close()
                    mime = (
                        Image.MIME.get(image.format)
                        or mimetypes.guess_type(native.name)[0]
                    )
            _publish_asset_pair(
                output_root=output_root,
                root_fd=root_fd,
                images_fd=images_fd,
                native_fd=native_fd,
                preview_fd=preview_fd,
                native_temp_name=native_temp_name,
                preview_temp_name=preview_temp_name,
                native_temp_fd=native_temp_fd,
                preview_temp_fd=preview_temp_fd,
                native_name=native_name,
                preview_name=preview_name,
            )
        except _AssetPublicationRollbackError:
            raise
        except Exception:
            _cleanup_incomplete_pair(
                native_fd,
                native_name,
                preview_fd,
                preview_name,
            )
            raise
        finally:
            if native_temp_fd is not None:
                os.close(native_temp_fd)
            if preview_temp_fd is not None:
                os.close(preview_temp_fd)
            _unlink_at(native_temp_name, native_fd)
            _unlink_at(preview_temp_name, preview_fd)
    return {
        "asset_id": asset_id,
        "file": source.name,
        "source_files": [source.name],
        "path": _relative(native, output_root),
        "preview_path": _relative(preview, output_root),
        "preview_mime": "image/jpeg",
        "source_type": source_type,
        "source_url": source_url,
        "parent_file": parent_file,
        "page": page,
        "render_dpi": render_dpi,
        "figure_label": None,
        "sha256": digest,
        "width": width,
        "height": height,
        "exif_orientation": exif_orientation,
        "mime": mime or "application/octet-stream",
    }


def _exception_text(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


def _render_pdf_pages(pdf_path: Path, temp_dir: Path):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ImageDependencyError(
            'PDF image rendering requires `pip install "paperconan[image]"`'
        ) from exc
    doc = pdfium.PdfDocument(str(pdf_path))
    scale = _PDF_DPI / 72.0
    try:
        page_count = len(doc)
        for index in range(page_count):
            page = None
            bitmap = None
            image = None
            dest = None
            page_error = None
            try:
                page = doc[index]
                width, height = page.get_size()
                pixel_width = math.ceil(width * scale)
                pixel_height = math.ceil(height * scale)
                if pixel_width * pixel_height > _MAX_IMAGE_PIXELS:
                    raise ValueError(
                        f"{pdf_path.name} page {index + 1}: exceeds "
                        f"PAPERCONAN_MAX_IMAGE_PIXELS={_MAX_IMAGE_PIXELS}"
                    )
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                dest = temp_dir / f"{pdf_path.stem}.p{index + 1}.png"
                image.save(dest, format="PNG")
            except Exception as exc:
                page_error = _exception_text(exc)
            finally:
                for resource in (image, bitmap, page):
                    if resource is None:
                        continue
                    try:
                        resource.close()
                    except Exception as exc:
                        if page_error is None:
                            page_error = _exception_text(exc)
                if page_error is not None and dest is not None:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception as exc:
                        cleanup_error = _exception_text(exc)
                        page_error = (
                            f"{page_error}; partial page cleanup failed: "
                            f"{cleanup_error}"
                        )
            yield (
                index + 1,
                page_count,
                None if page_error is not None else dest,
                page_error,
            )
    finally:
        doc.close()


def _stable_name_key(name: str) -> tuple[str, str]:
    return name.casefold(), name


def _asset_limit_error() -> str:
    return (
        "image asset limit reached; set "
        "PAPERCONAN_MAX_IMAGE_ASSETS to raise"
    )


def prepare_image_assets(
    in_dir: str,
    out_dir: str,
    *,
    provenance: dict | None = None,
    render_pdf: bool = True,
) -> tuple[list[dict], list[dict]]:
    source_root = Path(in_dir)
    output_root = Path(out_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    downloads = _source_provenance(source_root)
    downloads.update(_provided_provenance(provenance))
    candidates = sorted(
        [
            path for path in source_root.iterdir()
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        ],
        key=lambda path: _stable_name_key(path.name),
    )
    pdfs = sorted(
        [
            path for path in source_root.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ],
        key=lambda path: _stable_name_key(path.name),
    )
    assets_by_digest: dict[str, dict] = {}
    errors: list[dict] = []
    pdf_page_attempts = 0

    def add(path: Path, **metadata):
        try:
            if path.stat().st_size > _MAX_IMAGE_BYTES:
                raise ValueError(
                    f"{path.name}: exceeds PAPERCONAN_MAX_IMAGE_MB={_MAX_IMAGE_MB:g}"
                )
            digest = _sha256(path)
        except Exception as exc:
            errors.append({"file": path.name, "error": str(exc)})
            return "error"
        existing = assets_by_digest.get(digest)
        if existing is not None:
            existing["source_files"] = sorted(
                set(existing["source_files"] + [path.name]),
                key=_stable_name_key,
            )
            return "duplicate"
        if len(assets_by_digest) >= _MAX_IMAGE_ASSETS:
            errors.append({"file": path.name, "error": _asset_limit_error()})
            return "limit"
        try:
            asset = _record_image(
                path,
                output_root,
                digest=digest,
                **metadata,
            )
        except Exception as exc:
            errors.append({"file": path.name, "error": str(exc)})
            return "error"
        assets_by_digest[digest] = asset
        return "added"

    for path in candidates:
        prov = downloads.get(path.name) or {}
        add(
            path,
            source_type="fetched_image" if prov.get("source_url") else "local_image",
            source_url=prov.get("source_url"),
        )
    if render_pdf:
        with tempfile.TemporaryDirectory(
            prefix=".paperconan-rendered-",
            dir=output_root,
        ) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            for pdf in pdfs:
                try:
                    if pdf.stat().st_size > _MAX_IMAGE_BYTES:
                        raise ValueError(
                            f"{pdf.name}: exceeds "
                            f"PAPERCONAN_MAX_IMAGE_MB={_MAX_IMAGE_MB:g}"
                        )
                except Exception as exc:
                    errors.append({"file": pdf.name, "error": str(exc)})
                    continue
                if pdf_page_attempts >= _MAX_IMAGE_ASSETS:
                    errors.append({
                        "file": pdf.name,
                        "page": 1,
                        "error": _asset_limit_error(),
                    })
                    continue
                if len(assets_by_digest) >= _MAX_IMAGE_ASSETS:
                    errors.append({"file": pdf.name, "error": _asset_limit_error()})
                    continue
                pages = _render_pdf_pages(pdf, temp_dir)
                try:
                    for page_number, page_count, page_path, page_error in pages:
                        pdf_page_attempts += 1
                        if page_error is not None:
                            errors.append({
                                "file": pdf.name,
                                "page": page_number,
                                "error": page_error,
                            })
                        else:
                            try:
                                add(
                                    page_path,
                                    source_type="pdf_page",
                                    source_url=(
                                        downloads.get(pdf.name) or {}
                                    ).get("source_url"),
                                    parent_file=pdf.name,
                                    page=page_number,
                                    render_dpi=_PDF_DPI,
                                )
                            finally:
                                page_path.unlink(missing_ok=True)
                        if (
                            (
                                pdf_page_attempts >= _MAX_IMAGE_ASSETS
                                or len(assets_by_digest) >= _MAX_IMAGE_ASSETS
                            )
                            and page_number < page_count
                        ):
                            errors.append({
                                "file": pdf.name,
                                "page": page_number + 1,
                                "error": _asset_limit_error(),
                            })
                            break
                except Exception as exc:
                    errors.append({"file": pdf.name, "error": str(exc)})
                finally:
                    pages.close()
    assets = sorted(
        assets_by_digest.values(),
        key=lambda asset: (asset["asset_id"], asset["file"]),
    )
    return assets, errors
