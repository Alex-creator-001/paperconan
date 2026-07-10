from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "paperconan"
REF_DIR = SKILL_DIR / "references"


PUBLIC_REFS = [
    "output-schema.md",
    "detectors.md",
    "judgment-rubric.md",
    "interpretation.md",
    "adjudication-tiers.md",
    "report-templates.md",
    "adversarial-review.md",
    "batch-workflow.md",
    "case-patterns.md",
]


def test_skill_routes_all_public_references() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    for name in PUBLIC_REFS:
        assert (REF_DIR / name).exists(), f"missing reference file: {name}"
        assert f"references/{name}" in skill, f"SKILL.md does not route {name}"


def test_new_judgment_docs_keep_signal_not_verdict_boundary() -> None:
    docs = [
        REF_DIR / "adjudication-tiers.md",
        REF_DIR / "report-templates.md",
        REF_DIR / "adversarial-review.md",
        REF_DIR / "batch-workflow.md",
        REF_DIR / "case-patterns.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in docs)

    assert "signal-not-verdict" in text
    assert re.search(r"misconduct probabilit(?:y|ies)", text)
    assert "not a misconduct accusation" in text


def test_case_patterns_do_not_publish_real_paper_identifiers() -> None:
    text = (REF_DIR / "case-patterns.md").read_text(encoding="utf-8")

    doi_pattern = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
    assert not doi_pattern.search(text)
    assert "Nature" not in text
    assert "s414" not in text
    assert "s415" not in text


def test_readme_points_to_public_adjudication_docs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name in [
        "adjudication-tiers.md",
        "report-templates.md",
        "adversarial-review.md",
        "batch-workflow.md",
        "case-patterns.md",
    ]:
        assert f"skills/paperconan/references/{name}" in readme

    assert "不是造假概率" in readme


def test_skill_routes_adaptive_image_review() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    required = [
        "paperconan <input-dir> --images",
        "unavailable_no_multimodal",
        "image_assets",
        "image_findings",
        "image_refs",
        "deferred_asset_ids",
        "whole image",
        "native-pixel crop",
        "single unified report",
    ]
    for phrase in required:
        assert phrase in skill


def test_output_schema_and_report_template_document_image_contracts() -> None:
    output = (REF_DIR / "output-schema.md").read_text(encoding="utf-8")
    template = (REF_DIR / "report-templates.md").read_text(encoding="utf-8")
    for phrase in ("image_assets", "image_findings", "image_review"):
        assert phrase in output
    for phrase in ("finding_type", "image_refs", "review_status"):
        assert phrase in template


def test_deterministic_image_examples_use_two_regions_in_one_asset() -> None:
    for path in (
        REF_DIR / "output-schema.md",
        REF_DIR / "report-templates.md",
    ):
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.DOTALL)
        examples = [
            json.loads(block)
            for block in blocks
            if '"kind": "image_pair_similarity_signal"' in block
        ]
        assert examples, f"missing deterministic image example in {path.name}"
        for example in examples:
            assert example["asset_ids"] == ["img:a"]
            assert len(example["regions"]) == 2
            assert {
                region["asset_id"] for region in example["regions"]
            } == {"img:a"}


def test_image_coverage_status_normalization_is_documented() -> None:
    pattern = re.compile(
        r"unknown `image_review\.status`[^.\n]*`partial`",
        flags=re.IGNORECASE,
    )
    for path in (
        REF_DIR / "output-schema.md",
        REF_DIR / "report-templates.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert pattern.search(text), (
            f"{path.name} must document unknown coverage status normalization"
        )
