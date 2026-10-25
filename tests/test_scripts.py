import pytest

from scaps_mcp.scripts import (
    ScriptError,
    Simulation,
    Validator,
    canonical,
    check_filename,
    check_targets,
    edit_script,
    user_script,
)


def lines(script: str) -> list[str]:
    assert script.endswith("\r\n")
    return script.split("\r\n")[:-1]


@pytest.fixture
def v(catalog):
    return Validator(catalog)


def test_simulation_script(v):
    sim = Simulation(
        "my cell",
        ["iv", "qe"],
        profiles=["eb"],
        settings=[("layer1.thickness", 2), ("layer1.defect1.singleacceptor", None)],
        actions=[("workingpoint.voltage", 0.3), ("iv.stopV", 0.8)],
        spectrum="AM1_5G 1 sun.spe",
    )
    script, outputs = sim.script(v, "run")
    assert outputs == {"iv": "run.iv", "qe": "run.qe", "eb": "run.eb"}
    body = lines(script)
    assert body[:5] == [
        "set quitscript.quitSCAPS",
        "set errorhandling.overwritefile",
        "clear all",
        "load definitionfile my cell.def",
        "load spectrumfile AM1_5G 1 sun.spe",
    ]
    assert "set layer1.defect1.singleacceptor" in body
    first = body.index("calculate singleshot")
    assert body[first + 1 : first + 3] == ["save results.iv run.iv", "save results.qe run.qe"]
    second = body.index("calculate singleshot", first + 1)
    assert "action iv.doiv" not in body[first:second]
    assert "action workingpoint.voltage 0.3" in body[first:second]
    assert body[second + 1] == "save results.eb run.eb"


def test_batch_grid_and_zip(v):
    sim = Simulation("cell.def", ["iv"], settings=[("layer1.NA", 1e15)])
    script, points, outputs = sim.batch_script(
        v, "s", [("layer1.NA", [1e15, 1e16]), ("workingpoint.temperature", [280, 300])]
    )
    assert len(points) == 4
    assert outputs[0] == {"iv": "s_000.iv"}
    body = lines(script)
    assert body.count("calculate singleshot") == 4
    assert "set layer1.NA 1e+16" in body
    assert "action workingpoint.temperature 280" in body
    _, zipped, _ = sim.batch_script(v, "s", [("layer1.NA", [1, 2]), ("workingpoint.temperature", [280, 300])], "zip")
    assert len(zipped) == 2
    with pytest.raises(ScriptError):
        sim.batch_script(v, "s", [("layer1.NA", [1, 2]), ("workingpoint.temperature", [280])], "zip")


@pytest.mark.parametrize(
    ("verb", "key", "value"),
    [
        ("set", "layer1.bogus", 1),
        ("set", "layer1.thickness", "thick"),
        ("set", "layer1.defect1.singleacceptor", 3),
        ("set", "layer1.NA", None),
        ("action", "iv.points", 2.5),
        ("set", "layer1.thickness 2\nrunsystem x", 1),
    ],
)
def test_validator_rejects(v, verb, key, value):
    with pytest.raises(ScriptError):
        v.command(verb, key, value)


def test_validator_renders(v):
    assert v.command("set", "layer1.Eg.file", "profile one.grd") == "set layer1.Eg.file profile one.grd"
    assert v.command("set", "leftcontact.workfunction", 5.1) == "set contactleft.workfunction 5.1"
    assert v.command("set", "numerical.Shockley-Queisser_limit.always") == (
        "set numerical.Shockley-Queisser_limit.always"
    )
    assert v.verb_for("intensity.ND") == "action"
    assert v.verb_for("external.Rs") == "set"


def test_simulation_guards():
    with pytest.raises(ScriptError, match="cv or cf"):
        Simulation("c", ["iv"], profiles=["ac"])
    with pytest.raises(ScriptError, match="at most 201"):
        Simulation("c", ["iv"], actions=[("iv.startV", 0), ("iv.stopV", 1), ("iv.increment", 0.001)])
    with pytest.raises(ScriptError, match="not among the measurements"):
        Simulation("c", ["iv"], actions=[("qe.increment", 10)])
    with pytest.raises(ScriptError, match="measurements and illumination"):
        Simulation("c", ["iv"], actions=[("iv.doiv", None)])


@pytest.mark.parametrize("name", ["../x.def", "C:\\x.def", "a/b.def", ".hidden", "x\ny"])
def test_rejects_paths(name):
    with pytest.raises(ScriptError):
        check_filename(name)


def test_canonical_contact_names():
    assert canonical("rightcontact.Sn") == "contactright.Sn"
    assert canonical("contactleft.Sn") == "contactleft.Sn"


DEVICE = {"layers": [{"defects": [{}]}, {"defects": []}], "interfaces": [{"defects": [{}]}]}


def test_batch_reapplies_action_variables_before_profiles(v):
    sim = Simulation("cell.def", ["iv"], profiles=["eb"], actions=[("workingpoint.voltage", 0.2)])
    script, _, outputs = sim.batch_script(v, "s", [("workingpoint.temperature", [280, 320])])
    assert outputs[0] == {"iv": "s_000.iv", "eb": "s_000.eb"}
    body = lines(script)
    profile = body.index("save results.eb s_000.eb")
    cleared = max(i for i in range(profile) if body[i] == "clear actions")
    assert "action workingpoint.temperature 280" in body[cleared:profile]
    assert "action workingpoint.voltage 0.2" in body[cleared:profile]
    assert "action iv.doiv" not in body[cleared:profile]


def test_non_numeric_action_value_is_a_script_error(v):
    sim = Simulation("cell.def", ["iv"], actions=[("iv.points", "many")])
    with pytest.raises(ScriptError):
        sim.validate(v)


@pytest.mark.parametrize(
    "key",
    ["layer1.thickness", "layer1.defect1.Ntotal", "interface1.IFdefect1.Et", "layer3.add_default", "external.Rs"],
)
def test_targets_accepted(key):
    check_targets(DEVICE, [key])


@pytest.mark.parametrize(
    "key", ["layer3.Eg", "layer2.defect1.Ntotal", "interface2.IFdefect1.Et", "interface1.IFdefect2.Et"]
)
def test_targets_rejected(key):
    with pytest.raises(ScriptError):
        check_targets(DEVICE, [key])


@pytest.mark.parametrize(
    "keys",
    [
        ["layer1.defect1.duplicate", "layer1.defect2.gauss"],
        ["layer2.duplicate", "layer3.thickness", "interface2.1_default_IFdefect", "interface2.IFdefect1.Ntotal"],
        ["layer3.add_default", "layer3.Eg", "interface2.2_default_IFdefects", "interface2.IFdefect2.Et"],
        ["layer1.remove", "layer1.Eg"],
        ["layer1.defect.add_default", "layer1.defect2.Et"],
    ],
)
def test_structural_commands_extend_the_targets(keys):
    check_targets(DEVICE, keys)


@pytest.mark.parametrize(
    "keys",
    [
        ["layer1.remove", "layer2.Eg"],
        ["layer2.defect.add_default"],
        ["layer1.defect1.remove", "layer1.defect1.Et"],
        ["layer4.add_default"],
    ],
)
def test_structural_commands_are_checked_too(keys):
    with pytest.raises(ScriptError):
        check_targets(DEVICE, keys)


def test_edit_script(v):
    body = lines(edit_script(v, "base", [("layer1.Eg", 1.5)], "variant"))
    assert body[-2:] == ["set layer1.Eg 1.5", "save settings.definitionfile variant.def"]


def test_user_script(catalog):
    script, outputs, problems, warnings = user_script(
        "load definitionfile a.def // base\nset layer1.bogus 1\ncalculate singleshot\nsave results.iv out.iv\n", catalog
    )
    assert outputs == ["out.iv"]
    assert problems == []
    assert warnings and "layer1.bogus" in warnings[0]
    assert lines(script)[:3] == ["set quitscript.quitSCAPS", "set errorhandling.overwritefile", "clear all"]


@pytest.mark.parametrize(
    "text",
    [
        "runsystem calc.exe",
        "run system notepad",
        "load definitionfile ..\\x.def",
        "save results.iv C:\\out.iv",
        "load\tdefinitionfile ..\\x.def",
        "save\tresults.iv C:\\out.iv",
        "set layer1.Eg.file C:\\profile.grd",
        "runscript ..\\other.script",
        "sett layer1.Eg 1.2",
    ],
)
def test_user_script_blocks(text):
    _, _, problems, _ = user_script(text)
    assert problems


def test_user_script_keeps_run_dll_and_drops_dialogs():
    text = "rundll scapsuserfunction\nshow scriptvariables\nset errorhandling.toscreen\nset quitscript.interactiveSCAPS"
    script, _, problems, warnings = user_script(text)
    assert problems == []
    assert lines(script)[3:] == ["rundll scapsuserfunction"]
    assert len(warnings) == 3
