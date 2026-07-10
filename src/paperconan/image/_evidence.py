from __future__ import annotations

import base64
import mimetypes
from pathlib import Path


_SUPPORTED_PREVIEW_MIMES = frozenset({
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})


class EvidenceBudget:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self.used_bytes = 0

    def consume(self, size: int) -> bool:
        if size < 0 or self.used_bytes + size > self.max_bytes:
            return False
        self.used_bytes += size
        return True


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
