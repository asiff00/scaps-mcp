import pytest

from scaps_mcp.catalog import normalise


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("layer2.defect1.Ntotal", ("layer", "defect", "ntotal")),
        ("interface1.IFdefect3.Ntotal", ("interface", "ifdefect", "ntotal")),
        ("contactleft.workfunction", ("contact", "workfunction")),
        ("rightcontact.workfunction", ("contact", "workfunction")),
        ("layer1.absorptionmodelA.power1.alpha1", ("layer", "absorptionmodel", "power1", "alpha1")),
    ],
)
def test_normalise(key, expected):
    assert normalise(key) == expected


def test_lookup(catalog):
    assert catalog.lookup("set", "layer3.defect2.Ntotal").value_type == "NUMBER"
    assert catalog.lookup("action", "cf.points.per.decade").value_type == "INTEGER"
    assert catalog.lookup("set", "layer1.Eg.file").value_type == "FILENAME"
    assert catalog.lookup("set", "layer1.bogus") is None
    assert catalog.lookup("action", "layer1.thickness") is None


def test_search(catalog):
    found = catalog.search(["defect", "ntotal"])
    assert found == ["set layer.defect.Ntotal <number>", "set interface.IFdefect.Ntotal <number>"]
