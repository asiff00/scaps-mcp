"""Parsers for SCAPS result files, definition files and the script error log."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$|^[-+]?inf$|^nan$", re.IGNORECASE)
_METRIC = re.compile(r"^(Voc|Jsc|FF|eta|V_MPP|J_MPP)\s*=\s*(\S+)\s*(.*)$")
_UNIT = re.compile(r"\[([^\]]*)\]")


def _is_number(token: str) -> bool:
    return bool(_NUMBER.match(token))


def _finite(value: float) -> float | None:
    return float(f"{value:.12g}") if math.isfinite(value) else None


@dataclass
class Table:
    columns: list[str]
    rows: list[list[float | None]]
    title: str | None = None


@dataclass
class Result:
    kind: str | None = None
    info: dict[str, str] = field(default_factory=dict)
    tables: list[Table] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)

    @property
    def table(self) -> Table | None:
        """The main table: the last 'bulk' table of spatial profiles, otherwise the last table."""
        bulk = [t for t in self.tables if t.title == "bulk"]
        return (bulk or self.tables or [None])[-1]


def parse_result(text: str) -> Result:
    """Parse a SCAPS result file (.iv, .cv, .cf, .qe, .eb, .gen, .ac).

    Files saved without `clear simulations` between runs hold several blocks; every table is kept.
    Spatial profiles carry their section title ('bulk', 'interface').
    """
    result = Result()
    pending: list[str] | None = None
    header: list[str] = []
    title: str | None = None
    rows: list[list[float | None]] = []

    def flush() -> None:
        nonlocal rows, title
        if rows:
            columns = header if len(header) == len(rows[0]) else [f"col{i}" for i in range(len(rows[0]))]
            result.tables.append(Table(columns, rows, title))
            title = None
        rows = []

    def commit() -> None:
        nonlocal pending
        if pending and len(pending) >= 2 and not result.tables:
            result.info.setdefault(pending[0].rstrip(" :"), " ".join(pending[1:]))
        pending = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        tokens = stripped.split()
        if len(tokens) >= 2 and all(_is_number(t) for t in tokens):
            if pending is not None:
                header, pending = pending, None
            rows.append([_finite(float(t)) for t in tokens])
            continue
        flush()
        commit()
        metric = _METRIC.match(stripped)
        if metric:
            name, value, unit = metric.groups()
            result.metrics[name] = _finite(float(value)) if _is_number(value) else None
            result.units[name] = unit.strip()
        elif stripped.startswith("Simulation of "):
            result.kind = stripped.removeprefix("Simulation of ").removesuffix(" curves")
        elif "\t" in line:
            pending = [c.strip() for c in line.split("\t") if c.strip()]
        elif stripped in ("bulk", "interface"):
            title = stripped
    flush()
    commit()
    return result


_AREA = re.compile(
    r"^\s*(?:cell\s*area|cellarea|area)\b[\s:=]*([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*(cm\^?2|mm\^?2|m\^?2)?",
    re.IGNORECASE,
)
_UNITS = re.compile(r"^\s*units?\s*:?\s*(.*)$", re.IGNORECASE)
_UNIT_PAIR = re.compile(r"([A-Za-z]+)\s*:\s*([A-Za-z%/^0-9]+)")
_AREA_CM2 = {"cm2": 1.0, "cm^2": 1.0, "mm2": 0.01, "mm^2": 0.01, "m2": 1e4, "m^2": 1e4}
_MEASUREMENT_COLUMNS = {
    "iv": (("v", {"v": 1.0, "volt": 1.0, "mv": 1e-3}), ("i", {"ma": 1.0, "a": 1e3, "ua": 1e-3})),
    "cv": (("v", {"v": 1.0, "volt": 1.0, "mv": 1e-3}), ("c", {"nf": 1.0, "f": 1e9, "pf": 1e-3, "uf": 1e3})),
    "cf": (("f", {"hz": 1.0, "khz": 1e3, "mhz": 1e6}), ("c", {"nf": 1.0, "f": 1e9, "pf": 1e-3, "uf": 1e3})),
    "qe": (("lambda", {"nm": 1.0, "um": 1e3, "m": 1e9}), ("qe", {"%": 1.0, "1": 100.0})),
}
_SCALED_BY_AREA = ("iv", "cv", "cf")


def measurement_scales(text: str, kind: str) -> tuple[float, float]:
    """Factors that bring the first two columns of a measurement file to V/Hz/nm and mA/cm2, nF/cm2 or %.

    The header code words `area` (cm2, mm2 or m2) and `units:` follow the SCAPS measurement file format.
    """
    (x_name, x_units), (y_name, y_units) = _MEASUREMENT_COLUMNS[kind]
    x_scale, y_scale, area = 1.0, 1.0, 1.0
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) >= 2 and _is_number(tokens[0]) and _is_number(tokens[1]):
            break
        area_match = _AREA.match(line)
        if area_match:
            area = float(area_match.group(1)) * _AREA_CM2.get((area_match.group(2) or "cm2").lower(), 1.0)
        units_match = _UNITS.match(line)
        if units_match:
            for name, unit in _UNIT_PAIR.findall(units_match.group(1)):
                if name.lower() == x_name:
                    x_scale = x_units.get(unit.lower(), x_scale)
                elif name.lower() == y_name:
                    y_scale = y_units.get(unit.lower(), y_scale)
    if kind in _SCALED_BY_AREA and area > 0:
        y_scale /= area
    return x_scale, y_scale


@dataclass
class LogEntry:
    command: str | None
    message: str


def parse_error_log(text: str) -> list[LogEntry]:
    errors = []
    for block in re.split(r"^SCAPS error message #\d+[^\n]*\n", text, flags=re.MULTILINE)[1:]:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines if ln != "Unspecified SCAPS error message"]
        command = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("Script command:")), None)
        message = " ".join(ln for ln in lines if not ln.startswith("Script command:"))
        errors.append(LogEntry(command, message))
    return errors


_SI_TO_SCRIPT = {
    "m": (1e6, "um"),
    "/m^3": (1e-6, "cm^-3"),
    "/m^3 eV": (1e-6, "cm^-3 eV^-1"),
    "/m^3.eV": (1e-6, "cm^-3 eV^-1"),
    "/m^2": (1e-4, "cm^-2"),
    "/m^2.eV": (1e-4, "cm^-2 eV^-1"),
    "m^2": (1e4, "cm^2"),
    "m/s": (1e2, "cm/s"),
    "m^2/Vs": (1e4, "cm^2/Vs"),
    "m^3/s": (1e6, "cm^3/s"),
    "m^6/s": (1e12, "cm^6/s"),
}

_LAYER_KEYS = {
    "d": "thickness",
    "eps": "epsilon",
    "chi": "chi",
    "Eg": "Eg",
    "Nc": "NC",
    "Nv": "NV",
    "mu_n": "mun",
    "mu_p": "mup",
    "v_th_n": "vthn",
    "v_th_p": "vthp",
    "Na": "NA",
    "Nd": "ND",
    "K_rad": "radiative",
    "c_n_auger": "Augern",
    "c_p_auger": "Augerp",
}
_DEFECT_KEYS = {
    "Nt": "Ntotal",
    "Et": "Et",
    "Ekar": "Echar",
    "sigma_n": "capture_cross_section.electron",
    "sigma_p": "capture_cross_section.hole",
}
_IF_DEFECT_KEYS = {
    "N": "N",
    "Et": "Et",
    "Ekar": "Echar",
    "sigma_nleft": "sigma_n_left",
    "sigma_nright": "sigma_n_right",
    "sigma_pleft": "sigma_p_left",
    "sigma_pright": "sigma_p_right",
}
_CONTACT_KEYS = {"Sn": "Sn", "Sp": "Sp", "Fi_m": "workfunction", "flatband": "flatband"}
_DEFECT_REFERENCE = {0: "above EV", 1: "above Ei", 2: "below EC"}
_IF_DEFECT_REFERENCE = {
    3: "above highest EV",
    4: "below lowest EC",
    5: "above Ei left",
    6: "above Ei right",
    7: "above EV left",
    8: "above EV right",
    9: "above middle of interface gap",
}
_SECTIONS = {
    "convergence",
    "back contact",
    "front contact",
    "layer",
    "srhrecombination",
    "interface properties",
    "interface recombination",
    "working point",
}

DEFINITION_UNITS = {
    "thickness": "um",
    "Eg": "eV",
    "chi": "eV",
    "epsilon": "-",
    "NC": "cm^-3",
    "NV": "cm^-3",
    "mun": "cm^2/Vs",
    "mup": "cm^2/Vs",
    "vthn": "cm/s",
    "vthp": "cm/s",
    "NA": "cm^-3",
    "ND": "cm^-3",
    "radiative": "cm^3/s",
    "Augern": "cm^6/s",
    "Augerp": "cm^6/s",
    "Ntotal": "cm^-3",
    "Et": "eV",
    "Echar": "eV",
    "capture_cross_section": "cm^2",
    "Sn": "cm/s",
    "Sp": "cm/s",
    "workfunction": "eV",
}


def _value(rest: str) -> tuple[Any, str | None]:
    unit_match = _UNIT.search(rest)
    unit = unit_match.group(1).strip() if unit_match else None
    body = _UNIT.sub("", rest).strip()
    first = body.split()[0] if body.split() else ""
    if _is_number(first):
        number = float(first)
        if unit in _SI_TO_SCRIPT:
            factor, unit = _SI_TO_SCRIPT[unit]
            number *= factor
        return _finite(number), unit
    return body.split("(")[0].strip() or None, unit


def _new_defect(defects: list[dict[str, Any]]) -> dict[str, Any]:
    defect = {"index": len(defects) + 1}
    defects.append(defect)
    return defect


def parse_spectrum(text: str) -> list[tuple[float, float]]:
    """Return (wavelength nm, power W/m2 per interval) pairs from a SCAPS .spe file."""
    points = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) >= 2 and not line.startswith(">") and _is_number(tokens[0]) and _is_number(tokens[1]):
            points.append((float(tokens[0]), float(tokens[1])))
    return points


def parse_definition(text: str) -> dict[str, Any]:
    """Summarise a SCAPS .def file in the units and names used by `set` script commands.

    Graded properties report their base (first) value.
    """
    device: dict[str, Any] = {"layers": [], "interfaces": [], "contacts": {}, "working_point": {}}
    section = None
    layer: dict[str, Any] | None = None
    defect: dict[str, Any] | None = None
    interface: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        if line == "next":
            if section == "srhrecombination" and layer is not None:
                defect = _new_defect(layer["defects"])
            elif section == "interface recombination" and interface is not None:
                defect = _new_defect(interface["defects"])
            continue
        if line in _SECTIONS:
            section = line
            if section == "layer":
                layer = {"index": len(device["layers"]) + 1, "defects": []}
                device["layers"].append(layer)
            elif section == "srhrecombination" and layer is not None:
                defect = _new_defect(layer["defects"])
            elif section == "interface properties":
                left = len(device["layers"])
                interface = {"index": len(device["interfaces"]) + 1, "layers": [left, left + 1], "defects": []}
                device["interfaces"].append(interface)
            elif section == "interface recombination" and interface is not None:
                defect = _new_defect(interface["defects"])
            elif section in ("back contact", "front contact"):
                contact = {}
                device["contacts"]["left" if section == "back contact" else "right"] = contact
            continue
        if ":" not in line:
            continue
        key, rest = (part.strip() for part in line.split(":", 1))
        base = key.split("(")[0].strip()
        value, unit = _value(rest)

        if section == "layer" and layer is not None:
            if key == "name":
                layer["name"] = rest.strip()
            elif base in _LAYER_KEYS:
                layer[_LAYER_KEYS[base]] = value
            elif key.startswith("absorptionfile pure A"):
                layer["absorption"] = f"file: {rest.strip()}"
            elif key == "absorption model A, model":
                layer["absorption"] = rest.strip()
        elif section == "srhrecombination" and defect is not None and layer is not None:
            if key == "type":
                if "type" in defect:
                    defect = _new_defect(layer["defects"])
                defect["type"] = str(value).split()[0]
            elif key == "energy distribution":
                defect["distribution"] = value
            elif key == "Reference for defect energy" and isinstance(value, float):
                defect["Et_reference"] = _DEFECT_REFERENCE.get(int(value), int(value))
            elif base in _DEFECT_KEYS:
                defect[_DEFECT_KEYS[base]] = value
        elif section == "interface properties" and interface is not None and key == "interfacename":
            interface["name"] = rest.strip() or None
        elif section == "interface recombination" and defect is not None and interface is not None:
            if key == "type":
                if "type" in defect:
                    defect = _new_defect(interface["defects"])
                defect["type"] = str(value).split()[0]
            elif key == "energy distribution":
                defect["distribution"] = value
            elif key == "Reference for defect energy" and isinstance(value, float):
                defect["Et_reference"] = _IF_DEFECT_REFERENCE.get(int(value), int(value))
            elif key in _IF_DEFECT_KEYS:
                defect[_IF_DEFECT_KEYS[key]] = value
                if key == "N":
                    defect["N_unit"] = unit
        elif section in ("back contact", "front contact") and contact is not None and key in _CONTACT_KEYS:
            contact[_CONTACT_KEYS[key]] = int(value) if key == "flatband" and value is not None else value
        elif section == "convergence" and key == "Shockley-Queisser limit" and isinstance(value, float):
            device["sq_limit_mode"] = int(value)
        elif section == "working point":
            if key == "illuminated from":
                device["working_point"]["illuminated_from"] = value
            elif key == "voltage V applied to" and isinstance(value, float):
                device["working_point"]["voltage_applied_to"] = "right" if value > 0 else "left"
    return device


_MATERIAL_KEYS = {k: v for k, v in _LAYER_KEYS.items() if k not in ("d", "Na", "Nd")}


def parse_material(text: str) -> dict[str, Any]:
    """Pure-A properties of a .material file in script units, plus its absorption source.

    Material files store GUI units (cm based) even where older versions label them SI.
    """
    properties: dict[str, float] = {}
    absorption: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">") or ":" not in line:
            continue
        key, rest = (part.strip() for part in line.split(":", 1))
        base = key.split("(")[0].strip()
        first = _UNIT.sub("", rest).split()
        if base in _MATERIAL_KEYS and first and _is_number(first[0]):
            properties[_MATERIAL_KEYS[base]] = _finite(float(first[0]))
        elif key.startswith("absorptionfile pure A"):
            absorption["file"] = rest
        elif key == "absorption model A, model":
            absorption["model"] = rest
    for name in ("NC", "NV"):
        if (properties.get(name) or 0) > 1e22:
            properties[name] = _finite(properties[name] * 1e-6)
    return {"properties": properties, "absorption": absorption}
