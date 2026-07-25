"""Configuration loading for a knowledge vault."""

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


def _required(values: dict, section: str, key: str):
    field = f"{section}.{key}"
    table = values.get(section)
    if not isinstance(table, dict) or key not in table:
        raise ValueError(f"{field} is required")
    return table[key]


def _number(values: dict, section: str, key: str) -> float:
    field = f"{section}.{key}"
    value = _required(values, section, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


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

        provider = _required(values, "compiler", "provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("compiler.provider must be a non-empty string")

        poll_seconds = _number(values, "watcher", "poll_seconds")
        if not poll_seconds > 0:
            raise ValueError("watcher.poll_seconds must be greater than 0")

        stable_seconds = _number(values, "watcher", "stable_seconds")
        if not stable_seconds >= 0:
            raise ValueError("watcher.stable_seconds must be greater than or equal to 0")

        max_retries = _required(values, "queue", "max_retries")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 1
        ):
            raise ValueError("queue.max_retries must be an integer greater than or equal to 1")

        return cls(
            vault=path.parent.parent.resolve(),
            compiler=provider,
            poll_seconds=poll_seconds,
            stable_seconds=stable_seconds,
            max_retries=max_retries,
        )
