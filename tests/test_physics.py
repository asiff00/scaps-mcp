import math

import pytest

from scaps_mcp.physics import (
    EPS0,
    Q,
    Spectrum,
    band_offsets,
    integrated_jsc,
    mott_schottky,
    sq_limit,
)


@pytest.fixture
def flat():
    """1 W/m2 every 10 nm from 300 to 1500 nm."""
    return Spectrum.from_bins([(float(lam), 1.0) for lam in range(300, 1501, 10)])


def test_sq_limit_is_self_consistent(flat):
    limit = sq_limit(flat, 1.2)
    photons = sum(f for lam, f in zip(flat.wavelength_nm, flat.photon_flux, strict=True) if lam <= 1030)
    assert limit.jsc_ma_cm2 == pytest.approx(Q * photons * 0.1, rel=1e-3)
    voc = limit.voc_v / 0.025852
    assert limit.ff_pct / 100 == pytest.approx((voc - math.log(voc + 0.72)) / (voc + 1), abs=2e-3)
    assert limit.eta_pct == pytest.approx(limit.jsc_ma_cm2 * 10 * limit.voc_v * limit.ff_pct / 121, rel=2e-3)


def test_sq_limit_trends(flat):
    low, high = sq_limit(flat, 1.0), sq_limit(flat, 1.6)
    assert high.voc_v > low.voc_v
    assert high.jsc_ma_cm2 < low.jsc_ma_cm2
    assert sq_limit(flat, 1.2, 350).voc_v < sq_limit(flat, 1.2, 300).voc_v


def test_sq_limit_is_none_without_photons_or_dark_current(flat):
    assert sq_limit(flat, 4.5) is None
    assert sq_limit(flat, 1.2, 15) is None


def test_integrated_jsc_skips_missing_values(flat):
    assert integrated_jsc(flat, [400.0, 500.0, 600.0], [100.0, None, 100.0]) > 0


def test_integrated_jsc_matches_photon_count(flat):
    wavelengths = [float(lam) for lam in range(310, 991, 10)]
    jsc = integrated_jsc(flat, wavelengths, [100.0] * len(wavelengths))
    photons = sum(f for lam, f in zip(flat.wavelength_nm, flat.photon_flux, strict=True) if 310 <= lam <= 990)
    assert jsc == pytest.approx(Q * photons * 0.1, rel=0.02)


def test_mott_schottky_recovers_doping():
    eps_r, doping, vbi = 10.0, 1e16, 0.8
    eps = eps_r * EPS0 * 1e-2
    volts = [-1.0 + 0.1 * i for i in range(10)]
    caps = [math.sqrt(Q * eps * doping / (2 * (vbi - v))) * 1e9 for v in volts]
    fit = mott_schottky(volts, caps, eps_r)
    assert fit["doping_cm3"] == pytest.approx(doping, rel=1e-3)
    assert fit["built_in_voltage_v"] == pytest.approx(vbi, abs=1e-3)


def test_band_offsets():
    assert band_offsets({"chi": 4.5, "Eg": 1.2}, {"chi": 4.2, "Eg": 2.4}) == {
        "delta_ec_ev": 0.3,
        "delta_ev_ev": -0.9,
    }
    assert band_offsets({"chi": 4.5}, {"chi": 4.2, "Eg": 2.4}) is None
