from typing import Literal

from pydantic import Field

from .common import Artifact, Coverage, Id, StrictModel
from .evidence import EvidenceRecord
from .knowledge import WorkContent, WorkRecord


class PacketWorkRoute(StrictModel):
    work_id: Id
    source_id: Id
    context: str
    include_paths: list[str]


class InvestigationPacket(Artifact):
    packet_id: Id
    packet_digest: str
    project_id: Id
    project_title: str
    project_context: str
    base_generation_id: Id | None = None
    collection_revision: int = Field(ge=0)
    allowed_evidence_ids: list[str]
    pending_evidence_ids: list[str]
    evidence_aliases: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceRecord]
    prior_works: list[WorkRecord] = Field(default_factory=list)
    configured_work_ids: list[Id] = Field(default_factory=list)
    work_routes: list[PacketWorkRoute] = Field(default_factory=list)
    known_work_ids: list[Id] = Field(default_factory=list)
    coverage: Coverage


class ExcludedEvidence(StrictModel):
    evidence_id: str
    reason: Literal["irrelevant_to_work", "duplicate_context", "unavailable_content"]


class AnalysisProposal(Artifact):
    packet_id: Id
    packet_digest: str
    project_id: Id
    base_generation_id: Id | None = None
    collection_revision: int = Field(ge=0)
    project_summary: str = Field(min_length=1)
    work_updates: list[WorkContent] = Field(default_factory=list)
    analyzed_evidence_ids: list[str] = Field(default_factory=list)
    excluded_evidence: list[ExcludedEvidence] = Field(default_factory=list)

