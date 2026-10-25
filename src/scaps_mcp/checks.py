"""Realism checks for SCAPS devices and results.

Rules follow the SCAPS manual and published critiques of unrealistic SCAPS studies.
Levels: 'error' means physically impossible, 'warning' means likely unrealistic, 'note' means review.
"""

from __future__ import annotations

from typing import Any, TypedDict

from .physics import Limit, band_offsets
from .scripts import LIMITS

NC_300K = 2.509e19
CHARGED = {"acceptor", "donor", "singleacceptor", "singledonor", "doubleacceptor", "doubledonor", "amphoteric"}


class Finding(TypedDict):
    level: str
    code: str
    message: str


def finding(level: str, code: str, message: str) -> Finding:
    return {"level": level, "code": code, "message": message}


def absorber_index(device: dict[str, Any]) -> int | None:
    """1-based index of the absorber, taken as the thickest layer."""
    layers = device.get("layers", [])
    if not layers:
        return None
    return max(range(len(layers)), key=lambda i: layers[i].get("thickness") or 0) + 1


def _doping(layer: dict[str, Any]) -> float:
    return max(layer.get("NA") or 0.0, layer.get("ND") or 0.0)


def device_checks(device: dict[str, Any]) -> list[Finding]:
    found: list[Finding] = []
    layers = device.get("layers", [])
    a = absorber_index(device)
    if a is None:
        return [finding("error", "no_layers", "the device has no layers")]
    absorber = layers[a - 1]
    name = absorber.get("name") or f"layer{a}"

    if not (absorber.get("radiative") or absorber.get("Augern") or absorber.get("Augerp")) and not device.get(
        "sq_limit_mode"
    ):
        found.append(
            finding(
                "warning",
                "no_radiative_recombination",
                f"absorber {name} has no radiative or Auger recombination and the SCAPS Shockley-Queisser option is "
                f"off, so Voc and efficiency can exceed the detailed-balance limit; set layer{a}.radiative or "
                "numerical.Shockley-Queisser_limit.always",
            )
        )
    defects = absorber.get("defects", [])
    total = sum(d.get("Ntotal") or 0 for d in defects)
    if not defects:
        found.append(finding("warning", "absorber_without_defects", f"absorber {name} has no bulk defects"))
    elif total < 1e13:
        found.append(
            finding(
                "warning",
                "low_absorber_defects",
                f"absorber {name} bulk defect density {total:.2g} cm^-3 is below 1e13, rarely achieved in thin films",
            )
        )

    for i, layer in enumerate(layers, 1):
        found += _density_of_states(i, layer)
        doping = _doping(layer)
        for d in layer.get("defects", []):
            if d.get("type") in CHARGED and doping and (d.get("Ntotal") or 0) > 0.1 * doping:
                found.append(
                    finding(
                        "note",
                        "compensating_defect",
                        f"layer{i}.defect{d['index']} ({d['type']}, {d['Ntotal']:.2g} cm^-3) exceeds 10% of the "
                        f"doping ({doping:.2g} cm^-3) and will change the space charge",
                    )
                )
        if layer.get("absorption", "").startswith("sqrt(hv-Eg)") and i == a:
            found.append(
                finding(
                    "note",
                    "default_absorption",
                    f"absorber {name} uses the default sqrt(hv-Eg) absorption model; measured absorption data is "
                    "more reliable, especially for indirect or band-tail absorbers",
                )
            )

    for interface in device.get("interfaces", []):
        left, right = interface["layers"]
        if right > len(layers):
            continue
        offsets = band_offsets(layers[left - 1], layers[right - 1])
        hetero = offsets and (abs(offsets["delta_ec_ev"]) > 1e-6 or abs(offsets["delta_ev_ev"]) > 1e-6)
        if hetero and not interface.get("defects"):
            found.append(
                finding(
                    "warning",
                    "ideal_interface",
                    f"interface{interface['index']} (layers {left}/{right}) is a heterojunction without interface "
                    "defects; real heterointerfaces recombine",
                )
            )
        if offsets and a in (left, right):
            found += _offset_checks(interface["index"], a, left, right, layers, offsets)

    flat = [side for side, contact in device.get("contacts", {}).items() if contact.get("flatband") == 1]
    if flat:
        found.append(
            finding("note", "flatband_contact", f"{' and '.join(flat)} contact flat-band (ideal); set work functions")
        )
    if len(layers) > LIMITS["layers"]:
        found.append(finding("error", "too_many_layers", f"SCAPS supports at most {LIMITS['layers']} layers"))
    return found


def _density_of_states(index: int, layer: dict[str, Any]) -> list[Finding]:
    """NC and NV set the effective masses; values far from 1e17 to 1e20 cm^-3 distort ni and Voc."""
    found = []
    for key, band in (("NC", "conduction"), ("NV", "valence")):
        value = layer.get(key)
        if not value or 1e16 <= value <= 1e21:
            continue
        mass = (value / NC_300K) ** (2 / 3)
        level = "error" if not 1e14 <= value <= 1e23 else "warning"
        found.append(
            finding(
                level,
                "implausible_density_of_states",
                f"layer{index} ({layer.get('name') or 'unnamed'}) {key} = {value:.2g} cm^-3 implies a {band}-band "
                f"effective mass of {mass:.2g} m0; semiconductors have about 1e17 to 1e20 cm^-3, and a small value "
                "suppresses the intrinsic density and inflates Voc",
            )
        )
    return found


def _offset_checks(index: int, a: int, left: int, right: int, layers: list, offsets: dict) -> list[Finding]:
    other = right if left == a else left
    neighbour = layers[other - 1]
    n_type = (neighbour.get("ND") or 0) > (neighbour.get("NA") or 0)
    step_ec = offsets["delta_ec_ev"] if left == a else -offsets["delta_ec_ev"]
    step_ev = offsets["delta_ev_ev"] if left == a else -offsets["delta_ev_ev"]
    if n_type:
        value, carrier, band = step_ec, "electrons", "conduction"
    else:
        value, carrier, band = -step_ev, "holes", "valence"
    if value < -0.2:
        return [
            finding(
                "warning",
                "band_cliff",
                f"interface{index}: {band}-band cliff of {value:.2f} eV for {carrier} leaving the absorber "
                "raises interface recombination",
            )
        ]
    if value > 0.4:
        return [
            finding(
                "warning",
                "band_spike",
                f"interface{index}: {band}-band spike of {value:.2f} eV blocks {carrier} leaving the absorber",
            )
        ]
    return []


def result_checks(metrics: dict[str, Any] | None, limit: Limit | None, temperature_k: float | None) -> list[Finding]:
    found: list[Finding] = []
    if metrics and limit:
        pairs = (
            ("eta_pct", limit.eta_pct, 0.3, "efficiency"),
            ("voc_v", limit.voc_v, 0.005, "Voc"),
            ("jsc_ma_cm2", limit.jsc_ma_cm2, 0.3, "Jsc"),
            ("ff_pct", limit.ff_pct, 0.3, "FF"),
        )
        for key, bound, tolerance, label in pairs:
            value = metrics.get(key)
            if value is not None and value > bound + tolerance:
                found.append(
                    finding(
                        "error",
                        f"above_sq_{key}",
                        f"{label} {value:g} exceeds the Shockley-Queisser limit {bound:g} for Eg = "
                        f"{limit.band_gap_ev:g} eV; the parameter set is unphysical",
                    )
                )
        voc = metrics.get("voc_v")
        if voc is not None and voc > limit.band_gap_ev - 0.24:
            found.append(
                finding(
                    "warning",
                    "voc_deficit",
                    f"Voc deficit Eg/q - Voc = {limit.band_gap_ev - voc:.2f} V is below the radiative minimum of "
                    "about 0.25 V",
                )
            )
    if temperature_k is not None and abs(temperature_k - 300) > 1:
        found.append(
            finding(
                "note",
                "temperature",
                "SCAPS scales only NC, NV and thermal velocity with temperature; set Eg and mobilities for "
                f"{temperature_k:g} K explicitly",
            )
        )
    return found


def sweep_checks(parameter: str, points: list[dict[str, Any]]) -> list[Finding]:
    if not parameter.endswith("Ntotal"):
        return []
    scored = [(p["value"], (p.get("metrics") or {}).get("eta_pct")) for p in points]
    scored = [(value, eta) for value, eta in scored if eta is not None]
    if len(scored) < 3:
        return []
    best = max(scored, key=lambda s: s[1])
    if best[0] == max(v for v, _ in scored):
        return [
            finding(
                "warning",
                "efficiency_rises_with_defects",
                f"efficiency peaks at the highest {parameter} ({best[0]:g}); more defects should not help, check the "
                "defect type, energy and capture cross sections",
            )
        ]
    return []
