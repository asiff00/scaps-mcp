"""Script command catalogue, read from the scriptdescription file shipped with each SCAPS version."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_INDEXED = re.compile(r"^(layer|defect|interface|IFdefect|metadefect)\d+$", re.IGNORECASE)
_SIDED = re.compile(r"^(?:(left|right)contact|contact(left|right))$", re.IGNORECASE)
_AB = re.compile(r"^(absorptionmodel|absorption)[AB]$", re.IGNORECASE)
NUMERIC = {"NUMBER", "INTEGER", "SCALAR", "SCRIPT_SCALAR"}


@dataclass(frozen=True)
class Command:
    verb: str
    path: tuple[str, ...]
    value_type: str

    @property
    def key(self) -> str:
        return ".".join(self.path)

    def __str__(self) -> str:
        return f"{self.verb} {self.key} <{self.value_type.lower()}>"


def normalise(key: str) -> tuple[str, ...]:
    parts = []
    for part in key.split("."):
        part = _INDEXED.sub(r"\1", part)
        part = _SIDED.sub("contact", part)
        part = _AB.sub(r"\1", part)
        parts.append(part.lower())
    return tuple(parts)


class Catalog:
    def __init__(self, commands: list[Command], source: str | None = None):
        self.commands = commands
        self.source = source
        self._index = {(c.verb.lower(), tuple(p.lower() for p in c.path)): c for c in commands}

    @classmethod
    def from_text(cls, text: str, source: str | None = None) -> Catalog:
        commands = []
        for line in text.splitlines():
            if line.startswith("//"):
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 6 or not parts[0] or not parts[5].startswith("VALUE_TYPE_"):
                continue
            path = tuple(p for p in parts[1:5] if p)
            commands.append(Command(parts[0], path, parts[5].removeprefix("VALUE_TYPE_")))
        return cls(commands, source)

    @classmethod
    def for_home(cls, home: Path, exe_stem: str) -> Catalog | None:
        files = sorted(home.glob("scriptdescription*.txt"))
        if not files:
            return None
        digits = exe_stem.lower().removeprefix("scaps")
        source = next((f for f in files if f.stem.endswith(digits)), files[-1])
        return cls.from_text(source.read_text(encoding="cp1252", errors="replace"), source.name)

    def lookup(self, verb: str, key: str) -> Command | None:
        return self._index.get((verb.lower(), normalise(key)))

    def search(self, words: list[str]) -> list[str]:
        words = [w.lower() for w in words]
        return [str(c) for c in self.commands if all(w in str(c).lower() for w in words)]
