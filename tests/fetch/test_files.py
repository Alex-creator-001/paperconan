from paperconan.fetch import _files


def test_ext_of_lowercases_and_strips_dot():
    assert _files.ext_of("Data Sheet 1.XLSX") == "xlsx"
    assert _files.ext_of("table.csv") == "csv"
    assert _files.ext_of("readme") == ""


def test_is_tabular():
    assert _files.is_tabular("a.xlsx")
    assert _files.is_tabular("b.CSV")
    assert _files.is_tabular("c.tsv")
    assert not _files.is_tabular("d.zip")
    assert not _files.is_tabular("e.inp")


def test_make_fileref():
    ref = _files.make_fileref("t.csv", 1234, "https://x/t.csv")
    assert ref == {"name": "t.csv", "ext": "csv", "size": 1234, "download_url": "https://x/t.csv"}
