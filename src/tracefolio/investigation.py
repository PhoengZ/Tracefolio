from dataclasses import dataclass

from .collection import project_fingerprint, provider_for
from .contracts.packets import InvestigationPacket
from .errors import TracefolioError
from .privacy import path_allowed, sanitize_text
from .sources.base import ContextRequest, canonical, digest
from .state import StateStore, effective_works, work_evidence_refs


@dataclass
class PacketResult:
    status: str
    packet_id: str | None = None
    evidence_count: int = 0

    def as_dict(self):
        result = {"status": self.status, "packet_id": self.packet_id,
                  "evidence_count": self.evidence_count}
        if self.packet_id:
            result["relative_path"] = f"packets/{self.packet_id}/packet.json"
        return result


def packet_hash(packet):
    return digest(packet.model_dump(mode="json", exclude={"packet_id", "packet_digest"}))


def validate_packet(packet):
    if packet_hash(packet) != packet.packet_digest or packet.packet_id != "packet-" + packet.packet_digest[:24]:
        raise TracefolioError("packet_integrity")


def _finish(packet):
    hashed = packet_hash(packet)
    return packet.model_copy(update={"packet_id": "packet-" + hashed[:24],
                                     "packet_digest": hashed})


def _size(packet):
    return len(canonical(packet.model_dump(mode="json"))) + 1


def create_packet(store: StateStore, project_id: str, *, work_id=None) -> PacketResult:
    project = next((p for p in store.config.registry.projects
                    if p.project_id == project_id and p.enabled), None)
    if project is None:
        raise TracefolioError("unknown_project")
    with store.lock(project_id):
        ledger = store.read_state(project_id)
        if ledger.route_fingerprint != project_fingerprint(store.config, project):
            raise TracefolioError("collection_required")
        generation = store.current(project_id, ledger)
        previous = {w.work_id: w for w in effective_works(generation, ledger)}
        configured = sorted({w.work_id for s in project.sources for w in s.work_units})
        if not ledger.pending_analysis and not ledger.invalidated_work_ids and generation and not work_id:
            return PacketResult("noop")
        if not ledger.evidence_ids and not configured:
            return PacketResult("no_evidence")
        candidates = list(ledger.pending_analysis)
        selected, selected_works = {}, {}
        work_refs = {key: work_evidence_refs(work) for key, work in previous.items()}

        def include_work(work_key, target_evidence, target_works):
            work = previous[work_key]
            target_works[work_key] = work
            for ref in work_refs[work_key]:
                target_evidence[ref] = store.read_evidence(ref)

        if work_id:
            if work_id not in previous:
                raise TracefolioError("unknown_work")
            include_work(work_id, selected, selected_works)
        elif not candidates and ledger.invalidated_work_ids:
            include_work(ledger.invalidated_work_ids[0], selected, selected_works)

        def build(items, works, pending):
            return _finish(InvestigationPacket(
                packet_id="pending", packet_digest="pending", project_id=project_id,
                project_title=sanitize_text(project.title, store.config.policy).text,
                project_context=sanitize_text(project.context, store.config.policy).text,
                base_generation_id=ledger.current_generation_id,
                collection_revision=ledger.collection_revision,
                allowed_evidence_ids=list(items),
                pending_evidence_ids=pending, evidence=list(items.values()),
                prior_works=list(works.values()), configured_work_ids=configured,
                known_work_ids=sorted(previous), coverage=ledger.coverage(),
            ))

        consumed = []
        for ref in candidates:
            record = store.read_evidence(ref)
            trial = dict(selected)
            trial_works = dict(selected_works)
            trial[ref] = record
            for key, refs in work_refs.items():
                related = any(
                    (old.source_id, old.source_relative_path) ==
                    (record.source_id, record.source_relative_path)
                    for old in (store.read_evidence(r) for r in refs))
                if related:
                    include_work(key, trial, trial_works)
            for source in project.sources:
                if source.source_id == record.source_id and record.source_relative_path:
                    for unit in source.work_units:
                        if unit.work_id in previous and path_allowed(
                                record.source_relative_path, unit.include_paths, []):
                            include_work(unit.work_id, trial, trial_works)
            packet = build(trial, trial_works, consumed + [ref])
            if _size(packet) > store.config.registry.limits.max_packet_bytes:
                if not consumed:
                    return PacketResult("blocked_budget")
                break
            selected, selected_works = trial, trial_works
            consumed.append(ref)
        packet = build(selected, selected_works, consumed)
        if _size(packet) > store.config.registry.limits.max_packet_bytes:
            return PacketResult("blocked_budget")
        store.write_packet(packet)
        return PacketResult("created", packet.packet_id, len(packet.evidence))


def extend_packet(store: StateStore, request: ContextRequest) -> PacketResult:
    packet = store.read_packet(request.packet_id)
    validate_packet(packet)
    with store.lock(packet.project_id):
        ledger = store.read_state(packet.project_id)
        if (packet.collection_revision != ledger.collection_revision
                or packet.base_generation_id != ledger.current_generation_id):
            raise TracefolioError("stale_packet")
        req = store.source_request(packet.project_id, request.source_request.source.source_id)
        if not path_allowed(request.path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        revisions = {e.revision for e in packet.evidence if e.source_id == req.source.source_id}
        revisions.update(s.revision for s in packet.coverage.sources
                         if s.source_id == req.source.source_id)
        if request.revision not in revisions:
            raise TracefolioError("context_revision")
        revision = request.revision
        if req.source.kind == "local_directory":
            cursor = ledger.sources[req.source.source_id].cursor
            expected = cursor.inventory.get(request.path)
            if expected is None:
                raise TracefolioError("context_revision")
            revision = "sha256:" + expected
        result = provider_for(req.source.kind).read_context(
            ContextRequest(req, revision, request.path, packet.packet_id))
        items = {e.evidence_id: e for e in packet.evidence}
        fresh = []
        for record in result.evidence:
            if record.evidence_id not in items:
                items[record.evidence_id] = record
                fresh.append(record)
        if not fresh:
            return PacketResult("noop", packet.packet_id, len(packet.evidence))
        updated = packet.model_copy(update={
            "evidence": list(items.values()), "allowed_evidence_ids": list(items),
            "pending_evidence_ids": list(dict.fromkeys(packet.pending_evidence_ids +
                                                       [e.evidence_id for e in fresh])),
            "collection_revision": ledger.collection_revision + 1,
        })
        updated = _finish(updated)
        if _size(updated) > store.config.registry.limits.max_packet_bytes:
            return PacketResult("blocked_budget")
        for record in fresh:
            store.put_evidence(record)
            if record.evidence_id not in ledger.evidence_ids:
                ledger.evidence_ids.append(record.evidence_id)
            if record.evidence_id not in ledger.pending_analysis:
                ledger.pending_analysis.append(record.evidence_id)
        ledger.collection_revision += 1
        store.write_packet(updated)
        store.save_state(packet.project_id, ledger)
        return PacketResult("created", updated.packet_id, len(updated.evidence))

