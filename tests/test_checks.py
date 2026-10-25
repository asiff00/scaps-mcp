from scaps_mcp.checks import absorber_index, device_checks, result_checks, sweep_checks
from scaps_mcp.physics import Limit


def device(**absorber):
    layer = {"name": "absorber", "thickness": 1.0, "Eg": 1.5, "chi": 4.0, "NA": 1e16, "defects": [], **absorber}
    window = {"name": "window", "thickness": 0.05, "Eg": 3.0, "chi": 4.1, "ND": 1e18, "defects": []}
    return {
        "layers": [layer, window],
        "interfaces": [{"index": 1, "layers": [1, 2], "defects": []}],
        "contacts": {"left": {"flatband": 1}, "right": {"flatband": 0}},
    }


def codes(found):
    return {f["code"] for f in found}


def test_absorber_is_thickest_layer():
    assert absorber_index(device()) == 1


def test_device_checks_flag_ideal_device():
    found = codes(device_checks(device()))
    assert {"no_radiative_recombination", "absorber_without_defects", "ideal_interface", "flatband_contact"} <= found


def test_realistic_absorber_passes():
    good = device(radiative=1e-10, defects=[{"index": 1, "type": "neutral", "Ntotal": 1e14}])
    found = codes(device_checks(good))
    assert "no_radiative_recombination" not in found
    assert "low_absorber_defects" not in found


def test_band_spike_warning():
    spiky = device()
    spiky["layers"][1]["chi"] = 3.5
    assert "band_spike" in codes(device_checks(spiky))


def test_result_beyond_sq_limit_is_an_error():
    limit = Limit(1.5, 300, 28.8, 1.23, 90.0, 31.8)
    metrics = {"eta_pct": 35.0, "voc_v": 1.3, "jsc_ma_cm2": 28.0, "ff_pct": 88.0}
    found = result_checks(metrics, limit, None)
    assert {f["level"] for f in found if f["code"].startswith("above_sq")} == {"error"}
    assert "voc_deficit" in codes(found)
    assert result_checks({"eta_pct": 20.0, "voc_v": 1.0, "jsc_ma_cm2": 25.0, "ff_pct": 80.0}, limit, 300) == []


def test_efficiency_rising_with_defects():
    points = [{"value": n, "metrics": {"eta_pct": e}} for n, e in ((1e13, 10), (1e14, 11), (1e15, 12))]
    assert codes(sweep_checks("layer1.defect1.Ntotal", points)) == {"efficiency_rises_with_defects"}
    assert sweep_checks("layer1.thickness", points) == []
    assert sweep_checks("layer1.defect1.Ntotal", [*points[:2], {"value": 1e16, "metrics": None}]) == []


def test_implausible_density_of_states():
    tiny = device(NC=1e7)
    found = [f for f in device_checks(tiny) if f["code"] == "implausible_density_of_states"]
    assert [f["level"] for f in found] == ["error"]
    assert "NC = 1e+07" in found[0]["message"]
    low = device(NV=5e15)
    assert [f["level"] for f in device_checks(low) if f["code"] == "implausible_density_of_states"] == ["warning"]
    assert "implausible_density_of_states" not in codes(device_checks(device(NC=2.2e18, NV=1.8e19)))
