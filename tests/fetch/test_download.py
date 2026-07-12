import hashlib
import io
import json
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
        if not raced and Path(dst) == candidate:
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
    assert Path(destinations[0]).parent.resolve() == out_dir.resolve()
    assert Path(destinations[0]).name != Path(metadata_name).name


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
        path = Path(dest)
        path.write_bytes(_archive_bytes(archive_kind))
        path.unlink()
        path.symlink_to(outside_archive)
        return {"ok": True, "path": dest, "size": path.stat().st_size}

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
