"""Device physics used to analyse SCAPS results: detailed-balance limits, QE integration, Mott-Schottky."""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass

Q = 1.602176634e-19
H = 6.62607015e-34
C = 2.99792458e8
K = 1.380649e-23
EPS0 = 8.8541878128e-12


@dataclass(frozen=True)
class Spectrum:
    """Photon flux per wavelength interval, built from SCAPS (nm, W/m2 per interval) pairs."""

    wavelength_nm: tuple[float, ...]
    photon_flux: tuple[float, ...]
    power_w_m2: float

    @classmethod
    def from_bins(cls, bins: list[tuple[float, float]]) -> Spectrum:
        bins = sorted(bins)
        flux = tuple(p * lam * 1e-9 / (H * C) for lam, p in bins)
        return cls(tuple(lam for lam, _ in bins), flux, sum(p for _, p in bins))

    def density(self, wavelength_nm: float) -> float:
        """Spectral photon flux density in photons / (m2 s nm), linearly interpolated."""
        lam = self.wavelength_nm
        if not lam or wavelength_nm < lam[0] or wavelength_nm > lam[-1]:
            return 0.0
        widths = [
            (lam[min(i + 1, len(lam) - 1)] - lam[max(i - 1, 0)]) / (2 if 0 < i < len(lam) - 1 else 1)
            for i in range(len(lam))
        ]
        dens = [f / w if w > 0 else 0.0 for f, w in zip(self.photon_flux, widths, strict=True)]
        i = bisect_left(lam, wavelength_nm)
        if lam[i] == wavelength_nm or i == 0:
            return dens[i]
        t = (wavelength_nm - lam[i - 1]) / (lam[i] - lam[i - 1])
        return dens[i - 1] + t * (dens[i] - dens[i - 1])


@dataclass(frozen=True)
class Limit:
    band_gap_ev: float
    temperature_k: float
    jsc_ma_cm2: float
    voc_v: float
    ff_pct: float
    eta_pct: float


def _j0(band_gap_ev: float, temperature_k: float) -> float:
    """Radiative dark current density (A/m2) of an ideal absorber, from the Bose-Einstein integral."""
    kt = K * temperature_k
    eg = band_gap_ev * Q
    total = 0.0
    for n in range(1, 8):
        a = n / kt
        total += math.exp(-a * eg) * (eg**2 / a + 2 * eg / a**2 + 2 / a**3)
    return Q * 2 * math.pi / (H**3 * C**2) * total


def sq_limit(spectrum: Spectrum, band_gap_ev: float, temperature_k: float = 300.0) -> Limit | None:
    """Shockley-Queisser limit for a step absorber under the given spectrum.

    None when the spectrum has no photons above the gap or the dark current underflows at low temperature.
    """
    cutoff = H * C / (band_gap_ev * Q) * 1e9
    jsc = Q * sum(f for lam, f in zip(spectrum.wavelength_nm, spectrum.photon_flux, strict=True) if lam <= cutoff)
    j0 = _j0(band_gap_ev, temperature_k)
    if jsc <= 0 or j0 <= 0:
        return None
    vt = K * temperature_k / Q
    voc = vt * math.log(jsc / j0 + 1)
    lo, hi = 0.0, voc
    for _ in range(100):
        m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        p1 = m1 * (jsc - j0 * math.expm1(m1 / vt))
        p2 = m2 * (jsc - j0 * math.expm1(m2 / vt))
        lo, hi = (m1, hi) if p1 < p2 else (lo, m2)
    vmp = (lo + hi) / 2
    pmax = vmp * (jsc - j0 * math.expm1(vmp / vt))
    return Limit(
        band_gap_ev=band_gap_ev,
        temperature_k=temperature_k,
        jsc_ma_cm2=round(jsc * 0.1, 3),
        voc_v=round(voc, 4),
        ff_pct=round(100 * pmax / (voc * jsc), 2),
        eta_pct=round(100 * pmax / spectrum.power_w_m2, 2),
    )


def integrated_jsc(spectrum: Spectrum, wavelength_nm: list[float], qe_pct: list[float]) -> float:
    """Short-circuit current density (mA/cm2) from an external QE curve and the spectrum."""
    points = sorted((lam, q) for lam, q in zip(wavelength_nm, qe_pct, strict=True) if lam is not None and q is not None)
    total = 0.0
    for (l1, q1), (l2, q2) in zip(points, points[1:], strict=False):
        f1 = max(q1, 0) / 100 * spectrum.density(l1)
        f2 = max(q2, 0) / 100 * spectrum.density(l2)
        total += (f1 + f2) / 2 * (l2 - l1)
    return round(Q * total * 0.1, 3)


def mott_schottky(voltage_v: list[float], capacitance_nf_cm2: list[float], epsilon_r: float) -> dict | None:
    """Linear fit of 1/C^2 against V: built-in voltage and apparent doping of the lightly doped side."""
    pairs = [(v, c) for v, c in zip(voltage_v, capacitance_nf_cm2, strict=True) if c and c > 0]
    if len(pairs) < 3:
        return None
    xs = [v for v, _ in pairs]
    ys = [1 / (c * 1e-9) ** 2 for _, c in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    if slope >= 0:
        return None
    intercept = my - slope * mx
    epsilon = epsilon_r * EPS0 * 1e-2
    doping = -2 / (Q * epsilon * slope)
    return {"built_in_voltage_v": round(-intercept / slope, 4), "doping_cm3": float(f"{doping:.4g}"), "points": n}


def band_offsets(left: dict, right: dict) -> dict | None:
    """Conduction and valence band steps going from the left layer to the right layer (eV)."""
    try:
        ec = left["chi"] - right["chi"]
        ev = (left["chi"] + left["Eg"]) - (right["chi"] + right["Eg"])
    except (KeyError, TypeError):
        return None
    return {"delta_ec_ev": round(ec, 4), "delta_ev_ev": round(ev, 4)}
