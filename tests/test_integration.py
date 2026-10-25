"""End-to-end tests against a local SCAPS installation, found automatically or given by SCAPS_HOME."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from scaps_mcp.config import ConfigError, load_settings


def scaps_home() -> Path | None:
    try:
        return load_settings().home
    except ConfigError:
        return None


HOME = scaps_home()
pytestmark = [pytest.mark.scaps, pytest.mark.skipif(HOME is None, reason="no SCAPS installation found")]

DEFINITION = "example CIGS.def"


CREATED = ("integration test cell.def", "integration test cell edited.def")


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAPS_RUNS_DIR", str(tmp_path))
    from scaps_mcp import server

    monkeypatch.setattr(server, "STATE", server.State())
    yield
    for name in CREATED:
        (HOME / "def" / name).unlink(missing_ok=True)


def call(tool: str, arguments: dict):
    from scaps_mcp.server import mcp

    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(run)


def ok(tool: str, arguments: dict) -> dict:
    result = call(tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def test_simulate_with_profiles_and_analysis():
    data = ok(
        "scaps_simulate",
        {"definition": DEFINITION, "measurements": ["iv", "cv"], "profiles": ["eb"], "voltage_v": 0.2},
    )
    assert data["status"] == "completed"
    assert 0.3 < data["metrics"]["voc_v"] < data["analysis"]["sq_limit"]["voc_v"]
    assert data["analysis"]["mott_schottky"]["doping_cm3"] > 0
    table = ok("scaps_results", {"run_id": data["run_id"], "file": f"{data['run_id']}.eb", "max_rows": 3})
    assert table["title"] == "bulk"


def test_batch_over_temperature_and_thickness():
    data = ok(
        "scaps_batch",
        {
            "definition": DEFINITION,
            "variables": {"workingpoint.temperature": [280, 320], "layer1.thickness": [1, 2]},
            "mode": "zip",
            "profiles": ["eb"],
        },
    )
    voc = [p["metrics"]["voc_v"] for p in data["points"]]
    assert voc[0] > voc[1]
    assert all(any(f.endswith(".eb") for f in p["files"]) for p in data["points"])


def test_create_edit_and_simulate_device():
    created = ok(
        "scaps_create_device",
        {
            "name": "integration test cell",
            "overwrite": True,
            "layers": [
                {
                    "name": "absorber",
                    "thickness": 2,
                    "Eg": 1.2,
                    "NA": 1e16,
                    "radiative": 1e-10,
                    "defects": [{"singledonor": None, "Ntotal": 1e14}, {"Ntotal": 1e13}],
                },
                {"name": "window", "thickness": 0.1, "Eg": 2.4, "ND": 1e18},
            ],
            "interfaces": {1: [{"Ntotal": 1e10}]},
            "left_contact": {"workfunction": 5.2},
        },
    )
    device = created["device"]
    assert [len(layer["defects"]) for layer in device["layers"]] == [2, 0]
    assert device["contacts"]["left"]["workfunction"] == pytest.approx(5.2)
    edited = ok(
        "scaps_edit_device",
        {
            "base": created["definition"],
            "save_as": "integration test cell edited",
            "operations": {"layer1.thickness": 1.5},
            "numerics": {"maxiter": 2000},
            "overwrite": True,
        },
    )
    assert edited["device"]["layers"][0]["thickness"] == pytest.approx(1.5)
    assert ok("scaps_simulate", {"definition": edited["definition"]})["metrics"]["eta_pct"] > 0


def test_background_job_and_cancel():
    started = ok(
        "scaps_batch",
        {"definition": DEFINITION, "variables": {"layer1.thickness": [0.5, 1, 1.5, 2, 2.5, 3]}, "wait_seconds": 0},
    )
    assert started["status"] in ("queued", "running")
    cancelled = ok("scaps_jobs", {"action": "cancel", "job_id": started["run_id"]})
    assert cancelled["jobs"][0]["job_id"] == started["run_id"]


def test_fit_recovers_defect_density():
    truth = ok(
        "scaps_simulate",
        {"definition": DEFINITION, "parameters": {"layer1.defect1.Ntotal": 3e14}, "response_format": "detailed"},
    )
    rows = truth["curves"][0]["rows"]
    fitted = ok(
        "scaps_optimize",
        {
            "definition": DEFINITION,
            "goal": "fit",
            "measured": [{"kind": "iv", "x": [r[0] for r in rows], "y": [r[1] for r in rows]}],
            "variables": {"layer1.defect1.Ntotal": {"min": 1e13, "max": 1e16, "log": True}},
            "max_evaluations": 16,
            "wait_seconds": 1800,
        },
    )
    assert fitted["best"]["layer1.defect1.Ntotal"] == pytest.approx(3e14, rel=0.1)


def scaps_processes() -> int:
    listing = subprocess.run(["pgrep", "-fc", "^scaps[0-9]+\\.exe"], capture_output=True, text=True)
    return int(listing.stdout.strip() or 0)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_stops_the_running_scaps_process(tmp_path):
    env = {**os.environ, "SCAPS_RUNS_DIR": str(tmp_path)}
    server = subprocess.Popen(
        [sys.executable, "-m", "scaps_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    def send(message: dict) -> None:
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        assert json.loads(server.stdout.readline())["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "scaps_batch",
                    "arguments": {
                        "definition": DEFINITION,
                        "variables": {"layer1.thickness": [0.5, 1.0, 1.5]},
                        "wait_seconds": 0,
                    },
                },
            }
        )
        started = json.loads(server.stdout.readline())["result"]["structuredContent"]
        time.sleep(6)
        assert scaps_processes() == 1
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=60) == 0
    finally:
        if server.poll() is None:
            server.kill()
    assert scaps_processes() == 0
    assert json.loads((tmp_path / started["run_id"] / "summary.json").read_text())["status"] == "cancelled"
