"""Builders and validators for SCAPS script files."""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .catalog import NUMERIC, Catalog

Value = float | int | str | None
Assignment = tuple[str, Value]

MEASUREMENTS = {"iv": "iv.doiv", "cv": "cv.docv", "cf": "cf.docf", "qe": "qe.doqe"}
PROFILES = ("eb", "genrec", "ac")
EXTENSIONS = {"iv": "iv", "cv": "cv", "cf": "cf", "qe": "qe", "eb": "eb", "genrec": "gen", "ac": "ac"}
PRELUDE = ("set quitscript.quitSCAPS", "set errorhandling.overwritefile", "clear all")
LIMITS = {"layers": 7, "defects": 7, "interface_defects": 3}
MAX_POINTS = 201

_FILENAME = re.compile(r"^[^\\/:*?\"<>|;\x00-\x1f]{1,128}$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z0-9_\-]+)*$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.+\-\[\]]{1,64}$")
_VERBS = {
    "load",
    "save",
    "set",
    "action",
    "calculate",
    "clear",
    "get",
    "math",
    "loop",
    "plot",
    "extract",
    "show",
    "run",
}
_RUN_ALLOWED = re.compile(r"^(script|dll|dll26)\b", re.IGNORECASE)
_DROPPED = {
    ("set", "errorhandling.toscreen"): "would open blocking dialogs",
    ("set", "quitscript.interactivescaps"): "would keep SCAPS open",
    ("show", "scriptvariables"): "opens a dialog",
}
_TARGET = re.compile(r"^(layer|interface)(\d+)$")
_CHILD = re.compile(r"^(defect|IFdefect)(\d*)$", re.IGNORECASE)
_DEFAULT_IF = re.compile(r"^(\d|all)_default_IFdefects?$")


class ScriptError(ValueError):
    pass


def check_filename(name: str, suffix: str | None = None) -> str:
    """Return a bare SCAPS file name, rejecting paths and characters SCAPS cannot handle."""
    name = name.strip()
    if suffix and not name.lower().endswith(suffix):
        name += suffix
    if not _FILENAME.match(name) or name.startswith(".") or ".." in name:
        raise ScriptError(f"invalid file name {name!r}: use a bare file name without path separators")
    return name


def _number(value: Value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptError(f"expected a number, got {value!r}")
    return f"{value:.10g}"


def _as_float(value: Value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class Validator:
    """Renders script commands, checking keys and values against the installed SCAPS command list."""

    catalog: Catalog | None = None

    def verb_for(self, key: str) -> str:
        if self.catalog:
            for verb in ("set", "action"):
                if self.catalog.lookup(verb, key):
                    return verb
            raise ScriptError(f"{key!r} is neither a set nor an action command; see scaps_script_reference")
        return "action" if key.split(".")[0] in {*MEASUREMENTS, "workingpoint", "intensity"} else "set"

    def command(self, verb: str, key: str, value: Value = None) -> str:
        key = canonical(key)
        if not _KEY.match(key):
            raise ScriptError(f"invalid {verb} key {key!r}")
        kind = None
        if self.catalog:
            entry = self.catalog.lookup(verb, key)
            if entry is None:
                raise ScriptError(
                    f"'{verb} {key}' is not a SCAPS {self.catalog.source} command; see scaps_script_reference"
                )
            kind = entry.value_type
        return f"{verb} {key} {self._value(kind, key, value)}".rstrip()

    @staticmethod
    def _value(kind: str | None, key: str, value: Value) -> str:
        if kind == "NONE":
            if value not in (None, True):
                raise ScriptError(f"{key!r} takes no value; pass null")
            return ""
        if value is None:
            if kind is None:
                return ""
            raise ScriptError(f"{key!r} needs a value ({kind.lower()})")
        if kind in NUMERIC:
            text = _number(value)
            if kind == "INTEGER" and float(value) != int(float(value)):
                raise ScriptError(f"{key!r} needs an integer")
            return text
        if kind == "BINARY":
            if value in (True, 1, "1", "on"):
                return "1"
            if value in (False, 0, "0", "off"):
                return "0"
            raise ScriptError(f"{key!r} needs 0 or 1")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _number(value)
        text = str(value).strip()
        return text if _TOKEN.match(text) else check_filename(text)


def canonical(key: str) -> str:
    """SCAPS accepts contactleft/contactright; the manual's leftcontact/rightcontact are rewritten."""
    return re.sub(r"^(left|right)contact\.", r"contact\1.", key, flags=re.IGNORECASE)


def check_targets(device: Mapping, keys: Iterable[str]) -> None:
    """Reject keys that address layers, interfaces or defects the device does not have.

    Structural commands update a running count, so later keys may address what earlier ones create.
    """
    counts = {
        "layer": [len(layer["defects"]) for layer in device["layers"]],
        "interface": [len(interface["defects"]) for interface in device["interfaces"]],
    }
    for key in keys:
        parts = key.split(".")
        head = _TARGET.match(parts[0])
        if not head:
            continue
        kind, index = head.group(1), int(head.group(2))
        items, tail, interfaces = counts[kind], parts[1:], counts["interface"]
        if kind == "layer" and tail == ["add_default"]:
            if not 1 <= index <= len(items) + 1:
                raise ScriptError(f"{key!r}: a layer can be added at positions 1 to {len(items) + 1}")
            items.insert(index - 1, 0)
            interfaces.insert(min(index - 1, len(interfaces)), 0)
            continue
        if not 1 <= index <= len(items):
            raise ScriptError(f"{key!r}: the device has {len(items)} {kind}s")
        if kind == "layer" and tail[:1] in (["duplicate"], ["split"]):
            items.insert(index, items[index - 1])
            interfaces.insert(min(index, len(interfaces)), 0)
        elif kind == "layer" and tail == ["remove"]:
            items.pop(index - 1)
            if interfaces:
                interfaces.pop(min(index - 1, len(interfaces) - 1))
        elif kind == "interface" and len(tail) == 1 and _DEFAULT_IF.match(tail[0]):
            items[index - 1] += 3 if tail[0].startswith("all") else int(tail[0][0])
        elif tail and _CHILD.match(tail[0]):
            _check_child(key, kind, index, items, _CHILD.match(tail[0]), tail[1] if len(tail) > 1 else "")


def _check_child(key: str, kind: str, index: int, items: list[int], child: re.Match, action: str) -> None:
    label, sub, count = child.group(1), child.group(2), items[index - 1]
    if action == "add_default":
        if count == 0:
            raise ScriptError(f"{key!r}: {kind} {index} has no {label}s and a script cannot create the first one")
        items[index - 1] = count + 1
    elif sub:
        if not 1 <= int(sub) <= count:
            raise ScriptError(f"{key!r}: {kind} {index} has {count} {label}s")
        if action == "duplicate":
            items[index - 1] = count + 1
        elif action == "remove":
            items[index - 1] = count - 1


@dataclass
class Simulation:
    definition: str
    measurements: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    settings: list[Assignment] = field(default_factory=list)
    actions: list[Assignment] = field(default_factory=list)
    illumination: str = "light"
    spectrum: str | None = None

    def __post_init__(self) -> None:
        self.measurements = list(dict.fromkeys(self.measurements))
        self.profiles = list(dict.fromkeys(self.profiles))
        for m in self.measurements:
            if m not in MEASUREMENTS:
                raise ScriptError(f"unknown measurement {m!r}; choose from {sorted(MEASUREMENTS)}")
        for p in self.profiles:
            if p not in PROFILES:
                raise ScriptError(f"unknown profile {p!r}; choose from {list(PROFILES)}")
        if "ac" in self.profiles and not {"cv", "cf"} & set(self.measurements):
            raise ScriptError("the ac profile needs a cv or cf measurement")
        if not self.measurements and not self.profiles:
            raise ScriptError("request at least one measurement or profile")
        for key, _ in self.actions:
            group = key.split(".")[0]
            if key in ("light", "dark") or key.endswith((".doiv", ".docv", ".docf", ".doqe")):
                raise ScriptError(f"{key!r} is set through measurements and illumination")
            if group in MEASUREMENTS and group not in self.measurements:
                raise ScriptError(f"action {key!r} given but {group!r} is not among the measurements")
        sweep = dict(self.actions)
        for group in ("iv", "cv"):
            points = _as_float(sweep.get(f"{group}.points"))
            start, stop, step = (_as_float(sweep.get(f"{group}.{k}")) for k in ("startV", "stopV", "increment"))
            if None not in (start, stop, step) and step:
                points = abs(stop - start) / abs(step) + 1
            if points is not None and points > MAX_POINTS:
                raise ScriptError(f"{group} sweep has {points:.0f} points; SCAPS allows at most {MAX_POINTS}")

    def _actions(self, v: Validator, measurements: Sequence[str]) -> list[str]:
        lines = ["clear actions", f"action {self.illumination}"]
        lines += [
            v.command("action", k, val)
            for k, val in self.actions
            if k.split(".")[0] not in MEASUREMENTS or k.split(".")[0] in measurements
        ]
        lines += [f"action {MEASUREMENTS[m]}" for m in measurements]
        return lines

    def setup(self, v: Validator) -> list[str]:
        lines = [*PRELUDE, f"load definitionfile {check_filename(self.definition, '.def')}"]
        if self.spectrum:
            lines.append(f"load spectrumfile {check_filename(self.spectrum)}")
        return lines + [v.command("set", k, val) for k, val in self.settings]

    def validate(self, v: Validator) -> None:
        self.setup(v)
        self._actions(v, self.measurements)

    def outputs(self, stem: str) -> dict[str, str]:
        return {kind: f"{stem}.{EXTENSIONS[kind]}" for kind in [*self.measurements, *self.profiles]}

    def _calculate(self, v: Validator, outputs: Mapping[str, str]) -> list[str]:
        lines = []
        if self.measurements:
            lines.append("calculate singleshot")
            lines += [f"save results.{m} {outputs[m]}" for m in self.measurements]
            if "ac" in self.profiles:
                lines.append(f"save results.ac {outputs['ac']}")
        spatial = [p for p in self.profiles if p != "ac"]
        if spatial:
            if self.measurements:
                lines += self._actions(v, [])
            lines.append("calculate singleshot")
            lines += [f"save results.{p} {outputs[p]}" for p in spatial]
        return lines

    def script(self, v: Validator, stem: str) -> tuple[str, dict[str, str]]:
        outputs = self.outputs(stem)
        return render([*self.setup(v), *self._actions(v, self.measurements), *self._calculate(v, outputs)]), outputs

    def batch_script(
        self, v: Validator, stem: str, variables: Sequence[tuple[str, Sequence[Value]]], mode: str = "grid"
    ) -> tuple[str, list[dict[str, Value]], list[dict[str, str]]]:
        if not variables:
            raise ScriptError("give at least one variable")
        keys = [k for k, _ in variables]
        if len(set(keys)) != len(keys):
            raise ScriptError("each variable may appear only once")
        base = replace(
            self,
            settings=[s for s in self.settings if s[0] not in keys],
            actions=[a for a in self.actions if a[0] not in keys],
        )
        verbs = {k: v.verb_for(k) for k in keys}
        series = [list(values) for _, values in variables]
        if any(not s for s in series):
            raise ScriptError("every variable needs at least one value")
        if mode == "zip":
            if len({len(s) for s in series}) != 1:
                raise ScriptError("zip mode needs value lists of equal length")
            combos = list(zip(*series, strict=True))
        else:
            combos = list(itertools.product(*series))
        lines = base.setup(v)
        points, outputs = [], []
        for i, combo in enumerate(combos):
            point = dict(zip(keys, combo, strict=True))
            lines += [v.command("set", k, val) for k, val in point.items() if verbs[k] == "set"]
            swept = [(k, val) for k, val in point.items() if verbs[k] == "action"]
            current = replace(base, actions=[*base.actions, *swept])
            step = base.outputs(f"{stem}_{i:03d}")
            lines += [*current._actions(v, current.measurements), *current._calculate(v, step), "clear simulations"]
            points.append(point)
            outputs.append(step)
        return render(lines), points, outputs


def edit_script(v: Validator, base: str, operations: Sequence[Assignment], target: str) -> str:
    lines = [
        *PRELUDE,
        f"load definitionfile {check_filename(base, '.def')}",
        *(v.command("set", k, val) for k, val in operations),
        f"save settings.definitionfile {check_filename(target, '.def')}",
    ]
    return render(lines)


def user_script(text: str, catalog: Catalog | None = None) -> tuple[str, list[str], list[str], list[str]]:
    """Check a user script. Returns the runnable script, its result files, blocking problems and warnings.

    Unknown verbs, host commands and paths block; commands that would open dialogs or keep SCAPS open are
    removed with a warning; set and action keys missing from the installed command list only warn, because
    SCAPS also accepts older aliases and logs truly unknown commands.
    """
    outputs, lines, problems, warnings = [], [], [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        tokens = line.split(None, 1)
        verb, rest = tokens[0].lower(), tokens[1] if len(tokens) > 1 else ""
        if verb.startswith("run") and verb != "run":
            verb, rest = "run", f"{tokens[0][3:]} {rest}".strip()
        if verb not in _VERBS:
            problems.append(f"line {number}: {tokens[0]!r} is not a SCAPS command verb")
            continue
        if verb == "run" and not _RUN_ALLOWED.match(rest):
            problems.append(f"line {number}: only 'run script' and 'run dll' are allowed")
            continue
        words = rest.split(None, 1)
        key, value = (words[0] if words else ""), (words[1] if len(words) > 1 else "")
        if (verb in ("load", "save", "run") or "file" in key.lower()) and re.search(r"[\\/:]|\.\.", rest):
            problems.append(f"line {number}: file names must not contain paths")
            continue
        reason = _DROPPED.get((verb, key.lower()))
        if reason:
            warnings.append(f"line {number}: {line!r} removed; it {reason}")
            continue
        checkable = verb in ("set", "action") and key and catalog and not key.startswith("scriptvariable")
        if checkable and catalog.lookup(verb, key) is None:
            warnings.append(f"line {number}: '{verb} {key}' is not in the installed command list")
        if verb in ("set", "get") and re.match(r"^(left|right)contact\.", key, re.IGNORECASE):
            line = f"{verb} {canonical(key)} {value}".rstrip()
        match = re.match(r"^save\s+results\.(\w+)\s+(.+)$", line, re.IGNORECASE)
        if match:
            try:
                outputs.append(check_filename(match.group(2)))
            except ScriptError as error:
                problems.append(f"line {number}: {error}")
        lines.append(line)
    return render([*PRELUDE, *lines]), outputs, problems, warnings


def render(lines: Iterable[str]) -> str:
    lines = list(lines)
    if any("\n" in line or "\r" in line for line in lines):
        raise ScriptError("script lines must not contain line breaks")
    return "\r\n".join(lines) + "\r\n"
