"""MCP server exposing SCAPS-1D: devices, simulations, batches, results and checks."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import math
import os
import re
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, checks, devices, inputs
from .catalog import Catalog
from .config import ConfigError, Settings, load_settings
from .jobs import Job, Jobs
from .models import (
    Analysis,
    BatchPoint,
    BatchResult,
    Curve,
    Device,
    FileList,
    ImportResult,
    JobsResult,
    Limits,
    Metrics,
    OptimisationResult,
    SavedDefinition,
    ScapsMessage,
    ScriptCommands,
    ScriptFile,
    ScriptResult,
    SimulationResult,
    SQLimit,
    Status,
    Table,
    WrittenInput,
)
from .optimize import Variable, fit_error, nelder_mead
from .parsing import (
    DEFINITION_UNITS,
    Result,
    measurement_scales,
    parse_definition,
    parse_material,
    parse_result,
    parse_spectrum,
)
from .physics import Limit, Spectrum, band_offsets, integrated_jsc, mott_schottky, sq_limit
from .runner import ENCODING, ERROR_LOG, Runner, RunOutcome, new_run_id
from .scripts import (
    EXTENSIONS,
    Assignment,
    ScriptError,
    Simulation,
    Validator,
    check_filename,
    check_targets,
    edit_script,
    user_script,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Runs SCAPS-1D (University of Gent) on the user's own installation, headless.
Start with scaps_list_files and scaps_describe_device. Simulate with scaps_simulate, vary parameters with \
scaps_batch, read curves and spatial profiles with scaps_results, and plot with scaps_plot.
Keys and units follow the SCAPS script language and are validated against the installed version \
(scaps_script_reference): thickness um, densities cm^-3, interface densities cm^-2, mobility cm^2/Vs, \
velocity cm/s, cross sections cm^2, energies eV, temperature K. Layers, defects and interfaces count from 1; \
layer 1 touches the left (back) contact.
Overrides apply to one run; scaps_edit_device and scaps_create_device write new .def files.
Runs that exceed wait_seconds continue as jobs: poll, wait or cancel them with scaps_jobs.
Treat 'error' checks as unphysical results and report scaps_errors; never present such numbers as valid."""

RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
DEFAULT_SPECTRUM = "AM1_5G 1 sun.spe"
FOLDERS = {
    "definitions": ("def", (".def",)),
    "actions": ("def", (".act",)),
    "spectra": ("spectrum", None),
    "absorption": ("absorption", None),
    "materials": ("materials", None),
    "filters": ("filter", None),
    "generation": ("generation", None),
    "grading": ("grading", None),
    "measurements": ("measurements", None),
    "scripts": ("script", (".script",)),
}
KEY_COLUMNS = {
    "iv": ("v(V)", "jtot(mA/cm2)"),
    "qe": ("lambda(nm)", "QE(%)"),
    "cv": ("v(V)", "C(nF/cm2)", "G(S/cm2)"),
    "cf": ("f(Hz)", "C(nF/cm2)", "G(S/cm2)"),
}
SPATIAL = ("eb", "genrec", "ac")
KIND_BY_EXTENSION = {ext: kind for kind, ext in EXTENSIONS.items()}
METRIC_NAMES = {
    "Voc": "voc_v",
    "Jsc": "jsc_ma_cm2",
    "FF": "ff_pct",
    "eta": "eta_pct",
    "V_MPP": "vmpp_v",
    "J_MPP": "jmpp_ma_cm2",
}


def hints(read_only: bool, destructive: bool = False, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only, destructive_hint=destructive, idempotent_hint=idempotent, open_world_hint=False
    )


Value = float | str | None
Definition = Annotated[str, Field(description="Definition file in SCAPS def/, e.g. 'example CIGS.def'.")]
Measurements = Annotated[
    list[Literal["iv", "qe", "cv", "cf"]],
    Field(description="Measurements: iv (J-V), qe, cv, cf. May be empty when profiles are requested."),
]
Profiles = Annotated[
    list[Literal["eb", "genrec", "ac"]],
    Field(
        description="Spatial results at the working point voltage: eb (bands, quasi-Fermi levels, carriers, field, "
        "currents, generation and recombination rates) and genrec (occupation of every defect level); "
        "ac (small signal) at the last point of a cv or cf measurement."
    ),
]
Parameters = Annotated[
    dict[str, Value],
    Field(
        description="Ordered SCAPS set commands in script units, e.g. {'layer1.thickness': 2, 'layer1.NA': 1e16, "
        "'layer1.defect1.Ntotal': 1e14, 'layer1.defect1.singleacceptor': null, 'interface1.IFdefect1.Ntotal': 1e10, "
        "'contactleft.flatband.off': null, 'contactleft.workfunction': 5.1, 'external.Rs': 0.5, "
        "'illumination.fromleft': null, "
        "'numerical.Shockley-Queisser_limit.always': null}. null marks commands without a value."
    ),
]
Actions = Annotated[
    dict[str, Value],
    Field(
        description="SCAPS action commands, e.g. {'iv.startV': 0, 'iv.stopV': 1.2, 'iv.increment': 0.02, "
        "'qe.startlambda': 300, 'cf.points.per.decade': 5, 'intensity.ND': 1, 'spectrumcutoff.on': null, "
        "'spectrumcutoff.longlambda': 800}. Omitted settings keep SCAPS defaults."
    ),
]
Illumination = Annotated[Literal["light", "dark"], Field(description="Calculate under the spectrum or in the dark.")]
Temperature = Annotated[float | None, Field(ge=1, le=1000, description="Working point temperature in K.")]
Voltage = Annotated[float | None, Field(ge=-100, le=100, description="Working point bias in V.")]
Frequency = Annotated[float | None, Field(gt=0, le=1e12, description="Working point frequency in Hz.")]
SpectrumFile = Annotated[str | None, Field(description="Spectrum file in SCAPS spectrum/; default AM1.5G 1 sun.")]
WaitSeconds = Annotated[
    float, Field(ge=0, le=3600, description="Wait this long for the result; longer runs continue as a job.")
]


@dataclass
class State:
    settings: Settings | None = None
    runner: Runner | None = None
    catalog: Catalog | None = None


STATE = State()
JOBS = Jobs(lambda: settings().runs_dir)


@asynccontextmanager
async def lifespan(_: MCPServer) -> AsyncIterator[None]:
    async with anyio.create_task_group() as tasks:
        JOBS.tasks = tasks
        if sys.platform != "win32":
            tasks.start_soon(stop_on_signal)
        try:
            yield
        finally:
            JOBS.tasks = None
            tasks.cancel_scope.cancel()


async def stop_on_signal() -> None:
    """On SIGTERM or SIGINT, stop the running SCAPS jobs, wait for their cleanup and exit."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _ in signals:
            break
    log.warning("signal received; stopping SCAPS jobs")
    with anyio.move_on_after(60):
        await JOBS.stop()
    os._exit(0)


mcp = MCPServer("scaps", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan)


def settings() -> Settings:
    if STATE.settings is None:
        try:
            STATE.settings = load_settings()
        except ConfigError as error:
            raise ToolError(f"SCAPS is not configured: {error}") from None
        STATE.runner = Runner(STATE.settings)
        STATE.catalog = Catalog.for_home(STATE.settings.home, STATE.settings.exe.stem)
    return STATE.settings


def runner() -> Runner:
    settings()
    return STATE.runner


def validator() -> Validator:
    settings()
    return Validator(STATE.catalog)


def find_file(folder: str, name: str, suffix: str | None = None) -> str:
    try:
        name = check_filename(name, suffix)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    directory = settings().home / folder
    if not directory.is_dir():
        raise ToolError(f"the SCAPS installation has no {folder}/ folder")
    if (directory / name).is_file():
        return name
    match = next((p.name for p in directory.iterdir() if p.name.lower() == name.lower()), None)
    if match is None:
        raise ToolError(f"{name!r} not found in SCAPS {folder}/; call scaps_list_files")
    return match


def read_text(path: Path) -> str:
    return path.read_text(encoding=ENCODING, errors="replace")


def load_device(name: str) -> dict[str, Any]:
    return parse_definition(read_text(settings().home / "def" / name))


def load_spectrum(name: str | None) -> Spectrum | None:
    path = settings().home / "spectrum" / (name or DEFAULT_SPECTRUM)
    if not path.is_file():
        return None
    bins = parse_spectrum(read_text(path))
    return Spectrum.from_bins(bins) if bins else None


def apply_overrides(device: dict[str, Any], parameters: dict[str, Value]) -> dict[str, Any]:
    """Copy of a device summary with numeric layer and defect overrides applied, for analysis and checks."""
    device = json.loads(json.dumps(device))
    for key, value in parameters.items():
        match = re.match(r"^layer(\d+)\.(?:defect(\d+)\.)?([A-Za-z]+)$", key)
        if not match or not isinstance(value, (int, float)):
            continue
        index, defect = int(match.group(1)), match.group(2)
        if index > len(device["layers"]):
            continue
        target = device["layers"][index - 1]
        if defect:
            if int(defect) > len(target["defects"]):
                continue
            target = target["defects"][int(defect) - 1]
        target[match.group(3)] = value
    if "numerical.Shockley-Queisser_limit.always" in parameters:
        device["sq_limit_mode"] = 2
    return device


def device_summary(name: str, device: dict[str, Any]) -> Device:
    layers = device["layers"]
    for interface in device["interfaces"]:
        left, right = interface["layers"]
        if right <= len(layers):
            interface["band_offsets"] = band_offsets(layers[left - 1], layers[right - 1])
    return {
        "definition": name,
        "units": DEFINITION_UNITS,
        "absorber_layer": checks.absorber_index(device),
        "layers": layers,
        "interfaces": device["interfaces"],
        "contacts": device["contacts"],
        "working_point": device["working_point"],
        "sq_limit_mode": device.get("sq_limit_mode"),
        "checks": checks.device_checks(device),
    }


def metrics_of(result: Result) -> Metrics | None:
    if not result.metrics:
        return None
    return {key: result.metrics.get(name) for name, key in METRIC_NAMES.items()}  # type: ignore[return-value]


def curve_of(kind: str, name: str, result: Result) -> Curve | None:
    table = result.table
    if table is None or kind not in KEY_COLUMNS:
        return None
    keep = [table.columns.index(c) for c in KEY_COLUMNS[kind] if c in table.columns] or [0, 1]
    return {
        "kind": kind,
        "file": name,
        "columns": [table.columns[i] for i in keep],
        "rows": [[row[i] for i in keep] for row in table.rows],
    }


def messages(outcome: RunOutcome) -> list[ScapsMessage]:
    return [{"command": e.command, "message": e.message} for e in outcome.errors]


def run_status(outcome: RunOutcome, produced: int, expected: int) -> str:
    if outcome.timed_out:
        return "timed_out"
    if produced == expected:
        return "completed"
    return "partial" if produced else "failed"


def collect(outcome: RunOutcome, outputs: dict[str, str]) -> dict[str, Result]:
    parsed = {}
    for kind, name in outputs.items():
        path = outcome.files.get(name)
        if path is not None:
            result = parse_result(read_text(path))
            if result.table is not None:
                parsed[kind] = result
    return parsed


def analyse(
    parsed: dict[str, Result], device: dict[str, Any], spectrum: Spectrum | None, temperature: float, light: bool
) -> Analysis:
    analysis: Analysis = {}
    absorber = checks.absorber_index(device)
    if absorber is None:
        return analysis
    analysis["absorber_layer"] = absorber
    band_gap = device["layers"][absorber - 1].get("Eg")
    limit = sq_limit(spectrum, band_gap, temperature) if spectrum and band_gap and light else None
    if limit:
        analysis["sq_limit"] = asdict(limit)  # type: ignore[typeddict-item]
    if "qe" in parsed and spectrum:
        rows = parsed["qe"].table.rows
        analysis["qe_jsc_ma_cm2"] = integrated_jsc(spectrum, [r[0] for r in rows], [r[1] for r in rows])
        analysis["qe_range_nm"] = [rows[0][0], rows[-1][0]]
    if "cv" in parsed:
        table = parsed["cv"].table
        if "C(nF/cm2)" in table.columns and "v(V)" in table.columns:
            ci, vi = table.columns.index("C(nF/cm2)"), table.columns.index("v(V)")
            column = "eps_s to determine N"
            eps = table.rows[0][table.columns.index(column)] if column in table.columns else None
            eps = eps or device["layers"][absorber - 1].get("epsilon") or 10.0
            reverse = [r for r in table.rows if r[vi] is not None and r[vi] <= 0] or table.rows
            fit = mott_schottky([r[vi] for r in reverse], [r[ci] for r in reverse], eps)
            if fit:
                analysis["mott_schottky"] = fit
    return analysis


def limit_of(analysis: Analysis) -> Limit | None:
    data = analysis.get("sq_limit")
    return Limit(**data) if data else None


def job_progress(job: Job) -> Callable[[int, int], Awaitable[None]]:
    async def report(done: int, total: int) -> None:
        await job.report(done, total, f"{done}/{total} result files written")

    return report


def client_progress(ctx: Context | None):
    if ctx is None:
        return None

    async def report(done: int, total: int, message: str) -> None:
        await ctx.report_progress(done, total, message)

    return report


async def submit(ctx: Context | None, kind: str, run_id: str, work, wait_seconds: float) -> dict[str, Any]:
    if JOBS.tasks is None:
        raise ToolError("the server job runner is not running")
    snapshot = await JOBS.submit(run_id, kind, work, wait_seconds, client_progress(ctx))
    if snapshot["status"] == "failed" and snapshot.get("error"):
        raise ToolError(f"run {run_id} failed: {snapshot['error']}")
    return snapshot


def failure(result: dict[str, Any]) -> ToolError:
    detail = "; ".join(
        f"{e['command']}: {e['message']}" if e["command"] else e["message"] for e in result.get("scaps_errors", [])
    )
    hint = {
        "timed_out": "SCAPS did not finish; the solver may not converge. Narrow the voltage range, keep defect "
        "densities below the doping, raise the iteration limit, or raise SCAPS_TIMEOUT.",
        "failed": "SCAPS produced no results; fix the reported commands and retry.",
    }.get(result["status"], "")
    return ToolError(f"run {result['run_id']} {result['status']}. {detail} {hint}".strip())


def finish(snapshot: dict[str, Any], base: dict[str, Any], has_data: Callable[[dict], bool]) -> dict[str, Any]:
    result = snapshot.get("result")
    if result is None:
        return {**base, "status": snapshot["status"], "progress": snapshot.get("progress")}
    if result["status"] in ("failed", "timed_out") and not has_data(result):
        raise failure(result)
    return result


def build_simulation(
    definition: str,
    measurements: list[str],
    profiles: list[str],
    parameters: dict[str, Value],
    actions: dict[str, Value],
    illumination: str,
    temperature_k: float | None,
    voltage_v: float | None,
    frequency_hz: float | None,
    spectrum: str | None,
    extra_keys: tuple[str, ...] = (),
) -> tuple[Simulation, dict[str, Any]]:
    name = find_file("def", definition, ".def")
    working_point = (
        ("workingpoint.temperature", temperature_k),
        ("workingpoint.voltage", voltage_v),
        ("workingpoint.frequency", frequency_hz),
    )
    action_list: list[Assignment] = [(k, v) for k, v in working_point if v is not None] + list(actions.items())
    try:
        sim = Simulation(
            definition=name,
            measurements=list(measurements),
            profiles=list(profiles),
            settings=list(parameters.items()),
            actions=action_list,
            illumination=illumination,
            spectrum=find_file("spectrum", spectrum) if spectrum else None,
        )
        device = load_device(name)
        check_targets(device, [*parameters, *extra_keys])
        sim.validate(validator())
    except ScriptError as error:
        raise ToolError(str(error)) from None
    return sim, device


@mcp.tool(title="SCAPS installation status", annotations=hints(read_only=True))
async def scaps_status() -> Status:
    """Report the SCAPS installation in use and how it was found, other installations, run folder and jobs."""
    s = settings()
    return {
        "scaps_version": s.version,
        "executable": s.exe.name,
        "scaps_home": str(s.home),
        "scaps_home_source": s.source,
        "other_installations": [str(p) for p in s.others],
        "wine_prefix": str(s.wineprefix) if s.wineprefix else None,
        "runs_dir": str(s.runs_dir),
        "timeout_s": s.timeout,
        "busy": runner().busy,
        "script_commands": len(STATE.catalog.commands) if STATE.catalog else 0,
        "plotting": plotting_available(),
        "active_jobs": [j["job_id"] for j in JOBS.list(50) if j["status"] in ("queued", "running")],
    }


@mcp.tool(title="List SCAPS files", annotations=hints(read_only=True))
def scaps_list_files(
    kind: Annotated[
        Literal[
            "definitions",
            "actions",
            "spectra",
            "absorption",
            "materials",
            "filters",
            "generation",
            "grading",
            "measurements",
            "scripts",
        ],
        Field(description="SCAPS data folder to list."),
    ] = "definitions",
    contains: Annotated[str | None, Field(description="Case-insensitive substring filter.")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> FileList:
    """List files in a SCAPS data folder: definitions, spectra, absorption, grading, generation and more."""
    folder, suffixes = FOLDERS[kind]
    directory = settings().home / folder
    names = sorted(
        p.name
        for p in (directory.iterdir() if directory.is_dir() else [])
        if p.is_file()
        and (suffixes is None or p.suffix.lower() in suffixes)
        and not p.name.startswith("mcp_")
        and p.name.lower() != "thumbs.db"
        and (not contains or contains.lower() in p.name.lower())
    )
    return {"kind": kind, "total": len(names), "files": names[offset : offset + limit]}


@mcp.tool(title="Search SCAPS script commands", annotations=hints(read_only=True))
def scaps_script_reference(
    query: Annotated[str, Field(min_length=1, description="Words to match, e.g. 'defect Ntotal' or 'action qe'.")],
    limit: Annotated[int, Field(ge=1, le=300)] = 60,
) -> ScriptCommands:
    """Search the command list of the installed SCAPS version.

    layer, defect, interface and IFdefect stand for numbered forms (layer2, defect1); contact stands for
    contactleft or contactright; <none> marks commands without a value.
    """
    settings()
    if STATE.catalog is None:
        raise ToolError("no scriptdescription*.txt found in the SCAPS folder")
    found = STATE.catalog.search(query.split())
    return {"query": query, "source": STATE.catalog.source, "total": len(found), "commands": found[:limit]}


@mcp.tool(title="Describe a SCAPS device", annotations=hints(read_only=True))
def scaps_describe_device(definition: Definition) -> Device:
    """Read a .def file: layers, bulk defects, interfaces with band offsets, contacts and realism checks.

    Fields use script names and units, so any value can be passed back as a parameter
    (layers[0].NA -> 'layer1.NA'). Graded layers report their base value.
    """
    name = find_file("def", definition, ".def")
    return device_summary(name, load_device(name))


def resolve_reference(folder: str, name: str) -> str | None:
    """Find a referenced input file, tolerating differences in case, spaces and punctuation."""
    directory = settings().home / folder
    if not directory.is_dir():
        return None
    if (directory / name).is_file():
        return name
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return next((p.name for p in directory.iterdir() if re.sub(r"[^a-z0-9]", "", p.name.lower()) == key), None)


def material_settings(layer: int, material: str) -> tuple[list[Assignment], list[str]]:
    """Set commands that load a .material file into a layer (pure A values), as the GUI does."""
    name = find_file("materials", material, ".material")
    parsed = parse_material(read_text(settings().home / "materials" / name))
    assignments: list[Assignment] = [(f"layer{layer}.{k}", v) for k, v in parsed["properties"].items()]
    notes = []
    source = parsed["absorption"].get("file")
    if source:
        found = resolve_reference("absorption", source)
        if found:
            assignments.append((f"layer{layer}.absorption.file", found))
        else:
            notes.append(f"{name} refers to absorption file {source!r}, which is missing; absorption left unchanged")
    elif parsed["absorption"].get("model"):
        notes.append(
            f"{name} uses absorption model {parsed['absorption']['model']!r}; set it with absorptionmodel keys"
        )
    return assignments, notes


def finish_definition(path: Path, numerics: dict[str, float], assignments: list[Assignment]) -> None:
    """Apply what scripts cannot: solver numerics and left-side interface cross sections."""
    text = read_text(path)
    try:
        if numerics:
            text = devices.set_numerics(text, numerics)
        text = devices.set_interface_cross_sections(text, devices.interface_cross_sections(assignments))
    except ScriptError as error:
        raise ToolError(str(error)) from None
    path.write_text(text, encoding=ENCODING, newline="")


async def save_definition(script: str, target: str, numerics: dict[str, float], assignments: list[Assignment]) -> str:
    path = settings().home / "def" / target
    outcome = await runner().run(script, [])
    if outcome.errors or not path.exists():
        raise failure({"run_id": outcome.run_id, "status": "failed", "scaps_errors": messages(outcome)})
    finish_definition(path, numerics, assignments)
    return outcome.run_id


Numerics = Annotated[
    dict[str, float],
    Field(
        description="Numerical settings stored in the .def 'convergence' section, which scripts cannot set, "
        "e.g. {'maxiter': 2000, 'clamp_psi': 0.25, 'termination_psi': 1e-4, 'defectdiscretisations': 9}."
    ),
]


@mcp.tool(title="Edit a SCAPS device", annotations=hints(read_only=False, destructive=True))
async def scaps_edit_device(
    base: Annotated[str, Field(description="Existing definition to start from.")],
    save_as: Annotated[str, Field(description="New definition name, saved in SCAPS def/.")],
    operations: Annotated[
        dict[str, Value],
        Field(
            description="Ordered set commands, including structural ones: 'layer2.duplicate', 'layer3.remove', "
            "'layer1.defect1.duplicate', 'layer1.defect2.gauss', 'interface1.2_default_IFdefects', "
            "'layer1.Eg.file': 'profile.grd', 'layer1.absorption.file': 'material.abs'."
        ),
    ] = {},
    materials: Annotated[
        dict[int, str],
        Field(description="Load .material files into layers before the operations, e.g. {1: 'CIGSe.material'}."),
    ] = {},
    numerics: Numerics = {},
    overwrite: Annotated[bool, Field(description="Replace an existing file of that name.")] = False,
) -> SavedDefinition:
    """Change a device and save it as a .def file: parameters, layers, defects, materials and solver settings."""
    base = find_file("def", base, ".def")
    device = load_device(base)
    assignments: list[Assignment] = []
    notes: list[str] = []
    for layer, material in materials.items():
        loaded, remarks = material_settings(layer, material)
        assignments += loaded
        notes += remarks
    assignments += list(operations.items())
    try:
        target = check_filename(save_as, ".def")
        check_targets(device, [k for k, _ in assignments])
        script = edit_script(validator(), base, assignments, target)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    if (settings().home / "def" / target).exists() and not overwrite:
        raise ToolError(f"{target} already exists; choose another name or set overwrite=true")
    run_id = await save_definition(script, target, numerics, assignments)
    device = device_summary(target, load_device(target))
    return {"definition": target, "run_id": run_id, "device": device, "notes": notes}


@mcp.tool(title="Create a SCAPS device", annotations=hints(read_only=False, destructive=True))
async def scaps_create_device(
    name: Annotated[str, Field(description="New definition name, saved in SCAPS def/.")],
    layers: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            max_length=7,
            description="Layers from the left (back) contact. Each maps set-command suffixes to values, e.g. "
            "{'name': 'absorber', 'thickness': 0.5, 'Eg': 1.55, 'chi': 3.9, 'epsilon': 10, 'NC': 2.2e18, "
            "'NV': 1.8e19, 'mun': 2, 'mup': 2, 'NA': 1e15, 'radiative': 1e-11, 'absorption.file': 'material.abs', "
            "'material': 'CIGSe.material', "
            "'defects': [{'singleacceptor': null, 'gauss': null, 'Et': 0.6, 'Ntotal': 1e14, "
            "'capture_cross_section.electron': 1e-15, 'capture_cross_section.hole': 1e-15}]}. "
            "Unset properties keep SCAPS defaults; up to 7 defects per layer.",
        ),
    ],
    interfaces: Annotated[
        dict[int, list[dict[str, Any]]],
        Field(
            description="Interface defects by interface number (1 lies between layers 1 and 2), up to 3 each, "
            "e.g. {1: [{'Ntotal': 1e10, 'singledonor': null}]}."
        ),
    ] = {},
    left_contact: Annotated[
        dict[str, Value],
        Field(description="e.g. {'workfunction': 5.1, 'Sn': 1e5, 'Sp': 1e7}; a work function switches flat band off."),
    ] = {},
    right_contact: Annotated[dict[str, Value], Field(description="As left_contact.")] = {},
    options: Annotated[
        dict[str, Value],
        Field(
            description="Other set commands, e.g. {'illumination.fromleft': null, "
            "'numerical.Shockley-Queisser_limit.always': null}."
        ),
    ] = {},
    numerics: Numerics = {},
    overwrite: bool = False,
) -> SavedDefinition:
    """Build a new device from scratch and save it as a .def file.

    A layer may name a .material file ('material': 'CIGSe.material'); its properties apply first and
    explicit keys override them.
    """
    notes: list[str] = []
    expanded = []
    for layer in layers:
        if layer.get("material"):
            loaded, remarks = material_settings(1, layer["material"])
            notes += remarks
            layer = {**{k.removeprefix("layer1."): v for k, v in loaded}, **layer}
            layer.pop("material")
        expanded.append(layer)
    layers = expanded
    try:
        target = check_filename(name, ".def")
        devices.validate(layers, interfaces)
        v = validator()
        contacts = {side: c for side, c in (("left", left_contact), ("right", right_contact)) if c}
        first = devices.layer_script(v, layers, contacts, list(options.items()), target)
        second = devices.defect_script(v, target, layers, interfaces)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    path = settings().home / "def" / target
    if path.exists() and not overwrite:
        raise ToolError(f"{target} already exists; choose another name or set overwrite=true")
    outcome = await runner().run(first, [])
    if outcome.errors or not path.exists():
        raise failure({"run_id": outcome.run_id, "status": "failed", "scaps_errors": messages(outcome)})
    seeded = [i for i, layer in enumerate(layers, 1) if layer.get("defects")]
    finish_definition(path, numerics, [])
    if seeded or interfaces:
        text = read_text(path)
        path.write_text(devices.insert_seed_defects(text, seeded), encoding=ENCODING, newline="")
        outcome = await runner().run(second, [])
        if outcome.errors:
            raise failure({"run_id": outcome.run_id, "status": "failed", "scaps_errors": messages(outcome)})
        requested = [
            (f"interface{k}.IFdefect{j}.{key}", value)
            for k, defects in interfaces.items()
            for j, defect in enumerate(defects, 1)
            for key, value in defect.items()
        ]
        finish_definition(path, {}, requested)
    device = device_summary(target, load_device(target))
    return {"definition": target, "run_id": outcome.run_id, "device": device, "notes": notes}


@mcp.tool(title="Run a SCAPS simulation", annotations=hints(read_only=False, idempotent=False))
async def scaps_simulate(
    ctx: Context,
    definition: Definition,
    measurements: Measurements = ["iv"],
    profiles: Profiles = [],
    parameters: Parameters = {},
    actions: Actions = {},
    illumination: Illumination = "light",
    temperature_k: Temperature = None,
    voltage_v: Voltage = None,
    frequency_hz: Frequency = None,
    spectrum: SpectrumFile = None,
    response_format: Annotated[
        Literal["concise", "detailed"], Field(description="detailed adds the key columns of each curve.")
    ] = "concise",
    wait_seconds: WaitSeconds = 300,
) -> SimulationResult:
    """Simulate a device: J-V metrics, QE, C-V, C-f and spatial profiles, with analysis and realism checks.

    Analysis gives the absorber's Shockley-Queisser limit, QE-integrated Jsc and a Mott-Schottky fit of
    C-V. Overrides apply to this run only; files stay available through scaps_results and scaps_plot.
    """
    sim, device = build_simulation(
        definition,
        measurements,
        profiles,
        parameters,
        actions,
        illumination,
        temperature_k,
        voltage_v,
        frequency_hz,
        spectrum,
    )
    effective = apply_overrides(device, parameters)
    run_id = new_run_id()
    base: SimulationResult = {
        "run_id": run_id,
        "status": "queued",
        "progress": None,
        "definition": sim.definition,
        "measurements": sim.measurements,
        "profiles": sim.profiles,
        "metrics": None,
        "analysis": {},
        "files": [],
        "curves": [],
        "scaps_errors": [],
        "checks": [],
        "seconds": None,
    }

    async def work(job: Job) -> dict[str, Any]:
        script, outputs = sim.script(validator(), run_id)
        outcome = await runner().run(script, list(outputs.values()), run_id=run_id, progress=job_progress(job))
        parsed = collect(outcome, outputs)
        light = illumination == "light"
        analysis = analyse(parsed, effective, load_spectrum(sim.spectrum), temperature_k or 300.0, light)
        metrics = metrics_of(parsed["iv"]) if "iv" in parsed else None
        found = checks.device_checks(effective) + checks.result_checks(metrics, limit_of(analysis), temperature_k)
        curves = [curve_of(k, outputs[k], r) for k, r in parsed.items()] if response_format == "detailed" else []
        return {
            **base,
            "status": run_status(outcome, len(parsed), len(outputs)),
            "metrics": metrics,
            "analysis": analysis,
            "files": [outputs[k] for k in parsed],
            "curves": [c for c in curves if c],
            "scaps_errors": messages(outcome),
            "checks": found,
            "seconds": outcome.seconds,
        }

    snapshot = await submit(ctx, "simulation", run_id, work, wait_seconds)
    return finish(snapshot, base, lambda r: bool(r["files"]))  # type: ignore[return-value]


@mcp.tool(title="Batch SCAPS simulations", annotations=hints(read_only=False, idempotent=False))
async def scaps_batch(
    ctx: Context,
    definition: Definition,
    variables: Annotated[
        dict[str, list[float | str]],
        Field(
            min_length=1,
            description="Set or action keys and their values, e.g. {'layer1.thickness': [0.3, 0.5, 1]} or "
            "{'workingpoint.temperature': [280, 300, 320], 'intensity.ND': [0, 1, 2]}.",
        ),
    ],
    mode: Annotated[
        Literal["grid", "zip"], Field(description="grid runs every combination; zip pairs the lists element-wise.")
    ] = "grid",
    measurements: Measurements = ["iv"],
    profiles: Profiles = [],
    parameters: Parameters = {},
    actions: Actions = {},
    illumination: Illumination = "light",
    temperature_k: Temperature = None,
    spectrum: SpectrumFile = None,
    wait_seconds: WaitSeconds = 300,
) -> BatchResult:
    """Run one SCAPS session over many parameter values and tabulate the J-V metrics of every point.

    Covers thickness, doping, defect, band offset, temperature, intensity and bias studies. Every point's
    files are kept for scaps_results and scaps_plot.
    """
    sim, device = build_simulation(
        definition,
        measurements,
        profiles,
        parameters,
        actions,
        illumination,
        temperature_k,
        None,
        None,
        spectrum,
        extra_keys=tuple(variables),
    )
    v = validator()
    try:
        _, points, _ = sim.batch_script(v, "check", list(variables.items()), mode)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    if len(points) > 500:
        raise ToolError(f"{len(points)} points requested; split the batch into runs of at most 500")
    run_id = new_run_id()
    base: BatchResult = {
        "run_id": run_id,
        "status": "queued",
        "progress": None,
        "definition": sim.definition,
        "variables": list(variables),
        "measurements": sim.measurements,
        "points": [],
        "scaps_errors": [],
        "checks": [],
        "seconds": None,
    }

    async def work(job: Job) -> dict[str, Any]:
        script, values, steps = sim.batch_script(v, run_id, list(variables.items()), mode)
        names = [name for step in steps for name in step.values()]
        timeout = settings().timeout * max(1, len(values))
        outcome = await runner().run(script, names, run_id=run_id, timeout=timeout, progress=job_progress(job))
        rows: list[BatchPoint] = []
        produced = 0
        for point, step in zip(values, steps, strict=True):
            parsed = collect(outcome, step)
            produced += len(parsed)
            metrics = metrics_of(parsed["iv"]) if "iv" in parsed else None
            rows.append({"values": point, "metrics": metrics, "files": [step[k] for k in parsed]})
        found = checks.device_checks(apply_overrides(device, parameters))
        for key in variables:
            found += checks.sweep_checks(key, [{"value": r["values"][key], "metrics": r["metrics"]} for r in rows])
        return {
            **base,
            "status": run_status(outcome, produced, len(names)),
            "points": rows,
            "scaps_errors": messages(outcome),
            "checks": found,
            "seconds": outcome.seconds,
        }

    snapshot = await submit(ctx, "batch", run_id, work, wait_seconds)
    return finish(snapshot, base, lambda r: any(p["files"] for p in r["points"]))  # type: ignore[return-value]


@mcp.tool(title="Manage SCAPS jobs", annotations=hints(read_only=False, idempotent=False))
async def scaps_jobs(
    action: Annotated[Literal["list", "status", "wait", "cancel"], Field(description="What to do.")] = "list",
    job_id: Annotated[str | None, Field(description="Job (run) id for status, wait and cancel.")] = None,
    wait_seconds: WaitSeconds = 60,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> JobsResult:
    """List recent runs, or get, wait for or cancel one. A finished job includes its full result."""
    settings()
    if action == "list":
        return {"jobs": JOBS.list(limit), "result": None}  # type: ignore[typeddict-item]
    if not job_id or not RUN_ID.match(job_id):
        raise ToolError("job_id is required: use a run id or call scaps_jobs with action='list'")
    try:
        if action == "wait":
            snapshot = await JOBS.wait(job_id, wait_seconds)
        elif action == "cancel":
            snapshot = JOBS.cancel(job_id)
        else:
            snapshot = JOBS.get(job_id)
    except KeyError:
        raise ToolError(f"job {job_id} not found") from None
    info = {k: snapshot.get(k) for k in ("job_id", "kind", "status", "progress", "error")}
    return {"jobs": [info], "result": snapshot.get("result")}  # type: ignore[typeddict-item]


def run_directory(run_id: str) -> Path:
    if not RUN_ID.match(run_id):
        raise ToolError("invalid run_id")
    directory = settings().runs_dir / run_id
    if not directory.is_dir():
        raise ToolError(f"run {run_id} not found")
    return directory


RUN_METADATA = {"input.script", "summary.json", ERROR_LOG}


def result_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.is_file() and p.name not in RUN_METADATA)


def select_table(parsed: Result, name: str, table: str | None):
    titles = [t.title or str(i) for i, t in enumerate(parsed.tables)]
    if table is None:
        chosen = parsed.table
    elif table in titles:
        chosen = parsed.tables[titles.index(table)]
    elif table.isdigit() and int(table) < len(parsed.tables):
        chosen = parsed.tables[int(table)]
    else:
        raise ToolError(f"table {table!r} not in {name}; tables: {titles}")
    if chosen is None:
        raise ToolError(f"{name} contains no data")
    return chosen, titles


@mcp.tool(title="Read SCAPS results", annotations=hints(read_only=True))
def scaps_results(
    run_id: Annotated[str, Field(description="Run id from scaps_simulate, scaps_batch or scaps_run_script.")],
    file: Annotated[str | None, Field(description="Result file of the run; omit to list the files.")] = None,
    table: Annotated[
        str | None, Field(description="Table title ('bulk', 'interface') or index; default the main table.")
    ] = None,
    columns: Annotated[list[str] | None, Field(description="Columns to keep; default all.")] = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    max_rows: Annotated[int, Field(ge=1, le=2000)] = 200,
    step: Annotated[int, Field(ge=1, le=1000, description="Keep every n-th row to thin long profiles.")] = 1,
) -> Table:
    """Return one table of a result file: J-V, QE, C-V, C-f curves or band, generation and AC profiles."""
    directory = run_directory(run_id)
    files = result_files(directory)
    result: Table = {
        "run_id": run_id,
        "file": "",
        "kind": None,
        "title": None,
        "tables": files,
        "columns": [],
        "rows": [],
        "total_rows": 0,
        "offset": 0,
        "info": {},
    }
    if file is None:
        return result
    if file not in files:
        raise ToolError(f"{file!r} is not in run {run_id}; files: {', '.join(files) or 'none'}")
    parsed = parse_result(read_text(directory / file))
    chosen, titles = select_table(parsed, file, table)
    keep = list(range(len(chosen.columns)))
    if columns:
        missing = [c for c in columns if c not in chosen.columns]
        if missing:
            raise ToolError(f"unknown columns {missing}; available: {chosen.columns}")
        keep = [chosen.columns.index(c) for c in columns]
    rows = chosen.rows[::step]
    return {
        **result,
        "file": file,
        "kind": KIND_BY_EXTENSION.get(Path(file).suffix.lstrip(".").lower()),
        "title": chosen.title,
        "tables": titles,
        "columns": [chosen.columns[i] for i in keep],
        "rows": [[r[i] for i in keep] for r in rows[offset : offset + max_rows]],
        "total_rows": len(rows),
        "offset": offset,
        "info": parsed.info,
    }


def plotting_available() -> bool:
    return importlib.util.find_spec("matplotlib") is not None


@mcp.tool(title="Plot SCAPS results", annotations=hints(read_only=True))
def scaps_plot(
    run_id: Annotated[str, Field(description="Run id.")],
    files: Annotated[list[str], Field(min_length=1, max_length=50, description="Result files of the run to overlay.")],
    x: Annotated[str | None, Field(description="X column; default the natural axis of the file type.")] = None,
    y: Annotated[list[str] | None, Field(description="Y columns; default the key column of the file type.")] = None,
    table: Annotated[str | None, Field(description="Table title or index, as in scaps_results.")] = None,
    log_x: bool = False,
    log_y: bool = False,
    title: str | None = None,
    width_px: Annotated[int, Field(ge=300, le=1600)] = 800,
) -> Image:
    """Render result curves as a PNG image. Requires the optional 'plot' extra."""
    if not plotting_available():
        raise ToolError("plotting needs matplotlib: install scaps-mcp with the 'plot' extra")
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    directory = run_directory(run_id)
    available = result_files(directory)
    fig, ax = plt.subplots(figsize=(width_px / 100, width_px * 0.62 / 100), dpi=100)
    try:
        for name in files:
            if name not in available:
                raise ToolError(f"{name!r} is not in run {run_id}")
            chosen, _ = select_table(parse_result(read_text(directory / name)), name, table)
            kind = KIND_BY_EXTENSION.get(Path(name).suffix.lstrip(".").lower(), "")
            x_name = x or chosen.columns[1 if kind in SPATIAL and len(chosen.columns) > 1 else 0]
            y_names = y or [c for c in KEY_COLUMNS.get(kind, ())[1:2] if c in chosen.columns] or [chosen.columns[-1]]
            for column in [x_name, *y_names]:
                if column not in chosen.columns:
                    raise ToolError(f"column {column!r} not in {name}; available: {chosen.columns}")
            xi = chosen.columns.index(x_name)
            for column in y_names:
                yi = chosen.columns.index(column)
                points = [(r[xi], r[yi]) for r in chosen.rows if r[xi] is not None and r[yi] is not None]
                label = column if len(files) == 1 else f"{Path(name).stem} {column}"
                ax.plot([p[0] for p in points], [p[1] for p in points], label=label)
            ax.set_xlabel(x_name)
        ax.set_xscale("log" if log_x else "linear")
        ax.set_yscale("log" if log_y else "linear")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        ax.set_title(title or run_id, fontsize=10)
        buffer = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buffer, format="png")
    finally:
        plt.close(fig)
    return Image(data=buffer.getvalue(), format="png")


@mcp.tool(title="Run a SCAPS script", annotations=hints(read_only=False, destructive=True, idempotent=False))
async def scaps_run_script(
    ctx: Context,
    script: Annotated[str, Field(description="SCAPS script text, one command per line.")],
    validate_only: Annotated[bool, Field(description="Only check the script and report problems.")] = False,
    timeout_s: Annotated[float | None, Field(ge=1, le=86400, description="Override SCAPS_TIMEOUT.")] = None,
    wait_seconds: WaitSeconds = 300,
) -> ScriptResult:
    """Run a SCAPS script for workflows beyond the other tools: recorder, curve fitting, math, loops, tandem.

    The server prepends quit and error-log settings and 'clear all'. Commands are checked against the
    installed SCAPS; file names must be bare; system commands are rejected. Results saved with
    'save results.<kind> name' are collected into the run.
    """
    settings()
    try:
        text, outputs, problems, warnings = user_script(script, STATE.catalog)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    run_id = new_run_id()
    base: ScriptResult = {
        "run_id": run_id,
        "status": "queued",
        "progress": None,
        "problems": problems,
        "warnings": warnings,
        "files": [],
        "scaps_errors": [],
        "seconds": None,
    }
    if validate_only:
        return {**base, "status": "failed" if problems else "completed"}
    if problems:
        raise ToolError("script rejected: " + "; ".join(problems))

    async def work(job: Job) -> dict[str, Any]:
        outcome = await runner().run(text, outputs, run_id=run_id, timeout=timeout_s, progress=job_progress(job))
        files: list[ScriptFile] = []
        for name in outputs:
            path = outcome.files.get(name)
            if path is not None:
                parsed = parse_result(read_text(path))
                rows = len(parsed.table.rows) if parsed.table else 0
                files.append({"file": name, "kind": parsed.kind, "metrics": metrics_of(parsed), "rows": rows})
        produced = sum(1 for f in files if f["rows"])
        if outputs:
            status = run_status(outcome, produced, len(outputs))
        else:
            status = "timed_out" if outcome.timed_out else "completed"
        return {
            **base,
            "status": status,
            "files": files,
            "scaps_errors": messages(outcome),
            "seconds": outcome.seconds,
        }

    snapshot = await submit(ctx, "script", run_id, work, wait_seconds)
    return finish(snapshot, base, lambda r: True)  # type: ignore[return-value]


@mcp.tool(title="Shockley-Queisser limit", annotations=hints(read_only=True))
def scaps_sq_limit(
    band_gaps_ev: Annotated[list[float], Field(min_length=1, max_length=200, description="Band gaps in eV.")],
    temperature_k: Annotated[float, Field(ge=1, le=1000)] = 300.0,
    spectrum: SpectrumFile = None,
) -> Limits:
    """Detailed-balance Jsc, Voc, FF and efficiency limits for given band gaps under a SCAPS spectrum."""
    name = find_file("spectrum", spectrum) if spectrum else DEFAULT_SPECTRUM
    loaded = load_spectrum(name)
    if loaded is None:
        raise ToolError(f"spectrum {name!r} is missing or empty")
    if any(not 0.3 <= gap <= 5 for gap in band_gaps_ev):
        raise ToolError("band gaps must be between 0.3 and 5 eV")
    limits: list[SQLimit] = []
    for gap in band_gaps_ev:
        limit = sq_limit(loaded, gap, temperature_k)
        if limit is None:
            raise ToolError(f"no limit for Eg = {gap:g} eV at {temperature_k:g} K: no photons above the gap")
        limits.append(asdict(limit))  # type: ignore[arg-type]
    return {"spectrum": name, "limits": limits}


IMPORT_KINDS = {
    "definition": ("def", (".def",)),
    "actions": ("def", (".act",)),
    "material": ("materials", (".material",)),
    "absorption": ("absorption", (".abs",)),
    "spectrum": ("spectrum", (".spe",)),
    "generation": ("generation", (".gen",)),
    "filter": ("filter", (".ftr",)),
    "grading": ("grading", (".grd",)),
    "measurement": ("measurements", (".iv", ".cv", ".cf", ".qe")),
    "script": ("script", (".script",)),
}
REFERENCE_FOLDERS = {
    ".abs": "absorption",
    ".grd": "grading",
    ".ftr": "filter",
    ".opt": "absorption",
    ".gen": "generation",
}


def missing_references(text: str) -> list[str]:
    missing = []
    for line in text.splitlines():
        if ":" not in line or line.lstrip().startswith(">"):
            continue
        value = line.split(":", 1)[1].strip()
        folder = REFERENCE_FOLDERS.get(Path(value).suffix.lower())
        if folder and not (settings().home / folder / value).is_file():
            missing.append(f"{folder}/{value}")
    return sorted(set(missing))


@mcp.tool(title="Import a SCAPS file", annotations=hints(read_only=False, destructive=True))
def scaps_import_file(
    kind: Annotated[
        Literal[tuple(IMPORT_KINDS)],  # type: ignore[valid-type]
        Field(description="File type; decides the SCAPS folder."),
    ],
    name: Annotated[str, Field(description="File name with its SCAPS extension, e.g. 'perovskite cell.def'.")],
    content: Annotated[str, Field(max_length=5_000_000, description="Full text of the file.")],
    overwrite: bool = False,
) -> ImportResult:
    """Add a SCAPS file someone shared (device .def, .material, .abs, spectrum, measurement...) to the installation.

    Definitions and materials are parsed to confirm they are valid, and files they reference but that are
    missing (absorption, grading, filter) are listed.
    """
    home = settings().home
    folder, suffixes = IMPORT_KINDS[kind]
    try:
        target = check_filename(name)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    if Path(target).suffix.lower() not in suffixes:
        raise ToolError(f"a {kind} file needs one of the extensions {', '.join(suffixes)}")
    text = content.replace("\r\n", "\n").replace("\n", "\r\n")
    summary: dict[str, Any] = {}
    if kind == "script":
        _, _, problems, warnings = user_script(text, STATE.catalog)
        if problems:
            raise ToolError("script rejected: " + "; ".join(problems))
        summary = {"warnings": warnings}
    elif kind == "definition":
        device = parse_definition(text)
        if not device["layers"]:
            raise ToolError("no layers found: this is not a SCAPS problem definition file")
        summary = {"layers": [layer.get("name") for layer in device["layers"]], "interfaces": len(device["interfaces"])}
    elif kind == "material":
        parsed = parse_material(text)
        if not parsed["properties"]:
            raise ToolError("no material properties found: this is not a SCAPS .material file")
        summary = parsed
    elif kind != "actions":
        rows = sum(
            1 for line in text.splitlines() if len(line.split()) >= 2 and all(is_number(t) for t in line.split()[:2])
        )
        if rows < 2:
            raise ToolError("fewer than two numeric data rows found")
        summary = {"data_rows": rows}
    path = home / folder / target
    if path.exists() and not overwrite:
        raise ToolError(f"{folder}/{target} already exists; choose another name or set overwrite=true")
    path.write_text(text, encoding=ENCODING, errors="replace", newline="")
    return {"kind": kind, "file": target, "missing_references": missing_references(text), "summary": summary}


def is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


@mcp.tool(title="Write a SCAPS input file", annotations=hints(read_only=False, destructive=True))
def scaps_write_data(
    kind: Annotated[
        Literal["absorption", "spectrum", "generation", "filter", "grading", "measurement"],
        Field(description="Input file type."),
    ],
    name: Annotated[str, Field(description="File name; the SCAPS extension is added if missing.")],
    x: Annotated[
        list[float],
        Field(
            min_length=2,
            description="absorption: wavelength or photon energy (x_unit); spectrum, filter: wavelength nm; "
            "generation: position um; grading: position um, "
            "relative position or composition; measurement: V (iv, cv), Hz (cf) or nm (qe).",
        ),
    ],
    y: Annotated[
        list[float],
        Field(
            min_length=2,
            description="absorption: alpha or k (see quantity); spectrum: W/m2/nm or W/m2 per point; generation: "
            "G; filter: % transmission or reflection; grading: property in script units; measurement: mA, nF or QE %.",
        ),
    ],
    quantity: Annotated[
        str | None,
        Field(
            description="absorption: alpha_per_cm (default), alpha_per_m or k; spectrum: irradiance_w_m2_nm "
            "(default) or power_w_m2; generation: per_cm3_s (default) or per_m3_s; grading: the property, e.g. "
            "composition, Eg, NA, Nt; measurement: iv, cv, cf or qe."
        ),
    ] = None,
    x_unit: Annotated[
        Literal["nm", "um", "ev"], Field(description="absorption only: unit of x, wavelength or photon energy.")
    ] = "nm",
    extra: Annotated[list[float] | None, Field(description="measurement only: conductance G in S.")] = None,
    position: Annotated[
        Literal["absolute", "relative", "composition"], Field(description="grading only.")
    ] = "absolute",
    interpolation: Annotated[Literal["linear", "logarithmic"] | None, Field(description="grading only.")] = None,
    shared: Annotated[bool, Field(description="grading only: one profile over adjacent layers.")] = False,
    normalise_w_m2: Annotated[float | None, Field(gt=0, description="spectrum only: scale total power.")] = None,
    area_cm2: Annotated[float, Field(gt=0, description="measurement only.")] = 1.0,
    temperature_k: Annotated[float | None, Field(description="measurement only.")] = None,
    bias_v: Annotated[float | None, Field(description="measurement only.")] = None,
    frequency_hz: Annotated[float | None, Field(description="measurement only.")] = None,
    pin_mw_cm2: Annotated[float | None, Field(description="measurement only.")] = None,
    dark: Annotated[bool, Field(description="measurement only.")] = False,
    note: Annotated[str | None, Field(description="Source or description, written as comment lines.")] = None,
    overwrite: bool = False,
) -> WrittenInput:
    """Write numeric data as a SCAPS input file with the right format and units.

    Use it for measured absorption (alpha or extinction coefficient k), custom spectra, external optical
    generation profiles, contact filters, grading profiles and measurement files for comparison.
    """
    try:
        if kind == "absorption":
            made = inputs.absorption(name, x, y, quantity or "alpha_per_cm", note, x_unit)
        elif kind == "spectrum":
            made = inputs.spectrum(name, x, y, quantity or "irradiance_w_m2_nm", normalise_w_m2, note)
        elif kind == "generation":
            made = inputs.generation(name, x, y, quantity or "per_cm3_s", note)
        elif kind == "filter":
            made = inputs.optical_filter(name, x, y, note)
        elif kind == "grading":
            made = inputs.grading(name, x, y, quantity or "composition", position, interpolation, shared, note)
        else:
            made = inputs.measurement(
                name,
                quantity or "iv",
                x,
                y,
                extra,
                area_cm2,
                temperature_k,
                bias_v,
                frequency_hz,
                pin_mw_cm2,
                dark,
                note,
            )
        target = check_filename(made.name)
    except ScriptError as error:
        raise ToolError(str(error)) from None
    path = settings().home / made.folder / target
    if path.exists() and not overwrite:
        raise ToolError(f"{made.folder}/{target} already exists; choose another name or set overwrite=true")
    path.write_text(made.text, encoding=ENCODING, errors="replace", newline="")
    return {"kind": kind, "file": target, "folder": made.folder, "points": made.points}


FIT_COLUMNS = {"iv": "jtot(mA/cm2)", "qe": "QE(%)", "cv": "C(nF/cm2)", "cf": "C(nF/cm2)"}


@dataclass
class Target:
    kind: str
    x: list[float]
    y: list[float]
    log: bool
    weight: float
    illumination: str
    temperature_k: float | None


def load_targets(measured: list[dict[str, Any]]) -> list[Target]:
    targets = []
    for item in measured:
        kind = item.get("kind")
        if kind not in FIT_COLUMNS:
            raise ToolError("each measurement needs kind iv, qe, cv or cf")
        if item.get("file"):
            name = find_file("measurements", item["file"])
            text = read_text(settings().home / "measurements" / name)
            table = parse_result(text).table
            if table is None or len(table.columns) < 2:
                raise ToolError(f"{name} has no data columns")
            sx, sy = measurement_scales(text, kind)
            rows = [r for r in table.rows if r[0] is not None and r[1] is not None]
            xs, ys = [r[0] * sx for r in rows], [r[1] * sy for r in rows]
        else:
            xs, ys = item.get("x") or [], item.get("y") or []
        if len(xs) != len(ys) or len(xs) < 3:
            raise ToolError("each measurement needs x and y of equal length, at least 3 points")
        illumination = item.get("illumination", "dark" if kind in ("cv", "cf") else "light")
        if illumination not in ("light", "dark"):
            raise ToolError("illumination must be light or dark")
        targets.append(
            Target(
                kind,
                xs,
                ys,
                bool(item.get("log")),
                float(item.get("weight", 1.0)),
                illumination,
                item.get("temperature_k"),
            )
        )
    return targets


@mcp.tool(title="Fit or optimise a SCAPS device", annotations=hints(read_only=False, idempotent=False))
async def scaps_optimize(
    ctx: Context,
    definition: Definition,
    variables: Annotated[
        dict[str, dict[str, float | bool]],
        Field(
            min_length=1,
            max_length=9,
            description="Set or action keys to vary with bounds, e.g. {'layer1.defect1.Ntotal': {'min': 1e12, "
            "'max': 1e16, 'log': true}, 'layer2.thickness': {'min': 0.03, 'max': 0.12, 'start': 0.05}}.",
        ),
    ],
    goal: Annotated[
        Literal["fit", "maximize", "minimize", "target"],
        Field(description="fit measured curves, maximise or minimise a J-V metric, or reach a target value."),
    ],
    metric: Annotated[
        Literal["eta_pct", "voc_v", "jsc_ma_cm2", "ff_pct"] | None,
        Field(description="Metric for maximize, minimize and target."),
    ] = None,
    target: Annotated[float | None, Field(description="Target value of the metric for goal 'target'.")] = None,
    measured: Annotated[
        list[dict[str, Any]],
        Field(
            description="For fit: curves to match together, e.g. [{'kind': 'iv', 'file': 'Numos Ex 1 light.iv'}, "
            "{'kind': 'iv', 'x': [...], 'y': [...], 'illumination': 'dark', 'log': true, 'weight': 1}]. Files come "
            "from SCAPS measurements/ and are scaled by their area and units headers. Inline data: x in V, Hz or "
            "nm; y in mA/cm2 (SCAPS sign), nF/cm2 or %. Default illumination is light for iv and qe, dark for cv "
            "and cf."
        ),
    ] = [],
    parameters: Parameters = {},
    actions: Actions = {},
    temperature_k: Temperature = None,
    spectrum: SpectrumFile = None,
    max_evaluations: Annotated[int, Field(ge=3, le=400)] = 60,
    wait_seconds: WaitSeconds = 30,
) -> OptimisationResult:
    """Fit device parameters to measured curves, or optimise efficiency, Voc, Jsc or FF, by bounded Nelder-Mead.

    Replaces the GUI-only SCAPS curve fitter and supports several measurements at once (light and dark J-V,
    QE, C-V). Each simplex step is one SCAPS session per measurement condition; long runs continue as a
    job past wait_seconds, so poll with scaps_jobs.
    """
    if goal == "fit":
        if not measured:
            raise ToolError("fit needs measured curves")
        targets = load_targets(measured)
    elif metric is None or (goal == "target" and target is None):
        raise ToolError(f"{goal} needs a metric" + (" and a target value" if goal == "target" else ""))
    else:
        targets = [Target("iv", [], [], False, 1.0, "light", None)]
    try:
        space = [
            Variable(key, float(spec["min"]), float(spec["max"]), spec.get("start"), bool(spec.get("log", False)))
            for key, spec in variables.items()
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise ToolError(f"invalid variable bounds: {error}") from None
    conditions: dict[tuple[str, float | None], list[Target]] = {}
    for t in targets:
        conditions.setdefault((t.illumination, t.temperature_k or temperature_k), []).append(t)
    sims = {}
    device: dict[str, Any] = {}
    for (illumination, temperature), group in conditions.items():
        kinds = sorted({t.kind for t in group})
        own = {k: val for k, val in actions.items() if k.split(".")[0] not in FIT_COLUMNS or k.split(".")[0] in kinds}
        sims[(illumination, temperature)], device = build_simulation(
            definition,
            kinds,
            [],
            parameters,
            own,
            illumination,
            temperature,
            None,
            None,
            spectrum,
            extra_keys=tuple(variables),
        )
    v = validator()
    run_id = new_run_id()
    description = {
        "fit": f"fit {', '.join(t.kind for t in targets)}",
        "target": f"{metric} = {target}",
    }.get(goal, f"{goal} {metric}")
    base: OptimisationResult = {
        "run_id": run_id,
        "status": "queued",
        "progress": None,
        "objective": description,
        "best": {},
        "best_value": None,
        "best_metrics": None,
        "evaluations": 0,
        "history": [],
        "checks": [],
    }

    def curve_error(t: Target, parsed: Result | None) -> float:
        if parsed is None or parsed.table is None or FIT_COLUMNS[t.kind] not in parsed.table.columns:
            return math.inf
        table = parsed.table
        yi = table.columns.index(FIT_COLUMNS[t.kind])
        return fit_error([r[0] for r in table.rows], [r[yi] for r in table.rows], t.x, t.y, t.log)

    def objective(value: float | None) -> float:
        if value is None:
            return math.inf
        return {"maximize": -value, "minimize": value}.get(goal, abs(value - (target or 0.0)))

    async def work(job: Job) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        sessions = 0

        async def evaluate(points: list[list[float]]) -> list[float]:
            nonlocal sessions
            values = [[var.from_unit(u) for var, u in zip(space, point, strict=True)] for point in points]
            series = [(var.key, [vals[i] for vals in values]) for i, var in enumerate(space)]
            totals = [0.0] * len(points)
            weights = sum(t.weight for t in targets)
            iv_metrics: list[Metrics | None] = [None] * len(points)
            for condition, sim in sims.items():
                stem = f"{run_id}_{sessions:03d}"
                sessions += 1
                script, combos, steps = sim.batch_script(v, stem, series, "zip")
                names = [name for step in steps for name in step.values()]
                outcome = await runner().run(script, names, run_id=run_id, timeout=settings().timeout * len(combos))
                for i, step in enumerate(steps):
                    parsed = collect(outcome, step)
                    if condition[0] == "light" and "iv" in parsed:
                        iv_metrics[i] = metrics_of(parsed["iv"])
                    if goal == "fit":
                        for t in conditions[condition]:
                            totals[i] += t.weight * curve_error(t, parsed.get(t.kind)) / weights
            scores = []
            keys = [var.key for var in space]
            for i, vals in enumerate(values):
                combo = dict(zip(keys, vals, strict=True))
                value = totals[i] if goal == "fit" else objective((iv_metrics[i] or {}).get(metric))
                history.append(
                    {"values": combo, "value": value if math.isfinite(value) else None, "metrics": iv_metrics[i]}
                )
                scores.append(value)
            await job.report(len(history), max_evaluations, f"{len(history)}/{max_evaluations} evaluations")
            return scores

        start = [var.to_unit(var.start) if var.start is not None else 0.5 for var in space]
        _, _, count = await nelder_mead(evaluate, start, max_evaluations)
        finite = [h for h in history if h["value"] is not None]
        best = min(finite, key=lambda h: h["value"]) if finite else None
        reported = None if best is None else (-best["value"] if goal == "maximize" else best["value"])
        effective = apply_overrides(device, {**parameters, **(best["values"] if best else {})})
        return {
            **base,
            "status": "completed" if best else "failed",
            "best": best["values"] if best else {},
            "best_value": reported,
            "best_metrics": best["metrics"] if best else None,
            "evaluations": count,
            "history": history[-50:],
            "checks": checks.device_checks(effective),
        }

    snapshot = await submit(ctx, "optimisation", run_id, work, wait_seconds)
    return finish(snapshot, base, lambda r: bool(r["best"]))  # type: ignore[return-value]


@mcp.resource("scaps://definitions/{name}", title="SCAPS definition file", mime_type="text/plain")
def definition_resource(name: str) -> str:
    """Raw contents of a .def file in SCAPS def/."""
    try:
        return read_text(settings().home / "def" / find_file("def", name, ".def"))
    except ToolError as error:
        raise ResourceError(str(error)) from None


@mcp.resource("scaps://runs/{run_id}/{file}", title="SCAPS run file", mime_type="text/plain")
def run_resource(run_id: str, file: str) -> str:
    """Raw contents of a run file: results, input.script, summary.json or the SCAPS error log."""
    try:
        path = run_directory(run_id) / check_filename(file)
    except (ToolError, ScriptError) as error:
        raise ResourceError(str(error)) from None
    if not path.is_file():
        raise ResourceError(f"{file} not found in run {run_id}")
    return read_text(path)


def doctor() -> int:
    try:
        s = load_settings()
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    catalog = Catalog.for_home(s.home, s.exe.stem)
    report = {
        "scaps_version": s.version,
        "executable": s.exe.name,
        "scaps_home": str(s.home),
        "scaps_home_source": s.source,
        "other_installations": [str(p) for p in s.others],
        "native": s.native,
        "wine": s.wine,
        "wine_prefix": str(s.wineprefix) if s.wineprefix else None,
        "wineserver": s.wineserver,
        "xvfb_run": s.xvfb_run,
        "writable": writable(s.home),
        "script_commands": len(catalog.commands) if catalog else 0,
        "plotting": plotting_available(),
    }
    print(json.dumps(report, indent=1))
    if not report["writable"]:
        print("error: the SCAPS folder is not writable; install SCAPS outside protected folders", file=sys.stderr)
        return 1
    return 0


def writable(home: Path) -> bool:
    """Whether the SCAPS working folders accept new files, which every run needs."""
    for folder in ("script", "def", "results"):
        probe = home / folder / ".scaps-mcp-write-test"
        try:
            probe.write_text("")
            probe.unlink()
        except OSError:
            return False
    return True


def main() -> None:
    if sys.argv[1:] == ["doctor"]:
        sys.exit(doctor())
    mcp.run()


if __name__ == "__main__":
    main()
