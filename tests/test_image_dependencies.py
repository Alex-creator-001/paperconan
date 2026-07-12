from __future__ import annotations

import builtins
import importlib

import pytest

from paperconan import scan_dir
from paperconan import _audit
from paperconan._html import write_html_report
from paperconan.image import ImageDependencyError


def _csv_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.csv").write_text(
        "a,b\n1,2\n2,3\n3,4\n",
        encoding="utf-8",
    )
    return source


def _missing(monkeypatch, *missing_names):
    real_import_module = importlib.import_module
    real_import = builtins.__import__
    missing = set(missing_names)

    def import_module(name):
        if name in missing:
            raise ImportError(f"synthetic missing dependency: {name}")
        return real_import_module(name)

    def import_hook(name, globals=None, locals=None, fromlist=(), level=0):
        if name in missing or any(
            candidate.startswith(f"{name}.")
            for candidate in missing
        ):
            raise ImportError(f"synthetic missing dependency: {name}")
        return real_import(name, globals, locals, fromlist, level)

    try:
        dependencies = real_import_module("paperconan.image._dependencies")
    except ImportError:
        monkeypatch.setattr(builtins, "__import__", import_hook)
    else:
        monkeypatch.setattr(dependencies, "_import_module", import_module)


def test_images_preflight_missing_pillow_before_processing(tmp_path, monkeypatch):
    source = _csv_source(tmp_path)
    output = tmp_path / "audit"
    _missing(monkeypatch, "PIL.Image")

    with pytest.raises(ImageDependencyError) as exc:
        scan_dir(
            str(source),
            str(output),
            write_html=False,
            images=True,
        )

    assert 'pip install "paperconan[image]"' in str(exc.value)
    assert not output.exists()


def test_pdf_render_preflight_missing_renderer_before_processing(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    output = tmp_path / "audit"
    _missing(monkeypatch, "pypdfium2")

    with pytest.raises(ImageDependencyError) as exc:
        scan_dir(
            str(source),
            str(output),
            write_html=False,
            images=True,
        )

    assert "PDF image rendering requires" in str(exc.value)
    assert 'pip install "paperconan[image]"' in str(exc.value)
    assert not output.exists()


def test_diagnostics_preflight_missing_opencv_before_partial_image_work(
    tmp_path,
    monkeypatch,
):
    source = _csv_source(tmp_path)
    output = tmp_path / "audit"
    _missing(monkeypatch, "cv2")

    with pytest.raises(ImageDependencyError) as exc:
        scan_dir(
            str(source),
            str(output),
            write_html=False,
            images=True,
            image_diagnostics=True,
        )

    assert "image diagnostics require" in str(exc.value)
    assert 'pip install "paperconan[image]"' in str(exc.value)
    assert not output.exists()


def test_pdf_renderer_is_not_required_without_pdf_sources(tmp_path, monkeypatch):
    source = _csv_source(tmp_path)
    _missing(monkeypatch, "pypdfium2")

    scan = scan_dir(
        str(source),
        str(tmp_path / "audit"),
        write_html=False,
        images=True,
    )

    assert scan["n_files"] == 1
    assert scan["image_assets"] == []


def test_numeric_only_scan_does_not_preflight_image_dependencies(
    tmp_path,
    monkeypatch,
):
    source = _csv_source(tmp_path)
    _missing(monkeypatch, "PIL.Image", "pypdfium2", "cv2")

    scan = scan_dir(
        str(source),
        str(tmp_path / "audit"),
        write_html=False,
    )

    assert scan["n_files"] == 1
    assert scan["image_assets"] == []


def test_numeric_only_report_ignores_image_dependency_and_limit_state(
    tmp_path,
    monkeypatch,
):
    _missing(monkeypatch, "PIL.Image", "PIL.ImageOps")
    monkeypatch.setenv(
        "PAPERCONAN_MAX_IMAGE_EVIDENCE_MB",
        "not-a-number",
    )
    out = tmp_path / "report.html"

    write_html_report(
        {
            "input_dir": str(tmp_path / "input"),
            "relations_blocks": [],
            "cross_sheet_findings": [],
            "image_assets": [],
            "image_findings": [],
        },
        str(out),
    )

    assert out.exists()


def test_cli_exits_nonzero_with_image_install_guidance(
    tmp_path,
    monkeypatch,
):
    source = _csv_source(tmp_path)
    _missing(monkeypatch, "PIL.Image")
    monkeypatch.setattr(
        _audit.sys,
        "argv",
        [
            "paperconan",
            str(source),
            "--out",
            str(tmp_path / "audit"),
            "--no-html",
            "--images",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        _audit.main()

    assert exc.value.code != 0
    assert 'pip install "paperconan[image]"' in str(exc.value.code)


def test_scan_propagates_late_pdf_dependency_error(tmp_path, monkeypatch):
    from paperconan.image import _assets, _dependencies

    source = tmp_path / "source"
    source.mkdir()
    (source / "supp.pdf").write_bytes(b"synthetic-pdf")
    error = ImageDependencyError(
        'PDF image rendering requires `pip install "paperconan[image]"`'
    )

    def unavailable(*args, **kwargs):
        raise error
        yield

    monkeypatch.setattr(
        _dependencies,
        "preflight_image_dependencies",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        _assets,
        "preflight_image_dependencies",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(_assets, "_render_pdf_pages", unavailable)

    with pytest.raises(ImageDependencyError) as exc:
        scan_dir(
            str(source),
            str(tmp_path / "audit"),
            write_html=False,
            images=True,
        )

    assert exc.value is error
