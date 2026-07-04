"""TC-B02/B03 — the code-computed confidence function.

Assertions are ORDERING + BOUNDS, not an exact disputed constant: the dev-pack
formula yields 0.62 for (1,1,1) while its prose says "~0.42". These tests pin
what the spec actually cares about (a lone source reads low and strictly below a
corroborated one; a wall of agreement sits high but never certain).
"""
from app.core.confidence import confidence


def test_always_in_range():
    for args in [(0, 0, 0), (1, 1, 1), (5, 5, 3), (100, 100, 100), (-3, -3, -3)]:
        v = confidence(*args)
        assert 0.05 <= v <= 0.95
        assert round(v, 2) == v  # 2dp


def test_lone_source_is_low_and_below_corroborated():
    lone = confidence(1, 1, 1)
    strong = confidence(5, 5, 3)
    assert lone < strong
    assert lone < 0.7                 # visibly not-confident on a single source
    assert 0.8 < strong <= 0.95       # corroborated evidence lands high but bounded


def test_more_sources_and_agents_never_decrease_confidence():
    assert confidence(1, 1, 1) <= confidence(3, 3, 2) <= confidence(5, 5, 3)


def test_clamps_extremes():
    assert confidence(1000, 1000, 1000) == 0.95
    assert confidence(-10, -10, -10) >= 0.05
