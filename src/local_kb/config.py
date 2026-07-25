"""Configuration loading for a knowledge vault."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    vault: Path
    compiler: str = "claude"
    poll_seconds: float = 2.0
    stable_seconds: float = 5.0
    max_retries: int = 3

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load vault defaults from its config file."""
        with path.open("rb") as config_file:
            values = tomllib.load(config_file)

        watcher = values.get("watcher", {})
        queue = values.get("queue", {})
        return cls(
            vault=path.parent.parent.resolve(),
            compiler=values.get("compiler", "claude"),
            poll_seconds=watcher.get("poll_seconds", 2.0),
            stable_seconds=watcher.get("stable_seconds", 5.0),
            max_retries=queue.get("max_retries", 3),
        )
