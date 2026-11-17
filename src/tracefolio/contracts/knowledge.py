from typing import Literal

from pydantic import Field, model_validator

from .common import Artifact, Coverage, Id, StrictModel, Utc, utc_now


class Metric(StrictModel):
    value: str = Field(pattern=r"^-?\d+(?:\.\d+)?$")
    unit: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    period_start: Utc | None = None
    period_end: Utc | None = None
    evidence_refs: list[str] = Field(min_length=1)


class Claim(StrictModel):
    claim_id: Id
    statement: str = Field(min_length=1)
    status: Literal["observed", "inferred", "user_asserted", "needs_review"]
    evidence_refs: list[str] = Field(min_length=1)
    metric: Metric | None = None


class Skill(StrictModel):
    name: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class Provenance(StrictModel):
    source_ids: list[Id] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    collection_ids: list[str] = Field(default_factory=list)
    packet_ids: list[str] = Field(default_factory=list)


class WorkContent(StrictModel):
    work_id: Id
    title: str = Field(min_length=1)
    problem: str | None = None
    actions: list[str] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    contribution: Literal["self_authored", "collaborative", "unverified"] = "unverified"
    implementation_status: Literal["unknown", "prototype", "implemented", "deployed"] = "unknown"
    analysis_status: Literal["ready", "partial", "pending"] = "partial"
    review_status: Literal["unreviewed", "user_reviewed"] = "unreviewed"
    unknowns: list[str] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def unique_claims(self):
        ids = [c.claim_id for c in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate claim")
        return self


class WorkRecord(WorkContent, Artifact):
    project_id: Id
    record_revision: int = Field(ge=1)
    generated_at: Utc = Field(default_factory=utc_now)


class ProjectRecord(Artifact):
    project_id: Id
    record_revision: int = Field(ge=1)
    title: str
    summary: str
    review_status: Literal["unreviewed", "user_reviewed"] = "unreviewed"
    source_ids: list[Id] = Field(default_factory=list)
    work_ids: list[Id] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    coverage: Coverage
    unknowns: list[str] = Field(default_factory=list)
    generated_at: Utc = Field(default_factory=utc_now)

