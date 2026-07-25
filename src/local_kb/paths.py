"""Path definitions for a knowledge vault."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VaultPaths:
    """Provide named paths for every top-level vault location."""

    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "00_inbox"

    @property
    def raw(self) -> Path:
        return self.root / "10_raw"

    @property
    def wiki(self) -> Path:
        return self.root / "20_wiki"

    @property
    def answers(self) -> Path:
        return self.root / "30_answers"

    @property
    def index(self) -> Path:
        return self.root / "40_index"

    @property
    def system(self) -> Path:
        return self.root / "80_system"

    @property
    def logs(self) -> Path:
        return self.root / "90_logs"

    @property
    def trash(self) -> Path:
        return self.root / "99_trash"

    @property
    def kb(self) -> Path:
        return self.root / ".kb"

    @property
    def state(self) -> Path:
        return self.kb

    @property
    def queue(self) -> Path:
        return self.kb / "queue"

    @property
    def staging(self) -> Path:
        return self.kb / "staging"

    @property
    def config(self) -> Path:
        return self.system / "config.toml"
