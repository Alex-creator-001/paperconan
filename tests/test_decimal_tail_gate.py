from __future__ import annotations

from paperconan._audit import _decimal_tail_constant_transform


def _pairs(va_vb):
    # detector pairs are (key_a, key_b, value_a, value_b, tail_sig)
    return [((0, 0), (0, 0), va, vb, "sig") for va, vb in va_vb]


def test_constant_offset_is_gated():
    # vb = va + 0.1 for every pair -> benign offset, tails preserved incidentally
    p = _pairs([(0.1129167, 0.2129167), (0.1195833, 0.2195833),
                (0.1329167, 0.2329167), (0.1529167, 0.2529167)])
    assert _decimal_tail_constant_transform(p) is True


def test_large_constant_offset_is_gated():
    p = _pairs([(10.5, 156.5), (20.5, 166.5), (30.5, 176.5), (40.5, 186.5)])  # +146
    assert _decimal_tail_constant_transform(p) is True


def test_constant_ratio_is_gated():
    p = _pairs([(2.0, 4.0), (3.0, 6.0), (5.0, 10.0), (7.0, 14.0)])  # x2
    assert _decimal_tail_constant_transform(p) is True


def test_irregular_differences_not_gated():
    # genuine leading-digit fabrication (38842-6 style): irregular per-pair diffs
    p = _pairs([(14.70300997, 6.70300997), (7.592733983, 4.592733983), (9.123456, 2.123456)])
    assert _decimal_tail_constant_transform(p) is False


def test_too_few_pairs_not_gated():
    assert _decimal_tail_constant_transform(_pairs([(1.0, 2.0), (2.0, 3.0)])) is False
