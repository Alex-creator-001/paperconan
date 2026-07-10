from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL.Image")

from paperconan import scan_dir
from paperconan import _audit
from paperconan.image import _assets
from paperconan.schema import PaperconanInputError


def _image(path: Path, size=(80, 60), color=(20, 90, 180)):
    PIL.new("RGB", size, color).save(path)


def _fake_pdfium(monkeypatch, page_sizes):
    events = {
        "rendered": [],
        "pil_closed": [],
        "bitmap_closed": [],
        "page_closed": [],
        "document_closed": 0,
    }

    class FakePILImage:
        def __init__(self, page_number):
            self.page_number = page_number
            self.image = PIL.new("RGB", (12, 8), (page_number, 20, 30))

        def save(self, path, format=None):
            self.image.save(path, format=format)

        def close(self):
            events["pil_closed"].append(self.page_number)
            self.image.close()

    class FakeBitmap:
        def __init__(self, page_number):
            self.page_number = page_number

        def to_pil(self):
            return FakePILImage(self.page_number)

        def close(self):
            events["bitmap_closed"].append(self.page_number)

    class FakePage:
        def __init__(self, page_number, size):
            self.page_number = page_number
            self.size = size

        def get_size(self):
            return self.size

        def render(self, scale):
            events["rendered"].append(self.page_number)
            return FakeBitmap(self.page_number)

        def close(self):
            events["page_closed"].append(self.page_number)

    class StubDocument:
        def __init__(self, path):
            self.pages = [
                FakePage(index, size)
                for index, size in enumerate(page_sizes, 1)
            ]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            events["document_closed"] += 1

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(PdfDocument=StubDocument),
    )
    return events


def test_prepare_image_assets_preserves_native_pixels_and_stable_ids(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png", size=(80, 60))
    _image(source / "FigB.png", size=(40, 30), color=(180, 60, 20))
    out = tmp_path / "audit"

    first, errors = _assets.prepare_image_assets(str(source), str(out))
    second, errors2 = _assets.prepare_image_assets(str(source), str(out))

    assert errors == errors2 == []
    assert [a["asset_id"] for a in first] == [a["asset_id"] for a in second]
    assert [a["file"] for a in first] == ["FigA.png", "FigB.png"]
    native = PIL.open(out / first[0]["path"])
    assert native.size == (80, 60)
    assert (out / first[0]["path"]).read_bytes() == (source / "FigA.png").read_bytes()
    assert first[0]["width"] == 80 and first[0]["height"] == 60
    assert first[0]["path"] != first[0]["preview_path"]


def test_exact_duplicate_files_are_one_asset_with_all_source_names(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "A.png")
    (source / "B.png").write_bytes((source / "A.png").read_bytes())
    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))
    assert errors == []
    assert len(assets) == 1
    assert assets[0]["source_files"] == ["A.png", "B.png"]


def test_prepare_image_assets_renders_pdf_pages(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pdf = Path("tests/fixtures/supp_table.pdf")
    (source / "supp.pdf").write_bytes(pdf.read_bytes())
    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))
    pages = [a for a in assets if a["source_type"] == "pdf_page"]
    assert errors == []
    assert pages
    assert pages[0]["parent_file"] == "supp.pdf"
    assert pages[0]["page"] == 1
    assert pages[0]["render_dpi"] == 200


def test_pdf_asset_limit_stops_later_pages_and_closes_resources(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = _fake_pdfium(monkeypatch, [(72, 72), (72, 72), (72, 72)])
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 1)

    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))

    assert len(assets) == 1
    assert any("PAPERCONAN_MAX_IMAGE_ASSETS" in item["error"] for item in errors)
    assert events == {
        "rendered": [1],
        "pil_closed": [1],
        "bitmap_closed": [1],
        "page_closed": [1],
        "document_closed": 1,
    }


def test_pdf_pixel_limit_is_checked_before_render_and_resources_close(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = _fake_pdfium(monkeypatch, [(1000, 1000)])
    monkeypatch.setattr(_assets, "_MAX_IMAGE_PIXELS", 100)

    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))

    assert assets == []
    assert any("PAPERCONAN_MAX_IMAGE_PIXELS" in item["error"] for item in errors)
    assert events == {
        "rendered": [],
        "pil_closed": [],
        "bitmap_closed": [],
        "page_closed": [1],
        "document_closed": 1,
    }


def test_pdf_rendered_temp_is_removed_after_each_registration(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    _fake_pdfium(monkeypatch, [(72, 72), (72, 72), (72, 72)])
    output = tmp_path / "audit"
    temp_dir = output / "images" / ".rendered"
    observed_temp_files = []
    real_sha256 = _assets._sha256
    real_record_image = _assets._record_image

    def observe_and_hash(path):
        observed_temp_files.append(sorted(item.name for item in temp_dir.glob("*.png")))
        if path.name in {"supp.p1.png", "supp.p2.png"}:
            return "a" * 64
        return real_sha256(path)

    def reject_third_page(path, *args, **kwargs):
        if path.name == "supp.p3.png":
            raise ValueError("synthetic page rejection")
        return real_record_image(path, *args, **kwargs)

    monkeypatch.setattr(_assets, "_sha256", observe_and_hash)
    monkeypatch.setattr(_assets, "_record_image", reject_third_page)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert observed_temp_files == [
        ["supp.p1.png"],
        ["supp.p2.png"],
        ["supp.p3.png"],
    ]
    assert len(assets) == 1
    assert assets[0]["source_files"] == ["supp.p1.png", "supp.p2.png"]
    assert errors == [{
        "file": "supp.p3.png",
        "error": "synthetic page rejection",
    }]
    assert not temp_dir.exists()
    with PIL.open(output / assets[0]["path"]) as native:
        assert native.size == (12, 8)
    with PIL.open(output / assets[0]["preview_path"]) as preview:
        assert preview.size == (12, 8)


def test_image_asset_limit_is_explicit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "A.png")
    _image(source / "B.png", color=(1, 2, 3))
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 1)
    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))
    assert len(assets) == 1
    assert any("PAPERCONAN_MAX_IMAGE_ASSETS" in e["error"] for e in errors)


def test_duplicate_is_merged_after_unique_asset_limit_is_reached(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "A.png")
    (source / "B.png").write_bytes((source / "A.png").read_bytes())
    _image(source / "C.png", color=(1, 2, 3))
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 1)

    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))

    assert len(assets) == 1
    assert assets[0]["source_files"] == ["A.png", "B.png"]
    assert [item["file"] for item in errors] == ["C.png"]


def test_multiframe_image_is_recorded_in_errors_not_silently_truncated(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    first = PIL.new("RGB", (20, 20), (1, 2, 3))
    second = PIL.new("RGB", (20, 20), (4, 5, 6))
    first.save(source / "stack.tiff", save_all=True, append_images=[second])
    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))
    assert assets == []
    assert any("multi-frame images are not silently truncated" in e["error"] for e in errors)


def test_case_tied_filenames_have_deterministic_metadata_and_error_order(
    tmp_path,
    monkeypatch,
):
    duplicate_source = tmp_path / "duplicates"
    duplicate_source.mkdir()
    _image(duplicate_source / "A.png")
    (duplicate_source / "a.PNG").write_bytes(
        (duplicate_source / "A.png").read_bytes()
    )
    error_source = tmp_path / "errors"
    error_source.mkdir()
    _image(error_source / "A.png")
    _image(error_source / "a.PNG", color=(1, 2, 3))
    real_iterdir = Path.iterdir

    def reverse_case_ties(path):
        if path == duplicate_source:
            return iter([path / "a.PNG", path / "A.png"])
        if path == error_source:
            return iter([path / "a.PNG", path / "A.png"])
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", reverse_case_ties)
    assets, errors = _assets.prepare_image_assets(
        str(duplicate_source),
        str(tmp_path / "duplicate-audit"),
    )
    assert errors == []
    assert assets[0]["file"] == "A.png"
    assert assets[0]["source_files"] == ["A.png", "a.PNG"]

    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 0)
    assets, errors = _assets.prepare_image_assets(
        str(error_source),
        str(tmp_path / "error-audit"),
    )
    assert assets == []
    assert [item["file"] for item in errors] == ["A.png", "a.PNG"]


def test_exif_orientation_changes_preview_dimensions_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = PIL.new("RGB", (40, 20), (20, 90, 180))
    exif = PIL.Exif()
    exif[274] = 6
    original.save(source / "oriented.jpg", exif=exif)
    source_bytes = (source / "oriented.jpg").read_bytes()

    assets, errors = _assets.prepare_image_assets(str(source), str(tmp_path / "audit"))

    assert errors == []
    asset = assets[0]
    assert asset["width"] == 40
    assert asset["height"] == 20
    assert asset["exif_orientation"] == 6
    assert (tmp_path / "audit" / asset["path"]).read_bytes() == source_bytes
    with PIL.open(tmp_path / "audit" / asset["preview_path"]) as preview:
        assert preview.size == (20, 40)


def test_image_only_scan_requires_opt_in_and_preserves_numeric_file_count(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")

    with pytest.raises(PaperconanInputError):
        scan_dir(str(source), str(tmp_path / "default"), write_html=False)

    scan = scan_dir(
        str(source),
        str(tmp_path / "images"),
        write_html=False,
        images=True,
    )
    assert scan["n_files"] == 0
    assert scan["n_image_source_files"] == 1
    assert scan["n_image_assets"] == 1
    assert scan["image_findings"] == []


def test_uppercase_image_only_scan_is_admitted_with_images_enabled(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FIG.PNG")

    scan = scan_dir(
        str(source),
        str(tmp_path / "audit"),
        write_html=False,
        images=True,
    )

    assert scan["n_files"] == 0
    assert scan["n_image_source_files"] == 1
    assert scan["n_image_assets"] == 1


def test_cli_rejects_image_diagnostics_without_images(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        _audit.sys,
        "argv",
        ["paperconan", str(tmp_path), "--image-diagnostics"],
    )
    with pytest.raises(SystemExit) as exc:
        _audit.main()
    assert exc.value.code == 2
    assert "--image-diagnostics requires --images" in capsys.readouterr().err
