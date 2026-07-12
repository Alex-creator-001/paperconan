from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import math
import mimetypes
import os
from pathlib import Path
import secrets
import stat
import sys

from ._budget import (
    ImageArtifactBudget,
    regular_file_size,
)


_SUPPORTED_PREVIEW_MIMES = frozenset({
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
_PNG_NATIVE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"})
_DEFAULT_MAX_IMAGE_MB = 100.0
_DEFAULT_MAX_IMAGE_PIXELS = 100_000_000


class EvidenceBudget:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self.used_bytes = 0

    def can_consume(self, size: int) -> bool:
        return size >= 0 and self.used_bytes + size <= self.max_bytes

    def consume(self, size: int) -> bool:
        if not self.can_consume(size):
            return False
        self.used_bytes += size
        return True


def _base64_encoded_size(raw_size: int) -> int:
    if raw_size < 0:
        raise ValueError("encoded evidence size cannot be negative")
    return 4 * ((raw_size + 2) // 3)


def _crop_encoding(image) -> tuple[str, str, dict]:
    if image.mode in _PNG_NATIVE_MODES:
        return ".png", "PNG", {}
    return ".tif", "TIFF", {"compression": "tiff_deflate"}


def _unlink_at(name: str | None, directory_fd: int) -> None:
    if name is None:
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _open_output_child_directory(
    parent_fd: int,
    name: str,
    display_path: Path,
) -> int:
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            f"image evidence destination cannot create {display_path}"
        ) from exc
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ValueError(
            "secure image evidence destination handling is unavailable"
        )
    try:
        return os.open(
            name,
            os.O_RDONLY | directory | nofollow,
            dir_fd=parent_fd,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            f"image evidence destination escapes artifact root: {display_path}"
        ) from exc


def _verify_directory_entry(
    parent_fd: int,
    name: str,
    child_fd: int,
    display_path: Path,
) -> None:
    opened = os.fstat(child_fd)
    try:
        current = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            f"image evidence destination changed during publication: {display_path}"
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
    ):
        raise ValueError(
            f"image evidence destination changed during publication: {display_path}"
        )


def _verify_artifact_root(root: Path, root_fd: int) -> None:
    try:
        current = os.stat(root, follow_symlinks=False)
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            "image artifact root changed during evidence publication"
        ) from exc
    opened = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
    ):
        raise ValueError(
            "image artifact root changed during evidence publication"
        )


def _verify_evidence_directories(
    root: Path,
    root_fd: int,
    images_fd: int,
    evidence_fd: int,
) -> None:
    _verify_artifact_root(root, root_fd)
    images = root / "images"
    evidence = images / "evidence"
    _verify_directory_entry(root_fd, "images", images_fd, images)
    _verify_directory_entry(images_fd, "evidence", evidence_fd, evidence)


@contextmanager
def _pinned_artifact_root(root: Path):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise ValueError(
            "secure image evidence destination handling is unavailable"
        )
    root_fd = None
    try:
        try:
            root_fd = os.open(
                root,
                os.O_RDONLY | directory | nofollow,
            )
        except (OSError, TypeError, NotImplementedError) as exc:
            raise ValueError(
                "image evidence destination escapes artifact root"
            ) from exc
        _verify_artifact_root(root, root_fd)
        yield root_fd
    finally:
        if root_fd is not None:
            os.close(root_fd)


@contextmanager
def _evidence_output_directory(root: Path, *, root_fd: int | None = None):
    images_fd = evidence_fd = None
    root_context = None
    if root_fd is None:
        root_context = _pinned_artifact_root(root)
        root_fd = root_context.__enter__()
    try:
        _verify_artifact_root(root, root_fd)
        images_fd = _open_output_child_directory(
            root_fd,
            "images",
            root / "images",
        )
        evidence_fd = _open_output_child_directory(
            images_fd,
            "evidence",
            root / "images" / "evidence",
        )
        _verify_evidence_directories(
            root,
            root_fd,
            images_fd,
            evidence_fd,
        )
        yield root_fd, images_fd, evidence_fd
    finally:
        for fd in (evidence_fd, images_fd):
            if fd is not None:
                os.close(fd)
        if root_context is not None:
            root_context.__exit__(None, None, None)


def _stage_file(evidence_fd: int) -> tuple[str, int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError(
            "secure image evidence destination handling is unavailable"
        )
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow
    for _ in range(128):
        name = f".paperconan-evidence-{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(
                name,
                flags,
                0o600,
                dir_fd=evidence_fd,
            )
        except FileExistsError:
            continue
        except (OSError, TypeError, NotImplementedError) as exc:
            raise ValueError(
                "image evidence destination changed during staging"
            ) from exc
    raise ValueError("could not allocate image evidence staging file")


def _verify_regular_file_entry(
    evidence_fd: int,
    name: str,
    file_fd: int,
) -> None:
    opened = os.fstat(file_fd)
    try:
        current = os.stat(
            name,
            dir_fd=evidence_fd,
            follow_symlinks=False,
        )
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            "image evidence staging entry changed during publication"
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
    ):
        raise ValueError(
            "image evidence staging entry changed during publication"
        )


def _stage_image(
    image,
    evidence_fd: int,
    image_format: str,
    **save_kwargs,
) -> tuple[str, int]:
    temp_name, temp_fd = _stage_file(evidence_fd)
    try:
        os.ftruncate(temp_fd, 0)
        os.lseek(temp_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(temp_fd), "wb") as fh:
            image.save(fh, format=image_format, **save_kwargs)
            fh.flush()
        _verify_regular_file_entry(
            evidence_fd,
            temp_name,
            temp_fd,
        )
        return temp_name, temp_fd
    except Exception:
        os.close(temp_fd)
        _unlink_at(temp_name, evidence_fd)
        raise


def _publish_staged_images(
    root: Path,
    root_fd: int,
    images_fd: int,
    evidence_fd: int,
    staged: list[tuple[str, int, str]],
) -> None:
    _verify_evidence_directories(
        root,
        root_fd,
        images_fd,
        evidence_fd,
    )
    for temp_name, temp_fd, final_name in staged:
        _verify_regular_file_entry(
            evidence_fd,
            temp_name,
            temp_fd,
        )
        os.replace(
            temp_name,
            final_name,
            src_dir_fd=evidence_fd,
            dst_dir_fd=evidence_fd,
        )
        _verify_regular_file_entry(
            evidence_fd,
            final_name,
            temp_fd,
        )
    _verify_evidence_directories(
        root,
        root_fd,
        images_fd,
        evidence_fd,
    )


def _max_image_bytes() -> int:
    name = "PAPERCONAN_MAX_IMAGE_MB"
    raw = os.environ.get(name, str(_DEFAULT_MAX_IMAGE_MB))
    try:
        value = float(raw)
        byte_value = value * 1024 * 1024
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name} limit") from exc
    if (
        not math.isfinite(value)
        or value < 0
        or not math.isfinite(byte_value)
        or byte_value > sys.maxsize
    ):
        raise ValueError(f"invalid {name} limit")
    return int(byte_value)


def _max_image_pixels() -> int:
    name = "PAPERCONAN_MAX_IMAGE_PIXELS"
    raw = os.environ.get(name, str(_DEFAULT_MAX_IMAGE_PIXELS))
    try:
        value = int(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name} limit") from exc
    if value < 0 or value > sys.maxsize:
        raise ValueError(f"invalid {name} limit")
    return value


def _same_opened_entry(opened: os.stat_result, current: os.stat_result) -> bool:
    return (
        opened.st_dev == current.st_dev
        and opened.st_ino == current.st_ino
        and stat.S_IFMT(opened.st_mode) == stat.S_IFMT(current.st_mode)
    )


def _verify_open_artifact_chain(
    root: Path,
    root_fd: int,
    opened_directories: list[tuple[int, str, int]],
    final_parent_fd: int,
    final_name: str,
    final_fd: int,
) -> None:
    try:
        current_root = os.stat(root, follow_symlinks=False)
        if not _same_opened_entry(os.fstat(root_fd), current_root):
            raise OSError("artifact root changed")
        for parent_fd, name, child_fd in opened_directories:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_opened_entry(os.fstat(child_fd), current):
                raise OSError("artifact directory changed")
        current_file = os.stat(
            final_name,
            dir_fd=final_parent_fd,
            follow_symlinks=False,
        )
        if not _same_opened_entry(os.fstat(final_fd), current_file):
            raise OSError("artifact file changed")
    except (OSError, TypeError, NotImplementedError) as exc:
        raise ValueError(
            "registered evidence path is not a stable regular file "
            "under artifact root"
        ) from exc


@contextmanager
def _open_artifact_regular_from_root_fd(
    root: Path,
    root_fd: int,
    relative_path: Path,
    *,
    verify_stable: bool = False,
):
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    parts = relative_path.parts
    if (
        nofollow is None
        or directory is None
        or relative_path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(
            "registered evidence path is not a stable regular file "
            "under artifact root"
        )
    directory_fds: list[int] = []
    opened_directories: list[tuple[int, str, int]] = []
    file_fd = -1
    fh = None
    try:
        try:
            _verify_artifact_root(root, root_fd)
            parent_fd = root_fd
            for part in parts[:-1]:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
                opened_directories.append((parent_fd, part, child_fd))
                parent_fd = child_fd
                directory_fds.append(child_fd)
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | nofollow,
                dir_fd=parent_fd,
            )
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("registered evidence entry is not a regular file")
            fh = os.fdopen(file_fd, "rb")
            file_fd = -1
        except (OSError, TypeError, NotImplementedError) as exc:
            raise ValueError(
                "registered evidence path is not a stable regular file "
                "under artifact root"
            ) from exc
        with fh:
            yield fh
            if verify_stable:
                _verify_open_artifact_chain(
                    root,
                    root_fd,
                    opened_directories,
                    parent_fd,
                    parts[-1],
                    fh.fileno(),
                )
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


@contextmanager
def _open_artifact_regular(
    root: Path,
    relative_path: Path,
    *,
    verify_stable: bool = False,
):
    with _pinned_artifact_root(root) as root_fd:
        with _open_artifact_regular_from_root_fd(
            root,
            root_fd,
            relative_path,
            verify_stable=verify_stable,
        ) as fh:
            yield fh


@contextmanager
def _open_registered_artifact_regular(
    artifact_dir: str | None,
    relative_path: object,
    *,
    verify_stable: bool = False,
):
    parts = _registered_relative_parts(relative_path)
    if not artifact_dir or parts is None:
        raise ValueError(
            "registered evidence path is not a stable regular file "
            "under artifact root"
        )
    root = Path(os.path.abspath(artifact_dir))
    with _open_artifact_regular(
        root,
        Path(*parts),
        verify_stable=verify_stable,
    ) as fh:
        yield fh


def _registered_relative_parts(relative_path: object) -> tuple[str, ...] | None:
    if not isinstance(relative_path, str) or not relative_path:
        return None
    if "\x00" in relative_path:
        return None
    if (
        Path(relative_path).is_absolute()
        or relative_path.startswith(("/", "\\"))
        or "\\" in relative_path
        or (len(relative_path) >= 2 and relative_path[1] == ":")
    ):
        return None
    parts = tuple(relative_path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def _registered_artifact_location(
    artifact_dir: str | None,
    relative_path: object,
) -> tuple[Path, Path, Path] | None:
    if not artifact_dir:
        return None
    parts = _registered_relative_parts(relative_path)
    if parts is None:
        return None
    root = Path(artifact_dir).resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return root, candidate, relative


def _validated_crop_box(
    box: object,
    *,
    width: int,
    height: int,
    max_pixels: int,
) -> tuple[int, int, int, int]:
    try:
        coordinates = tuple(box)
    except TypeError as exc:
        raise ValueError("crop box must contain exactly four integers") from exc
    if len(coordinates) != 4 or any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in coordinates
    ):
        raise ValueError("crop box must contain exactly four integers")
    x0, y0, x1, y1 = coordinates
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("crop box must be non-empty and within image bounds")
    if (x1 - x0) * (y1 - y0) > max_pixels:
        raise ValueError(
            "crop box exceeds PAPERCONAN_MAX_IMAGE_PIXELS="
            f"{max_pixels}"
        )
    return coordinates


def resolve_registered_path(artifact_dir: str | None, relative_path: object) -> Path | None:
    location = _registered_artifact_location(artifact_dir, relative_path)
    if location is None:
        return None
    _, candidate, _ = location
    return candidate if candidate.is_file() else None


def registered_preview_data_uri(
    asset: dict,
    artifact_dir: str | None,
    budget: EvidenceBudget,
) -> str | None:
    preview_path = asset.get("preview_path")
    parts = _registered_relative_parts(preview_path)
    if not artifact_dir or parts is None:
        return None
    try:
        with _open_registered_artifact_regular(
            artifact_dir,
            preview_path,
            verify_stable=True,
        ) as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > _max_image_bytes():
                return None
            encoded_size = _base64_encoded_size(size)
            if not budget.can_consume(encoded_size):
                return None
            try:
                from PIL import Image

                max_pixels = _max_image_pixels()
                with Image.open(fh) as image:
                    width, height = image.size
                    if (
                        width <= 0
                        or height <= 0
                        or width * height > max_pixels
                    ):
                        return None
                    image.verify()
            except Exception:
                return None
            fh.seek(0)
            payload = fh.read(size + 1)
            if len(payload) != size:
                return None
            encoded = base64.b64encode(payload).decode("ascii")
            if len(encoded) != encoded_size:
                return None
    except (OSError, ValueError):
        return None
    if not budget.consume(len(encoded)):
        return None
    metadata_mime = asset.get("preview_mime")
    mime = str(metadata_mime).strip().lower() if isinstance(metadata_mime, str) else ""
    if mime not in _SUPPORTED_PREVIEW_MIMES:
        guessed = mimetypes.guess_type(parts[-1])[0] or ""
        mime = guessed if guessed in _SUPPORTED_PREVIEW_MIMES else "image/jpeg"
    return f"data:{mime};base64,{encoded}"


def write_native_pair_evidence(
    image_path: str,
    box_a: tuple[int, int, int, int],
    box_b: tuple[int, int, int, int],
    output_root: str,
    evidence_id: str,
    artifact_budget: ImageArtifactBudget | None = None,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    from PIL import Image

    if (
        not evidence_id
        or evidence_id != Path(evidence_id).name
        or "/" in evidence_id
        or "\\" in evidence_id
    ):
        raise ValueError("evidence_id must be a single path-safe name")
    root = Path(os.path.abspath(output_root))
    budget = artifact_budget or ImageArtifactBudget.from_environment()
    source = Path(os.path.abspath(image_path))
    try:
        source_relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError("image evidence source escapes artifact root") from exc
    if not source_relative.parts:
        raise ValueError("image evidence source escapes artifact root")
    out_dir = root / "images" / "evidence"
    with _pinned_artifact_root(root) as pinned_root_fd:
        with _open_artifact_regular_from_root_fd(
            root,
            pinned_root_fd,
            source_relative,
            verify_stable=expected_sha256 is not None,
        ) as source_fh:
            if expected_sha256 is not None:
                digest = hashlib.sha256()
                for chunk in iter(lambda: source_fh.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise ValueError("registered image changed after scoring")
                source_fh.seek(0)
            with Image.open(source_fh) as image:
                max_pixels = _max_image_pixels()
                validated_a = _validated_crop_box(
                    box_a,
                    width=image.width,
                    height=image.height,
                    max_pixels=max_pixels,
                )
                validated_b = _validated_crop_box(
                    box_b,
                    width=image.width,
                    height=image.height,
                    max_pixels=max_pixels,
                )
                crop_a = image.crop(validated_a)
                crop_b = image.crop(validated_b)
        suffix_a, format_a, save_a = _crop_encoding(crop_a)
        suffix_b, format_b, save_b = _crop_encoding(crop_b)
        crop_a_path = out_dir / f"{evidence_id}-a{suffix_a}"
        crop_b_path = out_dir / f"{evidence_id}-b{suffix_b}"

        preview_a = crop_a.copy()
        preview_b = crop_b.copy()
        preview_a.thumbnail((760, 760))
        preview_b.thumbnail((760, 760))
        height = max(preview_a.height, preview_b.height)
        canvas = Image.new(
            "RGB",
            (preview_a.width + preview_b.width + 20, height),
            "white",
        )
        canvas.paste(preview_a.convert("RGB"), (0, 0))
        canvas.paste(preview_b.convert("RGB"), (preview_a.width + 20, 0))
        preview_path = out_dir / f"{evidence_id}-preview.jpg"
        with _evidence_output_directory(
            root,
            root_fd=pinned_root_fd,
        ) as directory_fds:
            root_fd, images_fd, evidence_fd = directory_fds
            budget.initialize_from_images_fd(images_fd)
            staged: list[tuple[str, int, str]] = []
            try:
                temp_name, temp_fd = _stage_image(
                    crop_a,
                    evidence_fd,
                    format_a,
                    **save_a,
                )
                staged.append((temp_name, temp_fd, crop_a_path.name))
                _verify_evidence_directories(
                    root,
                    root_fd,
                    images_fd,
                    evidence_fd,
                )
                temp_name, temp_fd = _stage_image(
                    crop_b,
                    evidence_fd,
                    format_b,
                    **save_b,
                )
                staged.append((temp_name, temp_fd, crop_b_path.name))
                temp_name, temp_fd = _stage_image(
                    canvas,
                    evidence_fd,
                    "JPEG",
                    quality=88,
                    optimize=True,
                )
                staged.append((temp_name, temp_fd, preview_path.name))
                existing_size = sum(
                    regular_file_size(evidence_fd, final_name)
                    for _, _, final_name in staged
                )
                staged_size = sum(
                    os.fstat(temp_fd).st_size
                    for _, temp_fd, _ in staged
                )
                budget.require_replacement(
                    existing_size=existing_size,
                    staged_size=staged_size,
                )
                _publish_staged_images(
                    root,
                    root_fd,
                    images_fd,
                    evidence_fd,
                    staged,
                )
                budget.commit_replacement(
                    existing_size=existing_size,
                    staged_size=staged_size,
                )
            finally:
                for temp_name, temp_fd, _ in staged:
                    os.close(temp_fd)
                    _unlink_at(temp_name, evidence_fd)
    return {
        "crop_a_path": _relative_path(crop_a_path, root),
        "crop_b_path": _relative_path(crop_b_path, root),
        "preview_path": _relative_path(preview_path, root),
    }


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
