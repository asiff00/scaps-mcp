import anyio
import pytest

from scaps_mcp.optimize import Variable, fit_error, interpolate, nelder_mead


def test_variable_scaling():
    linear = Variable("x", 0.0, 10.0)
    assert linear.from_unit(linear.to_unit(2.5)) == pytest.approx(2.5)
    log = Variable("n", 1e12, 1e16, log=True)
    assert log.to_unit(1e14) == pytest.approx(0.5)
    assert log.from_unit(1.7) == pytest.approx(1e16)
    with pytest.raises(ValueError):
        Variable("n", 0.0, 1.0, log=True)


def test_nelder_mead_finds_minimum():
    calls = []

    async def evaluate(points):
        calls.append(len(points))
        return [(p[0] - 0.3) ** 2 + (p[1] - 0.7) ** 2 for p in points]

    best, value, count = anyio.run(nelder_mead, evaluate, [0.5, 0.5], 200)
    assert best == pytest.approx([0.3, 0.7], abs=0.02)
    assert value < 1e-3
    assert count == sum(calls)


def test_fit_error():
    xs = [0.0, 1.0, 2.0, 3.0]
    assert interpolate(xs, [0, 10, 20, 30], 1.5) == pytest.approx(15)
    assert interpolate(xs, [0, 10, 20, 30], 5) is None
    assert fit_error(xs, [0, 10, 20, 30], [0.5, 1.5, 2.5], [5, 15, 25], log=False) == pytest.approx(0)
    assert fit_error(xs, [1, 10, 100, 1000], [1, 2, 3], [20, 200, 2000], log=True) == pytest.approx(0.30103, rel=1e-4)
