from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

from ._evidence import write_native_pair_evidence


_MIN_PANEL_SIDE = 64
_SIMILARITY_THRESHOLD = 0.92
_MAX_PANEL_CANDIDATES = 64
_MAX_PANEL_PAIR_COMPARISONS = 4096


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


def _max_image_findings() -> int:
    return max(0, int(os.environ.get("PAPERCONAN_MAX_IMAGE_FINDINGS", "200")))


def diagnose_image_assets(
    assets: list[dict],
    artifact_dir: str,
) -> tuple[list[dict], list[dict]]:
    cv2 = _cv2()
    root = Path(artifact_dir).resolve()
    candidates, errors = [], []
    for asset in sorted(assets, key=lambda item: item["asset_id"]):
        native = (root / asset["path"]).resolve()
        try:
            native.relative_to(root)
        except ValueError:
            errors.append({
                "file": asset.get("file"),
                "error": "asset path escapes artifact root",
            })
            continue
        image = cv2.imread(
            str(native),
            cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION,
        )
        if image is None:
            errors.append({
                "file": asset.get("file"),
                "error": "unable to decode registered image",
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
                if comparisons >= _MAX_PANEL_PAIR_COMPARISONS:
                    pairs_omitted = True
                    break
                box_a, box_b = boxes[left_index], boxes[right_index]
                a = image[box_a[1]:box_a[3], box_a[0]:box_a[2]]
                b = image[box_b[1]:box_b[3], box_b[0]:box_b[2]]
                comparisons += 1
                score, transform = transform_robust_similarity(a, b)
                if score < _SIMILARITY_THRESHOLD:
                    continue
                identity = {
                    "asset_ids": [asset["asset_id"]],
                    "boxes": [list(box_a), list(box_b)],
                    "method": "panel_pair_similarity",
                    "transform": transform,
                }
                finding_id = _finding_id(identity)
                candidates.append({
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
                })
            if pairs_omitted:
                break
        if pairs_omitted:
            errors.append({
                "file": asset.get("file"),
                "error": (
                    "image panel-pair comparisons omitted; "
                    f"limit is {_MAX_PANEL_PAIR_COMPARISONS}"
                ),
            })
    candidates.sort(key=lambda item: (-item["score"], item["finding_id"]))
    max_findings = _max_image_findings()
    if len(candidates) > max_findings:
        errors.append({
            "error": (
                f"{len(candidates) - max_findings} image findings omitted; "
                "set PAPERCONAN_MAX_IMAGE_FINDINGS to raise"
            )
        })
        candidates = candidates[:max_findings]

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
