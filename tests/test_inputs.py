import math

import pytest

from scaps_mcp import inputs
from scaps_mcp.parsing import parse_spectrum
from scaps_mcp.scripts import ScriptError


def data(text: str) -> list[list[float]]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        try:
            rows.append([float(p) for p in parts])
        except ValueError:
            continue
    return [r for r in rows if len(r) >= 2]


def test_absorption_from_k_and_alpha():
    made = inputs.absorption("film", [500, 600], [0.1, 0.05], "k", "measured by ellipsometry")
    assert made.name == "film.abs" and made.folder == "absorption"
    first = data(made.text)[0]
    assert first[1] == pytest.approx(4 * math.pi * 0.1 / 500e-9, rel=1e-5)
    assert "measured by ellipsometry" in made.text
    per_cm = inputs.absorption("film", [500, 600], [1e4, 2e4], "alpha_per_cm", None)
    assert data(per_cm.text)[0][1] == pytest.approx(1e6)


def test_spectrum_converts_irradiance_and_normalises():
    made = inputs.spectrum("flat", [400, 500, 600], [1.0, 1.0, 1.0], "irradiance_w_m2_nm", 1000, None)
    bins = parse_spectrum(made.text)
    assert sum(p for _, p in bins) == pytest.approx(1000)
    assert bins[1][1] == pytest.approx(1000 * 100 / 300)


def test_generation_and_grading_units():
    gen = inputs.generation("g", [0, 1], [1e21, 2e21], "per_cm3_s", None)
    assert data(gen.text)[0][1] == pytest.approx(1e27)
    grd = inputs.grading("na", [0, 0.5, 1], [1e15, 1e16, 1e17], "NA", "relative", "logarithmic", False, None)
    assert "position: relative" in grd.text and "interpolation: logarithmic" in grd.text
    assert data(grd.text)[2][1] == pytest.approx(1e23)


def test_measurement_header():
    made = inputs.measurement("light", "iv", [0, 0.5], [-30, 10], None, 1.0, 300, None, None, 100, False, None)
    assert made.name == "light.iv" and made.folder == "measurements"
    assert "temperature\t300" in made.text and "Pin\t100" in made.text


@pytest.mark.parametrize(
    "call",
    [
        lambda: inputs.absorption("a", [400, 400], [1, 2], "k", None),
        lambda: inputs.absorption("a", [400, 500], [1], "k", None),
        lambda: inputs.optical_filter("f", [400, 500], [50, 120], None),
        lambda: inputs.grading("g", [0, 1], [1, 2], "bogus", "absolute", None, False, None),
        lambda: inputs.spectrum("s", [400, 500], [1, -1], "power_w_m2", None, None),
    ],
)
def test_rejects_bad_data(call):
    with pytest.raises(ScriptError):
        call()


def test_absorption_from_photon_energy():
    made = inputs.absorption("film", [3.0, 2.0, 1.5], [1e5, 5e4, 1e3], "alpha_per_cm", None, "ev")
    rows = data(made.text)
    assert [round(r[0]) for r in rows] == [413, 620, 827]
    assert rows[0][1] == pytest.approx(1e7)
