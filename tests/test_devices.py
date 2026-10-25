import pytest
from conftest import read

from scaps_mcp import devices
from scaps_mcp.parsing import parse_definition
from scaps_mcp.scripts import ScriptError, Validator


def test_seed_defects_are_inserted_into_the_right_layers():
    text = read("synthetic.def").replace("srhrecombination", "removed").replace("interface recombination", "removed")
    device = parse_definition(devices.insert_seed_defects(text, [2]))
    assert [len(layer["defects"]) for layer in device["layers"]] == [0, 1]
    assert device["layers"][1]["defects"][0]["Ntotal"] == pytest.approx(1e14)


def test_layer_and_defect_scripts(catalog):
    v = Validator(catalog)
    layers = [{"name": "absorber", "thickness": 1.0, "defects": [{"Ntotal": 1e14}, {"singleacceptor": None}]}]
    first = devices.layer_script(v, layers, {"left": {"workfunction": 5.1}}, [], "new")
    assert "set layer1.add_default" in first
    assert first.index("set contactleft.flatband.off") < first.index("set contactleft.workfunction 5.1")
    second = devices.defect_script(v, "new", layers, {})
    assert "set layer1.defect1.duplicate" in second
    assert "set layer1.defect2.singleacceptor" in second


def test_limits():
    with pytest.raises(ScriptError):
        devices.validate([{}] * 8, {})
    with pytest.raises(ScriptError):
        devices.validate([{}, {}], {2: []})
    with pytest.raises(ScriptError):
        devices.validate([{}, {}], {1: [{}] * 4})


def test_set_numerics():
    text = devices.set_numerics(read("synthetic.def"), {"maxiter": 2500, "clamp_psi": 0.2})
    assert "maxiter : 2500" in text
    assert "clamp_psi : 0.2 units kT" in text
    with pytest.raises(ScriptError, match="unknown numerical settings"):
        devices.set_numerics(text, {"bogus": 1})


def test_interface_cross_sections_written_to_both_sides():
    requested = devices.interface_cross_sections(
        [("interface1.IFdefect1.capture_cross_section.electron", 1e-15), ("layer1.Eg", 1.2)]
    )
    assert requested == {(1, 1): {"electron": 1e-15}}
    text = devices.set_interface_cross_sections(read("synthetic.def"), requested)
    defect = parse_definition(text)["interfaces"][0]["defects"][0]
    assert defect["sigma_n_left"] == pytest.approx(1e-15)
    assert defect["sigma_n_right"] == pytest.approx(1e-15)
    assert defect["sigma_p_left"] == pytest.approx(1e-15)
