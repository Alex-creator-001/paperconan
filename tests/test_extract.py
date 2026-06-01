"""Tests for the PDF/Word table-extraction input path (_extract).

The pure normalization (`tables_to_sheets`) carries all the real logic and is
tested without any third-party dependency. The pdfplumber / python-docx adapters
are exercised end-to-end through `scan_dir`, skipped when the optional extra is
not installed.
"""
from __future__ import annotations

import os
import sys

import pytest

from paperconan._extract import tables_to_sheets

sys.path.insert(0, os.path.dirname(__file__))


# --- pure normalization (no optional deps) ---------------------------------

def test_tables_to_sheets_coerces_numbers_keeps_text_and_names_sheets():
    raw = [("p1_t1", [["sample", "x"], ["s1", "1.5"], ["s2", "2.5"]])]
    sheets = tables_to_sheets("supp", raw)
    assert list(sheets) == ["supp!p1_t1"]
    rows = sheets["supp!p1_t1"]
    assert rows[0] == ["sample", "x"]
    assert rows[1] == ["s1", 1.5]   # numeric string -> float
    assert rows[2] == ["s2", 2.5]


def test_tables_to_sheets_pads_ragged_rows():
    raw = [("t1", [["a", "b", "c"], ["1"], ["2", "3"]])]
    rows = tables_to_sheets("d", raw)["d!t1"]
    assert all(len(r) == 3 for r in rows), "rows should be padded to the widest"
    assert rows[1] == [1, None, None]


def test_tables_to_sheets_handles_none_cells():
    raw = [("t1", [["a", "b"], ["1.5", None]])]
    rows = tables_to_sheets("d", raw)["d!t1"]
    assert rows[1] == [1.5, None]


def test_tables_to_sheets_drops_fully_empty_tables():
    raw = [("t1", [["", ""], [None, None]]), ("t2", [["v"], ["1.5"]])]
    sheets = tables_to_sheets("d", raw)
    assert list(sheets) == ["d!t2"], "a table with no content should be dropped"


# --- adapters end-to-end through scan_dir (need optional extras) -----------

def _block_kinds(res):
    kinds = set()
    for blk in res.get("relations_blocks") or []:
        for group in ("relations", "progressions", "equal_pairs",
                      "within_col", "identical_after_rounding"):
            for f in blk.get(group, []) or []:
                kinds.add(f["kind"])
    return kinds


def test_docx_table_is_scanned_and_trips_detector(tmp_path):
    docx = pytest.importorskip("docx")
    from paperconan import scan_dir

    doc = docx.Document()
    table = doc.add_table(rows=7, cols=4)
    header = ["sample", "mass", "mass_copy", "note"]
    for c, h in enumerate(header):
        table.rows[0].cells[c].text = h
    for i in range(6):
        v = round(1.1 + i * 0.7, 4)
        cells = table.rows[i + 1].cells
        cells[0].text = f"s{i}"
        cells[1].text = str(v)
        cells[2].text = str(v)   # identical to mass -> identical_column
        cells[3].text = "ok"
    data = tmp_path / "data"
    data.mkdir()
    doc.save(str(data / "supplement.docx"))

    res = scan_dir(str(data), str(tmp_path / "out"), write_html=False)
    assert res["n_files"] == 1, "the .docx should be discovered and scanned"
    assert "identical_column" in _block_kinds(res), \
        "two identical numeric columns in a Word table should trip identical_column"


def test_pdf_table_is_scanned_and_trips_detector(tmp_path):
    pytest.importorskip("pdfplumber")
    from paperconan import scan_dir

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "supp_table.pdf")
    assert os.path.exists(fixture), "run tests/build_pdf_fixture.py to (re)generate"

    data = tmp_path / "data"
    data.mkdir()
    import shutil
    shutil.copy(fixture, data / "supp_table.pdf")

    res = scan_dir(str(data), str(tmp_path / "out"), write_html=False)
    assert res["n_files"] == 1, "the .pdf should be discovered and scanned"
    assert "identical_column" in _block_kinds(res), \
        "two identical numeric columns in a PDF table should trip identical_column"


def test_pdf_sheet_names_carry_page_and_table_index():
    pytest.importorskip("pdfplumber")
    from paperconan._extract import load_pdf_tables

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "supp_table.pdf")
    sheets = load_pdf_tables(fixture)
    assert sheets, "fixture should yield at least one table"
    assert all(name.startswith("supp_table!p") for name in sheets), \
        f"sheet names should be traceable to page/table, got {list(sheets)}"
