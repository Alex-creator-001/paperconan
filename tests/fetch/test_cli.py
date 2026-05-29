import json
from paperconan.fetch import _cli


def test_fetch_list_prints_candidates_json(monkeypatch, capsys):
    cands = [{"cand_id": "zenodo:1", "source": "zenodo", "doi": "10.x/z",
              "title": "T", "tabular_files": [{"name": "a.xlsx"}],
              "all_files_count": 1, "match_signals": {"doi_in_related": True,
              "title_overlap": None, "author_overlap": None}}]
    monkeypatch.setattr(_cli, "search_all", lambda q, per_source=5: cands)
    rc = _cli.fetch_main(["10.15761/JTS.1000455", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["cand_id"] == "zenodo:1"


def test_fetch_download_selected_candidate(monkeypatch, tmp_path):
    cands = [{"cand_id": "zenodo:1", "source": "zenodo", "doi": "10.x/z", "title": "T",
              "tabular_files": [{"name": "a.csv", "ext": "csv", "size": 3,
              "download_url": "u"}], "all_files_count": 1, "match_signals": None}]
    monkeypatch.setattr(_cli, "search_all", lambda q, per_source=5: cands)
    captured = {}
    monkeypatch.setattr(_cli, "download_candidate",
                        lambda c, out_dir, **kw: captured.update(cid=c["cand_id"], out=out_dir)
                        or {"downloaded": [out_dir + "/a.csv"], "skipped": []})
    rc = _cli.fetch_main(["10.x/paper", "--download", "zenodo:1", "--out", str(tmp_path)])
    assert rc == 0
    assert captured["cid"] == "zenodo:1"
