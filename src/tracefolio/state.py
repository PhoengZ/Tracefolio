import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import Field, ValidationError

from .contracts.common import Coverage, StrictModel
from .contracts.evidence import EvidenceRecord, SourceCursor
from .contracts.knowledge import ProjectRecord, WorkRecord
from .contracts.packets import InvestigationPacket
from .errors import TracefolioError
from .privacy import secure_private_root
from .sources.base import SourceRequest, digest

ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def logical_id(value):
    if not isinstance(value, str) or len(value) > 100 or not ID.fullmatch(value):
        raise TracefolioError("invalid_identifier")
    return value


class SourceState(StrictModel):
    cursor: SourceCursor
    coverage: Coverage
    fingerprint: str
    errors: list[str] = Field(default_factory=list)


class AnalysisLedger(StrictModel):
    current_generation_id: str | None = None
    collection_revision: int = 0
    route_fingerprint: str = ""
    sources: dict[str, SourceState] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    pending_analysis: list[str] = Field(default_factory=list)
    invalidated_work_ids: list[str] = Field(default_factory=list)
    accepted_proposals: dict[str, str] = Field(default_factory=dict)
    analysis_exclusions: dict[str, str] = Field(default_factory=dict)
    reconciliation_sources: list[str] = Field(default_factory=list)
    review_evidence_by_work: dict[str, list[str]] = Field(default_factory=dict)

    def coverage(self):
        sources = [s.model_copy(deep=True) for state in self.sources.values()
                   for s in state.coverage.sources]
        unavailable = "unavailable_content" in self.analysis_exclusions.values()
        for source in sources:
            if source.source_id in self.reconciliation_sources:
                source.limitations.append("history_reconciliation_required")
            source.analysis_state = "partial" if (
                self.pending_analysis or unavailable or self.invalidated_work_ids
                or source.source_id in self.reconciliation_sources) else "complete"
        complete = bool(sources) and all(s.collection_state == "complete" for s in sources)
        complete = (complete and not self.pending_analysis and not self.invalidated_work_ids
                    and not self.reconciliation_sources and not unavailable)
        return Coverage(state="complete" if complete else "partial", sources=sources,
                        pending_evidence_count=len(self.pending_analysis))


@dataclass
class Generation:
    generation_id: str
    project: ProjectRecord
    works: list[WorkRecord]

    def work(self, work_id):
        return next(w for w in self.works if w.work_id == work_id)


@dataclass
class PublicationResult:
    status: str
    generation_id: str

    def as_dict(self):
        return {"status": self.status, "generation_id": self.generation_id}


def work_evidence_refs(work):
    refs = set(work.provenance.evidence_refs)
    for claim in work.claims:
        refs.update(claim.evidence_refs)
        if claim.metric:
            refs.update(claim.metric.evidence_refs)
    for skill in work.skills:
        refs.update(skill.evidence_refs)
    return refs


def effective_works(generation, ledger):
    if not generation:
        return []
    result = []
    for original in generation.works:
        work = original.model_copy(deep=True)
        if work.work_id in ledger.invalidated_work_ids:
            work.review_status = "unreviewed"
            work.analysis_status = "partial"
            message = "Source or history changed; compare prior evidence with current pinned source revisions."
            if message not in work.unknowns:
                work.unknowns.append(message)
            for claim in work.claims:
                claim.status = "needs_review"
        result.append(work)
    return result


class StateStore:
    def __init__(self, config):
        self.config = config
        self.root = config.state_root

    @classmethod
    def open(cls, config, *, read_only=False):
        if not read_only:
            secure_private_root(config.state_root)
        store = cls(config)
        policy = store._path("policy.json")
        expected = {"privacy_policy": config.policy.epoch}
        if policy.exists() and store._read_json(policy) != expected:
            raise TracefolioError("privacy_policy_changed",
                                  "Use a new private state directory for the changed policy.")
        if not policy.exists() and not read_only:
            store._atomic_json(policy, expected)
        return store

    def _path(self, *parts):
        if any(not isinstance(p, str) or p in {"", ".", ".."} or "/" in p or "\\" in p
               or ":" in p for p in parts):
            raise TracefolioError("unsafe_state_path")
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise TracefolioError("unsafe_state_path")
        return path

    def _read_json(self, path, max_bytes=33554432):
        try:
            if path.stat().st_size > max_bytes:
                raise TracefolioError("state_read_budget", exit_code=4)
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise TracefolioError("state_read_failed", exit_code=4) from None

    def _atomic_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            raise TracefolioError("state_write_failed", exit_code=4) from None
        finally:
            if temp.exists():
                temp.unlink()

    @contextmanager
    def lock(self, project_id):
        logical_id(project_id)
        path = self._path("locks", project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()
        except FileExistsError:
            raise TracefolioError("project_locked", "Another writer owns this project lock.", 3) from None
        try:
            yield
        finally:
            path.rmdir()

    def read_state(self, project_id):
        logical_id(project_id)
        pointer = self._path("projects", project_id, "CURRENT")
        if not pointer.exists():
            return AnalysisLedger()
        revision = logical_id(self._read_json(pointer)["revision_id"])
        data = self._read_json(self._path("projects", project_id, "revisions",
                                         revision, "state.json"))
        try:
            return AnalysisLedger.model_validate(data)
        except ValidationError:
            raise TracefolioError("state_integrity", exit_code=4) from None

    def save_state(self, project_id, ledger):
        logical_id(project_id)
        revision = "revision-" + uuid.uuid4().hex
        self._atomic_json(self._path("projects", project_id, "revisions", revision, "state.json"),
                          ledger.model_dump(mode="json"))
        self._atomic_json(self._path("projects", project_id, "CURRENT"), {"revision_id": revision})

    def current(self, project_id, ledger=None):
        ledger = ledger or self.read_state(project_id)
        if not ledger.current_generation_id:
            return None
        generation = logical_id(ledger.current_generation_id)
        base = ("projects", logical_id(project_id), "generations", generation)
        try:
            project = ProjectRecord.model_validate(self._read_json(self._path(*base, "project.json")))
            works = [WorkRecord.model_validate(self._read_json(
                self._path(*base, "works", logical_id(w) + ".json"))) for w in project.work_ids]
            return Generation(generation, project, works)
        except ValidationError:
            raise TracefolioError("state_integrity", exit_code=4) from None

    def put_evidence(self, record):
        data = record.model_dump(mode="json")
        expected = digest({k: v for k, v in data.items()
                           if k not in {"evidence_id", "content_digest"}})
        if record.evidence_id != "ev-" + expected or record.content_digest != expected:
            raise TracefolioError("evidence_integrity")
        path = self._path("evidence", logical_id(record.evidence_id) + ".json")
        if path.exists():
            if self._read_json(path) != data:
                raise TracefolioError("evidence_collision", exit_code=4)
            return
        self._atomic_json(path, data)

    def read_evidence(self, evidence_id):
        data = self._read_json(self._path("evidence", logical_id(evidence_id) + ".json"))
        try:
            record = EvidenceRecord.model_validate(data)
        except ValidationError:
            raise TracefolioError("evidence_integrity", exit_code=4) from None
        expected = digest({k: v for k, v in record.model_dump(mode="json").items()
                           if k not in {"evidence_id", "content_digest"}})
        if record.evidence_id != evidence_id or evidence_id != "ev-" + expected:
            raise TracefolioError("evidence_integrity", exit_code=4)
        return record

    def read_packet(self, packet_id):
        try:
            return InvestigationPacket.model_validate(self._read_json(
                self._path("packets", logical_id(packet_id), "packet.json"),
                self.config.registry.limits.max_packet_bytes))
        except ValidationError:
            raise TracefolioError("packet_integrity") from None

    def write_packet(self, packet):
        self._atomic_json(self._path("packets", packet.packet_id, "packet.json"),
                          packet.model_dump(mode="json"))

    def source_request(self, project_id, source_id):
        project = next((p for p in self.config.registry.projects
                        if p.project_id == project_id and p.enabled), None)
        if project is None:
            raise TracefolioError("unknown_project")
        source = next((s for s in project.sources if s.source_id == source_id), None)
        if source is None:
            raise TracefolioError("unknown_source")
        return SourceRequest(project_id, source, self.config.locators[(project_id, source_id)],
                             self.config.registry.limits, self.config.policy,
                             self.config.author_emails)

    def publish(self, project_id, expected_base, project, works, ledger, *, proposal_digest=None):
        work_ids = [work.work_id for work in works]
        if (project.project_id != project_id
                or any(work.project_id != project_id for work in works)
                or len(work_ids) != len(set(work_ids))
                or len(project.work_ids) != len(set(project.work_ids))
                or set(project.work_ids) != set(work_ids)):
            raise TracefolioError("publication_integrity")
        ProjectRecord.model_validate(project.model_dump())
        for work in works:
            WorkRecord.model_validate(work.model_dump())
        with self.lock(project_id):
            current = self.read_state(project_id)
            if proposal_digest and proposal_digest in current.accepted_proposals:
                return PublicationResult("noop", current.accepted_proposals[proposal_digest])
            if (current.current_generation_id != expected_base
                    or current.collection_revision != ledger.collection_revision):
                raise TracefolioError("stale_proposal")
            generation = "gen-" + uuid.uuid4().hex
            base = ("projects", logical_id(project_id), "generations", generation)
            self._atomic_json(self._path(*base, "project.json"), project.model_dump(mode="json"))
            for work in works:
                self._atomic_json(self._path(*base, "works", work.work_id + ".json"),
                                  work.model_dump(mode="json"))
            ledger.current_generation_id = generation
            if proposal_digest:
                ledger.accepted_proposals[proposal_digest] = generation
            # Pending analysis and semantic generation become visible via one pointer.
            self.save_state(project_id, ledger)
            return PublicationResult("published", generation)

