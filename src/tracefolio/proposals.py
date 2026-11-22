from pathlib import Path

from pydantic import ValidationError

from .collection import project_fingerprint
from .contracts.knowledge import ProjectRecord, Provenance, WorkRecord
from .contracts.packets import AnalysisProposal
from .errors import TracefolioError
from .investigation import validate_packet
from .privacy import sanitize_text
from .private_config import validate_proposal_path
from .sources.base import digest
from .state import PublicationResult, StateStore, work_evidence_refs


def _safe_strings(value, policy):
    if isinstance(value, str):
        if sanitize_text(value, policy).text != value:
            raise TracefolioError("unsafe_proposal_content")
    elif isinstance(value, list):
        for item in value:
            _safe_strings(item, policy)
    elif isinstance(value, dict):
        for item in value.values():
            _safe_strings(item, policy)


def _resolve_proposal(proposal: AnalysisProposal, packet) -> AnalysisProposal:
    aliases = packet.evidence_aliases or {}

    def resolve(ref):
        return aliases.get(ref, ref)

    excluded = [item.model_copy(update={"evidence_id": resolve(item.evidence_id)})
                for item in proposal.excluded_evidence]
    excluded_ids = {item.evidence_id for item in excluded}
    if proposal.analyzed_evidence_ids == ["*"]:
        analyzed = [eid for eid in packet.allowed_evidence_ids if eid not in excluded_ids]
    else:
        analyzed = [resolve(ref) for ref in proposal.analyzed_evidence_ids]
    updates = []
    for update in proposal.work_updates:
        claims = []
        for claim in update.claims:
            metric = (claim.metric.model_copy(update={
                "evidence_refs": [resolve(r) for r in claim.metric.evidence_refs]})
                if claim.metric else None)
            claims.append(claim.model_copy(update={
                "evidence_refs": [resolve(r) for r in claim.evidence_refs],
                "metric": metric,
            }))
        skills = [skill.model_copy(update={
            "evidence_refs": [resolve(r) for r in skill.evidence_refs]})
            for skill in update.skills]
        prov = update.provenance.model_copy(update={
            "evidence_refs": [resolve(r) for r in update.provenance.evidence_refs]})
        updates.append(update.model_copy(update={
            "claims": claims, "skills": skills, "provenance": prov}))
    return proposal.model_copy(update={
        "analyzed_evidence_ids": analyzed,
        "excluded_evidence": excluded,
        "work_updates": updates,
    })


def apply_proposal(store: StateStore, proposal_path: Path) -> PublicationResult:
    path = validate_proposal_path(store.config, Path(proposal_path))
    try:
        limit = store.config.registry.limits.max_packet_bytes
        if path.stat().st_size > limit:
            raise TracefolioError("proposal_budget")
        proposal = AnalysisProposal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        raise TracefolioError("invalid_proposal") from None
    _safe_strings(proposal.model_dump(mode="json"), store.config.policy)
    project = next((p for p in store.config.registry.projects
                    if p.project_id == proposal.project_id and p.enabled), None)
    if project is None:
        raise TracefolioError("unknown_project")
    ledger = store.read_state(project.project_id)
    proposal_digest = digest(proposal.model_dump(mode="json"))
    if proposal_digest in ledger.accepted_proposals:
        return PublicationResult("noop", ledger.accepted_proposals[proposal_digest])
    packet = store.read_packet(proposal.packet_id)
    validate_packet(packet)
    if (packet.project_id != proposal.project_id or packet.packet_digest != proposal.packet_digest
            or packet.base_generation_id != proposal.base_generation_id):
        raise TracefolioError("packet_mismatch")
    if (ledger.collection_revision != packet.collection_revision
            or proposal.collection_revision != ledger.collection_revision
            or ledger.current_generation_id != proposal.base_generation_id
            or ledger.route_fingerprint != project_fingerprint(store.config, project)):
        raise TracefolioError("stale_proposal")
    allowed = set(packet.allowed_evidence_ids)
    if (len(allowed) != len(packet.allowed_evidence_ids)
            or {e.evidence_id for e in packet.evidence} != allowed):
        raise TracefolioError("packet_integrity")
    evidence = {}
    for item in packet.evidence:
        original = store.read_evidence(item.evidence_id)
        if original != item or item.project_id != proposal.project_id:
            raise TracefolioError("packet_integrity")
        evidence[item.evidence_id] = item
    for prior_work in packet.prior_works:
        for ref in work_evidence_refs(prior_work):
            if ref not in evidence:
                original = store.read_evidence(ref)
                if original.project_id != proposal.project_id:
                    raise TracefolioError("packet_integrity")
                evidence[ref] = original
    proposal = _resolve_proposal(proposal, packet)
    accounted = proposal.analyzed_evidence_ids + [
        item.evidence_id for item in proposal.excluded_evidence]
    if len(accounted) != len(set(accounted)) or set(accounted) != allowed:
        raise TracefolioError("evidence_accounting")
    work_ids = [w.work_id for w in proposal.work_updates]
    if len(work_ids) != len(set(work_ids)):
        raise TracefolioError("duplicate_work")
    generation = store.current(project.project_id, ledger)
    works = {w.work_id: w for w in generation.works} if generation else {}
    excluded = {item.evidence_id for item in proposal.excluded_evidence}
    prior = {w.work_id: w for w in packet.prior_works}
    active_sources = {s.source_id for s in project.sources}
    for update in proposal.work_updates:
        if update.review_status != "unreviewed":
            raise TracefolioError("machine_review_forbidden")
        old = works.get(update.work_id)
        prior_refs = work_evidence_refs(old) if old else set()
        if old and (update.work_id not in prior
                    or work_evidence_refs(prior[update.work_id]) != prior_refs):
            raise TracefolioError("prior_context_required")
        refs = work_evidence_refs(update)
        if not refs <= (allowed | prior_refs):
            raise TracefolioError("foreign_evidence")
        if refs & excluded:
            raise TracefolioError("excluded_evidence_reference")
        if update.analysis_status == "ready" and any(
                claim.status == "needs_review" for claim in update.claims):
            raise TracefolioError("unresolved_claim")
        if update.analysis_status == "ready" and not refs:
            raise TracefolioError("ready_requires_evidence")
        if update.implementation_status in {"prototype", "implemented", "deployed"} and not refs:
            raise TracefolioError("implementation_requires_evidence")
        reconciling = bool(old and any(
            s in ledger.reconciliation_sources for s in old.provenance.source_ids))
        merged_refs = refs if (not old or reconciling) else (refs | prior_refs)
        has_identity = bool(store.config.author_emails or store.config.github_user_id)
        if update.contribution == "self_authored" and (
                not has_identity or not any(
                    evidence[ref].attribution == "self" for ref in merged_refs)):
            raise TracefolioError("ownership_unverified")
        if update.contribution == "collaborative" and (
                not has_identity or not any(
                    evidence[ref].attribution in {"self", "shared"} for ref in merged_refs)):
            raise TracefolioError("ownership_unverified")
        if update.implementation_status == "deployed" and not any(
                evidence[ref].evidence_type in {"deployment", "user_note"} for ref in merged_refs):
            raise TracefolioError("deployment_unverified")
        for claim in update.claims:
            if claim.status == "user_asserted" and not any(
                    evidence[ref].origin == "user_note" for ref in claim.evidence_refs):
                raise TracefolioError("assertion_unverified")
            if claim.metric and not any(
                    evidence[ref].evidence_type in {"measurement", "user_note"}
                    for ref in claim.metric.evidence_refs):
                raise TracefolioError("measurement_unverified")
        source_ids = {evidence[ref].source_id for ref in merged_refs}
        if (not source_ids <= active_sources
                or not set(update.provenance.source_ids) <= source_ids):
            raise TracefolioError("evidence_source_out_of_scope")
        old_packets = set(old.provenance.packet_ids) if old else set()
        if not set(update.provenance.packet_ids) <= old_packets | {packet.packet_id}:
            raise TracefolioError("foreign_packet_reference")
        for collection_id in update.provenance.collection_ids:
            from .state import logical_id
            data = store._read_json(store._path("collections", logical_id(collection_id) + ".json"))
            if data.get("project_id") != project.project_id:
                raise TracefolioError("foreign_collection_reference")
        record = WorkRecord(
            **update.model_dump(exclude={"provenance", "review_status"}),
            project_id=project.project_id, record_revision=old.record_revision + 1 if old else 1,
            review_status="unreviewed", provenance=Provenance(
                source_ids=sorted(source_ids), evidence_refs=sorted(merged_refs),
                collection_ids=update.provenance.collection_ids,
                packet_ids=sorted(old_packets | {packet.packet_id})))
        works[update.work_id] = record

    ledger.pending_analysis = [ref for ref in ledger.pending_analysis if ref not in allowed]
    for ref in proposal.analyzed_evidence_ids:
        ledger.analysis_exclusions.pop(ref, None)
    for item in proposal.excluded_evidence:
        ledger.analysis_exclusions[item.evidence_id] = item.reason

    for work_id in work_ids:
        if work_id not in ledger.invalidated_work_ids:
            continue
        refs = work_evidence_refs(works[work_id])
        if work_id in prior:
            refs |= work_evidence_refs(prior[work_id])
        affected = {(evidence[ref].source_id, evidence[ref].source_relative_path)
                    for ref in refs if ref in evidence}
        pending_related = any(
            (ev.source_id, ev.source_relative_path) in affected
            for ev in (store.read_evidence(r) for r in ledger.pending_analysis))
        complete_sources = all(
            ledger.sources[source_id].coverage.state == "complete"
            for source_id, _ in affected if source_id in ledger.sources)
        if not pending_related and complete_sources:
            ledger.invalidated_work_ids.remove(work_id)
            ledger.review_evidence_by_work.pop(work_id, None)

    pending_sources = {store.read_evidence(ref).source_id for ref in ledger.pending_analysis}
    invalidated_sources = {source for work_id in ledger.invalidated_work_ids
                           for source in works[work_id].provenance.source_ids}
    ledger.reconciliation_sources = [source for source in ledger.reconciliation_sources
                                     if source in pending_sources | invalidated_sources]
    coverage = ledger.coverage()
    configured = {w.work_id for s in project.sources for w in s.work_units}
    unknowns = []
    if not configured <= works.keys() or any(
            w.analysis_status != "ready" for w in works.values()):
        coverage.state = "partial"
        unknowns.append("Some configured or existing work items are not fully analyzed.")
    project_record = ProjectRecord(
        project_id=project.project_id,
        record_revision=generation.project.record_revision + 1 if generation else 1,
        title=sanitize_text(project.title, store.config.policy).text,
        summary=proposal.project_summary, source_ids=sorted(active_sources),
        work_ids=sorted(works), evidence_ids=ledger.evidence_ids,
        coverage=coverage, unknowns=unknowns,
    )
    return store.publish(project.project_id, packet.base_generation_id, project_record,
                         [works[key] for key in sorted(works)], ledger,
                         proposal_digest=proposal_digest)

