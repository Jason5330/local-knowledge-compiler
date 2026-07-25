"""Core data models for the local knowledge catalog."""

from dataclasses import asdict, dataclass, field
from typing import Literal


Space = str

JobState = Literal[
    "discovered",
    "stable",
    "fingerprinted",
    "archived",
    "extracted",
    "compiled",
    "validated",
    "published",
    "retrying",
    "pending_attention",
]

SourceStatus = Literal[
    "archived",
    "extracted",
    "pending_extractor",
    "compiled",
    "validated",
    "published",
]


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    version_id: str
    space: Space
    original_name: str
    relative_path: str
    sha256: str
    media_type: str
    status: SourceStatus
    previous_version_id: str | None = None
    created_sequence: int | None = None


@dataclass(frozen=True)
class SearchHit:
    version_id: str
    source_id: str
    space: Space
    relative_path: str
    locator: str
    text: str
    score: float


@dataclass
class Job:
    job_id: str
    source_path: str
    state: JobState = "discovered"
    attempts: int = 0
    error: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
