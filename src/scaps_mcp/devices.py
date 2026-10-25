"""Build new SCAPS definitions from a structured description.

SCAPS scripts can add layers and interface defects but cannot create the first bulk defect of a
layer, so creation runs in two scripts: layers and contacts first, then defects, with one seed
defect block inserted per layer in between.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .scripts import LIMITS, Assignment, ScriptError, Validator, check_filename, render

SEED_DEFECT = (
    "srhrecombination",
    "",
    "type : neutral ( 1 level(s) )",
    "energy distribution : single",
    "Reference for defect energy :  0 ; 0 = above EV (SCAPS<2.7); 1 = above Ei; 2 = below EC",
    "Ekar :  0.1000 [eV]",
    "Nt(x) :\t 1.000000e+20\t 1.000000e+20\t 1.000000e+20\t 1.000000e+00\t 1.000000e+00\t 1.000000e+20\t"
    " 1.000000e+20\t 0\t 1\t[/m^3]",
    "profile : homogenous",
    "Lkar : 1.000e+00 [m]",
    "Nleft : 1.000e+20 [/m^3]",
    "Nright : 1.000e+20 [/m^3]",
    "leveltype : neutral\t",
    "sigma_n : 1.000e-19\t[m^2]",
    "sigma_p : 1.000e-19\t[m^2]",
    "Et :   0.600\t[eV]",
    "",
    "",
)
_SECTION_AFTER_LAYER = ("interface properties", "front contact", "layer")
_IF_DEFECT_COMMANDS = {1: "1_default_IFdefect", 2: "2_default_IFdefects", 3: "3_default_IFdefects"}
_IF_SIGMA = re.compile(r"^interface(\d+)\.IFdefect(\d+)\.capture_cross_section\.(electron|hole)$")
_HEADER = ("set quitscript.quitSCAPS", "set errorhandling.overwritefile")


def validate(layers: Sequence[Mapping[str, Any]], interfaces: Mapping[int, Sequence[Mapping]]) -> None:
    if not 1 <= len(layers) <= LIMITS["layers"]:
        raise ScriptError(f"a device needs 1 to {LIMITS['layers']} layers")
    for i, layer in enumerate(layers, 1):
        if len(layer.get("defects", [])) > LIMITS["defects"]:
            raise ScriptError(f"layer {i}: SCAPS allows at most {LIMITS['defects']} defects per layer")
    for k, defects in interfaces.items():
        if not 1 <= k < len(layers):
            raise ScriptError(f"interface {k} does not exist; a {len(layers)}-layer device has {len(layers) - 1}")
        if len(defects) > LIMITS["interface_defects"]:
            raise ScriptError(f"interface {k}: SCAPS allows at most {LIMITS['interface_defects']} defects")


def layer_script(
    v: Validator,
    layers: Sequence[Mapping[str, Any]],
    contacts: Mapping[str, Mapping[str, Any]],
    settings: Sequence[Assignment],
    target: str,
) -> str:
    lines = [*_HEADER, *(["set layer1.add_default"] * len(layers))]
    for i, layer in enumerate(layers, 1):
        lines += [v.command("set", f"layer{i}.{k}", val) for k, val in layer.items() if k != "defects"]
    for side, properties in contacts.items():
        if "workfunction" in properties and not any(k.startswith("flatband") for k in properties):
            lines.append(v.command("set", f"contact{side}.flatband.off"))
        lines += [v.command("set", f"contact{side}.{k}", val) for k, val in properties.items()]
    lines += [v.command("set", k, val) for k, val in settings]
    lines.append(f"save settings.definitionfile {check_filename(target, '.def')}")
    return render(lines)


def insert_seed_defects(text: str, layer_indices: Sequence[int]) -> str:
    """Insert one default bulk defect into each listed layer (1-based) of a SCAPS-written definition."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    starts = [i for i, line in enumerate(lines) if line.strip() == "layer"]
    for index in sorted(layer_indices, reverse=True):
        start = starts[index - 1]
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip() in _SECTION_AFTER_LAYER),
            len(lines),
        )
        lines[end:end] = list(SEED_DEFECT)
    return newline.join(lines)


def defect_script(
    v: Validator,
    target: str,
    layers: Sequence[Mapping[str, Any]],
    interfaces: Mapping[int, Sequence[Mapping[str, Any]]],
) -> str:
    lines = [*_HEADER, f"load definitionfile {check_filename(target, '.def')}"]
    for i, layer in enumerate(layers, 1):
        defects = layer.get("defects", [])
        lines += [f"set layer{i}.defect1.duplicate"] * (len(defects) - 1)
        for j, defect in enumerate(defects, 1):
            lines += [v.command("set", f"layer{i}.defect{j}.{k}", val) for k, val in defect.items()]
    for k, defects in sorted(interfaces.items()):
        if defects:
            lines.append(f"set interface{k}.{_IF_DEFECT_COMMANDS[len(defects)]}")
        for j, defect in enumerate(defects, 1):
            lines += [v.command("set", f"interface{k}.IFdefect{j}.{key}", val) for key, val in defect.items()]
    lines.append(f"save settings.definitionfile {check_filename(target, '.def')}")
    return render(lines)


def set_numerics(text: str, values: Mapping[str, float]) -> str:
    """Set numerical settings of the 'convergence' section, e.g. maxiter or clamp_psi, in .def text."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    start = next((i for i, line in enumerate(lines) if line.strip() == "convergence"), None)
    if start is None:
        raise ScriptError("the definition has no convergence section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "back contact"), len(lines))
    pending = dict(values)
    for i in range(start + 1, end):
        key, sep, rest = lines[i].partition(":")
        if sep and key.strip() in pending:
            tokens = rest.split(None, 1)
            if tokens:
                value = pending.pop(key.strip())
                lines[i] = f"{key}: {value:.6g}" + (f" {tokens[1]}" if len(tokens) > 1 else "")
    if pending:
        known = [line.partition(":")[0].strip() for line in lines[start + 1 : end] if ":" in line]
        raise ScriptError(f"unknown numerical settings {sorted(pending)}; available: {known}")
    return newline.join(lines)


def interface_cross_sections(assignments: Sequence[Assignment]) -> dict[tuple[int, int], dict[str, float]]:
    """Interface-defect capture cross sections (cm^2) requested by set commands, keyed by (interface, defect)."""
    found: dict[tuple[int, int], dict[str, float]] = {}
    for key, value in assignments:
        match = _IF_SIGMA.match(key)
        if match and isinstance(value, (int, float)):
            found.setdefault((int(match.group(1)), int(match.group(2))), {})[match.group(3)] = float(value)
    return found


def set_interface_cross_sections(text: str, values: Mapping[tuple[int, int], Mapping[str, float]]) -> str:
    """Write interface-defect cross sections to both sides of the interface.

    The script command sets only the right-hand side; the left-hand value would otherwise keep its old value.
    """
    if not values:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    interface = defect = 0
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "interface properties":
            interface, defect, in_block = interface + 1, 0, False
        elif stripped == "interface recombination":
            defect, in_block = defect + 1, True
        elif stripped == "next" and in_block:
            defect += 1
        elif stripped in ("layer", "front contact", "srhrecombination"):
            in_block = False
        elif in_block and stripped.startswith(("sigma_n", "sigma_p")) and ":" in stripped:
            requested = values.get((interface, defect), {})
            carrier = "electron" if stripped.startswith("sigma_n") else "hole"
            if carrier in requested:
                key = stripped.split(":", 1)[0].strip()
                lines[i] = f"{key} : {requested[carrier] * 1e-4:.3e} [m^2]"
    return newline.join(lines)
