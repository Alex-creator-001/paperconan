from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
import tempfile


_SUPPORTED_PREVIEW_MIMES = frozenset({
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})
_PNG_NATIVE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"})


class EvidenceBudget:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self.used_bytes = 0

    def consume(self, size: int) -> bool:
        if size < 0 or self.used_bytes + size > self.max_bytes:
            return False
        self.used_bytes += size
        return True


def _crop_encoding(image) -> tuple[str, str, dict]:
    if image.mode in _PNG_NATIVE_MODES:
        return ".png", "PNG", {}
    return ".tif", "TIFF", {"compression": "tiff_deflate"}


def _stage_image(image, final_path: Path, image_format: str, **save_kwargs) -> Path:
    fd, temp_name = tempfile.mkstemp(
        dir=final_path.parent,
        prefix=f".{final_path.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        image.save(temp_path, format=image_format, **save_kwargs)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _replace_staged_images(staged: list[tuple[Path, Path]]) -> None:
    try:
        for temp_path, final_path in staged:
            os.replace(temp_path, final_path)
    finally:
        for temp_path, _ in staged:
            temp_path.unlink(missing_ok=True)


def resolve_registered_path(artifact_dir: str | None, relative_path: object) -> Path | None:
    if not artifact_dir or not isinstance(relative_path, str) or not relative_path:
        return None
    root = Path(artifact_dir).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def registered_preview_data_uri(
    asset: dict,
    artifact_dir: str | None,
    budget: EvidenceBudget,
) -> str | None:
    path = resolve_registered_path(artifact_dir, asset.get("preview_path"))
    if path is None:
        return None
    size = path.stat().st_size
    if not budget.consume(size):
        return None
    metadata_mime = asset.get("preview_mime")
    mime = str(metadata_mime).strip().lower() if isinstance(metadata_mime, str) else ""
    if mime not in _SUPPORTED_PREVIEW_MIMES:
        guessed = mimetypes.guess_type(path.name)[0] or ""
        mime = guessed if guessed in _SUPPORTED_PREVIEW_MIMES else "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def write_native_pair_evidence(
    image_path: str,
    box_a: tuple[int, int, int, int],
    box_b: tuple[int, int, int, int],
    output_root: str,
    evidence_id: str,
) -> dict[str, str]:
    from PIL import Image

    if (
        not evidence_id
        or evidence_id != Path(evidence_id).name
        or "/" in evidence_id
        or "\\" in evidence_id
    ):
        raise ValueError("evidence_id must be a single path-safe name")
    root = Path(output_root).resolve()
    source = Path(image_path).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ValueError("image evidence source escapes artifact root") from exc
    out_dir = root / "images" / "evidence"
    try:
        out_dir.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "image evidence destination escapes artifact root"
        ) from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        crop_a = image.crop(box_a)
        crop_b = image.crop(box_b)
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
        staged = []
        try:
            staged.append((
                _stage_image(crop_a, crop_a_path, format_a, **save_a),
                crop_a_path,
            ))
            staged.append((
                _stage_image(crop_b, crop_b_path, format_b, **save_b),
                crop_b_path,
            ))
            staged.append((
                _stage_image(
                    canvas,
                    preview_path,
                    "JPEG",
                    quality=88,
                    optimize=True,
                ),
                preview_path,
            ))
            _replace_staged_images(staged)
        finally:
            for temp_path, _ in staged:
                temp_path.unlink(missing_ok=True)
    return {
        "crop_a_path": _relative_path(crop_a_path, root),
        "crop_b_path": _relative_path(crop_b_path, root),
        "preview_path": _relative_path(preview_path, root),
    }


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
