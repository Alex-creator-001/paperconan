# Cross-Sheet Prefilter Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add enough local label context to cross-sheet findings so PaperConan can safely auto-drop/downweight common shared-control/shared-axis false positives without suppressing true perfect duplicates.

**Architecture:** Keep the current broad cross-sheet detector, but enrich each finding with semantic context extracted from the original `Sheet` rows around matched cells. Then feed those labels into the existing relation prefilter path used by batch2 packets. The hard safety rule is unchanged: cross-figure/file `perfect_dup`, `mass=true`, or `fraction_of_smaller >= 0.9` must survive for judging.

**Tech Stack:** Python 3.14, pytest, existing `paperconan._audit.detect_collisions`, `paperconan._prefilter.make_finding`, batch2 `collect_paper.distill_and_filter`.

## Global Constraints

- Do not hard-code DOI, journal, paper title, school, author, or one-off numeric constants from a single paper.
- Prefer downweight over drop unless the benign explanation is structural and label-supported.
- Cross-sheet `perfect_dup`, `mass=true`, and `fraction_of_smaller >= 0.9` are KEEP-protected.
- Reuse existing detector/profile plumbing; do not add a second packet format.
- Verify against already judged frontier KEEP packets: no KEEP paper may become all-drop.

---

## File Structure

- Modify `src/paperconan/_audit.py`
  - Add cross-sheet context extraction from `Sheet` objects.
  - Pass both decimal grids and source sheets into collision detection.
  - Attach `label_context` and `shared_context` to cross-sheet findings.
- Modify `src/paperconan/_prefilter.py`
  - Use enriched labels, not only sheet names, for cross-sheet shared-control/shared-axis rules.
  - Keep the existing protected-repeat guard.
- Modify `recheck/codex_task/batch2/collect_paper.py`
  - Pass `shared_context`/`label_context` fields from cross-sheet scan findings into packet findings.
- Modify `tests/test_collisions.py`
  - Add unit tests proving label context extraction works and true duplicates stay high.
- Modify `tests/test_batch2_prefilter.py`
  - Add packet-level prefilter tests using enriched cross-sheet context.

---

### Task 1: Attach Local Labels To Cross-Sheet Matches

**Files:**
- Modify: `src/paperconan/_audit.py`
- Test: `tests/test_collisions.py`

**Interfaces:**
- Produces: `_label_context_for_matches(sheet: Sheet, matches: list[tuple[tuple[int, int], float]]) -> dict`
- Produces finding fields:
  - `label_context_a: dict`
  - `label_context_b: dict`
  - each dict has `column_labels: list[str]`, `row_labels: list[str]`, `nearby_labels: list[str]`, `text: str`
- Consumes: existing `Sheet` class and `detect_collisions` grids.

- [ ] **Step 1: Write failing tests for local label extraction**

Add to `tests/test_collisions.py`:

```python
def test_cross_sheet_finding_carries_matched_control_labels():
    from paperconan._audit import Sheet, detect_collisions

    rows_a = [
        ["condition", "day", "control", "treated"],
        ["rep1", 0.0, 1.23, 9.11],
        ["rep2", 1.0, 1.45, 9.31],
        ["rep3", 2.0, 1.67, 9.51],
        ["rep4", 3.0, 1.89, 9.71],
        ["rep5", 4.0, 2.01, 9.91],
        ["rep6", 5.0, 2.23, 10.11],
    ]
    rows_b = [
        ["condition", "day", "vehicle control", "drug B"],
        ["rep1", 0.0, 1.23, 4.11],
        ["rep2", 1.0, 1.45, 4.31],
        ["rep3", 2.0, 1.67, 4.51],
        ["rep4", 3.0, 1.89, 4.71],
        ["rep5", 4.0, 2.01, 4.91],
        ["rep6", 5.0, 2.23, 5.11],
    ]
    sheet_a = Sheet.from_rows(rows_a)
    sheet_b = Sheet.from_rows(rows_b)
    ga = {(r, c): sheet_a.float_at(r, c) for r in range(sheet_a.nrows) for c in range(sheet_a.ncols)
          if sheet_a.float_at(r, c) == sheet_a.float_at(r, c)}
    gb = {(r, c): sheet_b.float_at(r, c) for r in range(sheet_b.nrows) for c in range(sheet_b.ncols)
          if sheet_b.float_at(r, c) == sheet_b.float_at(r, c)}

    findings = detect_collisions(
        {("a.xlsx", "Fig. 1 control"): ga, ("b.xlsx", "Fig. 2 control"): gb},
        sheets={("a.xlsx", "Fig. 1 control"): sheet_a, ("b.xlsx", "Fig. 2 control"): sheet_b},
    )

    cf = findings[0]
    assert "control" in cf["label_context_a"]["text"].lower()
    assert "vehicle control" in cf["label_context_b"]["text"].lower()
    assert cf["shared_context"]["shared_control_or_baseline"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_collisions.py::test_cross_sheet_finding_carries_matched_control_labels -q
```

Expected: FAIL because `detect_collisions()` does not accept `sheets=` and no label context exists.

- [ ] **Step 3: Implement label-context extraction**

In `src/paperconan/_audit.py`, update `detect_collisions` signature:

```python
def detect_collisions(grids, profile="review", sheets=None):
```

Add helpers near `_axis_columns`:

```python
def _text_cell(sheet, r, c):
    if sheet is None or r < 0 or c < 0 or r >= sheet.nrows or c >= sheet.ncols:
        return ""
    v = sheet.cell(r, c)
    if isinstance(v, str):
        return v.strip()
    return ""


def _label_context_for_matches(sheet, shared, max_labels=40):
    if sheet is None:
        return {"column_labels": [], "row_labels": [], "nearby_labels": [], "text": ""}
    col_labels, row_labels, nearby = [], [], []
    for (r, c), _v in shared[:max_labels]:
        for rr in range(max(0, r - 3), r):
            label = _text_cell(sheet, rr, c)
            if label:
                col_labels.append(label)
        for cc in range(max(0, c - 3), c):
            label = _text_cell(sheet, r, cc)
            if label:
                row_labels.append(label)
        for rr in range(max(0, r - 2), min(sheet.nrows, r + 3)):
            for cc in range(max(0, c - 2), min(sheet.ncols, c + 3)):
                label = _text_cell(sheet, rr, cc)
                if label:
                    nearby.append(label)
    def uniq(vals):
        out = []
        seen = set()
        for val in vals:
            key = val.lower()
            if key not in seen:
                seen.add(key)
                out.append(val)
        return out[:max_labels]
    ctx = {
        "column_labels": uniq(col_labels),
        "row_labels": uniq(row_labels),
        "nearby_labels": uniq(nearby),
    }
    ctx["text"] = " ".join(ctx["column_labels"] + ctx["row_labels"] + ctx["nearby_labels"])
    return ctx
```

Inside `detect_collisions`, after computing `shared`, attach:

```python
label_context_a = _label_context_for_matches((sheets or {}).get(keys[i]), shared)
label_context_b = _label_context_for_matches((sheets or {}).get(keys[j]), shared)
```

Include those two fields in appended findings.

- [ ] **Step 4: Pass sheets from `scan_dir`**

In `src/paperconan/_audit.py::scan_dir`, initialize:

```python
grid_sheets = {}
```

When storing a grid:

```python
grids[(os.path.basename(f), sn)] = _grid_from_rows(sheet)
grid_sheets[(os.path.basename(f), sn)] = sheet
```

Call:

```python
cross_sheet_findings = detect_collisions(grids, profile=profile, sheets=grid_sheets)
```

- [ ] **Step 5: Run tests**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_collisions.py -q
```

Expected: PASS.

---

### Task 2: Compute Shared-Control And Shared-Axis Context From Labels

**Files:**
- Modify: `src/paperconan/_audit.py`
- Test: `tests/test_collisions.py`

**Interfaces:**
- Produces: `_shared_cross_sheet_context(ctx_a: dict, ctx_b: dict, pattern: str, fraction: float) -> dict`
- Adds `shared_context` to cross-sheet findings:
  - `shared_control_or_baseline: bool`
  - `shared_axis_or_coordinate: bool`
  - `context_reason: str | None`

- [ ] **Step 1: Write failing tests for shared-axis labels and perfect-dup protection**

Add:

```python
def test_cross_sheet_context_marks_time_axis_but_not_perfect_dup_drop_signal():
    from paperconan._audit import Sheet, detect_collisions

    rows_a = [["sample", "time", "signal"], ["r1", 0, 1.1], ["r2", 1, 1.3], ["r3", 2, 1.5],
              ["r4", 3, 1.7], ["r5", 4, 1.9], ["r6", 5, 2.1]]
    rows_b = [["sample", "time", "signal"], ["r1", 0, 8.1], ["r2", 1, 8.3], ["r3", 2, 8.5],
              ["r4", 3, 8.7], ["r5", 4, 8.9], ["r6", 5, 9.1]]
    sheet_a, sheet_b = Sheet.from_rows(rows_a), Sheet.from_rows(rows_b)
    ga = {(r, c): sheet_a.float_at(r, c) for r in range(sheet_a.nrows) for c in range(sheet_a.ncols)
          if sheet_a.float_at(r, c) == sheet_a.float_at(r, c)}
    gb = {(r, c): sheet_b.float_at(r, c) for r in range(sheet_b.nrows) for c in range(sheet_b.ncols)
          if sheet_b.float_at(r, c) == sheet_b.float_at(r, c)}

    cf = detect_collisions(
        {("a.xlsx", "Fig. 1"): ga, ("b.xlsx", "Fig. 2"): gb},
        sheets={("a.xlsx", "Fig. 1"): sheet_a, ("b.xlsx", "Fig. 2"): sheet_b},
    )[0]

    assert cf["shared_context"]["shared_axis_or_coordinate"] is True
    assert cf["delta"]["pattern"] != "perfect_dup"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_collisions.py::test_cross_sheet_context_marks_time_axis_but_not_perfect_dup_drop_signal -q
```

Expected: FAIL because `shared_context` does not exist.

- [ ] **Step 3: Implement label context classifiers**

In `src/paperconan/_audit.py`, add regexes near `_FIG_RE`:

```python
_CONTROL_BASELINE_LABEL_RE = _re.compile(
    r"\b(?:control|ctrl|baseline|vehicle|untreated|wt|wild[- ]?type|reference|mock|"
    r"naive|sham|pbs|dmso)\b|参照|对照|基线",
    _re.I,
)
_AXIS_CONTEXT_LABEL_RE = _re.compile(
    r"\b(?:time|day|dose|conc(?:entration)?|wavelength|m/z|mz|position|chr|"
    r"coordinate|coord|index|bin)\b|波长|时间|剂量",
    _re.I,
)
```

Add:

```python
def _shared_cross_sheet_context(ctx_a, ctx_b, pattern, fraction):
    text_a = (ctx_a or {}).get("text", "")
    text_b = (ctx_b or {}).get("text", "")
    both_control = bool(_CONTROL_BASELINE_LABEL_RE.search(text_a) and _CONTROL_BASELINE_LABEL_RE.search(text_b))
    either_axis = bool(_AXIS_CONTEXT_LABEL_RE.search(text_a) or _AXIS_CONTEXT_LABEL_RE.search(text_b))
    non_perfect = pattern != "perfect_dup" and (fraction is None or fraction < 0.9)
    reason = None
    if non_perfect and both_control:
        reason = "matched cells are labelled as shared control/baseline/reference context"
    elif non_perfect and either_axis:
        reason = "matched cells are labelled as shared axis/coordinate context"
    return {
        "shared_control_or_baseline": bool(non_perfect and both_control),
        "shared_axis_or_coordinate": bool(non_perfect and either_axis),
        "context_reason": reason,
    }
```

When appending position-identical findings, compute:

```python
fraction = same_pos / smaller
shared_context = _shared_cross_sheet_context(
    label_context_a, label_context_b, ctx_fields["delta"]["pattern"], fraction
)
```

Add `label_context_a`, `label_context_b`, `shared_context` to the finding dict.

- [ ] **Step 4: Run tests**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_collisions.py -q
```

Expected: PASS.

---

### Task 3: Route Enriched Cross-Sheet Context Into Packet Prefilter

**Files:**
- Modify: `src/paperconan/_prefilter.py`
- Modify: `recheck/codex_task/batch2/collect_paper.py`
- Test: `tests/test_batch2_prefilter.py`

**Interfaces:**
- Consumes `shared_context` and `label_context_a/b` via `make_finding(..., shared_context=..., label_context_a=..., label_context_b=...)`.
- Produces same packet schema plus these extra context fields.

- [ ] **Step 1: Write failing packet-level tests**

Add to `tests/test_batch2_prefilter.py`:

```python
def test_prefilter_drops_cross_sheet_enriched_shared_control_context():
    cp = _collector()
    f = cp.prefilter_relation_finding(
        "cross_sheet:value_tweaked",
        "Fig. 3B",
        "Fig. S6B",
        24,
        0.5,
        "24/48 same-position values",
        None,
        None,
        shared_context={"shared_control_or_baseline": True, "shared_axis_or_coordinate": False},
        label_context_a={"text": "control baseline"},
        label_context_b={"text": "vehicle control"},
    )

    assert f["prefilter"] == "drop"
    assert f["prefilter_reason"] == "shared_control_or_baseline"


def test_prefilter_downweights_cross_sheet_enriched_axis_context():
    cp = _collector()
    f = cp.prefilter_relation_finding(
        "cross_sheet:value_tweaked",
        "Fig. 1",
        "Fig. 2",
        40,
        0.45,
        "40/89 same-position values",
        None,
        None,
        shared_context={"shared_control_or_baseline": False, "shared_axis_or_coordinate": True},
        label_context_a={"text": "time"},
        label_context_b={"text": "time"},
    )

    assert f["prefilter"] == "downweight"
    assert f["prefilter_reason"] == "shared_axis_overlap"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_batch2_prefilter.py::test_prefilter_drops_cross_sheet_enriched_shared_control_context tests/test_batch2_prefilter.py::test_prefilter_downweights_cross_sheet_enriched_axis_context -q
```

Expected: FAIL because `_prefilter.py` ignores `shared_context`.

- [ ] **Step 3: Implement context-aware flags in `_prefilter.py`**

In `make_finding`, before `flags = { ... }`:

```python
shared_context = extra.get("shared_context") or {}
label_context_a = extra.get("label_context_a") or {}
label_context_b = extra.get("label_context_b") or {}
ctx_side_a = " ".join([side_a, str(label_context_a.get("text") or "")])
ctx_side_b = " ".join([side_b, str(label_context_b.get("text") or "")])
```

Change cross-sheet flags to:

```python
"cross_sheet_shared_control": (
    cross_sheet_non_perfect
    and (
        bool(shared_context.get("shared_control_or_baseline"))
        or _shared_control_or_baseline(ctx_side_a, ctx_side_b)
    )
),
"cross_sheet_axis_overlap": (
    cross_sheet_non_perfect
    and (
        bool(shared_context.get("shared_axis_or_coordinate"))
        or _cross_sheet_axis_overlap(ctx_side_a, ctx_side_b)
    )
),
```

After `finding.update(extra)`, make sure packet carries the extra fields unchanged.

- [ ] **Step 4: Pass fields from batch2 collector**

In `recheck/codex_task/batch2/collect_paper.py::distill_and_filter`, in the cross-sheet `findings.append(make_finding(...))` call, add:

```python
label_context_a=f.get("label_context_a"),
label_context_b=f.get("label_context_b"),
shared_context=f.get("shared_context"),
fraction_of_smaller=f.get("fraction_of_smaller"),
```

If `make_finding` already receives `frac`, do not use `fraction_of_smaller` to override; this field is for packet readability only.

- [ ] **Step 5: Run tests**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_batch2_prefilter.py -q
```

Expected: PASS.

---

### Task 4: Measure FP Reduction Against Judged Frontier Packets

**Files:**
- Create: `recheck/codex_task/batch2/eval_frontier_prefilter_context.py`

**Interfaces:**
- Consumes local cached packet JSON from `recheck/codex_task/batch2/judged` and remote DB verdict list.
- Produces stdout JSON summary:
  - `old_survivor_findings`
  - `new_drop_findings`
  - `paper_zero_relevant_survivor`
  - grouped by `cohort/verdict`

- [ ] **Step 1: Create evaluation script**

Create `recheck/codex_task/batch2/eval_frontier_prefilter_context.py` with the replay logic already used manually:

```python
#!/usr/bin/env python3
"""Replay current relation prefilter over locally cached judged frontier packets."""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import subprocess
import sys

from paperconan.detectors import prefilter_relation_finding


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "../../.."))


def slug(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi)


def find_packet(doi: str) -> str | None:
    s = slug(doi)
    candidates = [
        os.path.join(HERE, "judged", s + ".json"),
        os.path.join(HERE, "to_judge", s + ".json"),
    ]
    candidates += glob.glob(os.path.join(HERE, "batches", "*", "to_judge", s + ".json"))
    candidates += glob.glob(os.path.join(HERE, "batches", "*", "archived", s + ".json"))
    return next((p for p in candidates if os.path.exists(p)), None)


def parse_frac(f: dict) -> float | None:
    if f.get("fraction_of_smaller") is not None:
        try:
            return float(f["fraction_of_smaller"])
        except (TypeError, ValueError):
            pass
    if str(f.get("kind", "")) == "cross_sheet:perfect_dup":
        return 1.0
    m = re.search(r"\b(\d+)\s*/\s*(\d+)\b", f.get("rule") or "")
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    return None


def cohort_match(cohort: str, kind: str | None) -> bool:
    k = str(kind or "")
    return (
        cohort == "cross_sheet" and k.startswith("cross_sheet:")
    ) or (
        cohort == "identical_col" and k in {"identical_column", "many_equal_pairs"}
    )


def fetch_rows() -> list[dict]:
    w1 = os.environ.get("PCWATCH_W1", "20.119.176.244")
    key = os.path.expanduser("~/.ssh/id_rsa")
    sql = r'''
import json, os, psycopg
conn = psycopg.connect(os.environ["PCWATCH_DSN"])
cur = conn.cursor()
cur.execute("""
    SELECT doi, cohort, verdict
    FROM batch2
    WHERE phase='frontier'
      AND verdict IN ('DROP','KEEP','NEEDS_HUMAN')
      AND cohort IN ('cross_sheet','identical_col')
    ORDER BY cohort, verdict, doi
""")
for doi, cohort, verdict in cur.fetchall():
    print(json.dumps({"doi": doi, "cohort": cohort, "verdict": verdict}, ensure_ascii=False))
'''
    cmd = "sudo bash -c 'set -a;. /opt/pcwatch/env;set +a; /opt/pcwatch/venv/bin/python -'"
    p = subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
         f"azureuser@{w1}", cmd],
        input=sql.encode(),
        capture_output=True,
        timeout=180,
    )
    if p.returncode:
        sys.stderr.write(p.stderr.decode())
        raise SystemExit(p.returncode)
    return [json.loads(x) for x in p.stdout.decode().splitlines() if x.startswith("{")]


def main() -> None:
    rows = fetch_rows()
    stats = collections.defaultdict(collections.Counter)
    reasons = collections.defaultdict(collections.Counter)
    missing = []
    for row in rows:
        path = find_packet(row["doi"])
        if not path:
            missing.append(row["doi"])
            continue
        packet = json.load(open(path, encoding="utf-8"))
        old = [f for f in packet.get("findings", [])
               if f.get("prefilter") != "drop" and cohort_match(row["cohort"], f.get("kind"))]
        new = []
        for f in old:
            pf = prefilter_relation_finding(
                f.get("kind"), f.get("col_a"), f.get("col_b"), int(f.get("n") or 0),
                parse_frac(f), f.get("rule"), f.get("top5_a") or f.get("col_a_sample"),
                f.get("top5_b") or f.get("col_b_sample"),
                slope=f.get("slope"), intercept=f.get("intercept"),
                figure_a=f.get("figure_a"), figure_b=f.get("figure_b"),
                label_context_a=f.get("label_context_a"), label_context_b=f.get("label_context_b"),
                shared_context=f.get("shared_context"),
            )
            new.append(pf)
            if pf.get("prefilter") == "drop" and f.get("prefilter") != "drop":
                reasons[(row["cohort"], row["verdict"])][pf.get("prefilter_reason")] += 1
        key = (row["cohort"], row["verdict"])
        stats[key]["papers"] += 1
        stats[key]["old_survivor_findings"] += len(old)
        stats[key]["new_survivor_findings"] += sum(1 for pf in new if pf.get("prefilter") != "drop")
        stats[key]["new_drop_findings"] += sum(1 for pf in new if pf.get("prefilter") == "drop")
        if old and new and all(pf.get("prefilter") == "drop" for pf in new):
            stats[key]["paper_zero_relevant_survivor"] += 1
    print(json.dumps({"db_rows": len(rows), "missing_packets": len(missing)}, ensure_ascii=False))
    for key in sorted(stats):
        c = stats[key]
        old = c["old_survivor_findings"]
        print(json.dumps({
            "cohort": key[0],
            "verdict": key[1],
            **dict(c),
            "finding_reduction": round(c["new_drop_findings"] / old, 4) if old else None,
            "paper_zero_rate": round(c["paper_zero_relevant_survivor"] / c["papers"], 4) if c["papers"] else None,
            "drop_reasons": dict(reasons[key]),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run evaluation**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python recheck/codex_task/batch2/eval_frontier_prefilter_context.py
```

Expected: JSON summary. Success threshold for this work:

- `cross_sheet/DROP` finding reduction should materially exceed the current 0%.
- `cross_sheet/KEEP` `paper_zero_relevant_survivor` must be 0.
- Any `KEEP` finding newly dropped must be inspected manually; if it is the KEEP-driving evidence, tighten rules.

---

### Task 5: Run Full Verification And Deploy Decision

**Files:**
- No new source files.

**Interfaces:**
- Consumes outputs of Tasks 1-4.
- Produces deployment decision.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest tests/test_collisions.py tests/test_batch2_prefilter.py tests/test_profiles.py tests/test_within_col_prefilter.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full tests**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python -m pytest -q
```

Expected: all pass, currently around `292 passed, 3 skipped`.

- [ ] **Step 3: Run frontier replay**

Run:

```bash
/Users/xiaotong/Dev/paperconan/.venv/bin/python recheck/codex_task/batch2/eval_frontier_prefilter_context.py
```

Expected:

- No KEEP paper becomes all-drop.
- Report exact cross_sheet DROP reduction and identical_col DROP reduction.

- [ ] **Step 4: Decide deploy**

Deploy to fleet only if:

- Full pytest passes.
- Frontier KEEP paper-level FN is 0.
- cross_sheet DROP paper-zero or finding-reduction is high enough to save real judging time.

If cross_sheet reduction is still low, do not deploy as an optimization; instead inspect whether source sheet labels are missing from the source formats or whether label extraction needs row/column-window tuning.

---

## Self-Review

- Spec coverage: covers the missing shared-control/shared-axis context, packet routing, KEEP protection, and replay measurement.
- Placeholder scan: no TBD placeholders; each task has concrete files, functions, commands, and expected outputs.
- Type consistency: `label_context_a/b` and `shared_context` are created in `_audit.py`, passed through `collect_paper.py`, consumed by `_prefilter.py`, and measured by `eval_frontier_prefilter_context.py`.
