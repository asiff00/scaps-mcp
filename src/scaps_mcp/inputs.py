"""Writers for SCAPS input files: absorption, spectrum, generation, optical filter, grading, measurement.

SCAPS input files use SI units except wavelength (nm) and position (um). Writers accept the units
researchers usually hold data in and convert.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .scripts import ScriptError

FOLDERS = {
    "absorption": ("absorption", ".abs"),
    "spectrum": ("spectrum", ".spe"),
    "generation": ("generation", ".gen"),
    "filter": ("filter", ".ftr"),
    "grading": ("grading", ".grd"),
    "measurement": ("measurements", None),
}
MEASUREMENT_KINDS = ("iv", "cv", "cf", "qe")
HC_EV_NM = 1239.841984
GRADING_TO_SI = {
    "composition": 1.0,
    "Eg": 1.0,
    "chi": 1.0,
    "epsilon": 1.0,
    "meff": 1.0,
    "NC": 1e6,
    "NV": 1e6,
    "NA": 1e6,
    "ND": 1e6,
    "Nt": 1e6,
    "mun": 1e-4,
    "mup": 1e-4,
    "vthn": 1e-2,
    "vthp": 1e-2,
    "radiative": 1e-6,
    "Augern": 1e-12,
    "Augerp": 1e-12,
}


@dataclass(frozen=True)
class InputFile:
    folder: str
    name: str
    text: str
    points: int


def _pairs(x: Sequence[float], y: Sequence[float], ascending: bool = True) -> list[tuple[float, float]]:
    if len(x) != len(y):
        raise ScriptError("x and y must have the same length")
    if len(x) < 2:
        raise ScriptError("give at least two data points")
    if any(not math.isfinite(v) for v in (*x, *y)):
        raise ScriptError("data must be finite numbers")
    pairs = list(zip(x, y, strict=True))
    if ascending and any(b[0] <= a[0] for a, b in zip(pairs, pairs[1:], strict=False)):
        raise ScriptError("x values must be strictly ascending")
    return pairs


def _comments(prefix: str, note: str | None, lines: Sequence[str]) -> list[str]:
    body = [*lines, *(note.splitlines() if note else [])]
    return [f"{prefix} {line}".rstrip() for line in body]


def _name(name: str, suffix: str | None) -> str:
    return name if suffix is None or name.lower().endswith(suffix) else name + suffix


def to_nanometre(x: Sequence[float], unit: str) -> list[float]:
    """Convert a spectral axis in nm, um or photon energy (eV) to wavelength in nm."""
    if unit == "nm":
        return list(x)
    if unit == "um":
        return [v * 1000 for v in x]
    if unit == "ev":
        if any(v <= 0 for v in x):
            raise ScriptError("photon energies must be positive")
        return [HC_EV_NM / v for v in x]
    raise ScriptError("unit must be nm, um or ev")


def absorption(
    name: str,
    wavelength: Sequence[float],
    values: Sequence[float],
    quantity: str,
    note: str | None,
    unit: str = "nm",
) -> InputFile:
    if len(wavelength) != len(values):
        raise ScriptError("x and y must have the same length")
    ordered = sorted(zip(to_nanometre(wavelength, unit), values, strict=True))
    pairs = _pairs([p[0] for p in ordered], [p[1] for p in ordered])
    if quantity == "alpha_per_cm":
        alpha = [(lam, a * 100) for lam, a in pairs]
    elif quantity == "alpha_per_m":
        alpha = list(pairs)
    elif quantity == "k":
        alpha = [(lam, 4 * math.pi * k / (lam * 1e-9)) for lam, k in pairs]
    else:
        raise ScriptError("quantity must be alpha_per_cm, alpha_per_m or k")
    if any(a < 0 for _, a in alpha):
        raise ScriptError("absorption must not be negative")
    head = _comments(">", note, ["absorption constant", "lambda (nm)\talpha (1/m)"])
    rows = [f"{lam:.6g}\t{a:.6e}" for lam, a in alpha]
    folder, suffix = FOLDERS["absorption"]
    return InputFile(folder, _name(name, suffix), "\r\n".join([*head, *rows]) + "\r\n", len(rows))


def spectrum(
    name: str,
    wavelength_nm: Sequence[float],
    values: Sequence[float],
    quantity: str,
    normalise_w_m2: float | None,
    note: str | None,
) -> InputFile:
    pairs = _pairs(wavelength_nm, values)
    if len(pairs) > 2500:
        raise ScriptError("SCAPS reads at most 2500 wavelengths")
    if any(p < 0 for _, p in pairs):
        raise ScriptError("spectral power must not be negative")
    lam = [p[0] for p in pairs]
    if quantity == "irradiance_w_m2_nm":
        widths = [
            lam[1] - lam[0] if i == 0 else lam[-1] - lam[-2] if i == len(lam) - 1 else (lam[i + 1] - lam[i - 1]) / 2
            for i in range(len(lam))
        ]
        power = [p * w for (_, p), w in zip(pairs, widths, strict=True)]
    elif quantity == "power_w_m2":
        power = [p for _, p in pairs]
    else:
        raise ScriptError("quantity must be irradiance_w_m2_nm or power_w_m2")
    total = sum(power)
    if normalise_w_m2:
        if total <= 0:
            raise ScriptError("cannot normalise a spectrum with zero power")
        power = [p * normalise_w_m2 / total for p in power]
    head = _comments(">", note, [f"total incident power {sum(power):.6g} W/m2", "lambda (nm)\tP (W/m2) per interval"])
    rows = [f"{x:.6g}\t{p:.6e}" for x, p in zip(lam, power, strict=True)]
    folder, suffix = FOLDERS["spectrum"]
    return InputFile(folder, _name(name, suffix), "\r\n".join([*head, *rows]) + "\r\n", len(rows))


def generation(
    name: str, position_um: Sequence[float], values: Sequence[float], unit: str, note: str | None
) -> InputFile:
    pairs = _pairs(position_um, values)
    factor = {"per_cm3_s": 1e6, "per_m3_s": 1.0}.get(unit)
    if factor is None:
        raise ScriptError("unit must be per_cm3_s or per_m3_s")
    if any(g < 0 for _, g in pairs):
        raise ScriptError("generation must not be negative")
    head = _comments(">", note, ["generation profile", "x (um)\tG (/m3/s)"])
    rows = [f"{x:.6g}\t{g * factor:.6e}" for x, g in pairs]
    folder, suffix = FOLDERS["generation"]
    return InputFile(folder, _name(name, suffix), "\r\n".join([*head, *rows]) + "\r\n", len(rows))


def optical_filter(name: str, wavelength_nm: Sequence[float], percent: Sequence[float], note: str | None) -> InputFile:
    pairs = _pairs(wavelength_nm, percent)
    if any(not 0 <= p <= 100 for _, p in pairs):
        raise ScriptError("filter values are percentages between 0 and 100")
    head = _comments(
        "/",
        note,
        ["optical filter: transmission at the illuminated side, reflection at the rear", "lambda[nm]\tT or R [%]"],
    )
    rows = [f"{lam:.6g}\t{p:.6g}" for lam, p in pairs]
    folder, suffix = FOLDERS["filter"]
    return InputFile(folder, _name(name, suffix), "\r\n".join([*head, *rows]) + "\r\n", len(rows))


def grading(
    name: str,
    x: Sequence[float],
    values: Sequence[float],
    prop: str,
    position: str,
    interpolation: str | None,
    shared: bool,
    note: str | None,
) -> InputFile:
    pairs = _pairs(x, values)
    if prop not in GRADING_TO_SI:
        raise ScriptError(f"property must be one of {sorted(GRADING_TO_SI)}")
    if position not in ("absolute", "relative", "composition"):
        raise ScriptError("position must be absolute, relative or composition")
    factor = GRADING_TO_SI[prop]
    head = _comments(">", note, [f"grading of {prop}"])
    if position != "composition":
        head.append(f"position: {position}")
    if interpolation:
        if interpolation not in ("linear", "logarithmic"):
            raise ScriptError("interpolation must be linear or logarithmic")
        head.append(f"interpolation: {interpolation}")
    if shared:
        head.append("range: shared over adjacent layers")
    rows = [f"{a:.6g}\t{b * factor:.6e}" for a, b in pairs]
    folder, suffix = FOLDERS["grading"]
    return InputFile(folder, _name(name, suffix), "\r\n".join([*head, *rows]) + "\r\n", len(rows))


def measurement(
    name: str,
    kind: str,
    x: Sequence[float],
    y: Sequence[float],
    extra: Sequence[float] | None,
    area_cm2: float,
    temperature_k: float | None,
    bias_v: float | None,
    frequency_hz: float | None,
    pin_mw_cm2: float | None,
    dark: bool,
    note: str | None,
) -> InputFile:
    if kind not in MEASUREMENT_KINDS:
        raise ScriptError(f"kind must be one of {MEASUREMENT_KINDS}")
    pairs = _pairs(x, y, ascending=False)
    if extra is not None and len(extra) != len(pairs):
        raise ScriptError("extra must have the same length as x")
    head = _comments(">", note, [f"{kind} measurement"])
    head.append(f"area\t{area_cm2:g}")
    for word, value in (("temperature", temperature_k), ("bias", bias_v), ("frequency", frequency_hz)):
        if value is not None:
            head.append(f"{word}\t{value:g}")
    if pin_mw_cm2 is not None:
        head.append(f"Pin\t{pin_mw_cm2:g}")
    if dark:
        head.append("dark")
    rows = [
        "\t".join(f"{v:.8g}" for v in (a, b, *([extra[i]] if extra is not None else [])))
        for i, (a, b) in enumerate(pairs)
    ]
    folder, _ = FOLDERS["measurement"]
    return InputFile(folder, _name(name, f".{kind}"), "\r\n".join([*head, *rows]) + "\r\n", len(rows))
