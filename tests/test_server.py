"""Server tests against a fake SCAPS folder; tools that start SCAPS are covered by test_integration."""

import shutil

import anyio
import pytest
from conftest import FIXTURES, read
from mcp.client import Client

from scaps_mcp import server
from scaps_mcp.catalog import Catalog
from scaps_mcp.config import Settings
from scaps_mcp.runner import Runner

TOOLS = {
    "scaps_status",
    "scaps_list_files",
    "scaps_script_reference",
    "scaps_describe_device",
    "scaps_edit_device",
    "scaps_create_device",
    "scaps_import_file",
    "scaps_write_data",
    "scaps_simulate",
    "scaps_batch",
    "scaps_optimize",
    "scaps_jobs",
    "scaps_results",
    "scaps_plot",
    "scaps_run_script",
    "scaps_sq_limit",
}
RUN_ID = "20260101-000000-abcdef"


@pytest.fixture(autouse=True)
def installation(tmp_path, monkeypatch):
    home = tmp_path / "Scaps9999"
    for folder in ("def", "results", "script", "spectrum", "materials", "absorption", "measurements", "grading"):
        (home / folder).mkdir(parents=True)
    (home / "scaps9999.exe").touch()
    shutil.copy(FIXTURES / "scriptdescription9999.txt", home)
    shutil.copy(FIXTURES / "seeded.def", home / "def")
    shutil.copy(FIXTURES / "test.material", home / "materials")
    bins = "\n".join(f"{lam}\t10.0" for lam in range(300, 1300, 10))
    (home / "spectrum" / "AM1_5G 1 sun.spe").write_text(f"> flat test spectrum\n{bins}\n")
    runs = tmp_path / "runs"
    (runs / RUN_ID).mkdir(parents=True)
    shutil.copy(FIXTURES / "synthetic.iv", runs / RUN_ID / "a.iv")
    shutil.copy(FIXTURES / "seeded.eb", runs / RUN_ID / "b.eb")
    settings = Settings(home=home, exe=home / "scaps9999.exe", timeout=60, runs_dir=runs, native=True)
    monkeypatch.setattr(server, "STATE", server.State(settings, Runner(settings), Catalog.for_home(home, "scaps9999")))
    return home


def call(tool: str, arguments: dict | None = None):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(run)


def test_tool_surface():
    async def run():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools

    tools = anyio.run(run)
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.title and tool.description
        hints = tool.annotations
        assert None not in (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint, hints.open_world_hint)
        assert hints.open_world_hint is False
        if tool.name != "scaps_plot":
            assert tool.output_schema


def test_instructions_stay_short():
    assert len(server.INSTRUCTIONS) < 2500


def test_describe_device():
    data = call("scaps_describe_device", {"definition": "SEEDED"}).structured_content
    assert data["definition"] == "seeded.def"
    assert data["absorber_layer"] == 1
    assert len(data["layers"][0]["defects"]) == 2
    assert data["interfaces"][0]["band_offsets"] == {"delta_ec_ev": 0.0, "delta_ev_ev": -1.2}
    assert any(c["code"] == "no_radiative_recombination" for c in data["checks"])


def test_missing_definition_is_a_tool_error():
    result = call("scaps_describe_device", {"definition": "nope"})
    assert result.is_error
    assert "scaps_list_files" in result.content[0].text


def test_list_files_and_reference():
    files = call("scaps_list_files", {"kind": "materials"}).structured_content
    assert files["files"] == ["test.material"]
    reference = call("scaps_script_reference", {"query": "contact"}).structured_content
    assert "set contact.workfunction <number>" in reference["commands"]


def test_invalid_override_is_rejected_before_running():
    result = call("scaps_simulate", {"definition": "seeded", "parameters": {"layer9.Eg": 1.2}})
    assert result.is_error and "the device has 2 layers" in result.content[0].text
    result = call("scaps_simulate", {"definition": "seeded", "parameters": {"layer1.bogus": 1}})
    assert result.is_error and "scaps_script_reference" in result.content[0].text


def test_import_file(installation):
    data = call(
        "scaps_import_file", {"kind": "definition", "name": "shared cell.def", "content": read("synthetic.def")}
    ).structured_content
    assert data["summary"]["layers"] == ["absorber", "window"]
    assert (installation / "def" / "shared cell.def").exists()
    again = call(
        "scaps_import_file", {"kind": "definition", "name": "shared cell.def", "content": read("synthetic.def")}
    )
    assert again.is_error and "overwrite" in again.content[0].text
    material = call(
        "scaps_import_file", {"kind": "material", "name": "copy.material", "content": read("test.material")}
    ).structured_content
    assert material["missing_references"] == ["absorption/test film.abs"]
    assert call("scaps_import_file", {"kind": "absorption", "name": "x.abs", "content": "no data"}).is_error
    nested = call("scaps_import_file", {"kind": "script", "name": "nested.script", "content": "runsystem calc\n"})
    assert nested.is_error and "run script" in nested.content[0].text
    assert not (installation / "script" / "nested.script").exists()


def test_write_data(installation):
    data = call(
        "scaps_write_data",
        {"kind": "absorption", "name": "film", "x": [400, 500, 600], "y": [1e5, 5e4, 1e4], "note": "test"},
    ).structured_content
    assert data == {"kind": "absorption", "file": "film.abs", "folder": "absorption", "points": 3}
    assert (installation / "absorption" / "film.abs").exists()


def test_sq_limit_tool():
    data = call("scaps_sq_limit", {"band_gaps_ev": [1.1, 1.5]}).structured_content
    first, second = data["limits"]
    assert first["jsc_ma_cm2"] > second["jsc_ma_cm2"]
    assert second["voc_v"] > first["voc_v"]
    beyond = call("scaps_sq_limit", {"band_gaps_ev": [4.5]})
    assert beyond.is_error and "no photons" in beyond.content[0].text


def test_results_and_plot():
    listing = call("scaps_results", {"run_id": RUN_ID}).structured_content
    assert listing["tables"] == ["a.iv", "b.eb"]
    table = call("scaps_results", {"run_id": RUN_ID, "file": "b.eb", "columns": ["x(um)", "Ec(eV)"], "step": 50})
    data = table.structured_content
    assert data["kind"] == "eb" and data["tables"] == ["bulk", "interface"]
    assert data["columns"] == ["x(um)", "Ec(eV)"]
    plot = call("scaps_plot", {"run_id": RUN_ID, "files": ["a.iv"]})
    assert not plot.is_error
    assert plot.content[0].type == "image"
    assert call("scaps_results", {"run_id": "../etc"}).is_error


def test_script_validation_only():
    data = call(
        "scaps_run_script", {"script": "load definitionfile seeded.def\nrunsystem calc", "validate_only": True}
    ).structured_content
    assert data["status"] == "failed"
    assert any("run script" in p for p in data["problems"])


def test_doctor_reports_writable_installation(installation, monkeypatch, capsys):
    monkeypatch.setattr(server, "load_settings", lambda: server.STATE.settings)
    assert server.doctor() == 0
    assert '"writable": true' in capsys.readouterr().out


def test_interrupted_job_is_persisted_as_cancelled(tmp_path):
    from scaps_mcp.jobs import Jobs

    jobs = Jobs(lambda: tmp_path)

    async def forever(job):
        await anyio.sleep_forever()

    async def run():
        async with anyio.create_task_group() as tasks:
            jobs.tasks = tasks
            await jobs.submit("20260101-000000-abcdef", "batch", forever, 0.05, None)
            tasks.cancel_scope.cancel()

    anyio.run(run)
    assert jobs.get("20260101-000000-abcdef")["status"] == "cancelled"
