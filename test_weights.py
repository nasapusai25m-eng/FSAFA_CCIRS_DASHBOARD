"""Linked weight sliders: rebalance maths, resets via callbacks, totals always 100."""
import pytest

import config
import scoring as S


# ------------------------------------------------------------------ rebalance maths
def test_rebalance_proportional_and_exact_100():
    out = S.rebalance({"A": 30, "B": 25, "C": 25, "D": 20}, "A", 50)
    assert out == {"A": 50, "B": 18, "C": 18, "D": 14} and sum(out.values()) == 100


def test_rebalance_others_zero_split_equally():
    assert S.rebalance({"A": 30, "B": 0, "C": 0, "D": 0}, "A", 40) == {"A": 40, "B": 20, "C": 20, "D": 20}


def test_rebalance_rounding_goes_to_largest_other():
    out = S.rebalance({"a": 10, "b": 10, "c": 10, "d": 70}, "a", 11)     # remainder 89 over 10/10/70
    assert sum(out.values()) == 100 and out["d"] == 89 - out["b"] - out["c"]


@pytest.mark.parametrize("v,expect", [(150, 100), (-20, 0), (37.6, 38)])
def test_rebalance_clips_and_rounds(v, expect):
    out = S.rebalance({"A": 30, "B": 25, "C": 25, "D": 20}, "A", v)
    assert out["A"] == expect and sum(out.values()) == 100 and all(0 <= x <= 100 for x in out.values())


def test_rebalance_many_random_moves_stay_valid():
    import random
    rnd = random.Random(1)
    w = {"A": 30, "B": 25, "C": 25, "D": 20}
    for _ in range(500):
        k = rnd.choice(list(w))
        w = S.rebalance(w, k, rnd.randint(0, 100))
        assert sum(w.values()) == 100 and all(isinstance(x, int) and 0 <= x <= 100 for x in w.values())


def test_to_whole_100():
    assert S.to_whole_100({"x": 1, "y": 1, "z": 1}) == {"x": 34, "y": 33, "z": 33}
    assert sum(S.to_whole_100({"a": 0.3, "b": 0.7}).values()) == 100


def test_default_subweights_sum_100():
    for spec in config.PILLARS.values():
        assert sum(spec["ratios"].values()) == 100


# ------------------------------------------------------------------ app behaviour
def _demo_app():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    at.sidebar.radio[0].set_value("Demo mode (hypothetical sample)").run()
    at.sidebar.button[0].click().run()
    assert not at.exception
    return at


def _pillars(at):
    return {p: at.session_state[f"w_p_{p}"] for p in config.PILLARS}


def test_moving_pillar_slider_rebalances_others():
    at = _demo_app()
    at.slider(key="w_p_A").set_value(50).run()
    assert not at.exception
    assert _pillars(at) == {"A": 50, "B": 18, "C": 18, "D": 14}
    assert any(m.value == "**Total: 100%**" for m in at.markdown)


def test_moving_subweight_rebalances_within_pillar():
    at = _demo_app()
    at.slider(key="w_r_A_cce").set_value(55).run()
    assert not at.exception
    sub = {k: at.session_state[f"w_r_A_{k}"] for k in config.PILLARS["A"]["ratios"]}
    assert sub["cce"] == 55 and sum(sub.values()) == 100
    assert _pillars(at) == {"A": 30, "B": 25, "C": 25, "D": 20}                # other groups untouched


def test_reset_buttons_do_not_raise_and_restore_defaults():
    at = _demo_app()
    at.slider(key="w_p_B").set_value(70).run()
    at.slider(key="w_r_C_sloan").set_value(80).run()
    at.button(key="reset_sub_C").click().run()
    assert not at.exception
    assert {k: at.session_state[f"w_r_C_{k}"] for k in config.PILLARS["C"]["ratios"]} == config.PILLARS["C"]["ratios"]
    at.button(key="reset_weights").click().run()
    assert not at.exception                                                 # was StreamlitWidgetAlreadyInstantiatedError
    assert _pillars(at) == {"A": 30, "B": 25, "C": 25, "D": 20}


def test_score_uses_slider_weights():
    import re
    at = _demo_app()
    pat = re.compile(r"([\d.]+) / 100")          # the CCIRS-tab headline card
    grab = lambda: float(next(pat.search(m.value).group(1) for m in at.markdown if pat.search(m.value)))
    base = grab()
    at.slider(key="w_p_C").set_value(80).run()
    assert grab() != base
    assert any("current weights" in c.value for c in at.caption)            # sensitivity notes default comparison
