"""Read-only scheduling decisions, independent of portfolio completeness labels."""

from .collection import valid_project_fingerprints
from .errors import TracefolioError


def inspect_project(store, route) -> dict:
    result = dict(project_id=route.project_id, generation_id=None, coverage_state=None,
                  pending_evidence_count=0, invalidated_work_ids=[],
                  reconciliation_source_ids=[], missing_work_ids=[], limitations=[],
                  analysis_action='disabled' if not route.enabled else 'missing', reasons=[])
    if not route.enabled:
        return result
    try:
        ledger = store.read_state(route.project_id)
        generation = store.current(route.project_id, ledger)
        coverage = ledger.coverage()
        works = generation.works if generation else []
        configured = {w.work_id for s in route.sources for w in s.work_units}
        missing = sorted(configured - {w.work_id for w in works})
        incomplete = any(s.coverage.state != 'complete' or s.errors
                         for s in ledger.sources.values())
        limitations = sorted({v for s in ledger.sources.values()
                              for v in [*s.errors, *(v for c in s.coverage.sources
                                                     for v in c.limitations)]})
        result.update(generation_id=generation.generation_id if generation else None,
                      coverage_state=coverage.state,
                      pending_evidence_count=len(ledger.pending_analysis),
                      invalidated_work_ids=ledger.invalidated_work_ids,
                      reconciliation_source_ids=ledger.reconciliation_sources,
                      missing_work_ids=missing, limitations=limitations)
        decisions = []
        if ledger.route_fingerprint and ledger.route_fingerprint not in (
                valid_project_fingerprints(store.config, route)):
            decisions.append(('blocked', 'collection_required'))
        if ledger.pending_analysis:
            decisions.append(('analyze', 'pending_evidence'))
        if ledger.invalidated_work_ids or ledger.reconciliation_sources:
            affected = set(ledger.reconciliation_sources)
            for work in works:
                if work.work_id in ledger.invalidated_work_ids:
                    affected.update(work.provenance.source_ids)
            blocked = any(s not in ledger.sources or
                          ledger.sources[s].coverage.state != 'complete' or
                          ledger.sources[s].errors for s in affected)
            decisions.append(('blocked', 'source_incomplete') if blocked else
                             ('analyze', 'review_required'))
        elif incomplete:
            decisions.append(('blocked', 'source_incomplete'))
        if not generation:
            decisions.append(('missing', 'no_generation'))
        if missing:
            decisions.append(('missing', 'configured_work_missing'))
        if any(w.analysis_status != 'ready' or any(c.status == 'needs_review' for c in w.claims)
               for w in works):
            decisions.append(('blocked', 'explicit_review_required'))
        result['analysis_action'] = decisions[0][0] if decisions else 'reuse'
        result['reasons'] = list(dict.fromkeys(reason for _, reason in decisions))
    except TracefolioError as error:
        result.update(analysis_action='blocked', reasons=[error.code])
    except Exception:  # noqa: BLE001 - same privacy boundary as portfolio aggregation
        result.update(analysis_action='blocked', reasons=['state_integrity'])
    return result
