from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)]
EnvRef = Annotated[str, StringConstraints(pattern=r"^TRACEFOLIO_[A-Z0-9_]+$")]
State = Literal["complete", "partial", "unavailable"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def valid_utc(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError("UTC Z timestamp required")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


Utc = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"),
    AfterValidator(valid_utc),
    Field(json_schema_extra={"format": "date-time"}),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Artifact(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    language: Literal["en"] = "en"


class SourceCoverage(StrictModel):
    source_id: Id
    revision: str | None = None
    collection_state: State = "partial"
    analysis_state: State = "partial"
    limitations: list[str] = Field(default_factory=list)


class Coverage(StrictModel):
    state: State = "partial"
    pending_evidence_count: int = Field(default=0, ge=0)
    sources: list[SourceCoverage] = Field(default_factory=list)

