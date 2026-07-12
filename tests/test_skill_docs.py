from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

from paperconan._neutral_language import contains_blocked_language


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
    assert "review priority labels" in text
    assert "not author-intent conclusions" in text


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

    assert "不是作者意图判断" in readme


def _python_comments_and_docstrings(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    comments = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    tree = ast.parse(source, filename=str(path))
    docstrings = [
        value
        for node in ast.walk(tree)
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
        if (value := ast.get_docstring(node, clean=False)) is not None
    ]
    return "\n".join([*comments, *docstrings])


def _python_product_text(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    comments_and_docstrings = _python_comments_and_docstrings(path)
    tree = ast.parse(source, filename=str(path))
    runtime_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return "\n".join([comments_and_docstrings, *runtime_strings])


def test_tracked_product_surfaces_follow_neutral_language_policy() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    violations = []
    for relative in tracked:
        path = ROOT / relative
        if relative in {"README.md", "pyproject.toml"} or relative.startswith(
            ("docs/", "examples/", "skills/")
        ):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        elif relative.endswith(".py") and relative.startswith("src/"):
            text = _python_product_text(path)
        elif relative.endswith(".py") and relative.startswith("tests/"):
            text = _python_comments_and_docstrings(path)
        else:
            continue
        if contains_blocked_language(text):
            violations.append(relative)
    assert violations == []


@pytest.mark.parametrize(
    "text",
    [
        "fr" + "aud",
        "fr" + "audulent",
        "de" + "frauded",
        "fabri" + "cate",
        "fabri" + "cated",
        "fabri" + "cation",
        "fa" + "ke",
        "fa" + "ked",
        "fa" + "king",
        "fal" + "sify",
        "fal" + "sified",
        "fal" + "sification",
        "mis" + "conduct",
        "mis" + "conducted",
        "guil" + "t",
        "guil" + "ty",
        "造" + "假",
        "伪" + "造",
        "捏" + "造",
        "作" + "假",
        "fr" + "audster",
        "de" + "frauder",
    ],
)
def test_neutral_language_matcher_blocks_expression_families(text: str) -> None:
    assert contains_blocked_language(f"prefix {text} suffix")


@pytest.mark.parametrize(
    "text",
    [
        "statistical signal",
        "data inconsistency",
        "request for clarification",
        "fabric",
        "microfabrication",
        "falsifiable hypothesis",
        "fakeroot package",
        "misconductance",
        "guiltless",
    ],
)
def test_neutral_language_matcher_allows_unrelated_words(text: str) -> None:
    assert not contains_blocked_language(text)


def test_image_budget_lock_scope_is_documented() -> None:
    cli = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")

    assert "PaperConan writers" in cli
    assert "external writers that ignore the lock" in cli
    assert "observed external changes" in cli


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
