from __future__ import annotations

import os
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


@pytest.mark.parametrize(
    "value",
    ["-1", "inf", "not-a-number", "1e10000", "1e10000000"],
)
def test_invalid_total_image_artifact_budget_fails_closed(
    tmp_path,
    monkeypatch,
    value,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    monkeypatch.setenv("PAPERCONAN_MAX_IMAGE_TOTAL_MB", value)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [{
        "error": "invalid PAPERCONAN_MAX_IMAGE_TOTAL_MB limit",
    }]
    assert not (output / "images").exists()


def test_total_image_artifact_budget_removes_rejected_staging(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    monkeypatch.setenv("PAPERCONAN_MAX_IMAGE_TOTAL_MB", "0")

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert len(errors) == 1
    assert "PAPERCONAN_MAX_IMAGE_TOTAL_MB" in errors[0]["error"]
    assert "budget exhausted" in errors[0]["error"]
    assert not list((output / "images").rglob("*.*"))


def test_total_image_artifact_budget_credits_rerun_replacements(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    first, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    total = sum(
        (output / first[0][key]).stat().st_size
        for key in ("path", "preview_path")
    )
    monkeypatch.setenv(
        "PAPERCONAN_MAX_IMAGE_TOTAL_MB",
        str(total / (1024 * 1024)),
    )
    _image(source / "FigB.png", color=(180, 60, 20))

    second, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert second == first
    assert len(rerun_errors) == 1
    assert rerun_errors[0]["file"] == "FigB.png"
    assert "PAPERCONAN_MAX_IMAGE_TOTAL_MB" in rerun_errors[0]["error"]
    assert "budget exhausted" in rerun_errors[0]["error"]


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
    observed_temp_dirs = []
    observed_temp_files = []
    real_record_image = _assets._record_image
    synthetic_digest = "a" * 64

    def observe_and_hash(_file_fd):
        temp_dirs = list(output.glob(".paperconan-rendered-*"))
        assert len(temp_dirs) == 1
        temp_dir = temp_dirs[0]
        opened = _assets.os.fstat(_file_fd)
        staged_files = list(temp_dir.glob("*.png"))
        if not any(
            (current := staged.stat()).st_dev == opened.st_dev
            and current.st_ino == opened.st_ino
            for staged in staged_files
        ):
            return synthetic_digest
        observed_temp_dirs.append(temp_dir)
        observed_temp_files.append(
            sorted(item.name for item in staged_files)
        )
        if staged_files[0].name == "supp.p3.png":
            return "b" * 64
        return synthetic_digest

    def reject_third_page(path, *args, **kwargs):
        if path.name == "supp.p3.png":
            raise ValueError("synthetic page rejection")
        return real_record_image(path, *args, **kwargs)

    monkeypatch.setattr(_assets, "_sha256_fd", observe_and_hash)
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
    assert len(set(observed_temp_dirs)) == 1
    temp_dir = observed_temp_dirs[0]
    assert temp_dir.parent == output
    assert temp_dir.name.startswith(".paperconan-rendered-")
    assert not temp_dir.exists()
    assert not list(output.glob(".paperconan-rendered-*"))
    with PIL.open(output / assets[0]["path"]) as native:
        assert native.size == (12, 8)
    with PIL.open(output / assets[0]["preview_path"]) as preview:
        assert preview.size == (12, 8)


def test_pdf_staging_does_not_follow_or_delete_images_symlink(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    _fake_pdfium(monkeypatch, [(72, 72)])
    output = tmp_path / "audit"
    output.mkdir()
    outside = tmp_path / "outside"
    rendered = outside / ".rendered"
    rendered.mkdir(parents=True)
    sentinel = rendered / "sentinel.txt"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    (output / "images").symlink_to(outside, target_is_directory=True)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "outside artifact root" in errors[0]["error"]
    assert (output / "images").is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "outside-sentinel"
    assert sorted(path.relative_to(outside) for path in outside.rglob("*")) == [
        Path(".rendered"),
        Path(".rendered/sentinel.txt"),
    ]
    assert not list(output.glob(".paperconan-rendered-*"))


def test_pdf_render_staging_respects_total_image_artifact_budget(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = _fake_pdfium(monkeypatch, [(72, 72)])
    output = tmp_path / "audit"
    monkeypatch.setenv("PAPERCONAN_MAX_IMAGE_TOTAL_MB", "0")

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert len(errors) == 1
    assert errors[0]["file"] == "supp.pdf"
    assert "PAPERCONAN_MAX_IMAGE_TOTAL_MB" in errors[0]["error"]
    assert events["rendered"] == [1]
    assert not list(output.glob(".paperconan-rendered-*"))
    assert not list((output / "images").rglob("*.*"))


def test_pdf_render_staging_is_created_under_pinned_root_fd(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    _fake_pdfium(monkeypatch, [(72, 72)])
    output = tmp_path / "audit"
    real_mkdir = _assets.os.mkdir
    staging_dir_fds = []

    def observe_mkdir(path, mode=0o777, *, dir_fd=None):
        if Path(path).name.startswith(".paperconan-rendered-"):
            staging_dir_fds.append(dir_fd)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(_assets.os, "mkdir", observe_mkdir)
    monkeypatch.setattr(
        _assets.os,
        "supports_dir_fd",
        frozenset(
            observe_mkdir if function is real_mkdir else function
            for function in _assets.os.supports_dir_fd
        ),
    )

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert errors == []
    assert assets
    assert staging_dir_fds
    assert all(directory_fd is not None for directory_fd in staging_dir_fds)


def test_failed_pdf_pages_consume_scan_wide_attempt_budget(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        (source / name).write_bytes(b"synthetic-pdf")
    events = {
        "documents_opened": [],
        "rendered": [],
        "pages_closed": [],
        "documents_closed": [],
    }

    class FailingPage:
        def __init__(self, pdf_name):
            self.pdf_name = pdf_name

        def get_size(self):
            return 72, 72

        def render(self, scale):
            events["rendered"].append(self.pdf_name)
            raise ValueError(f"{self.pdf_name} render failed")

        def close(self):
            events["pages_closed"].append(self.pdf_name)

    class FailingDocument:
        def __init__(self, path):
            self.pdf_name = Path(path).name
            events["documents_opened"].append(self.pdf_name)

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return FailingPage(self.pdf_name)

        def close(self):
            events["documents_closed"].append(self.pdf_name)

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(PdfDocument=FailingDocument),
    )
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 2)

    assets, errors = _assets.prepare_image_assets(
        str(source),
        str(tmp_path / "audit"),
    )

    assert assets == []
    assert errors == [
        {"file": "a.pdf", "page": 1, "error": "a.pdf render failed"},
        {"file": "b.pdf", "page": 1, "error": "b.pdf render failed"},
        {
            "file": "c.pdf",
            "page": 1,
            "error": _assets._asset_limit_error(),
        },
    ]
    assert events == {
        "documents_opened": ["a.pdf", "b.pdf"],
        "rendered": ["a.pdf", "b.pdf"],
        "pages_closed": ["a.pdf", "b.pdf"],
        "documents_closed": ["a.pdf", "b.pdf"],
    }


def test_partial_page_cleanup_failure_still_consumes_attempt_budget(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (source / name).write_bytes(b"synthetic-pdf")
    events = {
        "documents_opened": [],
        "rendered": [],
        "pages_closed": [],
        "documents_closed": [],
    }

    class FailingImage:
        def __init__(self, pdf_name):
            self.pdf_name = pdf_name

        def save(self, destination, format=None):
            destination.write(b"partial-page")
            destination.flush()
            raise ValueError(f"{self.pdf_name} save failed")

        def close(self):
            pass

    class FailingBitmap:
        def __init__(self, pdf_name):
            self.pdf_name = pdf_name

        def to_pil(self):
            return FailingImage(self.pdf_name)

        def close(self):
            pass

    class FailingPage:
        def __init__(self, pdf_name):
            self.pdf_name = pdf_name

        def get_size(self):
            return 72, 72

        def render(self, scale):
            events["rendered"].append(self.pdf_name)
            return FailingBitmap(self.pdf_name)

        def close(self):
            events["pages_closed"].append(self.pdf_name)

    class FailingDocument:
        def __init__(self, path):
            self.pdf_name = Path(path).name
            events["documents_opened"].append(self.pdf_name)

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return FailingPage(self.pdf_name)

        def close(self):
            events["documents_closed"].append(self.pdf_name)

    real_unlink_at = _assets._unlink_at

    def fail_partial_page_unlink(name, directory_fd):
        if name == "a.p1.png":
            raise OSError("cleanup failed")
        return real_unlink_at(name, directory_fd)

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(PdfDocument=FailingDocument),
    )
    monkeypatch.setattr(_assets, "_unlink_at", fail_partial_page_unlink)
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 1)
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [
        {
            "file": "a.pdf",
            "page": 1,
            "error": (
                "a.pdf save failed; partial page cleanup failed: cleanup failed"
            ),
        },
        {
            "file": "b.pdf",
            "page": 1,
            "error": _assets._asset_limit_error(),
        },
    ]
    assert events == {
        "documents_opened": ["a.pdf"],
        "rendered": ["a.pdf"],
        "pages_closed": ["a.pdf"],
        "documents_closed": ["a.pdf"],
    }
    assert not list(output.glob(".paperconan-rendered-*"))


def test_message_less_page_exception_is_page_numbered(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = {
        "rendered": 0,
        "page_closed": 0,
        "document_closed": 0,
    }

    class MessageLessPage:
        def get_size(self):
            return 72, 72

        def render(self, scale):
            events["rendered"] += 1
            raise ValueError()

        def close(self):
            events["page_closed"] += 1

    class MessageLessDocument:
        def __init__(self, path):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return MessageLessPage()

        def close(self):
            events["document_closed"] += 1

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(PdfDocument=MessageLessDocument),
    )

    assets, errors = _assets.prepare_image_assets(
        str(source),
        str(tmp_path / "audit"),
    )

    assert assets == []
    assert errors == [{
        "file": "supp.pdf",
        "page": 1,
        "error": "ValueError",
    }]
    assert events == {
        "rendered": 1,
        "page_closed": 1,
        "document_closed": 1,
    }


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


def test_asset_publication_uses_same_directory_temps_and_atomic_replace(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    replacements = []
    real_replace = os.replace

    def observe_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        src_name = Path(src).name
        dst_name = Path(dst).name
        if (
            src_name.startswith(".paperconan-image-")
            and not dst_name.startswith(".paperconan-image-")
        ):
            assert src_dir_fd is not None
            assert src_dir_fd == dst_dir_fd
            assert os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
            replacements.append(dst_name)
        real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(_assets.os, "replace", observe_replace)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert errors == []
    assert len(replacements) == 2
    assert set(replacements) == {
        Path(assets[0]["path"]).name,
        Path(assets[0]["preview_path"]).name,
    }
    assert not list((output / "images").rglob(".paperconan-image-*"))


def test_rerun_repairs_stale_native_and_preview_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    assets, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    native = output / assets[0]["path"]
    preview = output / assets[0]["preview_path"]
    native.write_bytes(b"stale-native")
    preview.write_bytes(b"stale-preview")

    rerun_assets, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert rerun_errors == []
    assert rerun_assets == assets
    assert native.read_bytes() == (source / "FigA.png").read_bytes()
    with PIL.open(preview) as repaired_preview:
        assert repaired_preview.size == (80, 60)


def test_rerun_replaces_final_asset_symlinks_without_touching_targets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    assets, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    native = output / assets[0]["path"]
    preview = output / assets[0]["preview_path"]
    native_target = tmp_path / "native-target"
    preview_target = tmp_path / "preview-target"
    native_target.write_bytes(b"native-sentinel")
    preview_target.write_bytes(b"preview-sentinel")
    native.unlink()
    preview.unlink()
    native.symlink_to(native_target)
    preview.symlink_to(preview_target)

    rerun_assets, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert rerun_errors == []
    assert rerun_assets == assets
    assert native_target.read_bytes() == b"native-sentinel"
    assert preview_target.read_bytes() == b"preview-sentinel"
    assert not native.is_symlink()
    assert not preview.is_symlink()
    assert native.read_bytes() == (source / "FigA.png").read_bytes()


@pytest.mark.parametrize(
    "unsafe_dir",
    ["images", "images/native", "images/preview"],
)
def test_output_asset_directory_symlink_outside_artifact_root_is_rejected(
    tmp_path,
    unsafe_dir,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe_path = output / unsafe_dir
    unsafe_path.parent.mkdir(parents=True, exist_ok=True)
    unsafe_path.symlink_to(outside, target_is_directory=True)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "outside artifact root" in errors[0]["error"]
    assert list(outside.iterdir()) == []


def test_output_artifact_root_symlink_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "audit"
    output.symlink_to(outside, target_is_directory=True)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "artifact root" in errors[0]["error"]
    assert list(outside.iterdir()) == []


def test_asset_preparation_pins_root_across_all_assets(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    _image(source / "FigB.png", color=(180, 60, 20))
    output = tmp_path / "audit"
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = tmp_path / "displaced-audit"
    real_record_image = _assets._record_image
    recorded = 0

    def record_then_swap(*args, **kwargs):
        nonlocal recorded
        result = real_record_image(*args, **kwargs)
        recorded += 1
        if recorded == 1:
            output.rename(displaced)
            output.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(_assets, "_record_image", record_then_swap)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets
    assert errors
    assert any("artifact root" in item["error"] for item in errors)
    assert list(outside.iterdir()) == []
    assert (displaced / assets[0]["path"]).is_file()


def test_failed_source_validation_leaves_no_published_asset_pair(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.png").write_bytes(b"not-an-image")
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    native_dir = output / "images" / "native"
    preview_dir = output / "images" / "preview"
    assert not native_dir.exists() or list(native_dir.iterdir()) == []
    assert not preview_dir.exists() or list(preview_dir.iterdir()) == []


def test_failed_preview_generation_leaves_no_published_asset_pair(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"

    def reject_preview(*args, **kwargs):
        raise ValueError("synthetic preview rejection")

    monkeypatch.setattr(_assets, "_write_preview", reject_preview)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [{
        "file": "FigA.png",
        "error": "synthetic preview rejection",
    }]
    native_dir = output / "images" / "native"
    preview_dir = output / "images" / "preview"
    assert not native_dir.exists() or list(native_dir.iterdir()) == []
    assert not preview_dir.exists() or list(preview_dir.iterdir()) == []


@pytest.mark.parametrize("swapped_dir", ["native", "preview"])
def test_directory_swap_after_validation_does_not_publish_outside_root(
    tmp_path,
    monkeypatch,
    swapped_dir,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_write_preview = _assets._write_preview

    def write_preview_then_swap(image, destination, *args, **kwargs):
        real_write_preview(image, destination, *args, **kwargs)
        directory = output / "images" / swapped_dir
        moved = outside / swapped_dir
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)

    monkeypatch.setattr(_assets, "_write_preview", write_preview_then_swap)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "changed during asset publication" in errors[0]["error"]
    assert list((outside / swapped_dir).iterdir()) == []


def test_staging_path_swap_does_not_publish_symlink_or_touch_target(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    outside_target = tmp_path / "outside-target"
    outside_target.write_bytes(b"outside-sentinel")
    real_write_preview = _assets._write_preview

    def write_preview_then_swap_staging(image, destination, *args, **kwargs):
        real_write_preview(image, destination, *args, **kwargs)
        native_dir = output / "images" / "native"
        staged_native = next(native_dir.glob(".paperconan-image-*.png"))
        staged_native.unlink()
        staged_native.symlink_to(outside_target)

    monkeypatch.setattr(
        _assets,
        "_write_preview",
        write_preview_then_swap_staging,
    )

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "changed during asset publication" in errors[0]["error"]
    assert outside_target.read_bytes() == b"outside-sentinel"
    assert not list((output / "images").rglob("img-*"))


def test_final_path_swap_after_replace_is_detected_and_rolled_back(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "FigA.png"
    _image(image_path)
    digest = _assets._sha256(image_path)
    stem = _assets._asset_id(digest).replace(":", "-")
    native_name = f"{stem}.png"
    outside_target = tmp_path / "outside-target"
    outside_target.write_bytes(b"outside-sentinel")
    real_replace = os.replace
    swapped = False

    def replace_then_swap_final(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal swapped
        real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if (
            not swapped
            and Path(src).name.startswith(".paperconan-image-")
            and Path(dst).name == native_name
        ):
            swapped = True
            os.unlink(dst, dir_fd=dst_dir_fd)
            os.symlink(outside_target, dst, dir_fd=dst_dir_fd)

    monkeypatch.setattr(_assets.os, "replace", replace_then_swap_final)
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors
    assert "changed during asset publication" in errors[0]["error"]
    assert outside_target.read_bytes() == b"outside-sentinel"
    assert not list((output / "images").rglob("img-*"))


def test_source_mutation_after_initial_hash_publishes_nothing(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "FigA.png"
    _image(image_path)
    original_digest = _assets._sha256(image_path)
    real_sha256 = _assets._sha256
    mutated = False

    def hash_then_mutate(path):
        nonlocal mutated
        digest = real_sha256(path)
        if not mutated and path == image_path:
            mutated = True
            _image(image_path, color=(1, 2, 3))
        return digest

    monkeypatch.setattr(_assets, "_sha256", hash_then_mutate)
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert original_digest != real_sha256(image_path)
    assert assets == []
    assert errors
    assert "source changed while preparing image asset" in errors[0]["error"]
    assert not list((output / "images").rglob("img-*"))


def test_second_temp_allocation_failure_cleans_first_temp(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    real_asset_temp_path = _assets._asset_temp_path
    calls = 0

    def fail_second_temp(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic temp allocation failure")
        return real_asset_temp_path(*args, **kwargs)

    monkeypatch.setattr(_assets, "_asset_temp_path", fail_second_temp)

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [{
        "file": "FigA.png",
        "error": "synthetic temp allocation failure",
    }]
    assert not list((output / "images").rglob(".paperconan-image-*"))
    assert not list((output / "images").rglob("img-*"))


def test_second_replacement_failure_leaves_no_fresh_pair(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "FigA.png"
    _image(image_path)
    digest = _assets._sha256(image_path)
    stem = _assets._asset_id(digest).replace(":", "-")
    preview_name = f"{stem}.jpg"
    real_replace = os.replace
    failed = False

    def fail_preview_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal failed
        if not failed and Path(dst).name == preview_name:
            failed = True
            raise OSError("synthetic preview replace failure")
        return real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(_assets.os, "replace", fail_preview_replace)
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [{
        "file": "FigA.png",
        "error": "synthetic preview replace failure",
    }]
    assert not os.path.lexists(output / "images" / "native" / f"{stem}.png")
    assert not os.path.lexists(output / "images" / "preview" / preview_name)


def test_second_replacement_failure_restores_prior_symlink_pair(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    assets, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    native = output / assets[0]["path"]
    preview = output / assets[0]["preview_path"]
    native_target = tmp_path / "native-target"
    preview_target = tmp_path / "preview-target"
    native_target.write_bytes(b"native-sentinel")
    preview_target.write_bytes(b"preview-sentinel")
    native.unlink()
    preview.unlink()
    native.symlink_to(native_target)
    preview.symlink_to(preview_target)
    real_replace = os.replace
    failed = False

    def fail_preview_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal failed
        if not failed and Path(dst).name == preview.name:
            failed = True
            raise OSError("synthetic preview replace failure")
        return real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(_assets.os, "replace", fail_preview_replace)

    rerun_assets, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert rerun_assets == []
    assert rerun_errors == [{
        "file": "FigA.png",
        "error": "synthetic preview replace failure",
    }]
    assert native.is_symlink()
    assert preview.is_symlink()
    assert native.readlink() == native_target
    assert preview.readlink() == preview_target
    assert native_target.read_bytes() == b"native-sentinel"
    assert preview_target.read_bytes() == b"preview-sentinel"


def test_rollback_restore_failure_preserves_recovery_backup(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    assets, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    native = output / assets[0]["path"]
    preview = output / assets[0]["preview_path"]
    native_target = tmp_path / "native-target"
    native_target.write_bytes(b"native-sentinel")
    native.unlink()
    native.symlink_to(native_target)
    preview.write_bytes(b"preview-sentinel")
    real_replace = os.replace
    preview_install_failed = False

    def fail_install_and_native_restore(
        src,
        dst,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
    ):
        nonlocal preview_install_failed
        src_name = Path(src).name
        dst_name = Path(dst).name
        if (
            not preview_install_failed
            and src_name.endswith(".jpg")
            and dst_name == preview.name
        ):
            preview_install_failed = True
            raise OSError("synthetic preview install failure")
        if src_name.endswith(".backup") and dst_name == native.name:
            raise OSError("synthetic native restore failure")
        return real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        _assets.os,
        "replace",
        fail_install_and_native_restore,
    )

    rerun_assets, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert rerun_assets == []
    assert rerun_errors
    error = rerun_errors[0]["error"]
    assert "rollback failed" in error
    assert "synthetic native restore failure" in error
    backups = list(native.parent.glob(".paperconan-image-*.backup"))
    assert len(backups) == 1
    assert f"images/native/{backups[0].name}" in error
    assert str(tmp_path) not in error
    assert backups[0].is_symlink()
    assert backups[0].readlink() == native_target
    assert native_target.read_bytes() == b"native-sentinel"
    assert preview.read_bytes() == b"preview-sentinel"


def test_preview_failure_cleans_preexisting_partial_final_pair(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    assets, errors = _assets.prepare_image_assets(str(source), str(output))
    assert errors == []
    native = output / assets[0]["path"]
    preview = output / assets[0]["preview_path"]
    preview.unlink()
    native.write_bytes(b"partial-native")

    def reject_preview(*args, **kwargs):
        raise ValueError("synthetic preview rejection")

    monkeypatch.setattr(_assets, "_write_preview", reject_preview)

    rerun_assets, rerun_errors = _assets.prepare_image_assets(
        str(source),
        str(output),
    )

    assert rerun_assets == []
    assert rerun_errors == [{
        "file": "FigA.png",
        "error": "synthetic preview rejection",
    }]
    assert not os.path.lexists(native)
    assert not os.path.lexists(preview)


def test_secure_dirfd_capability_unavailable_is_explicit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    monkeypatch.setattr(_assets.os, "supports_dir_fd", frozenset())

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert len(errors) == 1
    assert "secure image asset publication is unavailable" in errors[0]["error"]
    assert "dir_fd" in errors[0]["error"]
    assert not (output / "images").exists()


def test_secure_dirfd_runtime_error_includes_sanitized_context(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    _image(source / "FigA.png")
    output = tmp_path / "audit"
    output.mkdir()
    original_open = _assets.os.open

    def reject_root_open(path, flags, *args, **kwargs):
        if Path(path) == output:
            raise NotImplementedError(
                "synthetic runtime failure /private/sensitive token=top-secret"
            )
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(_assets.os, "open", reject_root_open)
    monkeypatch.setattr(
        _assets.os,
        "supports_dir_fd",
        frozenset(
            reject_root_open if function is original_open else function
            for function in _assets.os.supports_dir_fd
        ),
    )

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert len(errors) == 1
    error = errors[0]["error"]
    assert "NotImplementedError" in error
    assert "synthetic runtime failure" in error
    assert "/private/sensitive" not in error
    assert "top-secret" not in error


def test_oversized_pdf_is_rejected_before_pdfium_open(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = _fake_pdfium(monkeypatch, [])
    monkeypatch.setattr(_assets, "_MAX_IMAGE_BYTES", 1)
    output = tmp_path / "audit"

    assets, errors = _assets.prepare_image_assets(str(source), str(output))

    assert assets == []
    assert errors == [{
        "file": "supp.pdf",
        "error": "supp.pdf: exceeds PAPERCONAN_MAX_IMAGE_MB=100",
    }]
    assert events["document_closed"] == 0


def test_pdf_page_attempt_limit_counts_duplicate_pages(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    events = _fake_pdfium(
        monkeypatch,
        [(72, 72), (72, 72), (72, 72), (72, 72)],
    )
    monkeypatch.setattr(_assets, "_MAX_IMAGE_ASSETS", 2)
    real_sha256 = _assets._sha256

    def duplicate_page_hash(path):
        if path.parent.name.startswith(".paperconan-rendered-"):
            return "a" * 64
        return real_sha256(path)

    monkeypatch.setattr(_assets, "_sha256", duplicate_page_hash)
    monkeypatch.setattr(_assets, "_sha256_fd", lambda _fd: "a" * 64)

    assets, errors = _assets.prepare_image_assets(
        str(source),
        str(tmp_path / "audit"),
    )

    assert len(assets) == 1
    assert assets[0]["source_files"] == ["supp.p1.png", "supp.p2.png"]
    assert errors == [{
        "file": "supp.pdf",
        "page": 3,
        "error": _assets._asset_limit_error(),
    }]
    assert events["rendered"] == [1, 2]
    assert events["page_closed"] == [1, 2]
    assert events["document_closed"] == 1


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
