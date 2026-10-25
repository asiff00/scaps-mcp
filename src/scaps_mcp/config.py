"""Runtime configuration, read from environment variables, with SCAPS found automatically when SCAPS_HOME is unset."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_EXE = re.compile(r"^scaps(\d+)\.exe$", re.IGNORECASE)
_FOLDERS = ("def", "results", "script")
_WINESERVER_CANDIDATES = (
    "/usr/lib/wine/wineserver",
    "/usr/lib/wine/wineserver64",
    "/usr/lib/wine/wineserver32",
    "/usr/lib/i386-linux-gnu/wine/wineserver",
    "/usr/lib/x86_64-linux-gnu/wine/wineserver",
    "/opt/wine-stable/bin/wineserver",
    "/usr/local/bin/wineserver",
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    home: Path
    exe: Path
    timeout: float
    runs_dir: Path
    native: bool
    wine: str | None = None
    wineprefix: Path | None = None
    wineserver: str | None = None
    xvfb_run: str | None = None
    source: str = "SCAPS_HOME"
    others: tuple[Path, ...] = ()

    @property
    def version(self) -> str:
        digits = _EXE.match(self.exe.name).group(1)
        return ".".join([digits[0], digits[1], digits[2:]]) if len(digits) >= 3 else digits


def _exe_number(exe: Path) -> int:
    return int(_EXE.match(exe.name).group(1))


def _find_exe(home: Path, name: str | None) -> Path:
    if name:
        exe = home / name
        if not exe.is_file():
            raise ConfigError(f"SCAPS_EXE {name!r} not found in {home}")
        return exe
    candidates = [p for p in home.iterdir() if _EXE.match(p.name)]
    if not candidates:
        raise ConfigError(f"no scaps*.exe found in {home}")
    return max(candidates, key=_exe_number)


def _find_wineprefix(home: Path) -> Path | None:
    for parent in home.parents:
        if parent.name == "drive_c":
            return parent.parent
    return None


def _find_wineserver() -> str | None:
    found = shutil.which("wineserver")
    if found:
        return found
    return next((c for c in _WINESERVER_CANDIDATES if os.access(c, os.X_OK)), None)


def _folders(root: Path, keep: Callable[[str], bool]) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and keep(p.name))
    except OSError:
        return []


def _scaps_name(name: str) -> bool:
    return "scaps" in name.lower()


def _installation(folder: Path) -> Path | None:
    """The newest scaps*.exe of a folder laid out as a SCAPS installation."""
    try:
        exes = [p for p in folder.iterdir() if _EXE.match(p.name) and p.is_file()]
    except OSError:
        return None
    if not exes or not all((folder / sub).is_dir() for sub in _FOLDERS):
        return None
    return max(exes, key=_exe_number)


def search_roots(env: Mapping[str, str], platform: str = sys.platform) -> list[Path]:
    """Folders whose SCAPS-named subfolders are searched when SCAPS_HOME is unset."""
    user = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if platform == "win32":
        roots = [Path((env.get("SystemDrive") or "C:") + "\\")]
        roots += [user, user / "Documents", user / "Desktop", user / "Downloads"]
        roots += [Path(env[key]) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if env.get(key)]
        return roots
    prefixes = [Path(env[key]).expanduser() for key in ("SCAPS_WINEPREFIX", "WINEPREFIX") if env.get(key)]
    prefixes.append(user / ".wine")
    for folder in _folders(user, _scaps_name):
        prefixes += [folder, *_folders(folder, lambda _: True)]
    prefixes += _folders(user / ".local" / "share" / "wineprefixes", lambda _: True)
    roots = [user]
    for prefix in prefixes:
        drive = prefix / "drive_c"
        if drive.is_dir():
            roots += [drive, drive / "Program Files", drive / "Program Files (x86)"]
    return roots


def discover(env: Mapping[str, str] | None = None, platform: str = sys.platform) -> list[Path]:
    """SCAPS installations in the usual locations, newest version first."""
    env = os.environ if env is None else env
    found: dict[Path, int] = {}
    for root in search_roots(env, platform):
        for child in _folders(root, _scaps_name):
            for folder in (child, *_folders(child, _scaps_name)):
                exe = _installation(folder)
                if exe is not None:
                    found.setdefault(folder.resolve(), _exe_number(exe))
    return sorted(found, key=lambda folder: -found[folder])


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = {k: v for k, v in (os.environ if env is None else env).items() if not v.startswith("${")}
    raw_home = env.get("SCAPS_HOME")
    source, others = "SCAPS_HOME", ()
    if raw_home:
        home = Path(raw_home).expanduser().resolve()
    else:
        found = discover(env)
        if not found:
            raise ConfigError(
                "no SCAPS installation found in the usual locations; set SCAPS_HOME to the folder that "
                "contains scapsXXXX.exe"
            )
        home, others, source = found[0], tuple(found[1:]), "discovered"
    if not home.is_dir():
        raise ConfigError(f"SCAPS_HOME {home} is not a directory")
    for sub in _FOLDERS:
        if not (home / sub).is_dir():
            raise ConfigError(f"SCAPS_HOME {home} has no {sub}/ folder")

    try:
        timeout = float(env.get("SCAPS_TIMEOUT") or 300)
    except ValueError:
        raise ConfigError("SCAPS_TIMEOUT must be a number of seconds") from None
    if timeout <= 0:
        raise ConfigError("SCAPS_TIMEOUT must be positive")
    cache = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    settings = dict(
        home=home,
        exe=_find_exe(home, env.get("SCAPS_EXE")),
        timeout=timeout,
        runs_dir=Path(env.get("SCAPS_RUNS_DIR") or cache / "scaps" / "runs").expanduser(),
        native=sys.platform == "win32",
        source=source,
        others=others,
    )
    if settings["native"]:
        return Settings(**settings)

    wine = shutil.which(env.get("SCAPS_WINE", "wine"))
    if not wine:
        raise ConfigError("wine not found; install it or set SCAPS_WINE")
    explicit = env.get("SCAPS_WINEPREFIX")
    inferred = _find_wineprefix(home)
    fallback = env.get("WINEPREFIX")
    wineprefix = (
        Path(explicit).expanduser() if explicit else inferred or (Path(fallback).expanduser() if fallback else None)
    )
    if wineprefix is None:
        raise ConfigError("cannot infer the Wine prefix from the SCAPS folder; set SCAPS_WINEPREFIX")
    use_xvfb = env.get("SCAPS_XVFB", "1") != "0"
    xvfb_run = shutil.which("xvfb-run") if use_xvfb else None
    if use_xvfb and not xvfb_run:
        raise ConfigError("xvfb-run not found; install xvfb or set SCAPS_XVFB=0 to use the current DISPLAY")
    return Settings(
        **settings,
        wine=wine,
        wineprefix=wineprefix,
        wineserver=env.get("SCAPS_WINESERVER") or _find_wineserver(),
        xvfb_run=xvfb_run,
    )
