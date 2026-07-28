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
    def corrections(self) -> Path:
        return self.root / "50_corrections"

    @property
    def correction_records(self) -> Path:
        return self.corrections / "records"

    @property
    def correction_timeline(self) -> Path:
        return self.corrections / "timeline"

    @property
    def correction_index(self) -> Path:
        return self.index / "corrections.sqlite3"

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
    def runtime(self) -> Path:
        return self.root / ".kb"

    @property
    def queue(self) -> Path:
        return self.runtime / "queue"

    @property
    def staging(self) -> Path:
        return self.runtime / "staging"

    @property
    def config(self) -> Path:
        return self.system / "config.toml"
