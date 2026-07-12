import hashlib
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from paperconan.fetch import _download


class _Resp(io.BytesIO):
    def __init__(self, data, ctype="application/octet-stream"):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def info(self): return self.headers


def test_download_file_rejects_html_error_page(monkeypatch, tmp_path):
    monkeypatch.setattr(_download.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b"<html>nope</html>", "text/html"))
    res = _download.download_file("https://x/t.xlsx", str(tmp_path / "t.xlsx"))
    assert res["ok"] is False
    assert "html" in res["skipped_reason"].lower()
    assert not (tmp_path / "t.xlsx").exists()


def test_download_file_saves_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(_download.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b"col\n1\n2\n", "text/csv"))
    dest = tmp_path / "t.csv"
    res = _download.download_file("https://x/t.csv", str(dest))
    assert res["ok"] is True
    assert dest.read_bytes() == b"col\n1\n2\n"


def test_download_file_auth_required_message(monkeypatch, tmp_path):
    import urllib.error
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://x/t.csv", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(_download.urllib.request, "urlopen", boom)
    res = _download.download_file("https://x/t.csv", str(tmp_path / "t.csv"))
    assert res["ok"] is False
    assert "auth" in res["skipped_reason"].lower()
    assert not (tmp_path / "t.csv").exists()


def test_download_candidate_tabular_only(monkeypatch, tmp_path):
    saved = []
    def fake_dl(url, dest, **kw):
        open(dest, "wb").write(b"x"); saved.append(dest); return {"ok": True, "path": dest}
    monkeypatch.setattr(_download, "download_file", fake_dl)
    cand = {"cand_id": "zenodo:1", "tabular_files": [
        {"name": "a.csv", "ext": "csv", "size": 5, "download_url": "https://x/a.csv"}]}
    summary = _download.download_candidate(cand, str(tmp_path))
    assert len(summary["downloaded"]) == 1
    assert summary["downloaded"][0].endswith("a.csv")


def test_download_candidate_writes_provenance_sidecar(monkeypatch, tmp_path):
    """Downloading must record where the data came from, so the later audit can
    stamp scan.json with the paper's DOI/title (provenance for archiving)."""
    import json
    monkeypatch.setattr(_download, "download_file",
                        lambda url, dest, **kw: (open(dest, "wb").write(b"x"),
                                                 {"ok": True, "path": dest})[1])
    cand = {"cand_id": "zenodo:1", "source": "zenodo", "doi": "10.5281/zenodo.42",
            "title": "My deposited data", "related_dois": ["10.1038/paper"],
            "tabular_files": [{"name": "a.csv", "ext": "csv", "size": 1,
                               "download_url": "https://x/a.csv"}]}
    _download.download_candidate(cand, str(tmp_path))
    sidecar = tmp_path / "paperconan_source.json"
    assert sidecar.exists(), "expected a provenance sidecar next to the downloads"
    p = json.loads(sidecar.read_text(encoding="utf-8"))
    assert p["doi"] == "10.5281/zenodo.42"
    assert p["cand_id"] == "zenodo:1"
    assert p["source"] == "zenodo"


def test_download_candidate_extracts_tabular_from_supplementary_zip(monkeypatch, tmp_path):
    """Europe PMC serves supplementary material as one zip — download_candidate must
    extract only the tabular members (xlsx/csv/tsv) into out_dir, dropping the rest,
    and flatten any internal paths (no path traversal)."""
    import io, os, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nested/dir/table.xlsx", b"PK-fake-xlsx-bytes")
        z.writestr("figure.csv", b"a,b\n1,2\n")
        z.writestr("readme.txt", b"not data")
    zbytes = buf.getvalue()

    def fake_dl(url, dest, **kw):
        open(dest, "wb").write(zbytes)
        return {"ok": True, "path": dest}
    monkeypatch.setattr(_download, "download_file", fake_dl)

    cand = {"cand_id": "europepmc:PMC1", "source": "europepmc", "doi": "10.1038/x",
            "title": "T", "tabular_files": [],
            "supplementary_archive": {
                "url": "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/supplementaryFiles",
                "name": "PMC1_supplementary.zip"}}
    summary = _download.download_candidate(cand, str(tmp_path))

    names = sorted(os.path.basename(p) for p in summary["downloaded"])
    assert names == ["figure.csv", "table.xlsx"]
    assert not (tmp_path / "readme.txt").exists()
    assert not (tmp_path / "PMC1_supplementary.zip").exists(), "zip should be cleaned up"


def test_supplementary_archive_downloads_with_larger_cap_than_per_file(monkeypatch, tmp_path):
    """A supplementary zip bundles ALL supplementary material (often 100MB+ of video),
    yet we only extract its small tabular members. So the archive must download with a
    much larger byte cap than an individual file, or big-but-tabular zips get truncated
    and silently lost (the failure seen on Europe PMC archives)."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("table.csv", b"a,b\n1,2\n")
    zbytes = buf.getvalue()
    calls = []
    def fake_dl(url, dest, **kw):
        calls.append({"url": url, "max_bytes": kw.get("max_bytes")})
        open(dest, "wb").write(zbytes)
        return {"ok": True, "path": dest}
    monkeypatch.setattr(_download, "download_file", fake_dl)
    cand = {"cand_id": "europepmc:PMC1", "source": "europepmc", "tabular_files": [],
            "supplementary_archive": {"url": "https://ebi/PMC1/supplementaryFiles",
                                      "name": "PMC1.zip"}}
    _download.download_candidate(cand, str(tmp_path))
    arch_call = next(c for c in calls if c["url"].endswith("supplementaryFiles"))
    assert arch_call["max_bytes"] == _download._ARCHIVE_MAX
    assert _download._ARCHIVE_MAX > _download._DEFAULT_MAX


def test_supplementary_archive_extraction_still_caps_each_table(monkeypatch, tmp_path):
    """The larger archive cap must NOT relax the per-table cap: an individual table
    bigger than the per-file limit is still skipped (one bloated sheet shouldn't slip in
    just because it rode inside an archive)."""
    import io, os, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("small.csv", b"a,b\n1,2\n")        # ~8 bytes, kept
        z.writestr("huge.csv", b"x" * 500)            # 500 bytes, over the per-table cap
    zbytes = buf.getvalue()
    monkeypatch.setattr(_download, "download_file",
                        lambda url, dest, **kw: (open(dest, "wb").write(zbytes),
                                                 {"ok": True, "path": dest})[1])
    cand = {"cand_id": "europepmc:PMC1", "source": "europepmc", "tabular_files": [],
            "supplementary_archive": {"url": "https://ebi/PMC1/supplementaryFiles",
                                      "name": "PMC1.zip"}}
    summary = _download.download_candidate(cand, str(tmp_path), max_bytes=100)
    names = sorted(os.path.basename(p) for p in summary["downloaded"])
    assert names == ["small.csv"]
    assert not (tmp_path / "huge.csv").exists()


def test_download_file_rejects_non_http_scheme(tmp_path):
    res = _download.download_file("file:///etc/passwd", str(tmp_path / "x.csv"))
    assert res["ok"] is False
    assert "scheme" in res["skipped_reason"].lower()
    assert not (tmp_path / "x.csv").exists()


def test_download_file_rejects_oversize_via_content_length(monkeypatch, tmp_path):
    def big(req, timeout=None):
        r = _Resp(b"x", "text/csv")
        r.headers["Content-Length"] = "999999999"
        return r
    monkeypatch.setattr(_download.urllib.request, "urlopen", big)
    res = _download.download_file("https://x/t.csv", str(tmp_path / "t.csv"), max_bytes=1000)
    assert res["ok"] is False
    assert "max_bytes" in res["skipped_reason"]
    assert not (tmp_path / "t.csv").exists()


def test_download_file_rejects_oversize_via_body(monkeypatch, tmp_path):
    payload = b"a" * 50
    monkeypatch.setattr(_download.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(payload, "text/csv"))
    res = _download.download_file("https://x/t.csv", str(tmp_path / "t.csv"), max_bytes=10)
    assert res["ok"] is False
    assert "max_bytes" in res["skipped_reason"]
    assert not (tmp_path / "t.csv").exists()


def test_download_file_403_message(monkeypatch, tmp_path):
    import urllib.error
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("https://x/t.csv", 403, "Forbidden", {}, None)
    monkeypatch.setattr(_download.urllib.request, "urlopen", boom)
    res = _download.download_file("https://x/t.csv", str(tmp_path / "t.csv"))
    assert res["ok"] is False
    assert "auth" in res["skipped_reason"].lower()


def test_download_candidate_images_are_additive_and_default_stays_tabular(monkeypatch, tmp_path):
    calls = []

    def fake_download(url, dest, **kwargs):
        open(dest, "wb").write(b"x")
        calls.append(dest)
        return {
            "ok": True,
            "path": dest,
            "size": 1,
            "content_type": "application/octet-stream",
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    cand = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{"name": "data.csv", "download_url": "https://x/data.csv"}],
        "image_files": [{"name": "Fig1.png", "download_url": "https://x/Fig1.png"}],
        "all_files": [
            {"name": "data.csv", "download_url": "https://x/data.csv"},
            {"name": "Fig1.png", "download_url": "https://x/Fig1.png"},
        ],
    }

    default_dir = tmp_path / "default"
    default = _download.download_candidate(cand, str(default_dir))
    assert [Path(p).name for p in default["downloaded"]] == ["data.csv"]

    image_dir = tmp_path / "images"
    image = _download.download_candidate(cand, str(image_dir), include_images=True)
    assert sorted(Path(p).name for p in image["downloaded"]) == ["Fig1.png", "data.csv"]


def test_image_archive_same_basenames_do_not_overwrite(monkeypatch, tmp_path):
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("figures/Fig1.png", b"first-image")
        archive.writestr("supplement/Fig1.png", b"second-image")

    def fake_download(url, dest, **kwargs):
        Path(dest).write_bytes(payload.getvalue())
        return {"ok": True, "path": dest, "size": len(payload.getvalue())}

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "europepmc:PMC1",
        "source": "europepmc",
        "tabular_files": [],
        "image_files": [],
        "supplementary_archive": {
            "url": "https://example.test/supplementaryFiles",
            "name": "supplementary.zip",
        },
    }
    summary = _download.download_candidate(
        candidate,
        str(tmp_path),
        include_images=True,
    )
    names = sorted(Path(path).name for path in summary["downloaded"])
    assert len(names) == 2
    assert "Fig1.png" in names
    assert any(name.startswith("Fig1-") for name in names)


def test_default_direct_table_download_does_not_also_fetch_archive(monkeypatch, tmp_path):
    calls = []

    def fake_download(url, dest, **kwargs):
        calls.append(url)
        Path(dest).write_bytes(b"table")
        return {"ok": True, "path": dest, "size": 5}

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [
            {"name": "data.csv", "download_url": "https://example.test/data.csv"},
        ],
        "supplementary_archive": {
            "url": "https://example.test/supplementaryFiles",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(candidate, str(tmp_path))

    assert [Path(path).name for path in summary["downloaded"]] == ["data.csv"]
    assert calls == ["https://example.test/data.csv"]


def test_image_archive_runs_when_direct_table_download_succeeds(monkeypatch, tmp_path):
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("figures/Fig1.png", b"image")

    def fake_download(url, dest, **kwargs):
        if url.endswith("data.csv"):
            Path(dest).write_bytes(b"table")
            return {"ok": True, "path": dest, "size": 5}
        Path(dest).write_bytes(payload.getvalue())
        return {"ok": True, "path": dest, "size": len(payload.getvalue())}

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [
            {"name": "data.csv", "download_url": "https://example.test/data.csv"},
        ],
        "image_files": [],
        "supplementary_archive": {
            "url": "https://example.test/supplementaryFiles",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(
        candidate,
        str(tmp_path),
        include_images=True,
    )

    assert sorted(Path(path).name for path in summary["downloaded"]) == [
        "Fig1.png",
        "data.csv",
    ]


def test_identical_archive_file_preserves_direct_download_and_provenance(
    monkeypatch,
    tmp_path,
):
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("figures/Fig1.png", b"image")

    direct_url = "https://example.test/Fig1.png?signature=secret#fragment"

    def fake_download(url, dest, **kwargs):
        if url == direct_url:
            Path(dest).write_bytes(b"image")
            return {
                "ok": True,
                "path": dest,
                "size": 5,
                "content_type": "image/png",
                "source_url": url,
            }
        Path(dest).write_bytes(payload.getvalue())
        return {
            "ok": True,
            "path": dest,
            "size": len(payload.getvalue()),
            "content_type": "application/zip",
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "image_files": [
            {"name": "Fig1.png", "download_url": direct_url},
        ],
        "supplementary_archive": {
            "url": "https://example.test/supplementaryFiles?token=archive",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(
        candidate,
        str(tmp_path),
        include_images=True,
    )
    sidecar = json.loads(
        (tmp_path / "paperconan_source.json").read_text(encoding="utf-8")
    )

    assert [Path(path).name for path in summary["downloaded"]] == ["Fig1.png"]
    assert sidecar["downloads"] == [{
        "file": "Fig1.png",
        "source_url": "https://example.test/Fig1.png",
        "content_type": "image/png",
        "asset_type": "image",
        "size": 5,
    }]


def test_direct_files_with_same_name_publish_distinct_files_and_provenance(
    monkeypatch,
    tmp_path,
):
    payloads = {
        "https://example.test/first/Fig1.png": b"first-image",
        "https://example.test/second/Fig1.png": b"second-image",
    }

    def fake_download(url, dest, **kwargs):
        data = payloads[url]
        Path(dest).write_bytes(data)
        return {
            "ok": True,
            "path": dest,
            "size": len(data),
            "content_type": "image/png",
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "image_files": [
            {"name": "Fig1.png", "download_url": url}
            for url in payloads
        ],
    }

    summary = _download.download_candidate(
        candidate,
        str(tmp_path),
        include_images=True,
    )
    sidecar = json.loads(
        (tmp_path / _download.SOURCE_SIDECAR).read_text(encoding="utf-8")
    )

    published = {Path(path).name: Path(path).read_bytes() for path in summary["downloaded"]}
    second_digest = hashlib.sha256(payloads["https://example.test/second/Fig1.png"]).hexdigest()[:10]
    expected_sources = {
        "Fig1.png": "https://example.test/first/Fig1.png",
        f"Fig1-{second_digest}.png": "https://example.test/second/Fig1.png",
    }
    assert len(published) == 2
    assert set(published.values()) == set(payloads.values())
    assert {
        entry["file"]: entry["source_url"]
        for entry in sidecar["downloads"]
    } == expected_sources


def test_download_candidate_pins_output_root_across_publications(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = tmp_path / "displaced-out"
    payloads = {
        "https://example.test/Fig1.png": b"first-image",
        "https://example.test/Fig2.png": b"second-image",
    }

    def fake_download(url, dest, **kwargs):
        data = payloads[url]
        Path(dest).write_bytes(data)
        return {"ok": True, "path": dest, "size": len(data), "source_url": url}

    real_write_collision_safe = _download._write_collision_safe
    publications = 0

    def publish_then_swap(output, name, data, **kwargs):
        nonlocal publications
        result = real_write_collision_safe(
            output,
            name,
            data,
            **kwargs,
        )
        publications += 1
        if publications == 1:
            out_dir.rename(displaced)
            out_dir.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_write_collision_safe",
        publish_then_swap,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "image_files": [
            {"name": Path(url).name, "download_url": url}
            for url in payloads
        ],
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    assert summary["skipped"]
    assert list(outside.iterdir()) == []
    assert (displaced / "Fig1.png").read_bytes() == b"first-image"
    assert not (displaced / "Fig2.png").exists()


def test_download_candidate_rejects_root_replacement_after_direct_publication(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    displaced = tmp_path / "displaced-out"
    payload = b"a,b\n1,2\n"
    replacement = b"replacement,root\n"
    root_replaced = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    real_write_collision_safe = _download._write_collision_safe

    def publish_then_replace_root(output, name, data, **kwargs):
        nonlocal root_replaced
        published = real_write_collision_safe(
            output,
            name,
            data,
            **kwargs,
        )
        if not root_replaced:
            out_dir.rename(displaced)
            out_dir.mkdir()
            (out_dir / "data.csv").write_bytes(replacement)
            root_replaced = True
        return published

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_write_collision_safe",
        publish_then_replace_root,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert root_replaced
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "output directory changed" in summary["skipped"][0]["reason"]
    assert (out_dir / "data.csv").read_bytes() == replacement
    assert (displaced / "data.csv").read_bytes() == payload


def test_direct_download_staging_stays_on_pinned_root_during_replacement(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    displaced = tmp_path / "displaced-out"
    payload = b"a,b\n1,2\n"
    replacement_received_bytes = False

    def swap_then_download(url, destination, **kwargs):
        nonlocal replacement_received_bytes
        out_dir.rename(displaced)
        out_dir.mkdir()
        if hasattr(destination, "fd"):
            os.ftruncate(destination.fd, 0)
            os.lseek(destination.fd, 0, os.SEEK_SET)
            os.write(destination.fd, payload)
        else:
            path = Path(destination)
            path.write_bytes(payload)
            replacement_received_bytes = path.parent == out_dir
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", swap_then_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert summary["downloaded"] == []
    assert summary["skipped"]
    assert replacement_received_bytes is False
    assert list(out_dir.iterdir()) == []
    assert not list(displaced.glob(".paperconan-download-*"))


def test_provenance_sidecar_does_not_follow_or_replace_final_symlink(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    sidecar = out_dir / _download.SOURCE_SIDECAR
    sidecar.symlink_to(sentinel)

    def fake_download(url, dest, **kwargs):
        Path(dest).write_bytes(b"image")
        return {"ok": True, "path": dest, "size": 5, "source_url": url}

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "image_files": [{
            "name": "Fig1.png",
            "download_url": "https://example.test/Fig1.png",
        }],
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    assert len(summary["downloaded"]) == 1
    assert sidecar.is_symlink()
    assert sidecar.readlink() == sentinel
    assert sentinel.read_text(encoding="utf-8") == "outside-sentinel"
    assert not list(out_dir.glob(".paperconan-sidecar-*"))


@pytest.mark.parametrize("existing_sidecar", [False, True])
def test_final_output_replacement_rolls_back_provenance_sidecar(
    monkeypatch,
    tmp_path,
    existing_sidecar,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sidecar = out_dir / _download.SOURCE_SIDECAR
    previous_bytes = json.dumps({
        "cand_id": "previous:1",
        "downloads": [{"file": "previous.csv"}],
    }).encode("utf-8")
    if existing_sidecar:
        sidecar.write_bytes(previous_bytes)
    payload = b"a,b\n1,2\n"
    replacement = b"x,y\n9,8\n"
    sidecar_published = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    real_write_source_sidecar = _download._write_source_sidecar

    def publish_sidecar_then_replace_output(cand, output, downloads=None):
        nonlocal sidecar_published
        publication = real_write_source_sidecar(
            cand,
            output,
            downloads=downloads,
        )
        published = json.loads(sidecar.read_text(encoding="utf-8"))
        assert published["downloads"] == [{
            "file": "data.csv",
            "source_url": "https://example.test/data.csv",
            "content_type": None,
            "asset_type": "tabular",
            "size": len(payload),
        }]
        sidecar_published = True
        os.unlink("data.csv", dir_fd=output.fd)
        replacement_fd = os.open(
            "data.csv",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=output.fd,
        )
        try:
            os.write(replacement_fd, replacement)
        finally:
            os.close(replacement_fd)
        return publication

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_write_source_sidecar",
        publish_sidecar_then_replace_output,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert sidecar_published
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "stable regular file" in summary["skipped"][0]["reason"]
    if existing_sidecar:
        assert sidecar.read_bytes() == previous_bytes
    else:
        assert not sidecar.exists()
    assert not list(out_dir.glob(".paperconan-sidecar-*"))


def test_sidecar_rollback_uses_immutable_prior_bytes(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sidecar = out_dir / _download.SOURCE_SIDECAR
    previous_bytes = b'{"cand_id":"previous:1","downloads":[]}'
    mutated_bytes = b"x" * len(previous_bytes)
    sidecar.write_bytes(previous_bytes)
    old_writer_fd = os.open(sidecar, os.O_RDWR | os.O_NOFOLLOW)
    payload = b"a,b\n1,2\n"
    replacement = b"x,y\n9,8\n"
    old_inode_mutated = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    real_write_source_sidecar = _download._write_source_sidecar

    def publish_then_mutate_old_inode(cand, output, downloads=None):
        nonlocal old_inode_mutated
        publication = real_write_source_sidecar(
            cand,
            output,
            downloads=downloads,
        )
        published = json.loads(sidecar.read_text(encoding="utf-8"))
        assert published["downloads"][0]["file"] == "data.csv"
        os.lseek(old_writer_fd, 0, os.SEEK_SET)
        os.write(old_writer_fd, mutated_bytes)
        os.ftruncate(old_writer_fd, len(mutated_bytes))
        os.fsync(old_writer_fd)
        old_inode_mutated = True
        os.unlink("data.csv", dir_fd=output.fd)
        replacement_fd = os.open(
            "data.csv",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=output.fd,
        )
        try:
            os.write(replacement_fd, replacement)
        finally:
            os.close(replacement_fd)
        return publication

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_write_source_sidecar",
        publish_then_mutate_old_inode,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    try:
        summary = _download.download_candidate(candidate, str(out_dir))
        os.lseek(old_writer_fd, 0, os.SEEK_SET)
        old_inode_bytes = os.read(old_writer_fd, len(mutated_bytes))
    finally:
        os.close(old_writer_fd)

    assert old_inode_mutated
    assert old_inode_bytes == mutated_bytes
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert sidecar.read_bytes() == previous_bytes
    assert not list(out_dir.glob(".paperconan-sidecar-*"))


def test_direct_download_does_not_follow_existing_destination_symlink(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"outside")
    (out_dir / "Fig1.png").symlink_to(sentinel)

    def fake_download(url, dest, **kwargs):
        Path(dest).write_bytes(b"new-image")
        return {"ok": True, "path": dest, "size": 9, "source_url": url}

    monkeypatch.setattr(_download, "download_file", fake_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "image_files": [
            {
                "name": "Fig1.png",
                "download_url": "https://example.test/Fig1.png",
            },
        ],
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    assert sentinel.read_bytes() == b"outside"
    assert len(summary["downloaded"]) == 1
    published = Path(summary["downloaded"][0])
    assert published.parent == out_dir
    assert published.is_file()
    assert not published.is_symlink()
    assert published.read_bytes() == b"new-image"


def test_write_collision_safe_does_not_follow_candidate_or_digest_symlinks(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"outside")
    data = b"archive-image"
    digest = hashlib.sha256(data).hexdigest()[:10]
    candidate = out_dir / "Fig1.png"
    digest_path = out_dir / f"Fig1-{digest}.png"
    candidate.symlink_to(sentinel)
    digest_path.symlink_to(sentinel)

    published = Path(
        _download._write_collision_safe(str(out_dir), "nested/Fig1.png", data)
    )

    assert sentinel.read_bytes() == b"outside"
    assert published == out_dir / f"Fig1-{digest}-2.png"
    assert published.is_file()
    assert not published.is_symlink()
    assert published.read_bytes() == data


def test_download_file_replaces_destination_symlink_without_following_it(
    monkeypatch,
    tmp_path,
):
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"outside")
    dest = tmp_path / "data.csv"
    dest.symlink_to(sentinel)
    monkeypatch.setattr(
        _download.urllib.request,
        "urlopen",
        lambda req, timeout=None: _Resp(b"a,b\n1,2\n", "text/csv"),
    )

    result = _download.download_file(
        "https://example.test/data.csv",
        str(dest),
        retries=1,
    )

    assert result["ok"] is True
    assert sentinel.read_bytes() == b"outside"
    assert dest.is_file()
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"a,b\n1,2\n"


def test_write_collision_safe_preserves_different_candidate_and_digest_files(tmp_path):
    data = b"new-image"
    digest = hashlib.sha256(data).hexdigest()[:10]
    candidate = tmp_path / "Fig1.png"
    digest_path = tmp_path / f"Fig1-{digest}.png"
    candidate.write_bytes(b"existing-candidate")
    digest_path.write_bytes(b"existing-digest")

    published = Path(
        _download._write_collision_safe(str(tmp_path), "Fig1.png", data)
    )

    assert candidate.read_bytes() == b"existing-candidate"
    assert digest_path.read_bytes() == b"existing-digest"
    assert published == tmp_path / f"Fig1-{digest}-2.png"
    assert published.read_bytes() == data


def test_write_collision_safe_bounds_existing_file_comparison(
    monkeypatch,
    tmp_path,
):
    data = b"new-image"
    digest = hashlib.sha256(data).hexdigest()[:10]
    candidate = tmp_path / "Fig1.png"
    with candidate.open("wb") as fh:
        fh.truncate(1024 * 1024 * 1024)
    digest_path = tmp_path / f"Fig1-{digest}.png"
    digest_path.write_bytes(data)
    candidate_identity = (
        candidate.stat().st_dev,
        candidate.stat().st_ino,
    )
    digest_identity = (
        digest_path.stat().st_dev,
        digest_path.stat().st_ino,
    )
    candidate_read_sizes = []
    digest_read_sizes = []
    real_fdopen = os.fdopen

    class TrackingReader:
        def __init__(self, fh, read_sizes, *, sparse=False):
            self._fh = fh
            self._read_sizes = read_sizes
            self._sparse = sparse

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def read(self, size=-1):
            self._read_sizes.append(size)
            if self._sparse and size < 0:
                return b"different"
            return self._fh.read(size)

    def tracking_fdopen(fd, *args, **kwargs):
        identity = os.fstat(fd)
        identity = (identity.st_dev, identity.st_ino)
        fh = real_fdopen(fd, *args, **kwargs)
        if identity == candidate_identity:
            return TrackingReader(fh, candidate_read_sizes, sparse=True)
        if identity == digest_identity:
            return TrackingReader(fh, digest_read_sizes)
        return fh

    monkeypatch.setattr(_download.os, "fdopen", tracking_fdopen)

    published = Path(
        _download._write_collision_safe(str(tmp_path), "Fig1.png", data)
    )

    assert candidate.stat().st_size == 1024 * 1024 * 1024
    assert published == digest_path
    assert candidate_read_sizes == []
    assert digest_read_sizes
    assert all(size > 0 for size in digest_read_sizes)


def test_write_collision_safe_does_not_clobber_file_created_during_publish(
    monkeypatch,
    tmp_path,
):
    data = b"new-image"
    digest = hashlib.sha256(data).hexdigest()[:10]
    candidate = tmp_path / "Fig1.png"
    real_link = _download.os.link
    raced = False

    def racing_link(src, dst, *args, **kwargs):
        nonlocal raced
        if (
            not raced
            and Path(dst).name == candidate.name
            and kwargs.get("dst_dir_fd") is not None
        ):
            raced = True
            candidate.write_bytes(b"concurrent")
            raise FileExistsError(dst)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(_download.os, "link", racing_link)

    published = Path(
        _download._write_collision_safe(str(tmp_path), "Fig1.png", data)
    )

    assert raced is True
    assert candidate.read_bytes() == b"concurrent"
    assert published == tmp_path / f"Fig1-{digest}.png"
    assert published.read_bytes() == data


def _archive_bytes(kind, member_name="tables/data.csv", data=b"a,b\n1,2\n"):
    payload = io.BytesIO()
    if kind == "oa":
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    else:
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(member_name, data)
    return payload.getvalue()


def _zero_byte_archive_bytes(kind, prefix, count):
    payload = io.BytesIO()
    if kind == "oa":
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            for index in range(count):
                info = tarfile.TarInfo(f"tables/{prefix}-{index:03d}.csv")
                info.size = 0
                archive.addfile(info, io.BytesIO())
    else:
        with zipfile.ZipFile(payload, "w") as archive:
            for index in range(count):
                archive.writestr(f"tables/{prefix}-{index:03d}.csv", b"")
    return payload.getvalue()


def test_tar_member_ceiling_streams_without_materializing_member_list(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    payload = _zero_byte_archive_bytes("oa", "oa", 4)
    download_calls = []

    def fake_download(url, destination, **kwargs):
        download_calls.append(url)
        if url != "https://example.test/oa":
            raise AssertionError("shared archive member ceiling was not enforced")
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    def fail_getmembers(archive):
        raise AssertionError("tar member list was materialized")

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(tarfile.TarFile, "getmembers", fail_getmembers)
    monkeypatch.setattr(_download, "_MAX_PUBLISHED_FILES_PER_CANDIDATE", 10)
    monkeypatch.setattr(_download, "_MAX_ARCHIVE_MEMBERS_PER_CANDIDATE", 2)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "oa_package": {
            "url": "https://example.test/oa",
            "name": "oa.tar.gz",
        },
        "supplementary_archive": {
            "url": "https://example.test/supplementary",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    downloaded_names = [Path(path).name for path in summary["downloaded"]]
    sidecar = json.loads(
        (out_dir / _download.SOURCE_SIDECAR).read_text(encoding="utf-8")
    )
    assert download_calls == ["https://example.test/oa"]
    assert downloaded_names == ["oa-000.csv", "oa-001.csv"]
    assert [entry["file"] for entry in sidecar["downloads"]] == downloaded_names
    assert any(
        "archive member cardinality ceiling" in item["reason"]
        for item in summary["skipped"]
    )
    assert not list(out_dir.glob(".paperconan-*"))


def test_zero_byte_archive_member_ceiling_is_shared_across_tar_and_zip(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    payloads = {
        "https://example.test/oa": _zero_byte_archive_bytes("oa", "oa", 2),
        "https://example.test/supplementary": _zero_byte_archive_bytes(
            "supplementary",
            "supplementary",
            4,
        ),
    }

    def fake_download(url, destination, **kwargs):
        payload = payloads[url]
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, "_MAX_PUBLISHED_FILES_PER_CANDIDATE", 10)
    monkeypatch.setattr(_download, "_MAX_ARCHIVE_MEMBERS_PER_CANDIDATE", 3)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "oa_package": {
            "url": "https://example.test/oa",
            "name": "oa.tar.gz",
        },
        "supplementary_archive": {
            "url": "https://example.test/supplementary",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    downloaded_names = [Path(path).name for path in summary["downloaded"]]
    sidecar = json.loads(
        (out_dir / _download.SOURCE_SIDECAR).read_text(encoding="utf-8")
    )
    published_names = sorted(
        path.name
        for path in out_dir.iterdir()
        if path.name != _download.SOURCE_SIDECAR
    )
    assert downloaded_names == [
        "oa-000.csv",
        "oa-001.csv",
        "supplementary-000.csv",
    ]
    assert published_names == sorted(downloaded_names)
    assert [entry["file"] for entry in sidecar["downloads"]] == sorted(
        downloaded_names
    )
    assert any(
        "archive member cardinality ceiling" in item["reason"]
        for item in summary["skipped"]
    )
    assert not list(out_dir.glob(".paperconan-*"))


def test_published_file_ceiling_spans_direct_and_all_archive_sources(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    payloads = {
        "https://example.test/oa": _zero_byte_archive_bytes("oa", "oa", 2),
        "https://example.test/supplementary": _zero_byte_archive_bytes(
            "supplementary",
            "supplementary",
            2,
        ),
    }

    def fake_download(url, destination, **kwargs):
        payload = payloads.get(url, b"")
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, "_MAX_PUBLISHED_FILES_PER_CANDIDATE", 3)
    monkeypatch.setattr(_download, "_MAX_ARCHIVE_MEMBERS_PER_CANDIDATE", 10)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "direct.csv",
            "download_url": "https://example.test/direct.csv",
        }],
        "oa_package": {
            "url": "https://example.test/oa",
            "name": "oa.tar.gz",
        },
        "supplementary_archive": {
            "url": "https://example.test/supplementary",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(
        candidate,
        str(out_dir),
        include_images=True,
    )

    downloaded_names = [Path(path).name for path in summary["downloaded"]]
    sidecar = json.loads(
        (out_dir / _download.SOURCE_SIDECAR).read_text(encoding="utf-8")
    )
    assert downloaded_names == ["direct.csv", "oa-000.csv", "oa-001.csv"]
    assert len(sidecar["downloads"]) == 3
    assert {
        path.name
        for path in out_dir.iterdir()
        if path.name != _download.SOURCE_SIDECAR
    } == set(downloaded_names)
    assert any(
        "published file cardinality ceiling" in item["reason"]
        for item in summary["skipped"]
    )
    assert not list(out_dir.glob(".paperconan-*"))


@pytest.mark.parametrize("existing_sidecar", [False, True])
def test_oversized_generated_sidecar_is_not_published(
    monkeypatch,
    tmp_path,
    existing_sidecar,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sidecar = out_dir / _download.SOURCE_SIDECAR
    previous_bytes = b'{"cand_id":"previous:1","downloads":[]}'
    if existing_sidecar:
        sidecar.write_bytes(previous_bytes)

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, b"")
        return {
            "ok": True,
            "path": destination,
            "size": 0,
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, "_MAX_SOURCE_SIDECAR_BYTES", 32)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "title": "generated provenance exceeds the configured bound",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert [Path(path).name for path in summary["downloaded"]] == ["data.csv"]
    assert any(
        "new provenance sidecar exceeds" in item["reason"]
        for item in summary["skipped"]
    )
    if existing_sidecar:
        assert sidecar.read_bytes() == previous_bytes
    else:
        assert not sidecar.exists()
    assert not list(out_dir.glob(".paperconan-sidecar-*"))


def test_sidecar_commit_and_rollback_failures_preserve_recovery_context(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sidecar = out_dir / _download.SOURCE_SIDECAR
    sidecar.write_bytes(b'{"cand_id":"previous:1","downloads":[]}')
    retained_backup_names = []
    real_write_source_sidecar = _download._write_source_sidecar

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, b"")
        return {
            "ok": True,
            "path": destination,
            "size": 0,
            "source_url": url,
        }

    def track_publication(cand, output, downloads=None):
        publication = real_write_source_sidecar(
            cand,
            output,
            downloads=downloads,
        )
        retained_backup_names.append(publication.backup_name)
        return publication

    def fail_commit(publication):
        raise OSError(
            "commit unavailable at "
            "https://user:secret@example.test/source?token=commit-secret"
        )

    def fail_rollback(publication):
        raise OSError(
            "rollback unavailable at "
            "https://user:secret@example.test/recovery?token=rollback-secret"
        )

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, "_write_source_sidecar", track_publication)
    monkeypatch.setattr(_download._SidecarPublication, "commit", fail_commit)
    monkeypatch.setattr(_download._SidecarPublication, "rollback", fail_rollback)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }],
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert summary["downloaded"] == []
    assert len(retained_backup_names) == 1
    backup_name = retained_backup_names[0]
    assert backup_name
    assert (out_dir / backup_name).exists()
    reason = summary["skipped"][-1]["reason"]
    assert "commit unavailable" in reason
    assert "rollback unavailable" in reason
    assert backup_name in reason
    assert "commit-secret" not in reason
    assert "rollback-secret" not in reason
    assert "user:secret" not in reason


@pytest.mark.parametrize("publication_kind", ["direct", "oa", "supplementary"])
def test_download_candidate_rejects_equal_size_in_place_content_mutation(
    monkeypatch,
    tmp_path,
    publication_kind,
):
    out_dir = tmp_path / "out"
    original = b"a,b\n1,2\n"
    replacement = b"x,y\n9,8\n"
    assert len(replacement) == len(original)
    payload = (
        original
        if publication_kind == "direct"
        else _archive_bytes(publication_kind, data=original)
    )
    content_mutated = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    real_write_source_sidecar = _download._write_source_sidecar

    def mutate_content_then_write_sidecar(cand, output, downloads=None):
        nonlocal content_mutated
        entry_fd = os.open(
            "data.csv",
            os.O_WRONLY | os.O_NOFOLLOW,
            dir_fd=output.fd,
        )
        try:
            opened = os.fstat(entry_fd)
            os.lseek(entry_fd, 0, os.SEEK_SET)
            os.write(entry_fd, replacement)
            os.fsync(entry_fd)
            current = os.fstat(entry_fd)
        finally:
            os.close(entry_fd)
        assert (opened.st_dev, opened.st_ino) == (
            current.st_dev,
            current.st_ino,
        )
        assert opened.st_size == current.st_size
        content_mutated = True
        return real_write_source_sidecar(
            cand,
            output,
            downloads=downloads,
        )

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_write_source_sidecar",
        mutate_content_then_write_sidecar,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if publication_kind == "direct":
        candidate["tabular_files"] = [{
            "name": "data.csv",
            "download_url": "https://example.test/data.csv",
        }]
    else:
        archive = {
            "url": f"https://example.test/{publication_kind}",
            "name": f"{publication_kind}.archive",
        }
        if publication_kind == "oa":
            candidate["oa_package"] = archive
        else:
            candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(out_dir))

    assert content_mutated
    assert (out_dir / "data.csv").read_bytes() == replacement
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "content changed" in summary["skipped"][0]["reason"]


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
def test_archive_download_staging_stays_on_pinned_root_during_replacement(
    monkeypatch,
    tmp_path,
    archive_kind,
):
    out_dir = tmp_path / "out"
    displaced = tmp_path / "displaced-out"
    payload = _archive_bytes(archive_kind)
    replacement_received_bytes = False

    def swap_then_download(url, destination, **kwargs):
        nonlocal replacement_received_bytes
        out_dir.rename(displaced)
        out_dir.mkdir()
        if hasattr(destination, "fd"):
            os.ftruncate(destination.fd, 0)
            os.lseek(destination.fd, 0, os.SEEK_SET)
            os.write(destination.fd, payload)
        else:
            path = Path(destination)
            path.write_bytes(payload)
            replacement_received_bytes = path.parent == out_dir
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", swap_then_download)
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": f"{archive_kind}.archive",
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(out_dir))

    assert summary["downloaded"] == []
    assert summary["skipped"]
    assert replacement_received_bytes is False
    assert list(out_dir.iterdir()) == []
    assert not list(displaced.glob(".paperconan-archive-*"))


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
def test_archive_result_rejects_post_extraction_output_root_replacement(
    monkeypatch,
    tmp_path,
    archive_kind,
):
    out_dir = tmp_path / "out"
    displaced = tmp_path / "displaced-out"
    payload = _archive_bytes(archive_kind)
    extraction_complete = False
    root_replaced = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    extract_name = (
        "_extract_selected_tar"
        if archive_kind == "oa"
        else "_extract_selected_zip"
    )
    real_extract = getattr(_download, extract_name)
    real_verify = _download._PinnedOutputDirectory.verify

    def track_extraction(*args, **kwargs):
        nonlocal extraction_complete
        extracted = real_extract(*args, **kwargs)
        extraction_complete = True
        return extracted

    def verify_then_replace_root(output):
        nonlocal root_replaced
        real_verify(output)
        if extraction_complete and not root_replaced:
            out_dir.rename(displaced)
            out_dir.mkdir()
            (out_dir / "data.csv").write_bytes(b"replacement,root\n")
            root_replaced = True

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, extract_name, track_extraction)
    monkeypatch.setattr(
        _download._PinnedOutputDirectory,
        "verify",
        verify_then_replace_root,
    )
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": f"{archive_kind}.archive",
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(out_dir))

    assert root_replaced
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "output directory changed" in summary["skipped"][0]["reason"]
    assert (out_dir / "data.csv").read_bytes() == b"replacement,root\n"
    assert (displaced / "data.csv").read_bytes() == b"a,b\n1,2\n"


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
def test_download_candidate_rejects_root_replacement_after_archive_helper(
    monkeypatch,
    tmp_path,
    archive_kind,
):
    out_dir = tmp_path / "out"
    displaced = tmp_path / "displaced-out"
    payload = _archive_bytes(archive_kind)
    replacement = b"replacement,root\n"
    root_replaced = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    helper_name = (
        "_download_oa_package"
        if archive_kind == "oa"
        else "_download_supplementary_archive"
    )
    real_helper = getattr(_download, helper_name)

    def helper_then_replace_root(*args, **kwargs):
        nonlocal root_replaced
        extracted = real_helper(*args, **kwargs)
        out_dir.rename(displaced)
        out_dir.mkdir()
        (out_dir / "data.csv").write_bytes(replacement)
        root_replaced = True
        return extracted

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(_download, helper_name, helper_then_replace_root)
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": f"{archive_kind}.archive",
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(out_dir))

    assert root_replaced
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "output directory changed" in summary["skipped"][0]["reason"]
    assert (out_dir / "data.csv").read_bytes() == replacement
    assert (displaced / "data.csv").read_bytes() == b"a,b\n1,2\n"


def test_supplementary_archive_skips_replaced_final_entry(
    monkeypatch,
    tmp_path,
):
    out_dir = tmp_path / "out"
    payload = _archive_bytes("supplementary")
    replacement = b"replacement,entry\n"
    entry_replaced = False

    def fake_download(url, destination, **kwargs):
        os.ftruncate(destination.fd, 0)
        os.lseek(destination.fd, 0, os.SEEK_SET)
        os.write(destination.fd, payload)
        return {
            "ok": True,
            "path": destination,
            "size": len(payload),
            "source_url": url,
        }

    real_verify = _download._verify_published_output_file

    def replace_entry_then_verify(output, entry):
        nonlocal entry_replaced
        if not entry_replaced:
            os.unlink(entry.filename, dir_fd=output.fd)
            replacement_fd = os.open(
                entry.filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=output.fd,
            )
            try:
                os.write(replacement_fd, replacement)
            finally:
                os.close(replacement_fd)
            entry_replaced = True
        return real_verify(output, entry)

    monkeypatch.setattr(_download, "download_file", fake_download)
    monkeypatch.setattr(
        _download,
        "_verify_published_output_file",
        replace_entry_then_verify,
    )
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
        "supplementary_archive": {
            "url": "https://example.test/supplementary",
            "name": "supplementary.zip",
        },
    }

    summary = _download.download_candidate(candidate, str(out_dir))

    assert entry_replaced
    assert summary["downloaded"] == []
    assert len(summary["skipped"]) == 1
    assert "stable regular file" in summary["skipped"][0]["reason"]
    assert (out_dir / "data.csv").read_bytes() == replacement


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
@pytest.mark.parametrize("metadata_kind", ["symlink", "absolute", "parent"])
def test_archive_download_staging_ignores_unsafe_metadata_paths(
    monkeypatch,
    tmp_path,
    archive_kind,
    metadata_kind,
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel = tmp_path / f"{archive_kind}-{metadata_kind}-sentinel"
    sentinel.write_bytes(b"outside")
    if metadata_kind == "symlink":
        metadata_name = f"{archive_kind}.archive"
        (out_dir / metadata_name).symlink_to(sentinel)
    elif metadata_kind == "absolute":
        metadata_name = str(tmp_path / f"{archive_kind}-escaped.archive")
    else:
        metadata_name = f"../{archive_kind}-escaped.archive"

    destinations = []
    payload = _archive_bytes(archive_kind)

    def fake_download(url, dest, **kwargs):
        destinations.append(dest)
        Path(dest).write_bytes(payload)
        return {"ok": True, "path": dest, "size": len(payload)}

    monkeypatch.setattr(_download, "download_file", fake_download)
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": metadata_name,
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(out_dir))

    assert sentinel.read_bytes() == b"outside"
    assert [Path(path).name for path in summary["downloaded"]] == ["data.csv"]
    assert len(destinations) == 1
    assert hasattr(destinations[0], "fd")
    assert destinations[0].output.path == str(out_dir.resolve())
    assert destinations[0].name != Path(metadata_name).name


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
def test_failed_archive_download_removes_staging_file(
    monkeypatch,
    tmp_path,
    archive_kind,
):
    destinations = []

    def failed_download(url, dest, **kwargs):
        destinations.append(dest)
        return {"ok": False, "path": dest, "skipped_reason": "network unavailable"}

    monkeypatch.setattr(_download, "download_file", failed_download)
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": f"{archive_kind}.archive",
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    summary = _download.download_candidate(candidate, str(tmp_path))

    assert len(destinations) == 1
    assert summary["downloaded"] == []
    assert not list(tmp_path.glob(".paperconan-archive-*"))


def test_direct_download_rejects_staging_path_swapped_to_symlink(
    monkeypatch,
    tmp_path,
):
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"outside,bytes\n")

    def swapped_download(url, dest, **kwargs):
        if hasattr(dest, "fd"):
            os.ftruncate(dest.fd, 0)
            os.write(dest.fd, b"downloaded,bytes\n")
            os.unlink(dest.name, dir_fd=dest.output.fd)
            os.symlink(outside, dest.name, dir_fd=dest.output.fd)
        else:
            path = Path(dest)
            path.write_bytes(b"downloaded,bytes\n")
            path.unlink()
            path.symlink_to(outside)
        return {
            "ok": True,
            "path": dest,
            "size": len(b"downloaded,bytes\n"),
            "content_type": "text/csv",
            "source_url": url,
        }

    monkeypatch.setattr(_download, "download_file", swapped_download)
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [
            {
                "name": "data.csv",
                "download_url": "https://example.test/data.csv",
            },
        ],
    }

    summary = _download.download_candidate(candidate, str(tmp_path / "out"))

    assert outside.read_bytes() == b"outside,bytes\n"
    assert summary["downloaded"] == []
    assert "stable regular file" in summary["skipped"][0]["reason"]
    assert not (tmp_path / "out" / "data.csv").exists()


@pytest.mark.parametrize("archive_kind", ["oa", "supplementary"])
def test_archive_download_rejects_staging_path_swapped_to_symlink(
    monkeypatch,
    tmp_path,
    archive_kind,
):
    outside_archive = tmp_path / f"outside-{archive_kind}.archive"
    outside_archive.write_bytes(
        _archive_bytes(archive_kind, "outside.csv", b"outside,bytes\n")
    )

    def swapped_download(url, dest, **kwargs):
        payload = _archive_bytes(archive_kind)
        if hasattr(dest, "fd"):
            os.ftruncate(dest.fd, 0)
            os.write(dest.fd, payload)
            os.unlink(dest.name, dir_fd=dest.output.fd)
            os.symlink(outside_archive, dest.name, dir_fd=dest.output.fd)
        else:
            path = Path(dest)
            path.write_bytes(payload)
            path.unlink()
            path.symlink_to(outside_archive)
        return {"ok": True, "path": dest, "size": len(payload)}

    monkeypatch.setattr(_download, "download_file", swapped_download)
    archive = {
        "url": f"https://example.test/{archive_kind}",
        "name": f"{archive_kind}.archive",
    }
    candidate = {
        "cand_id": "source:1",
        "source": "source",
        "tabular_files": [],
    }
    if archive_kind == "oa":
        candidate["oa_package"] = archive
    else:
        candidate["supplementary_archive"] = archive

    out_dir = tmp_path / "out"
    summary = _download.download_candidate(candidate, str(out_dir))

    assert outside_archive.exists()
    assert summary["downloaded"] == []
    assert "stable regular file" in summary["skipped"][0]["reason"]
    assert not (out_dir / "outside.csv").exists()
    assert not list(out_dir.glob(".paperconan-archive-*"))
