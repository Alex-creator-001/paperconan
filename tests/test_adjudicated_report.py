from __future__ import annotations

import json
import subprocess
import sys

from build_fixture import build

from paperconan import scan_dir, write_adjudicated_report


def _verdict() -> dict:
    return {
        "verdict": "KEEP",
        "suspicion_tier": 1,
        "impact_scope": "supporting",
        "tier_why": "synthetic independent columns are identical",
        "drop_reason": None,
        "innocent_explanation": "source-data assembly error remains possible",
        "needs_author_data": "raw values and figure mapping",
        "review_status": "confirmed",
        "report_md": (
            "## Synthetic paper\n\n"
            "### 论文主结论\n"
            "This synthetic fixture tests report rendering.\n\n"
            "### 异常位置\n"
            "`ED_Fig1.xlsx` Sheet1 has an identical numeric column pair.\n\n"
            "### 标签含义\n"
            "The fixture labels two columns as separate measurements.\n\n"
            "### 为什么这是问题\n"
            "If independent, identical values need clarification.\n\n"
            "### 影响判断\n"
            "This is supporting evidence in a synthetic test.\n\n"
            "### 无辜解释的层次\n"
            "A duplicate export remains possible.\n\n"
            "### 需要作者澄清\n"
            "Please provide the raw source mapping.\n\n"
            "### 证据\n"
            "paperconan synthetic fixture, identical_column finding.\n"
        ),
    }


def test_write_adjudicated_report_renders_verdict_and_scan_evidence(tmp_path):
    data = tmp_path / "data"
    build(str(data))
    audit = tmp_path / "audit"
    scan = scan_dir(str(data), str(audit), write_html=False)
    out = tmp_path / "adjudication.html"

    write_adjudicated_report(scan, _verdict(), str(out))

    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "KEEP" in html
    assert "Tier 1" in html
    assert "supporting" in html
    assert "confirmed" in html
    assert "论文主结论" in html
    assert "异常位置" in html
    assert "identical_column" in html
    assert "ED_Fig1.xlsx" in html
    assert "signal, not verdict" in html
    assert "fabrication" not in html.lower()


def test_report_subcommand_writes_adjudicated_html(tmp_path):
    data = tmp_path / "data"
    build(str(data))
    audit = tmp_path / "audit"
    scan_dir(str(data), str(audit), write_html=False)
    verdict_path = tmp_path / "verdict.json"
    verdict_path.write_text(json.dumps(_verdict(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "adjudication.html"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "paperconan",
            "report",
            str(audit / "scan.json"),
            "--verdict",
            str(verdict_path),
            "--out",
            str(out),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "Synthetic paper" in html
    assert "关键证据" in html
    assert "identical_column" in html
    assert str(out) in proc.stdout
