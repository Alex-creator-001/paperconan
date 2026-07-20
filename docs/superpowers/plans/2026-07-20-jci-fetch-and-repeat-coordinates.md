# JCI Fetch Fallback And Repeated-Segment Coordinates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrieve official JCI supporting-data tables when repository archives yield no table, and report exact spreadsheet coordinates for within-row repeated segments.

**Architecture:** Add a bounded JCI `/sd/3` HTML resolver that returns ordinary tabular file references, then feed those references through the existing direct-download staging, validation, publication, and provenance path only after normal sources yield no table. Extend the existing repeated-segment finding from detector-local row and column indexes, and render those coordinates in the raw HTML finding summary.

**Tech Stack:** Python 3.10+, stdlib `html.parser`/`urllib.parse`, pytest, existing paperconan fetch and HTML-report infrastructure.

## Global Constraints

- Describe only statistical signals and data inconsistencies; final judgment requires human review.
- Match only DOI values of the form `10.1172/JCI<number>`, case-insensitively.
- Do not commit real paper data, the motivating DOI, judgments, or credentials.
- Reuse existing HTTP policy, byte caps, secure staging, collision-safe publication, and provenance handling.
- Keep detector thresholds and deduplication behavior unchanged.
- Preserve the original Europe PMC/OA/archive skipped reasons when the JCI fallback runs.
- Release the already-versioned `0.8.3` only after tests and independent code review.

---

### Task 1: Official JCI Supporting-Data Fallback

**Files:**
- Modify: `src/paperconan/fetch/_download.py`
- Test: `tests/fetch/test_download.py`
- Test: `tests/fetch/test_cli.py`

**Interfaces:**
- Consumes: candidate dictionaries accepted by `download_candidate()`, existing `_http.get_text()`, `_http.resolve_http_url()`, `download_file()`, and secure publication helpers.
- Produces: `_resolve_jci_tabular_files(cand) -> tuple[list[dict], str | None]`, where file dictionaries have `name`, `ext`, `size`, and `download_url`; the optional string is a bounded neutral skipped reason.

- [ ] **Step 1: Add failing resolver and download-path tests**

Add synthetic tests that:

```python
def test_jci_fallback_downloads_official_table_after_archive_failure(...):
    candidate = {
        "cand_id": "europepmc:PMC1",
        "source": "europepmc",
        "doi": "10.1172/JCI123456",
        "tabular_files": [],
        "supplementary_archive": {
            "url": "https://example.test/supplementaryFiles",
            "name": "PMC1_supplementary.zip",
        },
    }
    # Archive download returns HTTP 404; /sd/3 returns synthetic HTML linking table.xlsx.
    # Assert one xlsx is published, provenance records the final official URL, and the
    # original archive HTTP 404 remains in summary["skipped"].

def test_non_jci_candidate_never_resolves_jci_fallback(...):
    # Patch the resolver to raise if called and assert a non-JCI DOI does not call it.

def test_jci_fallback_reports_no_supported_table_without_erasing_archive_failure(...):
    # Return HTML containing only PDF/image links and assert both the archive failure and
    # a bounded "no supported tabular attachment" reason are present.
```

Add a CLI integration test using a synthetic confident Europe PMC candidate and the real `download_candidate()` implementation; patch only network responses and assert `fetch --auto` returns `0` and prints `downloaded 1 file(s)`.

- [ ] **Step 2: Run the new fetch tests and verify RED**

Run:

```bash
uv run pytest \
  tests/fetch/test_download.py::test_jci_fallback_downloads_official_table_after_archive_failure \
  tests/fetch/test_download.py::test_non_jci_candidate_never_resolves_jci_fallback \
  tests/fetch/test_download.py::test_jci_fallback_reports_no_supported_table_without_erasing_archive_failure \
  tests/fetch/test_cli.py::test_fetch_auto_uses_jci_fallback_after_archive_failure -q
```

Expected: failures because the JCI resolver/fallback behavior does not exist.

- [ ] **Step 3: Implement the bounded resolver and reuse direct publication**

In `src/paperconan/fetch/_download.py`:

```python
_JCI_DOI = re.compile(r"^10\.1172/JCI(\d+)$", re.I)
_JCI_PAGE_MAX_BYTES = 2 * 1024 * 1024

class _JCIHrefParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value)

def _resolve_jci_tabular_files(cand):
    match = _JCI_DOI.fullmatch(str(cand.get("doi") or "").strip())
    if match is None:
        return [], None
    page_url = f"https://www.jci.org/articles/view/{match.group(1)}/sd/3"
    # Fetch with a byte limit and same-origin redirect policy, parse links, resolve each
    # through _http.resolve_http_url(), and retain only supported tabular URL paths.
```

Refactor the existing direct-file loop into a local helper inside `download_candidate()` so both original direct references and fallback references use the identical secure path. After OA and supplementary archive attempts, call the resolver only when no published output has asset type `tabular`; append its neutral skipped reason if resolution yields no table.

- [ ] **Step 4: Run the focused fetch tests and verify GREEN**

Run the command from Step 2.

Expected: all four tests pass.

- [ ] **Step 5: Run the complete fetch test group**

Run:

```bash
uv run pytest tests/fetch -q
```

Expected: all fetch tests pass with only declared network skips.

### Task 2: Exact Repeated-Segment Coordinates

**Files:**
- Modify: `src/paperconan/_audit.py`
- Modify: `src/paperconan/_html.py`
- Test: `tests/test_within_row_repeated_segment.py`

**Interfaces:**
- Consumes: the existing zero-based row `r`, numeric-cell sequence `seq`, and non-overlapping sequence starts `chosen`.
- Produces: finding fields `row: int`, `row_idx: int`, and ordered `occurrences: list[dict]`, with occurrence keys `row`, `col_start`, `col_end`, and `range`.

- [ ] **Step 1: Add failing detector and HTML tests**

Create a synthetic sheet whose repeated four-value segment is on Excel row 19 at `B:E` and `H:K`, then assert:

```python
assert finding["row"] == 19
assert finding["row_idx"] == 18
assert finding["occurrences"] == [
    {"row": 19, "col_start": 2, "col_end": 5, "range": "B:E"},
    {"row": 19, "col_start": 8, "col_end": 11, "range": "H:K"},
]
assert "within row 19" in finding["rule"]
assert "(B:E ↔ H:K)" in finding["rule"]
```

Render a minimal scan with `write_html_report()` and assert its visible finding summary contains `row 19` and `B:E ↔ H:K`.

- [ ] **Step 2: Run the coordinate tests and verify RED**

Run:

```bash
uv run pytest tests/test_within_row_repeated_segment.py -q
```

Expected: new assertions fail because findings currently say only `within one row`.

- [ ] **Step 3: Add coordinate fields and HTML summary metadata**

In `src/paperconan/_audit.py`, convert each chosen sequence start to physical one-based spreadsheet columns:

```python
occurrences = []
for start in chosen:
    col_start = seq[start][0] + 1
    col_end = seq[start + len(vec) - 1][0] + 1
    occurrences.append({
        "row": r + 1,
        "col_start": col_start,
        "col_end": col_end,
        "range": f"{get_column_letter(col_start)}:{get_column_letter(col_end)}",
    })
```

Store `row=r + 1`, `row_idx=r`, and `occurrences=occurrences`; update the rule to name the row and join ranges with ` ↔ `. In `src/paperconan/_html.py`, add `row N · ranges` to `extra_meta` for `within_row_repeated_segment`.

- [ ] **Step 4: Run coordinate tests and verify GREEN**

Run the command from Step 2.

Expected: all repeated-segment tests pass.

- [ ] **Step 5: Run detector/report regression tests**

Run:

```bash
uv run pytest \
  tests/test_within_row_repeated_segment.py \
  tests/test_recurring_row_vector.py \
  tests/test_smoke.py \
  tests/test_golden_columnar.py -q
```

Expected: all selected tests pass.

### Task 3: Independent Review, Integration, And Release

**Files:**
- Modify only if review finds an issue in task-owned files.
- Build outputs: `dist/` (not committed).

**Interfaces:**
- Consumes: implementation diff from base `47c7689` to implementation HEAD.
- Produces: reviewed `main`, PyPI `paperconan==0.8.3`, and release provenance that does not silently rewrite an existing remote tag.

- [ ] **Step 1: Run the complete suite**

```bash
PYTHONPATH=/Users/xiaotong/Dev/paperconan uv run pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Commit only task-owned files**

```bash
git add \
  docs/superpowers/plans/2026-07-20-jci-fetch-and-repeat-coordinates.md \
  src/paperconan/fetch/_download.py \
  src/paperconan/_audit.py \
  src/paperconan/_html.py \
  tests/fetch/test_download.py \
  tests/fetch/test_cli.py \
  tests/test_within_row_repeated_segment.py
git commit -m "fix: retrieve JCI tables and report repeat coordinates"
```

- [ ] **Step 3: Dispatch independent code review**

Give a fresh code-review subagent the specification, this plan, base SHA `47c7689`, and implementation HEAD. Fix every Critical or Important finding with a new failing regression test where behavior changes, then rerun focused tests and the full suite.

- [ ] **Step 4: Push reviewed `main`**

```bash
git push origin main
```

Expected: push succeeds and remote `main` equals local `HEAD`.

- [ ] **Step 5: Confirm release state and build distributions**

Check the PyPI JSON API for `0.8.3`. If absent:

```bash
rm -rf dist build
uv build
python -m zipfile -l dist/paperconan-0.8.3-py3-none-any.whl
tar -tzf dist/paperconan-0.8.3.tar.gz
```

Inspect wheel/sdist metadata and confirm no local audit data or unrelated untracked files are packaged.

- [ ] **Step 6: Verify the wheel in a clean environment**

```bash
tmpvenv="$(mktemp -d)/venv"
python3 -m venv "$tmpvenv"
"$tmpvenv/bin/pip" install dist/paperconan-0.8.3-py3-none-any.whl
"$tmpvenv/bin/paperconan" --version
```

Expected: CLI reports `0.8.3`.

- [ ] **Step 7: Publish and verify PyPI**

Use configured `uv publish` or `twine upload` credentials without printing secrets. Then query the PyPI JSON API and install `paperconan==0.8.3` into another clean environment; both must report `0.8.3`.

- [ ] **Step 8: Reconcile the existing release tag**

The remote `v0.8.3` tag already exists at `57984f44e66a21ec536fdcddb8c8913b83900d8d`. Do not force-move it without explicit user approval. Report this provenance conflict after publishing, or obtain approval before replacing the remote tag so it points to the exact published commit.
