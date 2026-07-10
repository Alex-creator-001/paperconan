from __future__ import annotations

import json

import pytest

Image = pytest.importorskip("PIL.Image")

from paperconan import scan_dir, write_adjudicated_report


def test_mixed_numeric_and_image_workflow_produces_one_report(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.csv").write_text(
        "a,b\n1,2\n2,3\n3,4\n4,5\n",
        encoding="utf-8",
    )
    Image.new("RGB", (64, 48), (30, 100, 180)).save(source / "Fig1.png")
    audit = tmp_path / "audit"

    scan = scan_dir(
        str(source),
        str(audit),
        write_html=True,
        images=True,
        image_diagnostics=False,
    )
    assert (audit / "scan.json").exists()
    assert (audit / "report.html").exists()
    assert scan["relations_blocks"]
    assert len(scan["image_assets"]) == 1
    assert any(
        finding["kind"] == "constant_offset"
        for block in scan["relations_blocks"]
        for finding in block["relations"]
    )

    asset_id = scan["image_assets"][0]["asset_id"]
    verdict = {
        "title": "Synthetic mixed workflow",
        "verdict": "NEEDS_HUMAN",
        "paper_conclusion": (
            "The numeric and image material were reviewed together."
        ),
        "findings": [
            {
                "finding_type": "numeric",
                "title": "Numeric relation review",
                "finding_ref": {"kind": "constant_offset"},
                "review_status": "needs_human",
                "impact_scope": "supporting",
                "report_md": (
                    "The scanned constant offset requires source context."
                ),
            },
            {
                "finding_type": "image",
                "title": "Image review",
                "image_refs": [{"asset_id": asset_id, "label": "Fig. 1"}],
                "review_status": "unresolved",
                "impact_scope": "supporting",
                "report_md": (
                    "The available image does not provide enough context "
                    "for a conclusion."
                ),
            },
        ],
        "image_review": {
            "status": "completed",
            "reviewed_asset_ids": [],
            "unresolved_asset_ids": [asset_id],
            "unreadable_asset_ids": [],
            "deferred_asset_ids": [],
            "note": "reviewed with a multimodal Agent",
        },
    }
    verdict_path = audit / "verdict.json"
    verdict_path.write_text(
        json.dumps(verdict, indent=2),
        encoding="utf-8",
    )
    report = audit / "adjudication.html"
    write_adjudicated_report(
        scan,
        verdict,
        str(report),
        artifact_dir=str(audit),
    )
    html = report.read_text(encoding="utf-8")
    assert "Numeric relation review" in html
    assert "constant_offset" in html
    assert "Image review" in html
    assert "图像语义复核覆盖" in html
    assert "data:image/jpeg;base64," in html
    assert html.count('class="finding-block"') == 2
    assert html.count("<!DOCTYPE html>") == 1
