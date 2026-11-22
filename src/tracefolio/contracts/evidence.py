from typing import Literal

from pydantic import Field

from .common import Artifact, Coverage, Id, StrictModel, Utc, utc_now


class CellProvenance(StrictModel):
    cell_index: int = Field(ge=0)
    cell_type: Literal["code", "markdown"]
    source_digest: str


class EvidenceRecord(Artifact):
    evidence_id: str
    project_id: Id
    source_id: Id
    origin: Literal["git_commit", "snapshot", "context", "user_note"]
    revision: str
    source_relative_path: str | None = None
    content_digest: str
    payload: str
    attribution: Literal["self", "shared", "other", "unverified"] = "unverified"
    evidence_type: Literal["code", "measurement", "deployment", "user_note"] = "code"
    completeness: Literal["complete", "partial", "unavailable"] = "complete"
    redactions: list[str] = Field(default_factory=list)
    normalization: Literal["notebook-source-v1"] | None = None
    cells: list[CellProvenance] = Field(default_factory=list)


class SourceCursor(Artifact):
    collection_mode: Literal['commits', 'snapshot'] | None = None
    fingerprint: str = ""
    head: str | None = None
    target: str | None = None
    pending: list[str] = Field(default_factory=list)
    verification_pending: list[str] = Field(default_factory=list)
    processed: list[str] = Field(default_factory=list)
    inventory: dict[str, str] = Field(default_factory=dict)
    baseline: dict[str, str] = Field(default_factory=dict)
    page: int = Field(default=1, ge=1)
    listed_complete: bool = True
    base_head: str | None = None
    observed_at: Utc | None = None
    first_activity_at: Utc | None = None
    last_activity_at: Utc | None = None
    activity_source: Literal[
        "git_commit_dates", "github_repo_and_head_dates", "filesystem_mtime"
    ] | None = None


class CollectionResult(StrictModel):
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    cursor: SourceCursor
    coverage: Coverage
    invalidated_paths: list[str] = Field(default_factory=list)
    changed: bool = False
    warnings: list[str] = Field(default_factory=list)


class CollectionManifest(Artifact):
    collection_id: Id
    project_id: Id
    collection_revision: int = Field(ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    coverage: Coverage
    generated_at: Utc = Field(default_factory=utc_now)


class ContextResult(StrictModel):
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    coverage: Coverage

