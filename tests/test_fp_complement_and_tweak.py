"""FP-feedback regression tests from the 2026-06-24 batch2 KEEP audit review.

Two lessons, codified so the patterns stay covered:

1. Complementary percentages that sum to 100 (e.g. a Novel-Object-Recognition test's
   "Old" vs "New" exploration-time columns, or any two fractions that partition a whole)
   are a benign mathematical artifact, NOT data manipulation. The prefilter must DROP
   them even when neither column carries an explicit "%"/"percent" label — the decision
   has to fall back to the sample values summing to a constant. This was the cause of a
   false-positive KEEP (10.1038/s42003-025-08691-8, corrected to DROP).

2. A cross-sheet `value_tweaked` overlap is not one thing. When only a handful of aligned
   cells differ, it is the copy-then-edit fingerprint (someone duplicated a block and
   retyped a few numbers) — the strongest manual-edit signal. `value_tweak_subtype`
   classifies an existing `_value_delta` result as `copy_then_edit` vs `block_edit`
   without changing detector output (so the golden snapshot is untouched).
"""

from paperconan import detectors
from paperconan._audit import _value_delta, value_tweak_subtype


# --- 1. complement / sum-to-100 false positives -----------------------------------

def test_prefilter_drops_complement_sum_to_100_without_percent_label():
    # NOR test: Old + New exploration time partition to 100 by construction; labels carry
    # no "%" marker, so the drop must come from the values summing to 100.
    old = [30.8318, 42.1053, 30.2991, 18.1191, 32.2412]
    new = [69.1682, 57.8947, 69.7009, 81.8809, 67.7588]
    f = detectors.prefilter_relation_finding(
        "sum_constant", "Old", "New", 15, None, "col[0] + col[1] = 100", old, new
    )
    assert f["prefilter"] == "drop"
    assert f["prefilter_reason"] == "complement_percentage_sum_to_100"


def test_prefilter_drops_complement_exact_linear_negative_unit_intercept():
    # The same relationship restated as a line y = -x + 100 must also drop.
    old = [30.8318, 42.1053, 30.2991, 18.1191, 32.2412]
    new = [69.1682, 57.8947, 69.7009, 81.8809, 67.7588]
    f = detectors.prefilter_relation_finding(
        "exact_linear", "col0", "col1", 15, None, "col[1] = -1 * col[0] + 100",
        old, new, slope=-1, intercept=100,
    )
    assert f["prefilter"] == "drop"
    assert f["prefilter_reason"] == "complement_percentage_sum_to_100"


# --- 2. value_tweaked sub-typing (copy-then-edit vs block edit) --------------------

def _aligned_grid(n):
    return {(i, 0): float(i) + 0.5 for i in range(n)}


def test_value_delta_flags_copy_then_edit_for_few_cell_retype():
    ga = _aligned_grid(60)
    gb = dict(ga)
    gb[(7, 0)] = 999.123            # one cell retyped to a value unique to b
    d = _value_delta(ga, gb)
    assert d["pattern"] == "value_tweaked"
    assert d["modified_cells"] == 1
    assert value_tweak_subtype(d) == "copy_then_edit"


def test_value_delta_flags_block_edit_for_many_changed_cells():
    ga = _aligned_grid(60)
    gb = dict(ga)
    for i in range(20):            # 20/60 aligned cells changed -> heavier rewrite
        gb[(i, 0)] = 1000.0 + i
    d = _value_delta(ga, gb)
    assert d["pattern"] == "value_tweaked"
    assert d["modified_cells"] == 20
    assert value_tweak_subtype(d) == "block_edit"


def test_value_tweak_subtype_none_for_perfect_dup():
    ga = _aligned_grid(40)
    d = _value_delta(ga, dict(ga))
    assert d["pattern"] == "perfect_dup"
    assert value_tweak_subtype(d) is None
