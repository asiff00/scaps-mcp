from pathlib import Path

import pytest

from scaps_mcp.catalog import Catalog

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="cp1252")


@pytest.fixture
def catalog() -> Catalog:
    return Catalog.from_text(read("scriptdescription9999.txt"), "scriptdescription9999.txt")
