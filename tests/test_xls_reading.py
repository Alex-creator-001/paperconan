"""Legacy .xls Source Data must be READ, not silently skipped. openpyxl cannot read .xls;
calamine (a base dependency) can. These tests pin that .xls is globbed, read, and yields the
same finding substance as the identical .xlsx — the coverage hole that let a heavily-duplicated
Nature paper's 17 .xls Source Data files go unaudited (only its 1 .xlsx was scanned)."""
from __future__ import annotations

import pytest

from paperconan._audit import scan_dir

xlwt = pytest.importorskip("xlwt")
import openpyxl  # noqa: E402


# a block with a byte-identical duplicated column (identical_column, HIGH) + a normal column
_ROWS = [
    ["mass", "vol", "copy_of_mass"],
    [1.2345, 2.5101, 1.2345],
    [1.8923, 3.7842, 1.8923],
    [2.5612, 4.9183, 2.5612],
    [3.1456, 5.2734, 3.1456],
    [3.8721, 6.5912, 3.8721],
    [4.2389, 7.1148, 4.2389],
    [4.9156, 8.3429, 4.9156],
]


def _write_xls(path):
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    for r, row in enumerate(_ROWS):
        for c, v in enumerate(row):
            ws.write(r, c, v)
    wb.save(str(path))


def _write_xlsx(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for row in _ROWS:
        ws.append(row)
    wb.save(str(path))


def _kinds(scan):
    ks = set()
    for b in scan.get("relations_blocks", []):
        for g in ("relations", "equal_pairs", "within_col", "progressions",
                  "row_pairs", "identical_after_rounding", "grim"):
            for f in b.get(g, []):
                ks.add((f.get("kind"), f.get("severity")))
    return ks


def test_xls_is_read_not_skipped(tmp_path):
    d = tmp_path / "xls"
    d.mkdir()
    _write_xls(d / "ED_Fig1.xls")
    scan = scan_dir(str(d), str(d / "out"), write_md=False, write_html=False)
    # the .xls file was read (present in scan_stats, no read error)
    files = {f["file"]: f for f in scan["scan_stats"]["files"]}
    assert "ED_Fig1.xls" in files
    assert "error" not in files["ED_Fig1.xls"], files["ED_Fig1.xls"]
    assert files["ED_Fig1.xls"].get("n_sheets", 0) >= 1
    # and its duplicated column was detected
    assert any(k == "identical_column" and s == "high" for k, s in _kinds(scan))


def test_xls_and_xlsx_same_content_same_findings(tmp_path):
    dx, dq = tmp_path / "xls", tmp_path / "xlsx"
    dx.mkdir(); dq.mkdir()
    _write_xls(dx / "ED_Fig1.xls")
    _write_xlsx(dq / "ED_Fig1.xlsx")
    s_xls = scan_dir(str(dx), str(dx / "o"), write_md=False, write_html=False)
    s_xlsx = scan_dir(str(dq), str(dq / "o"), write_md=False, write_html=False)
    assert _kinds(s_xls) == _kinds(s_xlsx), "identical content in .xls vs .xlsx must yield identical findings"


def test_xls_included_in_glob_message(tmp_path):
    # an empty dir raises, and the message must no longer claim .xls is unsupported
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(Exception) as ei:
        scan_dir(str(d), str(d / "o"), write_md=False, write_html=False)
    assert "not supported" not in str(ei.value)
    assert ".xls" in str(ei.value)
