"""Pure helpers for classifying downloadable files by extension."""
from __future__ import annotations
import os

TABULAR_EXTS = {"xlsx", "csv", "tsv"}


def ext_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lstrip(".").lower()


def is_tabular(name: str) -> bool:
    return ext_of(name) in TABULAR_EXTS


def make_fileref(name: str, size, download_url: str) -> dict:
    return {"name": name, "ext": ext_of(name),
            "size": int(size) if isinstance(size, (int, float)) else None,
            "download_url": download_url}
