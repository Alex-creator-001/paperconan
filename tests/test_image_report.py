from __future__ import annotations

import base64
from pathlib import Path

import pytest

from paperconan._adjudicated_html import render_adjudicated_report
from paperconan._html import _all_findings, _render_finding_card
from paperconan.schema import ImageAsset, ImageFinding, ImageReview


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _scan(tmp_path: Path) -> dict:
    preview = tmp_path / "images" / "preview" / "img-a.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(PNG_1X1)
    return {
        "tool_version": "0.test",
        "profile": "review",
        "input_dir": str(tmp_path / "input"),
        "relations_blocks": [{
            "file": "data.csv",
            "sheet": "data.csv",
            "block": {"rows": "2-4", "cols": "1-2", "header": ["a", "b"]},
            "relations": [{
                "kind": "constant_offset",
                "severity": "medium",
                "rule": "b = a + 1",
                "profile_action": "kept",
                "evidence": {"headers": ["a", "b"], "rows": []},
            }],
        }],
        "cross_sheet_findings": [],
        "image_assets": [{
            "asset_id": "img:a",
            "file": "Fig1.png",
            "path": "images/native/img-a.png",
            "preview_path": "images/preview/img-a.png",
            "source_type": "local_image",
            "source_url": None,
            "parent_file": None,
            "page": None,
            "figure_label": "Fig. 1",
            "sha256": "a" * 64,
            "width": 1,
            "height": 1,
            "mime": "image/png",
        }],
        "image_findings": [{
            "finding_id": "image:pair:1",
            "kind": "image_pair_similarity_signal",
            "severity": "medium",
            "rule": "two registered regions retain high structural similarity",
            "asset_ids": ["img:a"],
            "regions": [{"asset_id": "img:a", "box": [0, 0, 1, 1]}],
            "method": "panel_pair_similarity",
            "score": 0.97,
            "transform": "flip",
            "profile_action": "kept",
        }],
    }


def test_schema_types_are_importable():
    asset: ImageAsset = {"asset_id": "img:a", "file": "Fig1.png"}
    finding: ImageFinding = {"finding_id": "image:pair:1"}
    review: ImageReview = {"status": "partial"}
    assert asset["asset_id"] == "img:a"
    assert finding["finding_id"] == "image:pair:1"
    assert review["status"] == "partial"


def test_all_findings_includes_image_scope(tmp_path):
    items = _all_findings(_scan(tmp_path))
    image = [item for item in items if item["scope"] == "image"]
    assert len(image) == 1
    assert image[0]["finding"]["finding_id"] == "image:pair:1"


def test_image_finding_card_renders_registered_regions(tmp_path):
    item = {
        "scope": "image",
        "file": "Fig1.png",
        "sheet": "image",
        "block_rows": "native pixels",
        "block_cols": "native pixels",
        "header": [],
        "finding": _scan(tmp_path)["image_findings"][0],
    }
    html = _render_finding_card(item)
    assert "img:a" in html
    assert "[0, 0, 1, 1]" in html
    assert "score=0.97" in html
    assert "transform=flip" in html


def test_mixed_numeric_and_agent_only_image_findings_share_one_report(tmp_path):
    scan = _scan(tmp_path)
    verdict = {
        "title": "Synthetic mixed review",
        "verdict": "NEEDS_HUMAN",
        "paper_conclusion": "Numeric and image evidence require contextual review.",
        "findings": [
            {
                "finding_type": "numeric",
                "title": "Numeric relation",
                "finding_ref": {"kind": "constant_offset"},
                "review_status": "needs_human",
                "impact_scope": "supporting",
                "report_md": "A numeric relation requires clarification.",
            },
            {
                "finding_type": "image",
                "title": "Image region pair",
                "image_refs": [{
                    "asset_id": "img:a",
                    "box": [0, 0, 1, 1],
                    "label": "A",
                }],
                "review_status": "unexpected-model-token",
                "impact_scope": "supporting",
                "report_md": "The registered region is unresolved at the available scale.",
            },
        ],
        "image_review": {
            "status": "completed",
            "reviewed_asset_ids": ["img:a"],
            "unresolved_asset_ids": [],
            "unreadable_asset_ids": [],
            "deferred_asset_ids": [],
            "note": "all registered assets reviewed",
        },
    }
    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))
    assert html.count('class="finding-block"') == 2
    assert "constant_offset" in html
    assert "Image region pair" in html
    assert "data:image/png;base64," in html
    assert '<span class="badge review">unresolved</span>' in html
    assert "unexpected-model-token" not in html
    assert "completed" in html


def test_agent_only_image_finding_never_falls_back_to_numeric_evidence(tmp_path):
    scan = _scan(tmp_path)
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "finding_type": "image",
            "title": "Agent-only image observation",
            "image_refs": [{"asset_id": "missing", "box": [0, 0, 1, 1]}],
            "review_status": "needs_human",
            "report_md": "The image reference did not resolve.",
        }],
    }
    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))
    assert "图像证据引用未命中" in html
    assert "constant_offset" not in html


def test_numeric_legacy_fallback_remains_compatible(tmp_path):
    html = render_adjudicated_report(
        _scan(tmp_path),
        {"verdict": "NEEDS_HUMAN", "report_md": "Numeric review."},
        artifact_dir=str(tmp_path),
    )
    assert "constant_offset" in html


def test_completed_coverage_with_missing_assets_becomes_partial(tmp_path):
    scan = _scan(tmp_path)
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [],
        "image_review": {
            "status": "completed",
            "reviewed_asset_ids": [],
            "unresolved_asset_ids": [],
            "unreadable_asset_ids": [],
            "deferred_asset_ids": [],
        },
    }
    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))
    assert "partial" in html
    assert "completed" not in html


def test_image_finding_ref_matches_exact_finding_id(tmp_path):
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "finding_type": "image",
            "title": "Registered image signal",
            "finding_ref": {"finding_id": "image:pair:1"},
            "image_refs": [{"asset_id": "img:a"}],
            "review_status": "needs_human",
            "report_md": "The registered signal requires contextual review.",
        }],
    }
    html = render_adjudicated_report(_scan(tmp_path), verdict, artifact_dir=str(tmp_path))
    assert "image_pair_similarity_signal" in html


def test_verdict_cannot_supply_an_arbitrary_image_path(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("PRIVATE-SENTINEL", encoding="utf-8")
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "finding_type": "image",
            "title": "Image reference",
            "image_refs": [{
                "asset_id": "missing",
                "box": [0, 0, 1, 1],
                "path": str(secret),
            }],
            "review_status": "needs_human",
            "report_md": "Registered evidence is unavailable.",
        }],
    }
    html = render_adjudicated_report(_scan(tmp_path), verdict, artifact_dir=str(tmp_path))
    assert "PRIVATE-SENTINEL" not in html
    assert str(secret) not in html


def test_registered_preview_cannot_escape_artifact_root(tmp_path):
    scan = _scan(tmp_path)
    scan["image_assets"][0]["preview_path"] = "../secret.png"
    (tmp_path.parent / "secret.png").write_bytes(PNG_1X1)
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "finding_type": "image",
            "title": "Image reference",
            "image_refs": [{"asset_id": "img:a"}],
            "review_status": "needs_human",
            "report_md": "Registered evidence is unavailable.",
        }],
    }
    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))
    assert "data:image/png;base64," not in html


def test_registered_preview_mime_cannot_inject_html_attributes(tmp_path):
    scan = _scan(tmp_path)
    scan["image_assets"][0]["preview_mime"] = 'image/png" data-injected="yes'
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "finding_type": "image",
            "title": "Image reference",
            "image_refs": [{"asset_id": "img:a"}],
            "review_status": "needs_human",
            "report_md": "Registered evidence is available.",
        }],
    }
    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))
    assert "data-injected=" not in html
    assert 'src="data:image/png;base64,' in html


def test_report_shares_preview_budget_across_image_findings(tmp_path, monkeypatch):
    scan = _scan(tmp_path)
    second_preview = tmp_path / "images" / "preview" / "img-b.png"
    second_preview.write_bytes(PNG_1X1)
    scan["image_assets"].append({
        **scan["image_assets"][0],
        "asset_id": "img:b",
        "file": "Fig2.png",
        "preview_path": "images/preview/img-b.png",
        "sha256": "b" * 64,
    })
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [
            {
                "finding_type": "image",
                "title": "First image reference",
                "image_refs": [{"asset_id": "img:a"}],
                "review_status": "needs_human",
            },
            {
                "finding_type": "image",
                "title": "Second image reference",
                "image_refs": [{"asset_id": "img:b"}],
                "review_status": "needs_human",
            },
        ],
    }
    budget_mb = (len(PNG_1X1) + 0.5) / (1024 * 1024)
    monkeypatch.setenv("PAPERCONAN_MAX_IMAGE_EVIDENCE_MB", str(budget_mb))

    html = render_adjudicated_report(scan, verdict, artifact_dir=str(tmp_path))

    assert html.count("data:image/png;base64,") == 1
    assert html.count('class="image-unavailable"') == 1
    assert "First image reference" in html
    assert "Second image reference" in html


def test_non_neutral_model_text_is_rejected_without_echo(tmp_path):
    blocked = "mis" + "conduct"
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "title": "Image reference",
            "report_md": f"This text makes a {blocked} conclusion.",
        }],
    }
    with pytest.raises(ValueError) as exc:
        render_adjudicated_report(_scan(tmp_path), verdict, artifact_dir=str(tmp_path))
    assert blocked not in str(exc.value).lower()
    assert "neutral-language policy" in str(exc.value)


def test_non_neutral_markdown_split_text_is_rejected_without_echo(tmp_path):
    blocked = "mis" + "conduct"
    split = blocked[:3] + "**" + blocked[3:6] + "**" + blocked[6:]
    verdict = {
        "verdict": "NEEDS_HUMAN",
        "findings": [{
            "title": "Image reference",
            "report_md": f"This text makes a {split} conclusion.",
        }],
    }
    with pytest.raises(ValueError) as exc:
        render_adjudicated_report(_scan(tmp_path), verdict, artifact_dir=str(tmp_path))
    assert blocked not in str(exc.value).lower()
    assert "neutral-language policy" in str(exc.value)
