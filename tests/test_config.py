import pytest

from scaps_mcp.config import ConfigError, discover, load_settings


def install(folder, *versions):
    for sub in ("def", "results", "script"):
        (folder / sub).mkdir(parents=True)
    for version in versions:
        (folder / f"scaps{version}.exe").touch()
    return folder


@pytest.fixture
def home(tmp_path):
    return install(tmp_path / "prefix" / "drive_c" / "Scaps3312", 3311, 3312)


@pytest.fixture
def wine(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def test_detects_newest_exe_and_prefix(home, wine):
    settings = load_settings({"SCAPS_HOME": str(home)})
    assert settings.exe.name == "scaps3312.exe"
    assert settings.version == "3.3.12"
    assert settings.wineprefix == home.parent.parent
    assert settings.timeout == 300
    assert settings.source == "SCAPS_HOME"


@pytest.mark.parametrize("value", [None, "", "${SCAPS_HOME}"])
def test_reports_when_nothing_is_found(tmp_path, value):
    env = {"HOME": str(tmp_path)} | ({} if value is None else {"SCAPS_HOME": value})
    with pytest.raises(ConfigError, match="no SCAPS installation found"):
        load_settings(env)


def test_rejects_folder_without_scaps_layout(tmp_path):
    with pytest.raises(ConfigError, match="no def/ folder"):
        load_settings({"SCAPS_HOME": str(tmp_path)})


@pytest.mark.parametrize("value", ["soon", "0", "-5"])
def test_rejects_bad_timeout(home, wine, value):
    with pytest.raises(ConfigError, match="SCAPS_TIMEOUT"):
        load_settings({"SCAPS_HOME": str(home), "SCAPS_TIMEOUT": value})


def test_discovers_newest_installation_in_wine_prefixes(tmp_path, wine):
    newest = install(tmp_path / "scaps" / "prefix" / "drive_c" / "Program Files" / "Scaps3312", 3312)
    older = install(tmp_path / ".wine" / "drive_c" / "Program Files (x86)" / "SCAPS" / "Scaps3311", 3311)
    install(tmp_path / ".wine" / "drive_c" / "Program Files" / "Other", 3310)
    (tmp_path / "scaps-notes").mkdir()
    settings = load_settings({"HOME": str(tmp_path), "WINEPREFIX": str(tmp_path / "elsewhere")})
    assert settings.home == newest.resolve()
    assert settings.others == (older.resolve(),)
    assert settings.source == "discovered"
    assert settings.wineprefix == tmp_path / "scaps" / "prefix"


def test_explicit_home_wins_over_discovery(tmp_path, wine):
    install(tmp_path / ".wine" / "drive_c" / "Scaps3312", 3312)
    chosen = install(tmp_path / "custom" / "drive_c" / "old", 3311)
    settings = load_settings({"HOME": str(tmp_path), "SCAPS_HOME": str(chosen)})
    assert settings.home == chosen.resolve()
    assert settings.source == "SCAPS_HOME"
    assert settings.others == ()


def test_discovers_windows_locations(tmp_path):
    user, programs = tmp_path / "user", tmp_path / "Program Files (x86)"
    install(programs / "Scaps3311", 3311)
    install(user / "Documents" / "SCAPS 3.3.12" / "Scaps3312", 3312)
    install(user / "Downloads" / "scaps-installer", 3312)
    (user / "Downloads" / "scaps-installer" / "results").rmdir()
    env = {"USERPROFILE": str(user), "ProgramFiles(x86)": str(programs), "SystemDrive": str(tmp_path / "none")}
    found = discover(env, platform="win32")
    assert found == [
        (user / "Documents" / "SCAPS 3.3.12" / "Scaps3312").resolve(),
        (programs / "Scaps3311").resolve(),
    ]
