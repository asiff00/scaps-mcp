"""Structured tool results."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from .checks import Finding

RunStatus = Literal["completed", "partial", "failed", "timed_out", "cancelled", "running", "queued"]


class Metrics(TypedDict):
    voc_v: float | None
    jsc_ma_cm2: float | None
    ff_pct: float | None
    eta_pct: float | None
    vmpp_v: float | None
    jmpp_ma_cm2: float | None


class SQLimit(TypedDict):
    band_gap_ev: float
    temperature_k: float
    jsc_ma_cm2: float
    voc_v: float
    ff_pct: float
    eta_pct: float


class Analysis(TypedDict, total=False):
    absorber_layer: int
    sq_limit: SQLimit
    qe_jsc_ma_cm2: float
    qe_range_nm: list[float]
    mott_schottky: dict[str, float]


class Curve(TypedDict):
    kind: str
    file: str
    columns: list[str]
    rows: list[list[float | None]]


class ScapsMessage(TypedDict):
    command: str | None
    message: str


class SimulationResult(TypedDict):
    run_id: str
    status: RunStatus
    progress: str | None
    definition: str
    measurements: list[str]
    profiles: list[str]
    metrics: Metrics | None
    analysis: Analysis
    files: list[str]
    curves: list[Curve]
    scaps_errors: list[ScapsMessage]
    checks: list[Finding]
    seconds: float | None


class BatchPoint(TypedDict):
    values: dict[str, Any]
    metrics: Metrics | None
    files: list[str]


class BatchResult(TypedDict):
    run_id: str
    status: RunStatus
    progress: str | None
    definition: str
    variables: list[str]
    measurements: list[str]
    points: list[BatchPoint]
    scaps_errors: list[ScapsMessage]
    checks: list[Finding]
    seconds: float | None


class ScriptFile(TypedDict):
    file: str
    kind: str | None
    metrics: Metrics | None
    rows: int


class ScriptResult(TypedDict):
    run_id: str
    status: RunStatus
    progress: str | None
    problems: list[str]
    warnings: list[str]
    files: list[ScriptFile]
    scaps_errors: list[ScapsMessage]
    seconds: float | None


class JobInfo(TypedDict):
    job_id: str
    kind: str | None
    status: str
    progress: str | None
    error: str | None


class JobsResult(TypedDict):
    jobs: list[JobInfo]
    result: dict[str, Any] | None


class Table(TypedDict):
    run_id: str
    file: str
    kind: str | None
    title: str | None
    tables: list[str]
    columns: list[str]
    rows: list[list[float | None]]
    total_rows: int
    offset: int
    info: dict[str, str]


class Device(TypedDict):
    definition: str
    units: dict[str, str]
    absorber_layer: int | None
    layers: list[dict[str, Any]]
    interfaces: list[dict[str, Any]]
    contacts: dict[str, dict[str, Any]]
    working_point: dict[str, Any]
    sq_limit_mode: int | None
    checks: list[Finding]


class SavedDefinition(TypedDict):
    definition: str
    run_id: str
    device: Device
    notes: list[str]


class FileList(TypedDict):
    kind: str
    total: int
    files: list[str]


class ScriptCommands(TypedDict):
    query: str
    source: str | None
    total: int
    commands: list[str]


class Limits(TypedDict):
    spectrum: str
    limits: list[SQLimit]


class Status(TypedDict):
    scaps_version: str
    executable: str
    scaps_home: str
    scaps_home_source: str
    other_installations: list[str]
    wine_prefix: str | None
    runs_dir: str
    timeout_s: float
    busy: bool
    script_commands: int
    plotting: bool
    active_jobs: list[str]


class ImportResult(TypedDict):
    kind: str
    file: str
    missing_references: list[str]
    summary: dict[str, Any]


class WrittenInput(TypedDict):
    kind: str
    file: str
    folder: str
    points: int


class OptimisationResult(TypedDict):
    run_id: str
    status: RunStatus
    progress: str | None
    objective: str
    best: dict[str, float]
    best_value: float | None
    best_metrics: Metrics | None
    evaluations: int
    history: list[dict[str, Any]]
    checks: list[Finding]
