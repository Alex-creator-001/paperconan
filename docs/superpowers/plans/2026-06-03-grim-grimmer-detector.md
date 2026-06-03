# GRIM / GRIMMER Detector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GRIM/GRIMMER detector that flags reported means and SDs that are mathematically impossible for integer-granularity data at the stated sample size.

**Architecture:** Three pure, independently-testable math helpers (`_decimals_of`, `grim_consistent`, `grimmer_consistent`) plus a block detector `detect_grim_grimmer` matching the existing detector signature, wired into `scan_dir`'s per-block loop and rendered generically by the existing HTML/MD machinery. Strict gating (header-located mean/SD/n triple + integer-data keyword + GRIM power gate) keeps false positives near zero, matching paperconan's brand.

**Tech Stack:** Python 3.10+, stdlib `math`/`re`/`fractions`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-03-grim-grimmer-detector-design.md`

---

## File Structure

- **Modify** `src/paperconan/_audit.py` — add `import re`; add helpers `_decimals_of`, `grim_consistent`, `grimmer_consistent`; add detector `detect_grim_grimmer` + role/keyword regexes; extend `benign_reason`; wire into `scan_dir` and `write_markdown_report`.
- **Modify** `src/paperconan/_html.py` — add `"grim"` to `_PER_BLOCK_GROUPS` (one-line change; card rendering is already generic).
- **Create** `tests/test_grim.py` — pure-helper unit tests + brute-force no-false-positive oracle.
- **Modify** `tests/build_fixture.py` — add an integer-keyword summary sheet with one GRIM-impossible mean.
- **Create** `tests/test_grim_e2e.py` — end-to-end `scan_dir` test + continuous-data false-positive guard.
- **Modify** `README.md` — add GRIM/GRIMMER rows to the detector table.

The three math helpers are the correctness core and are tested in complete isolation from table-parsing — that is the key decomposition decision.

---

## Task 1: `_decimals_of` + `grim_consistent` pure helpers

**Files:**
- Modify: `src/paperconan/_audit.py` (add `import re` near line 23; add helpers after `trailing_decimal_digits`, ~line 84)
- Test: `tests/test_grim.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_grim.py`:

```python
"""Unit tests for the GRIM/GRIMMER pure math helpers.

The decisive test is the brute-force oracle: every (mean, sd) reachable by an
actual integer dataset MUST be reported consistent, so the detector can never
false-positive on real integer data.
"""
from __future__ import annotations

import itertools
import math

from paperconan._audit import _decimals_of, grim_consistent, grimmer_consistent


def test_decimals_of_counts_displayed_places():
    assert _decimals_of(3.45) == 2
    assert _decimals_of(3.4) == 1
    assert _decimals_of(2.0) == 0
    assert _decimals_of(5) == 0
    assert _decimals_of(0.125) == 3


def test_grim_hand_oracles():
    # mean 3.45 with n=10 is impossible: integer totals give only x.x0 means.
    assert grim_consistent(3.45, 10, 2) is False
    assert grim_consistent(3.40, 10, 1) is True
    # n=3: achievable 2-dp means are round(t/3, 2); 3.50 is not one, 3.33 is.
    assert grim_consistent(3.50, 3, 2) is False
    assert grim_consistent(3.33, 3, 2) is True


def test_grim_never_flags_achievable_integer_means():
    # Brute-force oracle: any mean from a real integer dataset must be consistent.
    for n in range(2, 8):
        for combo in itertools.combinations_with_replacement(range(0, 7), n):
            for d in (1, 2):
                mean = round(sum(combo) / n, d)
                assert grim_consistent(mean, n, d) is True, (combo, n, d, mean)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grim.py -q`
Expected: FAIL with `ImportError: cannot import name '_decimals_of'`

- [ ] **Step 3: Add `import re` and the helpers**

In `src/paperconan/_audit.py`, add `import re` in the stdlib import block (after `import os`, ~line 29).

Then add these helpers immediately after `trailing_decimal_digits` (after line 83, before `load_workbook_rows`):

```python
def _decimals_of(x, cap=6):
    """Number of significant decimal places in x's shortest float repr, capped.

    Cells are coerced to float on load, so displayed trailing zeros are lost.
    Recovering decimals from the float repr therefore UNDER-counts precision for
    values like 2.50 -> 2.5. That is conservatively safe for GRIM: fewer decimals
    means a coarser grid and fewer flags, never a false flag."""
    s = repr(float(x))
    if "e" in s or "E" in s:
        return cap  # scientific notation: assume high precision (conservative)
    if "." not in s:
        return 0
    frac = s.split(".", 1)[1].rstrip("0")
    return min(len(frac), cap)


def grim_consistent(mean, n, decimals):
    """True if `mean`, reported to `decimals` places, is achievable as an integer
    total divided by `n`. Conservative: any bracketing integer total that rounds
    back to the reported mean counts as consistent (tolerant of the rounding
    convention used by the authors)."""
    if n <= 0:
        return True
    scale = 10 ** decimals
    target = round(mean * scale)
    base = mean * n
    for t in (math.floor(base), math.ceil(base), round(base)):
        if round((t / n) * scale) == target:
            return True
    return False
```

- [ ] **Step 4: Run the test to verify GRIM passes (GRIMMER import still fails)**

Run: `python -m pytest tests/test_grim.py -q`
Expected: still FAIL — `grimmer_consistent` not defined yet (import error). GRIM tests are covered by Task 1; they pass once Task 2 makes the import succeed. Proceed to Task 2.

- [ ] **Step 5: Commit**

```bash
git add src/paperconan/_audit.py tests/test_grim.py
git commit -m "feat(audit): GRIM helper + decimals helper (pure, brute-force tested)"
```

---

## Task 2: `grimmer_consistent` pure helper

**Files:**
- Modify: `src/paperconan/_audit.py` (add helper directly after `grim_consistent`)
- Test: `tests/test_grim.py` (add cases)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_grim.py`:

```python
def test_grimmer_hand_oracles():
    # dataset {1,2,3}: mean=2.00, sample sd=1.00, n=3 -> consistent.
    assert grimmer_consistent(2.00, 1.00, 3, 2, 2) is True
    # same mean & n but sd 1.05 is unreachable by any integer triple -> inconsistent.
    assert grimmer_consistent(2.00, 1.05, 3, 2, 2) is False


def _sample_sd(combo, n):
    m = sum(combo) / n
    var = sum((x - m) ** 2 for x in combo) / (n - 1)
    return math.sqrt(var)


def test_grimmer_never_flags_achievable_integer_sds():
    # Brute-force oracle: any (mean, sd) from a real integer dataset that already
    # passes GRIM must also pass GRIMMER. Guarantees no false positives.
    for n in range(2, 7):
        for combo in itertools.combinations_with_replacement(range(0, 7), n):
            for d in (1, 2):
                mean = round(sum(combo) / n, d)
                sd = round(_sample_sd(combo, n), d)
                if not grim_consistent(mean, n, d):
                    continue
                assert grimmer_consistent(mean, sd, n, d, d) is True, (combo, n, d, mean, sd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grim.py -q`
Expected: FAIL with `ImportError: cannot import name 'grimmer_consistent'`

- [ ] **Step 3: Implement the helper**

Add directly after `grim_consistent` in `src/paperconan/_audit.py`:

```python
def grimmer_consistent(mean, sd, n, mean_decimals, sd_decimals):
    """True if a sample of `n` integers can have both the reported `mean` and the
    reported `sd` (to their stated decimals). Implements the GRIMMER test: for the
    integer total T fixed by the mean, search the integer sum-of-squares values
    whose implied sd rounds to the reported sd, and require one with the correct
    parity (since sum(x^2) == sum(x) mod 2 for integers). Accepts either sample
    (n-1) or population (n) SD convention so an unknown convention never
    false-positives."""
    if n <= 1 or sd < 0:
        return True
    T = round(mean * n)
    half = 0.5 / (10 ** sd_decimals)
    lo_sd = max(0.0, sd - half)
    hi_sd = sd + half
    for ddof in (1, 0):
        denom = n - ddof
        if denom <= 0:
            continue
        corr = (T * T) / n
        ss_lo = lo_sd * lo_sd * denom + corr
        ss_hi = hi_sd * hi_sd * denom + corr
        for ss in range(math.ceil(ss_lo - 1e-9), math.floor(ss_hi + 1e-9) + 1):
            if ss < 0:
                continue
            if (ss % 2) != (T % 2):       # integer parity test
                continue
            if ss + 1e-9 >= corr:          # variance >= 0
                return True
    return False
```

- [ ] **Step 4: Run the full pure-helper suite**

Run: `python -m pytest tests/test_grim.py -q`
Expected: PASS (all GRIM + GRIMMER tests, including both brute-force oracles)

- [ ] **Step 5: Commit**

```bash
git add src/paperconan/_audit.py tests/test_grim.py
git commit -m "feat(audit): GRIMMER helper (integer sum-of-squares parity test)"
```

---

## Task 3: `detect_grim_grimmer` block detector

**Files:**
- Modify: `src/paperconan/_audit.py` (add regexes + detector in the `# ---------- detectors ----------` section, e.g. after `detect_identical_after_rounding`, ~line 531)
- Test: `tests/test_grim.py` (add detector unit tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_grim.py`:

```python
from paperconan._audit import detect_grim_grimmer


def _block(rows):
    # header row 0; data rows 1..len-1; full width.
    return rows, 1, len(rows), 0, len(rows[0]), [str(x) for x in rows[0]]


def test_detector_flags_impossible_mean_with_integer_keyword():
    rows = [
        ["group", "cell count mean", "sd", "n"],
        ["A", 3.45, 1.0, 10],   # 3.45 impossible at n=10
        ["B", 3.40, 1.0, 10],   # fine
    ]
    findings = detect_grim_grimmer(*_block(rows))
    kinds = {f["kind"] for f in findings}
    assert "grim_inconsistent" in kinds
    grim = next(f for f in findings if f["kind"] == "grim_inconsistent")
    assert grim["severity"] == "high"
    assert grim["n_failed"] == 1
    assert grim["failed_rows"][0]["row"] == 2  # 1-based sheet row of group A


def test_detector_skips_without_integer_keyword():
    # No count/score keyword -> assume continuous -> never run (no false positive).
    rows = [
        ["group", "concentration mean", "sd", "n"],
        ["A", 3.45, 1.0, 10],
        ["B", 3.40, 1.0, 10],
    ]
    assert detect_grim_grimmer(*_block(rows)) == []


def test_detector_skips_without_n_column():
    rows = [
        ["group", "score mean", "sd"],
        ["A", 3.45, 1.0],
    ]
    assert detect_grim_grimmer(*_block(rows)) == []


def test_detector_power_gate_skips_large_n():
    # n=1000 >= 10^2 -> GRIM has no power -> no finding even though keyword present.
    rows = [
        ["group", "score mean", "sd", "n"],
        ["A", 3.45, 1.0, 1000],
    ]
    assert detect_grim_grimmer(*_block(rows)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grim.py -q`
Expected: FAIL with `ImportError: cannot import name 'detect_grim_grimmer'`

- [ ] **Step 3: Implement the detector**

Add these module-level regexes near the top of the `# ---------- detectors ----------` section (just after the comment, ~line 306):

```python
_GRIM_MEAN_RE = re.compile(r"\b(mean|average|avg)\b|均值|平均", re.I)
_GRIM_SD_RE = re.compile(r"\b(s\.?d\.?|std|sem|s\.?e\.?m?\.?)\b|标准差|标准误", re.I)
_GRIM_N_RE = re.compile(r"\bn\b|sample.?size|样本量|例数", re.I)
_GRIM_INT_RE = re.compile(
    r"count|number|cells|foci|colon|nuclei|score|rating|likert"
    r"|个数|数目|计数|数量|评分|#", re.I)
```

Add the detector after `detect_identical_after_rounding` (~line 531):

```python
def detect_grim_grimmer(rows, r0, r1, c0, c1, header):
    """GRIM/GRIMMER: flag reported means (and SDs) impossible for integer-valued
    data at the stated n. Strictly gated — needs a header-located mean+n triple
    AND a count/score keyword signalling integer items — to stay false-positive-safe
    on continuous measurements where GRIM does not apply."""
    findings = []
    mean_i = sd_i = n_i = None
    for idx, h in enumerate(header):
        h = str(h or "")
        if mean_i is None and _GRIM_MEAN_RE.search(h):
            mean_i = idx
        elif sd_i is None and _GRIM_SD_RE.search(h):
            sd_i = idx
        elif n_i is None and _GRIM_N_RE.search(h):
            n_i = idx
    if mean_i is None or n_i is None:
        return findings
    blob = " ".join(str(h or "") for h in header)
    if not (_GRIM_INT_RE.search(str(header[mean_i] or "")) or _GRIM_INT_RE.search(blob)):
        return findings

    mean_c, n_c = c0 + mean_i, c0 + n_i
    sd_c = c0 + sd_i if sd_i is not None else None
    grim_fail, grimmer_fail, checked = [], [], 0
    for r in range(r0, r1):
        mv = rows[r][mean_c] if mean_c < len(rows[r]) else None
        nv = rows[r][n_c] if n_c < len(rows[r]) else None
        if not (is_num(mv) and is_num(nv)):
            continue
        n = int(round(float(nv)))
        if n < 2:
            continue
        mean = float(mv)
        d = _decimals_of(mean)
        if n >= 10 ** d:                 # power gate: no discriminating power
            continue
        checked += 1
        if not grim_consistent(mean, n, d):
            grim_fail.append((r, mean, n, d))
            continue                     # GRIM-failing rows are not re-reported
        if sd_c is not None:
            sv = rows[r][sd_c] if sd_c < len(rows[r]) else None
            if is_num(sv):
                sd = float(sv)
                ds = _decimals_of(sd)
                if not grimmer_consistent(mean, sd, n, d, ds):
                    grimmer_fail.append((r, mean, sd, n, ds))

    mean_name = str(header[mean_i] or f"col{mean_c}")
    n_name = str(header[n_i] or f"col{n_c}")
    sd_name = str(header[sd_i] or f"col{sd_c}") if sd_i is not None else None

    if grim_fail:
        f = dict(kind="grim_inconsistent", severity="high",
                 mean_col=mean_name, n_col=n_name, sd_col=sd_name,
                 col_a_idx=mean_c,
                 n=checked, n_rows_checked=checked, n_failed=len(grim_fail),
                 failed_rows=[dict(row=r + 1, mean=m, n=nn, decimals=dd,
                                   nearest_consistent=round(round(m * nn) / nn, dd))
                              for (r, m, nn, dd) in grim_fail[:8]],
                 example_cells=[[r + 1, mean_c + 1] for (r, *_rest) in grim_fail],
                 rule=(f"{len(grim_fail)}/{checked} rows report a mean impossible for "
                       f"integer data at the stated n (GRIM): col '{mean_name}'"))
        if sd_c is not None:
            f["col_b_idx"] = sd_c
        findings.append(f)
    if grimmer_fail:
        findings.append(dict(
            kind="grimmer_inconsistent", severity="high",
            mean_col=mean_name, n_col=n_name, sd_col=sd_name,
            col_a_idx=mean_c, col_b_idx=sd_c,
            n=checked, n_rows_checked=checked, n_failed=len(grimmer_fail),
            failed_rows=[dict(row=r + 1, mean=m, sd=s, n=nn, sd_decimals=ds)
                         for (r, m, s, nn, ds) in grimmer_fail[:8]],
            example_cells=[[r + 1, sd_c + 1] for (r, *_rest) in grimmer_fail],
            rule=(f"{len(grimmer_fail)}/{checked} rows report an SD impossible for "
                  f"integer data at the stated mean & n (GRIMMER): col '{sd_name}'")))
    return findings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_grim.py -q`
Expected: PASS (all helper + detector tests)

- [ ] **Step 5: Commit**

```bash
git add src/paperconan/_audit.py tests/test_grim.py
git commit -m "feat(audit): detect_grim_grimmer block detector (header-gated, integer-keyword-gated)"
```

---

## Task 4: Benign caveat for GRIM/GRIMMER findings

**Files:**
- Modify: `src/paperconan/_audit.py` (`benign_reason`, ~line 246-271)
- Test: `tests/test_grim.py` (add case)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_grim.py`:

```python
from paperconan._audit import benign_reason


def test_grim_findings_carry_benign_caveat():
    reason = benign_reason({"kind": "grim_inconsistent"})
    assert reason and "integer" in reason.lower()
    assert benign_reason({"kind": "grimmer_inconsistent"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_grim.py::test_grim_findings_carry_benign_caveat -q`
Expected: FAIL (`benign_reason` returns `None` → `assert reason` fails)

- [ ] **Step 3: Add the case**

In `benign_reason`, just before the final `return None` (line 271), add:

```python
    if kind in ("grim_inconsistent", "grimmer_inconsistent"):
        return ("GRIM/GRIMMER assume the statistic is a mean of integer-valued "
                "items (counts/scores); verify the measure is integer-granular "
                "before acting")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_grim.py::test_grim_findings_carry_benign_caveat -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/paperconan/_audit.py tests/test_grim.py
git commit -m "feat(audit): standing benign caveat on GRIM/GRIMMER findings"
```

---

## Task 5: Wire the detector into `scan_dir` and the markdown report

**Files:**
- Modify: `src/paperconan/_audit.py` (`scan_dir` per-block loop ~line 910-924; `write_markdown_report` ~line 995-998)
- Modify: `tests/build_fixture.py` (add a GRIM summary sheet)
- Test: `tests/test_grim_e2e.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_grim_e2e.py`:

```python
"""End-to-end: GRIM/GRIMMER surfaces through scan_dir, and continuous data does not."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from build_fixture import build  # noqa: E402

from paperconan import scan_dir  # noqa: E402


def _grim_findings(scan):
    out = []
    for blk in scan.get("relations_blocks", []) or []:
        out.extend(blk.get("grim", []) or [])
    return out


def test_scan_dir_surfaces_grim(tmp_path):
    d = tmp_path / "paper"
    d.mkdir()
    build(str(d))
    res = scan_dir(str(d), str(tmp_path / "out"), write_html=True)
    kinds = {f["kind"] for f in _grim_findings(res)}
    assert "grim_inconsistent" in kinds, f"expected grim_inconsistent, got {kinds}"
    # It must also render into the HTML report.
    html = (tmp_path / "out" / "report.html").read_text(encoding="utf-8")
    assert "grim_inconsistent" in html


def test_continuous_data_yields_no_grim(tmp_path):
    data = tmp_path / "cont"
    data.mkdir()
    # mean/sd/n columns but a continuous-measure header (no integer keyword).
    csv = "group,concentration mean,sd,n\nA,3.45,1.10,10\nB,3.51,1.20,10\nC,3.49,1.05,10\n"
    (data / "cont.csv").write_text(csv, encoding="utf-8")
    res = scan_dir(str(data), str(tmp_path / "out2"), write_html=False)
    assert _grim_findings(res) == []
```

- [ ] **Step 2: Add a GRIM sheet to the fixture builder**

In `tests/build_fixture.py`, inside `build()` just before `path = os.path.join(...)` (line 59), add a third sheet:

```python
    # Summary-statistics sheet with an integer-item keyword and one GRIM-impossible
    # mean (3.45 is unreachable as an integer total / 10).
    ws3 = wb.create_sheet("Fig3_counts")
    ws3.append(["group", "cell count mean", "sd", "n"])
    ws3.append(["control", 3.40, 1.0, 10])   # consistent
    ws3.append(["treated", 3.45, 1.0, 10])   # GRIM-impossible
    ws3.append(["rescue", 3.30, 1.0, 10])    # consistent
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_grim_e2e.py -q`
Expected: FAIL on `test_scan_dir_surfaces_grim` — `scan_dir` does not call the detector yet, so `blk.get("grim")` is empty.

- [ ] **Step 4: Wire the detector into `scan_dir`**

In `scan_dir` (~line 910-924), add the `gg` call and thread it through. Replace the block:

```python
                wc = detect_within_column_patterns(rows, r0, r1, c0, c1, header)
                iar = detect_identical_after_rounding(rows, r0, r1, c0, c1, header)
                if rel or ap or eq or wc or iar:
                    for group in (rel, ap, eq, wc, iar):
                        _attach_evidence(group, rows, r0, r1, c0, c1, header)
                        _attach_benign(group)
                    report_blocks.append(dict(file=os.path.basename(f), sheet=sn,
                                              block=dict(rows=f"{r0+1}-{r1}", cols=f"{c0+1}-{c1}", header=header),
                                              relations=rel, progressions=ap, equal_pairs=eq,
                                              within_col=wc, identical_after_rounding=iar))
```

with:

```python
                wc = detect_within_column_patterns(rows, r0, r1, c0, c1, header)
                iar = detect_identical_after_rounding(rows, r0, r1, c0, c1, header)
                gg = detect_grim_grimmer(rows, r0, r1, c0, c1, header)
                if rel or ap or eq or wc or iar or gg:
                    for group in (rel, ap, eq, wc, iar, gg):
                        _attach_evidence(group, rows, r0, r1, c0, c1, header)
                        _attach_benign(group)
                    report_blocks.append(dict(file=os.path.basename(f), sheet=sn,
                                              block=dict(rows=f"{r0+1}-{r1}", cols=f"{c0+1}-{c1}", header=header),
                                              relations=rel, progressions=ap, equal_pairs=eq,
                                              within_col=wc, identical_after_rounding=iar,
                                              grim=gg))
```

- [ ] **Step 5: Add GRIM findings to the markdown report**

In `write_markdown_report`, in the per-block loop (~line 997-998), after the `identical_after_rounding` loop add:

```python
        for r in b.get("grim", []):
            push(b, r)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_grim_e2e.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/paperconan/_audit.py tests/build_fixture.py tests/test_grim_e2e.py
git commit -m "feat(audit): wire GRIM/GRIMMER into scan_dir + markdown report + fixture"
```

---

## Task 6: Render the `grim` group in the HTML report

**Files:**
- Modify: `src/paperconan/_html.py` (`_PER_BLOCK_GROUPS`, line 40-41)
- Test: covered by `tests/test_grim_e2e.py::test_scan_dir_surfaces_grim` (HTML assertion)

- [ ] **Step 1: Confirm the HTML test currently fails on the HTML assertion**

Temporarily, the e2e test's HTML assertion (`assert "grim_inconsistent" in html`) passes only if the renderer iterates the `grim` group. Run:

Run: `python -m pytest "tests/test_grim_e2e.py::test_scan_dir_surfaces_grim" -q`
Expected: FAIL on the HTML assertion (`grim_inconsistent` not in html) — the scan finds it but the renderer skips the `grim` group.

- [ ] **Step 2: Add `"grim"` to `_PER_BLOCK_GROUPS`**

In `src/paperconan/_html.py`, change (line 40-41):

```python
_PER_BLOCK_GROUPS = ("relations", "progressions", "equal_pairs",
                     "within_col", "identical_after_rounding")
```

to:

```python
_PER_BLOCK_GROUPS = ("relations", "progressions", "equal_pairs",
                     "within_col", "identical_after_rounding", "grim")
```

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/test_grim_e2e.py -q`
Expected: PASS (both the scan and HTML assertions)

- [ ] **Step 4: Commit**

```bash
git add src/paperconan/_html.py
git commit -m "feat(html): render grim/grimmer findings in the HTML report"
```

---

## Task 7: Full regression run + README detector table

**Files:**
- Modify: `README.md` (detector table, ~lines 30-45 in the "它能找出什么" table)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: PASS — all pre-existing tests (smoke, benign, collisions, fdr, extract, relations_flood) plus the new `test_grim.py` and `test_grim_e2e.py`. If any pre-existing test fails, investigate before proceeding (the smoke test's `_collect_kinds` does not include `"grim"`, but the fixture's other sheets still trigger the detectors it asserts, so it should remain green).

- [ ] **Step 2: Add README detector rows**

In `README.md`, add two rows to the detector table (the markdown table under "它能找出什么"), after the `identical_after_rounding` row:

```markdown
| `grim_inconsistent` | 报告的均值在该 n 下对整数数据不可能（GRIM） | "n=10 的细胞计数均值出现 3.45——整数和除以 10 给不出这个值" |
| `grimmer_inconsistent` | 报告的 SD 在该均值与 n 下对整数数据不可能（GRIMMER） | "均值/ n 自洽，但这个 SD 没有任何整数样本能产生" |
```

- [ ] **Step 3: Verify the README renders the new rows**

Run: `grep -n "grim_inconsistent\|grimmer_inconsistent" README.md`
Expected: two matching lines.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document GRIM/GRIMMER detectors in the detector table"
```

---

## Self-Review

**Spec coverage:**
- Header-driven triple location → Task 3 (`_GRIM_MEAN_RE`/`_GRIM_SD_RE`/`_GRIM_N_RE`, skip when no n).
- Strict integer-data gate → Task 3 (`_GRIM_INT_RE` over mean header + block blob).
- Power gate `n ≥ 10^d` → Task 3 (skip row).
- GRIM bracketing-integer test → Task 1 (`grim_consistent`).
- GRIMMER integer-SS parity + round-trip, both ddof → Task 2 (`grimmer_consistent`).
- Aggregated one-finding-per-kind output with `failed_rows`, `example_cells` → Task 3.
- Benign caveat → Task 4.
- Wiring scan_dir + HTML + MD → Tasks 5, 6.
- Brute-force oracle + hand oracles + fixture + continuous-data FP guard → Tasks 1, 2, 5.
- README detector table → Task 7.

**Placeholder scan:** none — every code/command step shows full content.

**Type consistency:** `_decimals_of`, `grim_consistent(mean, n, decimals)`, `grimmer_consistent(mean, sd, n, mean_decimals, sd_decimals)`, `detect_grim_grimmer(rows, r0, r1, c0, c1, header)` used identically across tasks. Finding keys (`kind`, `severity`, `col_a_idx`, `col_b_idx`, `n`, `failed_rows`, `example_cells`, `rule`) match how `_attach_evidence` (reads `col_a_idx`/`col_b_idx`/`example_cells`) and `_render_finding_card` (reads `kind`/`severity`/`rule`/`n`/`evidence`/`likely_benign`) consume them. Block group key `"grim"` consistent across `scan_dir`, `_PER_BLOCK_GROUPS`, `write_markdown_report`, and both e2e helpers.
