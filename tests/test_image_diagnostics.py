from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
import pytest

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("cv2")

from paperconan.image._assets import prepare_image_assets
from paperconan.image import _diagnostics
from paperconan.image._diagnostics import diagnose_image_assets
from paperconan.image._evidence import write_native_pair_evidence


def _two_panel(path: Path):
    left = np.zeros((120, 140, 3), dtype=np.uint8)
    yy, xx = np.indices(left.shape[:2])
    left[:, :, 0] = (xx * 3 + yy * 5) % 255
    left[:, :, 1] = (xx * 7) % 255
    left[:, :, 2] = (yy * 11) % 255
    right = np.fliplr(left)
    canvas = np.full((140, 310, 3), 255, dtype=np.uint8)
    canvas[10:130, 10:150] = left
    canvas[10:130, 160:300] = right
    Image.fromarray(canvas).save(path)


def test_diagnostics_find_transform_related_panels_and_keep_assets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, errors = prepare_image_assets(str(source), str(out))
    before = [dict(asset) for asset in assets]

    findings, diagnostic_errors = diagnose_image_assets(assets, str(out))

    assert errors == diagnostic_errors == []
    assert assets == before
    assert findings
    finding = findings[0]
    assert finding["kind"] == "image_pair_similarity_signal"
    assert finding["transform"] == "flip"
    assert finding["score"] >= 0.92
    assert len(finding["regions"]) == 2


def test_cmyk_scan_diagnostics_never_abort_or_remove_assets(tmp_path):
    from paperconan import scan_dir

    template = tmp_path / "template.png"
    _two_panel(template)
    source = tmp_path / "source"
    source.mkdir()
    with Image.open(template) as image:
        image.convert("CMYK").save(source / "Fig1.jpg", quality=95)

    scan = scan_dir(
        str(source),
        str(tmp_path / "audit"),
        write_html=False,
        images=True,
        image_diagnostics=True,
    )

    assert scan["n_image_assets"] == 1
    assert len(scan["image_assets"]) == 1
    assert scan["image_findings"] or scan["scan_errors"]


def test_candidate_evidence_write_error_is_non_gating(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))

    def unavailable(*args, **kwargs):
        raise OSError("synthetic evidence write error")

    monkeypatch.setattr(_diagnostics, "write_native_pair_evidence", unavailable)

    findings, errors = diagnose_image_assets(assets, str(out))

    assert findings == []
    assert errors == [{
        "file": "Fig1.png",
        "error": "image evidence unavailable: synthetic evidence write error",
    }]


def test_scan_catches_unexpected_diagnostic_error(tmp_path, monkeypatch):
    from paperconan import scan_dir

    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic diagnostic error")

    monkeypatch.setattr(_diagnostics, "diagnose_image_assets", unavailable)

    scan = scan_dir(
        str(source),
        str(tmp_path / "audit"),
        write_html=False,
        images=True,
        image_diagnostics=True,
    )

    assert len(scan["image_assets"]) == 1
    assert scan["image_findings"] == []
    assert scan["scan_errors"] == [{
        "error": "optional image diagnostics unavailable: synthetic diagnostic error",
    }]


def test_native_evidence_crops_are_not_resized(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    findings, _ = diagnose_image_assets(assets, str(out))
    evidence = findings[0]["evidence"]
    for region, key in zip(findings[0]["regions"], ("crop_a_path", "crop_b_path")):
        image = Image.open(out / evidence[key])
        x0, y0, x1, y1 = region["box"]
        assert image.size == (x1 - x0, y1 - y0)
    preview = Image.open(out / evidence["preview_path"])
    assert preview.width <= 1600


def test_native_evidence_preserves_cmyk_crop_mode_and_channels(tmp_path):
    template = tmp_path / "template.png"
    _two_panel(template)
    source = tmp_path / "source"
    source.mkdir()
    with Image.open(template) as image:
        image.convert("CMYK").save(source / "Fig1.jpg", quality=95)
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    native = out / assets[0]["path"]
    box_a = (10, 10, 150, 130)
    box_b = (160, 10, 300, 130)

    evidence = write_native_pair_evidence(
        str(native),
        box_a,
        box_b,
        str(out),
        "image-pair-cmyk",
    )

    with Image.open(native) as image:
        expected_crops = [
            image.crop(box_a).copy(),
            image.crop(box_b).copy(),
        ]
    for key, expected in zip(
        ("crop_a_path", "crop_b_path"),
        expected_crops,
    ):
        assert evidence[key].endswith(".tif")
        with Image.open(out / evidence[key]) as crop:
            assert crop.mode == expected.mode == "CMYK"
            assert crop.size == expected.size == (140, 120)
            assert np.array_equal(np.asarray(crop), np.asarray(expected))


def test_diagnostics_use_native_coordinates_for_exif_oriented_images(tmp_path):
    template = tmp_path / "template.png"
    _two_panel(template)
    source = tmp_path / "source"
    source.mkdir()
    exif = Image.Exif()
    exif[274] = 6
    with Image.open(template) as image:
        image.save(source / "Fig1.jpg", quality=95, exif=exif)
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))

    findings, errors = diagnose_image_assets(assets, str(out))

    assert errors == []
    assert findings
    for region in findings[0]["regions"]:
        x0, y0, x1, y1 = region["box"]
        assert 0 <= x0 < x1 <= assets[0]["width"]
        assert 0 <= y0 < y1 <= assets[0]["height"]


def test_native_evidence_reads_only_from_artifact_root(tmp_path):
    outside = tmp_path / "outside.png"
    _two_panel(outside)
    out = tmp_path / "audit"

    with pytest.raises(ValueError, match="artifact root"):
        write_native_pair_evidence(
            str(outside),
            (10, 10, 150, 130),
            (160, 10, 300, 130),
            str(out),
            "image-pair-test",
        )


def test_native_evidence_rejects_path_components_in_evidence_id(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))

    with pytest.raises(ValueError, match="evidence_id"):
        write_native_pair_evidence(
            str(out / assets[0]["path"]),
            (10, 10, 150, 130),
            (160, 10, 300, 130),
            str(out),
            "../../../outside",
        )


def test_native_evidence_rejects_outside_symlink_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    outside = tmp_path / "outside"
    outside.mkdir()
    (out / "images" / "evidence").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="destination escapes artifact root"):
        write_native_pair_evidence(
            str(out / assets[0]["path"]),
            (10, 10, 150, 130),
            (160, 10, 300, 130),
            str(out),
            "image-pair-symlink",
        )

    assert list(outside.iterdir()) == []


def test_native_evidence_atomically_replaces_final_symlinks_and_reruns(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    evidence_id = "image-pair-final-symlink"
    evidence_dir = out / "images" / "evidence"
    evidence_dir.mkdir()
    outside_crop = tmp_path / "outside-crop.bin"
    outside_preview = tmp_path / "outside-preview.bin"
    crop_sentinel = b"outside crop sentinel"
    preview_sentinel = b"outside preview sentinel"
    outside_crop.write_bytes(crop_sentinel)
    outside_preview.write_bytes(preview_sentinel)
    final_crop = evidence_dir / f"{evidence_id}-a.png"
    final_preview = evidence_dir / f"{evidence_id}-preview.jpg"
    final_crop.symlink_to(outside_crop)
    final_preview.symlink_to(outside_preview)

    evidence = write_native_pair_evidence(
        str(out / assets[0]["path"]),
        (10, 10, 150, 130),
        (160, 10, 300, 130),
        str(out),
        evidence_id,
    )

    assert outside_crop.read_bytes() == crop_sentinel
    assert outside_preview.read_bytes() == preview_sentinel
    assert not final_crop.is_symlink()
    assert not final_preview.is_symlink()
    final_crop.write_bytes(b"stale crop")
    final_preview.write_bytes(b"stale preview")

    rerun = write_native_pair_evidence(
        str(out / assets[0]["path"]),
        (10, 10, 150, 130),
        (160, 10, 300, 130),
        str(out),
        evidence_id,
    )

    assert rerun == evidence
    with Image.open(final_crop) as crop:
        assert crop.size == (140, 120)
    with Image.open(final_preview) as preview:
        assert preview.width <= 1600
    assert not any(path.name.startswith(".") for path in evidence_dir.iterdir())


def test_diagnostic_finding_ids_and_order_are_deterministic(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    first, _ = diagnose_image_assets(assets, str(out))
    second, _ = diagnose_image_assets(assets, str(out))
    assert [f["finding_id"] for f in first] == [f["finding_id"] for f in second]
    assert first == second


def test_zero_finding_cap_writes_no_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    assets, _ = prepare_image_assets(str(source), str(out))
    monkeypatch.setenv("PAPERCONAN_MAX_IMAGE_FINDINGS", "0")

    findings, errors = diagnose_image_assets(assets, str(out))

    assert findings == []
    assert errors == [{
        "error": (
            "1 image findings omitted; "
            "set PAPERCONAN_MAX_IMAGE_FINDINGS to raise"
        ),
    }]
    assert not (out / "images" / "evidence").exists()


def test_scan_diagnostics_never_change_asset_inventory(tmp_path):
    from paperconan import scan_dir

    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    without = scan_dir(
        str(source),
        str(tmp_path / "without"),
        write_html=False,
        images=True,
        image_diagnostics=False,
    )
    with_hints = scan_dir(
        str(source),
        str(tmp_path / "with"),
        write_html=False,
        images=True,
        image_diagnostics=True,
    )
    comparable = lambda asset: {
        key: value for key, value in asset.items()
        if key not in {"path", "preview_path"}
    }
    assert [comparable(a) for a in without["image_assets"]] == [
        comparable(a) for a in with_hints["image_assets"]
    ]
    assert without["image_findings"] == []
    assert with_hints["image_findings"]


def test_deterministic_report_embeds_registered_image_evidence(tmp_path):
    from paperconan import scan_dir

    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    scan_dir(
        str(source),
        str(out),
        write_html=True,
        images=True,
        image_diagnostics=True,
    )
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "image_pair_similarity_signal" in html
    assert "data:image/jpeg;base64," in html


def test_report_ignores_finding_evidence_path_and_uses_registered_asset(tmp_path):
    from paperconan import scan_dir
    from paperconan._html import write_html_report

    source = tmp_path / "source"
    source.mkdir()
    _two_panel(source / "Fig1.png")
    out = tmp_path / "audit"
    scan = scan_dir(
        str(source),
        str(out),
        write_html=False,
        images=True,
        image_diagnostics=True,
    )
    sentinel = b"unregistered-image-evidence-sentinel"
    (out / "arbitrary.jpg").write_bytes(sentinel)
    scan["image_findings"][0]["evidence"]["preview_path"] = "arbitrary.jpg"

    write_html_report(scan, str(out / "report.html"))

    html = (out / "report.html").read_text(encoding="utf-8")
    sentinel_b64 = base64.b64encode(sentinel).decode("ascii")
    assert sentinel_b64 not in html
    encoded = html.split("data:image/jpeg;base64,", 1)[1].split('"', 1)[0]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as preview:
        assert preview.width <= 1600
