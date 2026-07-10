from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import shutil
from pathlib import Path

from . import ImageDependencyError


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
_MAX_IMAGE_MB = float(os.environ.get("PAPERCONAN_MAX_IMAGE_MB", "100"))
_MAX_IMAGE_BYTES = int(_MAX_IMAGE_MB * 1024 * 1024)
_MAX_IMAGE_PIXELS = int(os.environ.get("PAPERCONAN_MAX_IMAGE_PIXELS", "100000000"))
_MAX_IMAGE_ASSETS = int(os.environ.get("PAPERCONAN_MAX_IMAGE_ASSETS", "1000"))
_PDF_DPI = 200


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


def _write_preview(image, path: Path, max_side: int = 1400) -> None:
    preview = image.copy()
    preview.thumbnail((max_side, max_side))
    if preview.mode not in ("RGB", "L"):
        preview = preview.convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(path, format="JPEG", quality=86, optimize=True)


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
    native = output_root / "images" / "native" / f"{asset_id.replace(':', '-')}{suffix}"
    native.parent.mkdir(parents=True, exist_ok=True)
    if not native.exists():
        shutil.copyfile(source, native)
    with Image.open(native) as image:
        frame_count = int(getattr(image, "n_frames", 1))
        if frame_count != 1:
            raise ValueError(
                f"{source.name}: multi-frame images are not silently truncated; "
                "export each frame as a separate image"
            )
        exif_orientation = int(image.getexif().get(274, 1) or 1)
        width, height = image.size
        if width * height > _MAX_IMAGE_PIXELS:
            raise ValueError(
                f"{source.name}: exceeds PAPERCONAN_MAX_IMAGE_PIXELS={_MAX_IMAGE_PIXELS}"
            )
        display_image = ImageOps.exif_transpose(image)
        preview = (
            output_root / "images" / "preview"
            / f"{asset_id.replace(':', '-')}.jpg"
        )
        _write_preview(display_image, preview)
        mime = Image.MIME.get(image.format) or mimetypes.guess_type(native.name)[0]
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
            page = doc[index]
            bitmap = None
            image = None
            try:
                width, height = page.get_size()
                pixel_width = math.ceil(width * scale)
                pixel_height = math.ceil(height * scale)
                if pixel_width * pixel_height > _MAX_IMAGE_PIXELS:
                    yield (
                        index + 1,
                        page_count,
                        None,
                        (
                            f"{pdf_path.name} page {index + 1}: exceeds "
                            f"PAPERCONAN_MAX_IMAGE_PIXELS={_MAX_IMAGE_PIXELS}"
                        ),
                    )
                    continue
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                dest = temp_dir / f"{pdf_path.stem}.p{index + 1}.png"
                dest.parent.mkdir(parents=True, exist_ok=True)
                image.save(dest, format="PNG")
                result = (index + 1, page_count, dest, None)
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
            yield result
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
        temp_dir = output_root / "images" / ".rendered"
        try:
            for pdf in pdfs:
                if len(assets_by_digest) >= _MAX_IMAGE_ASSETS:
                    errors.append({"file": pdf.name, "error": _asset_limit_error()})
                    continue
                pages = _render_pdf_pages(pdf, temp_dir)
                try:
                    for page_number, page_count, page_path, page_error in pages:
                        if page_error:
                            errors.append({
                                "file": pdf.name,
                                "page": page_number,
                                "error": page_error,
                            })
                            continue
                        try:
                            add(
                                page_path,
                                source_type="pdf_page",
                                source_url=(downloads.get(pdf.name) or {}).get("source_url"),
                                parent_file=pdf.name,
                                page=page_number,
                                render_dpi=_PDF_DPI,
                            )
                        finally:
                            page_path.unlink(missing_ok=True)
                        if (
                            len(assets_by_digest) >= _MAX_IMAGE_ASSETS
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
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    assets = sorted(
        assets_by_digest.values(),
        key=lambda asset: (asset["asset_id"], asset["file"]),
    )
    return assets, errors
