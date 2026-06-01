"""Generate tests/fixtures/supp_table.pdf — a supplementary PDF carrying one
ruled table whose two numeric columns are byte-identical (trips identical_column).

reportlab is a test-only build dependency; the generated PDF is committed so the
test suite only needs the runtime extra (pdfplumber) to read it back. Re-run with
`python tests/build_pdf_fixture.py` if the fixture ever needs regenerating.
"""
import os

from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "fixtures", "supp_table.pdf")

# header + 6 rows; `mass` and `mass_copy` are identical -> identical_column.
_ROWS = [["sample", "mass", "mass_copy", "note"]]
for i in range(6):
    v = round(1.1 + i * 0.7, 4)
    _ROWS.append([f"s{i}", f"{v}", f"{v}", "ok"])


def build(out=OUT):
    doc = SimpleDocTemplate(out)
    table = Table(_ROWS)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    doc.build([table])
    return out


if __name__ == "__main__":
    print("wrote", build())
