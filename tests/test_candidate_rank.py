"""回归测试：r24_raw_surge_preload 的 candidate_rank_mode 分支实现。

与 duo-live/tests/test_candidate_rank.py 成对 —— 两处 ``_candidate_rank_score``
必须行为一致，否则实盘与回测会错位。本测试只跑纯函数 + 排序截断，不依赖 DB。
"""

from __future__ import annotations

import math

import pytest

from moonshot.r24_raw_surge_preload import _candidate_rank_score, _passes_sell_surge_gate


# ---- 纯函数 ---------------------------------------------------------------


def test_sr_mode_returns_sr_value():
    assert _candidate_rank_score(pct_chg=15.0, sr=80.0, mode="sr") == 80.0
    assert _candidate_rank_score(pct_chg=99.0, sr=5.0, mode="sr") == 5.0


def test_pct_log_sr_formula():
    pct, sr = 15.0, 80.0
    expected = pct * math.log(1.0 + sr)
    assert _candidate_rank_score(pct, sr, "pct_log_sr") == pytest.approx(expected)


def test_pct_log_sr_mixed_case_and_whitespace():
    assert _candidate_rank_score(10, 20, " PCT_log_SR ") == pytest.approx(
        10 * math.log(21.0)
    )


def test_unknown_mode_falls_back_to_sr(caplog):
    with caplog.at_level("WARNING"):
        score = _candidate_rank_score(10.0, 7.0, "not_a_mode")
    assert score == 7.0
    assert any("candidate_rank_mode" in r.message for r in caplog.records)


def test_none_mode_defaults_to_sr():
    assert _candidate_rank_score(10.0, 7.0, None) == 7.0  # type: ignore[arg-type]


def test_negative_sr_guarded_by_max():
    assert _candidate_rank_score(20.0, -3.0, "pct_log_sr") == 0.0


def test_sell_surge_gate_respects_upper_bound():
    assert _passes_sell_surge_gate(10.0001, min_sr=10.0, max_sr=None) is True
    assert _passes_sell_surge_gate(10.0, min_sr=10.0, max_sr=None) is False  # strict >
    assert _passes_sell_surge_gate(11.0, min_sr=10.0, max_sr=11.0) is True
    assert _passes_sell_surge_gate(11.0001, min_sr=10.0, max_sr=11.0) is False


# ---- 与 duo-live 实现对齐 -------------------------------------------------


@pytest.mark.parametrize(
    "pct,sr",
    [
        (13.0, 11.0),
        (15.0, 50.0),
        (30.0, 120.0),
        (99.9, 0.5),
    ],
)
def test_score_numerically_matches_manual(pct, sr):
    assert _candidate_rank_score(pct, sr, "sr") == sr
    assert _candidate_rank_score(pct, sr, "pct_log_sr") == pytest.approx(
        pct * math.log(1.0 + sr)
    )


# ---- 集成：超过 top_n 时两种模式 Top-N 不同 --------------------------------


def _rank_top_n(cands, mode, top_n):
    """复刻 r24_raw_surge_preload 中卖量门之后的排序+截断。

    cands: [(symbol, pct, sr, yv)]
    """
    rows = sorted(
        cands,
        key=lambda x: _candidate_rank_score(x[1], x[2], mode),
        reverse=True,
    )
    return [r[0] for r in rows[:top_n]]


def test_top_n_selection_differs_between_modes():
    # 与 duo-live 测试完全一致的 fixture —— 两处排序行为必须等价
    cands = [
        ("HIGH_SR",  13.0, 120.0, 0.0),
        ("HIGH_PCT", 50.0, 15.0, 0.0),
        ("BALANCED", 30.0, 40.0, 0.0),
        ("LOW",      14.0, 11.0, 0.0),
        ("MID_SR",   13.5, 80.0, 0.0),
    ]
    assert _rank_top_n(cands, "sr", 2) == ["HIGH_SR", "MID_SR"]
    assert _rank_top_n(cands, "pct_log_sr", 2) == ["HIGH_PCT", "BALANCED"]


def test_top_n_selection_identical_when_below_capacity():
    """candidates ≤ top_n → 两模式选集相同；解释 sr/pct_log_sr 两次回测相同。"""
    cands = [
        ("A", 20.0, 50.0, 0.0),
        ("B", 15.0, 80.0, 0.0),
    ]
    assert (
        set(_rank_top_n(cands, "sr", 5))
        == set(_rank_top_n(cands, "pct_log_sr", 5))
        == {"A", "B"}
    )


def test_rank_score_mode_independence_when_pct_constant():
    cands = [
        ("A", 20.0, 10.0, 0.0),
        ("B", 20.0, 30.0, 0.0),
        ("C", 20.0, 60.0, 0.0),
    ]
    assert _rank_top_n(cands, "sr", 3) == _rank_top_n(cands, "pct_log_sr", 3)
