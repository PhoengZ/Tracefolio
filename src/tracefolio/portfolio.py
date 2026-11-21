from pathlib import Path

from .collection import valid_project_fingerprints
from .contracts.knowledge import WorkRecord
from .contracts.portfolio import ActivityWindow, PortfolioEntry, PortfolioSnapshot, SafeError
from .errors import TracefolioError
from .private_config import validate_export_path
from .state import effective_works


def _project_activity_window(ledger) -> ActivityWindow | None:
    firsts = [s.cursor.first_activity_at for s in ledger.sources.values()
              if s.cursor.first_activity_at]
    lasts = [s.cursor.last_activity_at for s in ledger.sources.values()
             if s.cursor.last_activity_at]
    sources = sorted({s.cursor.activity_source for s in ledger.sources.values()
                      if s.cursor.activity_source})
    if not firsts and not lasts:
        return None
    return ActivityWindow(
        first_activity_at=min(firsts) if firsts else None,
        last_activity_at=max(lasts) if lasts else None,
        sources=sources,
    )


def summarize_portfolio(store, registry):
    entries = []
    for route in registry.projects:
        if not route.enabled:
            entries.append(PortfolioEntry(project_id=route.project_id, status="disabled"))
            continue
        try:
            # Read CURRENT once; both generation and analysis overlay use this revision.
            ledger = store.read_state(route.project_id)
            generation = store.current(route.project_id, ledger)
            works = effective_works(generation, ledger)
            coverage = ledger.coverage()
            errors = [SafeError(code=code) for state in ledger.sources.values()
                      for code in state.errors]
            if ledger.route_fingerprint not in valid_project_fingerprints(store.config, route):
                coverage.state = "partial"
                errors.append(SafeError(code="route_changed"))
                for work in works:
                    work.analysis_status = "partial"
                    work.review_status = "unreviewed"
                    for claim in work.claims:
                        claim.status = "needs_review"
            known = {work.work_id for work in works}
            for source in route.sources:
                for unit in source.work_units:
                    if unit.work_id not in known:
                        works.append(WorkRecord(
                            project_id=route.project_id, work_id=unit.work_id,
                            title=unit.work_id, record_revision=1, analysis_status="pending",
                            unknowns=["This configured work item has not been analyzed."]))
                        known.add(unit.work_id)
            ready = (generation is not None and bool(works) and not errors
                     and coverage.state == "complete"
                     and all(work.analysis_status == "ready" and not any(
                         claim.status == "needs_review" for claim in work.claims) for work in works))
            if not ready:
                coverage.state = "partial"
            project = generation.project.model_copy(deep=True) if generation else None
            if project:
                project.coverage = coverage
            status = "ready" if ready else "partial" if generation else "error" if errors and ledger.sources else "pending"
            entries.append(PortfolioEntry(
                project_id=route.project_id, status=status,
                generation_id=generation.generation_id if generation else None,
                project=project, works=works,
                activity_window=_project_activity_window(ledger),
                coverage=coverage, errors=errors))
        except TracefolioError as error:
            entries.append(PortfolioEntry(project_id=route.project_id, status="error",
                                          errors=[SafeError(code=error.code)]))
        except Exception:  # noqa: BLE001 - isolate corrupt project without leaking its content
            entries.append(PortfolioEntry(project_id=route.project_id, status="error",
                                          errors=[SafeError(code="state_integrity")]))
    return PortfolioSnapshot(configured_project_count=len(registry.projects), entries=entries)


def export_portfolio(store, snapshot, path):
    target = validate_export_path(store.config, Path(path))
    # State already has an owner-only ACL/mode. Never alter an arbitrary export folder's ACL.
    export_root = store.root.resolve() / "exports"
    if not target.is_relative_to(export_root) or target == export_root:
        raise TracefolioError("unsafe_export", "Export must be inside the private state's exports directory.")
    with store.lock('export-writer'):
        store._atomic_json(target, snapshot.model_dump(mode="json"))


def brief_portfolio(snapshot):
    """Project saved knowledge without sending historical provenance to the harness."""
    entries = []
    for entry in snapshot.entries:
        works_brief = []
        project_skills: set[str] = set()
        for work in entry.works:
            work_skills = sorted({s.name for s in work.skills})
            project_skills.update(work_skills)
            item = work.model_dump(include={
                'work_id', 'title', 'problem', 'actions', 'implementation_status',
                'contribution', 'analysis_status', 'review_status', 'unknowns'})
            item['skills'] = work_skills
            works_brief.append(item)
        entries.append({
            'project_id': entry.project_id, 'status': entry.status,
            'title': entry.project.title if entry.project else None,
            'summary': entry.project.summary if entry.project else None,
            'skills': sorted(project_skills),
            'activity_window': (None if entry.activity_window is None
                                else entry.activity_window.model_dump(mode="json")),
            'works': works_brief,
            'coverage': None if entry.coverage is None else {
                'state': entry.coverage.state,
                'pending_evidence_count': entry.coverage.pending_evidence_count,
                'limitations': sorted({v for s in entry.coverage.sources for v in s.limitations})},
            'errors': [e.code for e in entry.errors],
        })
    return {'configured_project_count': snapshot.configured_project_count, 'entries': entries}
