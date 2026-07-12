from __future__ import annotations

from bisect import bisect_left
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import numpy as np

from ._budget import ImageArtifactBudget
from ._evidence import (
    _max_image_bytes,
    _max_image_pixels,
    _open_registered_artifact_regular,
    _registered_relative_parts,
    write_native_pair_evidence,
)


_MIN_PANEL_SIDE = 64
_SIMILARITY_THRESHOLD = 0.92
_MAX_PANEL_CANDIDATES = 64
_MAX_PANEL_PAIR_COMPARISONS = 4096
_DEFAULT_MAX_IMAGE_FINDINGS = 200
_DEFAULT_MAX_IMAGE_COMPARISONS = 100_000


class _BoundedCandidates:
    def __init__(self, limit: int):
        self.limit = limit
        self.qualifying_count = 0
        self._ranked: list[tuple[tuple[float, str], dict]] = []

    def __len__(self) -> int:
        return len(self._ranked)

    @property
    def omitted_count(self) -> int:
        return self.qualifying_count - len(self._ranked)

    def note_qualifying(self) -> None:
        self.qualifying_count += 1

    def consider(self, candidate: dict) -> None:
        self.qualifying_count += 1
        if self.limit == 0:
            return
        key = (-candidate["score"], candidate["finding_id"])
        keys = [item[0] for item in self._ranked]
        index = bisect_left(keys, key)
        if index >= self.limit and len(self._ranked) >= self.limit:
            return
        self._ranked.insert(index, (key, candidate))
        if len(self._ranked) > self.limit:
            self._ranked.pop()

    def best(self) -> list[dict]:
        return [candidate for _, candidate in self._ranked]


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        from . import ImageDependencyError

        raise ImageDependencyError(
            'image diagnostics require `pip install "paperconan[image]"`'
        ) from exc
    return cv2


def _uniform_runs(values: np.ndarray) -> list[tuple[int, int]]:
    mask = values < 2.0
    runs = []
    start = None
    for index, flag in enumerate(mask.tolist() + [False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start >= 2:
                runs.append((start, index))
            start = None
    return runs


def _propose_panels_bounded(
    image: np.ndarray,
) -> tuple[list[tuple[int, int, int, int]], bool]:
    cv2 = _cv2()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    row_std = gray.std(axis=1)
    col_std = gray.std(axis=0)
    row_runs = _uniform_runs(row_std)
    col_runs = _uniform_runs(col_std)
    y_edges = [0] + [int((a + b) / 2) for a, b in row_runs] + [gray.shape[0]]
    x_edges = [0] + [int((a + b) / 2) for a, b in col_runs] + [gray.shape[1]]
    boxes = []
    omitted = False
    for y0, y1 in zip(y_edges, y_edges[1:]):
        for x0, x1 in zip(x_edges, x_edges[1:]):
            if x1 - x0 < _MIN_PANEL_SIDE or y1 - y0 < _MIN_PANEL_SIDE:
                continue
            patch = gray[y0:y1, x0:x1]
            if patch.std() >= 8:
                if len(boxes) >= _MAX_PANEL_CANDIDATES:
                    omitted = True
                    break
                boxes.append((x0, y0, x1, y1))
        if omitted:
            break
    return (
        boxes or [(0, 0, gray.shape[1], gray.shape[0])],
        omitted,
    )


def propose_panels(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    boxes, _ = _propose_panels_bounded(image)
    return boxes


def _normalized_gray(image: np.ndarray, size: int = 128) -> np.ndarray:
    cv2 = _cv2()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (size, size)).astype(np.float32)
    gray -= gray.mean()
    std = gray.std()
    return gray / std if std > 1.0 else gray


def transform_robust_similarity(
    a: np.ndarray,
    b: np.ndarray,
) -> tuple[float, str]:
    cv2 = _cv2()
    left = _normalized_gray(a)
    variants = {
        "identity": b,
        "flip": cv2.flip(b, 1),
        "rotate90": cv2.rotate(b, cv2.ROTATE_90_CLOCKWISE),
        "rotate180": cv2.rotate(b, cv2.ROTATE_180),
        "rotate270": cv2.rotate(b, cv2.ROTATE_90_COUNTERCLOCKWISE),
    }
    scored = [
        (float((left * _normalized_gray(value)).mean()), name)
        for name, value in variants.items()
    ]
    return max(scored, key=lambda item: (item[0], item[1]))


def _finding_id(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "image:pair:" + hashlib.sha256(raw).hexdigest()[:20]


def _nonnegative_int_limit(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name} limit") from exc
    if value < 0 or value > sys.maxsize:
        raise ValueError(f"invalid {name} limit")
    return value


def _max_image_findings() -> int:
    return _nonnegative_int_limit(
        "PAPERCONAN_MAX_IMAGE_FINDINGS",
        _DEFAULT_MAX_IMAGE_FINDINGS,
    )


def _max_image_comparisons() -> int:
    return _nonnegative_int_limit(
        "PAPERCONAN_MAX_IMAGE_COMPARISONS",
        _DEFAULT_MAX_IMAGE_COMPARISONS,
    )


def _registered_image_bytes(
    asset: dict,
    artifact_dir: str,
    *,
    max_bytes: int,
) -> bytes:
    try:
        with _open_registered_artifact_regular(
            artifact_dir,
            asset.get("path"),
            verify_stable=True,
        ) as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > max_bytes:
                raise ValueError(
                    "registered image exceeds PAPERCONAN_MAX_IMAGE_MB"
                )
            payload = fh.read(size + 1)
            if len(payload) != size:
                raise ValueError("unable to read complete registered image")
            return payload
    except ValueError as exc:
        message = str(exc)
        if message.startswith("registered image "):
            raise
        raise ValueError(
            "registered image path is not stable under artifact root"
        ) from exc


def _registered_image_dimensions(payload: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        from . import ImageDependencyError

        raise ImageDependencyError(
            'image diagnostics require `pip install "paperconan[image]"`'
        ) from exc
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception as exc:
        raise ValueError("unable to decode registered image") from exc


def _decode_registered_image(
    asset: dict,
    artifact_dir: str,
    cv2,
    *,
    max_bytes: int,
    max_pixels: int,
) -> tuple[np.ndarray, str]:
    payload = _registered_image_bytes(
        asset,
        artifact_dir,
        max_bytes=max_bytes,
    )
    width, height = _registered_image_dimensions(payload)
    if (
        width <= 0
        or height <= 0
        or width * height > max_pixels
    ):
        raise ValueError(
            "registered image exceeds PAPERCONAN_MAX_IMAGE_PIXELS"
        )
    encoded = np.frombuffer(payload, dtype=np.uint8)
    image = cv2.imdecode(
        encoded,
        cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION,
    )
    if image is None:
        raise ValueError("unable to decode registered image")
    return image, hashlib.sha256(payload).hexdigest()


def diagnose_image_assets(
    assets: list[dict],
    artifact_dir: str,
    *,
    artifact_budget: ImageArtifactBudget | None = None,
) -> tuple[list[dict], list[dict]]:
    try:
        budget = artifact_budget or ImageArtifactBudget.from_environment()
    except ValueError as exc:
        return [], [{"error": str(exc)}]
    cv2 = _cv2()
    try:
        max_findings = _max_image_findings()
        max_comparisons = _max_image_comparisons()
        max_image_bytes = _max_image_bytes()
        max_image_pixels = _max_image_pixels()
    except ValueError as exc:
        return [], [{"error": str(exc)}]
    root = Path(artifact_dir)
    retained = _BoundedCandidates(max_findings)
    errors = []
    scan_comparisons = 0
    scan_comparisons_omitted = False
    for asset in sorted(assets, key=lambda item: item["asset_id"]):
        relative_parts = _registered_relative_parts(asset.get("path"))
        if relative_parts is None:
            errors.append({
                "file": asset.get("file"),
                "error": "asset path escapes artifact root",
            })
            continue
        native = root.joinpath(*relative_parts)
        try:
            image, source_sha256 = _decode_registered_image(
                asset,
                artifact_dir,
                cv2,
                max_bytes=max_image_bytes,
                max_pixels=max_image_pixels,
            )
        except ValueError as exc:
            errors.append({
                "file": asset.get("file"),
                "error": str(exc),
            })
            continue
        if source_sha256 != asset.get("sha256"):
            errors.append({
                "file": asset.get("file"),
                "error": (
                    "registered image identity does not match asset manifest"
                ),
            })
            continue
        boxes, panels_omitted = _propose_panels_bounded(image)
        if panels_omitted:
            errors.append({
                "file": asset.get("file"),
                "error": (
                    "image panel candidates omitted; "
                    f"limit is {_MAX_PANEL_CANDIDATES}"
                ),
            })
        comparisons = 0
        pairs_omitted = False
        for left_index in range(len(boxes)):
            for right_index in range(left_index + 1, len(boxes)):
                if scan_comparisons >= max_comparisons:
                    scan_comparisons_omitted = True
                    break
                if comparisons >= _MAX_PANEL_PAIR_COMPARISONS:
                    pairs_omitted = True
                    break
                box_a, box_b = boxes[left_index], boxes[right_index]
                a = image[box_a[1]:box_a[3], box_a[0]:box_a[2]]
                b = image[box_b[1]:box_b[3], box_b[0]:box_b[2]]
                comparisons += 1
                scan_comparisons += 1
                score, transform = transform_robust_similarity(a, b)
                if score < _SIMILARITY_THRESHOLD:
                    continue
                if max_findings == 0:
                    retained.note_qualifying()
                    continue
                identity = {
                    "asset_ids": [asset["asset_id"]],
                    "boxes": [list(box_a), list(box_b)],
                    "method": "panel_pair_similarity",
                    "transform": transform,
                }
                finding_id = _finding_id(identity)
                retained.consider({
                    "finding_id": finding_id,
                    "kind": "image_pair_similarity_signal",
                    "severity": "medium",
                    "rule": (
                        "two registered image regions retain high structural "
                        f"similarity under {transform}"
                    ),
                    "asset_ids": [asset["asset_id"]],
                    "regions": [
                        {"asset_id": asset["asset_id"], "box": list(box_a)},
                        {"asset_id": asset["asset_id"], "box": list(box_b)},
                    ],
                    "method": "panel_pair_similarity",
                    "score": round(score, 6),
                    "transform": transform,
                    "profile_action": "kept",
                    "_native": str(native),
                    "_box_a": box_a,
                    "_box_b": box_b,
                    "_file": asset.get("file"),
                    "_source_sha256": source_sha256,
                })
            if pairs_omitted or scan_comparisons_omitted:
                break
        if pairs_omitted:
            errors.append({
                "file": asset.get("file"),
                "error": (
                    "image panel-pair comparisons omitted; "
                    f"limit is {_MAX_PANEL_PAIR_COMPARISONS}"
                ),
            })
        if scan_comparisons_omitted:
            break
    if scan_comparisons_omitted:
        errors.append({
            "error": (
                "image comparisons omitted; scan-wide limit is "
                f"{max_comparisons} (PAPERCONAN_MAX_IMAGE_COMPARISONS)"
            ),
        })
    if retained.omitted_count:
        errors.append({
            "error": (
                f"{retained.omitted_count} image findings omitted; "
                "set PAPERCONAN_MAX_IMAGE_FINDINGS to raise"
            )
        })

    candidates = retained.best()
    if candidates:
        try:
            budget.initialize_from_root(root)
        except ValueError as exc:
            errors.append({"error": str(exc)})
            return [], errors

    findings = []
    for candidate in candidates:
        evidence_id = candidate["finding_id"].replace(":", "-")
        try:
            evidence = write_native_pair_evidence(
                candidate["_native"],
                candidate["_box_a"],
                candidate["_box_b"],
                artifact_dir,
                evidence_id,
                artifact_budget=budget,
                expected_sha256=candidate["_source_sha256"],
            )
        except Exception as exc:
            errors.append({
                "file": candidate["_file"],
                "error": f"image evidence unavailable: {exc}",
            })
            continue
        finding = {
            key: value
            for key, value in candidate.items()
            if not key.startswith("_")
        }
        finding["evidence"] = evidence
        findings.append(finding)
    return findings, errors
