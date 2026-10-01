"""
Plain-pytest tests for SLABond's decision logic. No Studio, no GenVM needed.

The contract file marks its pure-Python core between PURE-LOGIC-BEGIN/END.
These tests extract that exact block and run it, so what is tested is what
gets deployed.

Run:  pip install pytest && pytest tests -v
"""
import json
import pathlib
import re

import pytest

CONTRACT = pathlib.Path(__file__).resolve().parent.parent / "contracts" / "sla_bond.py"
SOURCE = CONTRACT.read_text()


def _load_pure():
    m = re.search(r"# ---- PURE-LOGIC-BEGIN ----(.*?)# ---- PURE-LOGIC-END ----", SOURCE, re.S)
    assert m, "pure-logic markers missing from contract"
    ns = {"json": json}
    exec(m.group(1), ns)
    return ns


P = _load_pure()
extract, classify, equivalent = P["extract_metric_milli"], P["classify"], P["equivalent"]
decide, score = P["decide"], P["reputation_score"]


# ---------- extraction ----------
def test_extract_simple_and_nested():
    assert extract('{"uptime": 99.5}', "uptime") == 99500
    assert extract('{"a": {"b": {"c": "12.25"}}}', "a.b.c") == 12250
    assert extract('{"runs": [{"ms": 180}, {"ms": 220}]}', "runs.1.ms") == 220000


@pytest.mark.parametrize(
    "body,path",
    [
        ("not json", "x"),
        ('{"x": true}', "x"),
        ('{"x": null}', "x"),
        ('{"x": [1]}', "x"),
        ('{"x": -5}', "x"),
        ('{"x": "NaN"}', "x"),
        ('{"x": 1}', "y"),
        ('{"x": [1]}', "x.9"),
    ],
)
def test_extract_rejects_bad_input(body, path):
    assert extract(body, path) is None


# ---------- classification ----------
def test_classify_gte_bands():
    thr, tol = 99500, 100
    assert classify(99700, "gte", thr, tol) == "COMPLIANT"
    assert classify(99500, "gte", thr, tol) == "INCONCLUSIVE"
    assert classify(99401, "gte", thr, tol) == "INCONCLUSIVE"
    assert classify(99399, "gte", thr, tol) == "BREACH"
    assert classify(None, "gte", thr, tol) == "UNAVAILABLE"


def test_classify_lte_bands():
    thr, tol = 200000, 5000  # latency <= 200ms, +/-5ms grey zone
    assert classify(150000, "lte", thr, tol) == "COMPLIANT"
    assert classify(203000, "lte", thr, tol) == "INCONCLUSIVE"
    assert classify(260000, "lte", thr, tol) == "BREACH"


# ---------- validator equivalence ----------
def eq(lv, mv, cmp="gte", thr=99500, tol=100):
    return equivalent({"value_milli": lv}, {"value_milli": mv}, cmp, thr, tol)


def test_equivalent_accepts_small_drift_in_same_band():
    assert eq(98000, 98050)  # both clear breaches, 50 apart


def test_equivalent_rejects_drift_larger_than_tolerance():
    assert not eq(98000, 98300)


def test_equivalent_rejects_band_flip_even_when_close():
    assert not eq(99601, 99499)  # COMPLIANT vs INCONCLUSIVE


def test_equivalent_rejects_leader_lying_about_a_breach():
    # leader claims a breach, validator measured healthy service
    assert not eq(90000, 99900)


def test_equivalent_none_handling():
    assert eq(None, None)
    assert not eq(None, 99900)
    assert not eq(99900, None)


@pytest.mark.parametrize("bad", [{}, None, {"value_milli": "99900"}, {"value_milli": True}, []])
def test_equivalent_rejects_malformed_leader(bad):
    assert not equivalent(bad, {"value_milli": 99900}, "gte", 99500, 100)


# ---------- state transitions ----------
def test_compliant_resets_streak_no_breach():
    assert decide("COMPLIANT", 1, 3, 2) == ("compliant", 1, 0, False)


def test_grey_zone_never_penalises():
    assert decide("INCONCLUSIVE", 0, 3, 0) == ("inconclusive", 0, 0, False)


def test_breach_counts_and_terminates_at_max():
    assert decide("BREACH", 0, 2, 0) == ("breach", 1, 0, False)
    assert decide("BREACH", 1, 2, 0) == ("breach", 2, 0, True)


def test_withheld_data_becomes_breach_after_streak():
    e, b, s, t = decide("UNAVAILABLE", 0, 3, 0)
    assert (e, b, s, t) == ("no_data", 0, 1, False)
    e, b, s, t = decide("UNAVAILABLE", 0, 3, 1)
    assert (e, b, s, t) == ("no_data", 0, 2, False)
    e, b, s, t = decide("UNAVAILABLE", 0, 3, 2)
    assert (e, b, s, t) == ("breach", 1, 0, False)


def test_good_check_interrupts_withholding_streak():
    _, _, s, _ = decide("COMPLIANT", 0, 3, 2)
    assert s == 0


# ---------- reputation ----------
def test_reputation_score():
    assert score(0, 0) == 500
    assert score(10, 0) == 1000
    assert score(0, 4) == 0
    assert score(9, 1) == 750  # 9000 // 12


# ---------- structural guards on the contract file ----------
def test_pure_block_has_no_genlayer_calls():
    block = re.search(r"PURE-LOGIC-BEGIN ----(.*?)# ---- PURE-LOGIC-END", SOURCE, re.S).group(1)
    assert "gl." not in block and "self." not in block


def test_uses_custom_validator_not_strict_eq():
    assert "run_nondet_unsafe" in SOURCE
    assert "strict_eq" not in SOURCE


def test_no_rejected_runner_alias():
    assert "py-genlayer:test" not in SOURCE and "py-genlayer:latest" not in SOURCE


def test_nondet_calls_are_not_in_contract_methods():
    # web access lives only in the module-level helper used by leader/validator
    assert SOURCE.count("gl.nondet.web.get") == 1
