from dataclasses import dataclass

from .contracts.common import Coverage, SourceCoverage
from .contracts.evidence import CollectionManifest, SourceCursor
from .errors import TracefolioError
from .sources.base import digest
from .sources.github_public import GitHubPublicProvider
from .sources.local_directory import LocalDirectoryProvider
from .sources.local_git import LocalGitProvider
from .state import SourceState, StateStore, work_evidence_refs


def provider_for(kind):
    return {"local_git": LocalGitProvider, "local_directory": LocalDirectoryProvider,
            "github_public": GitHubPublicProvider}[kind]()


def source_fingerprint(config, project, source):
    return digest({"source": source.model_dump(), "project_context": project.context,
                   "locator": config.locators[(project.project_id, source.source_id)],
                   "authors": sorted(config.author_emails), "privacy": config.policy.epoch})


def project_fingerprint(config, project):
    return digest({"project": project.model_dump(),
                   "sources": [source_fingerprint(config, project, s) for s in project.sources]})


@dataclass
class CollectionReport:
    projects: list[dict]

    @property
    def partial(self):
        return any(p["status"] in {"partial", "error"} for p in self.projects)

    def as_dict(self):
        return {"projects": self.projects, "partial": self.partial}


def _meaningful(state):
    data = state.model_dump(mode="json")
    for source in data["sources"].values():
        source["cursor"].pop("observed_at", None)
    return data


def collect_projects(config, project_ids, *, providers=None, mode=None):
    lookup = {p.project_id: p for p in config.registry.projects}
    if any(p not in lookup for p in project_ids):
        raise TracefolioError("unknown_project")
    store = StateStore.open(config)
    reports = []
    for project_id in project_ids:
        project = lookup[project_id]
        if not project.enabled:
            reports.append({"project_id": project_id, "status": "disabled", "new_evidence": 0})
            continue
        try:
            with store.lock(project_id):
                ledger = store.read_state(project_id)
                before = _meaningful(ledger)
                generation = store.current(project_id, ledger)
                invalidated = set(ledger.invalidated_work_ids)
                changed_paths, changed_sources, count = set(), set(), 0
                for source in project.sources:
                    req = store.source_request(project_id, source.source_id)
                    if mode in {"commits", "snapshot"}:
                        req.source = req.source.model_copy(update={"collection_mode": mode})
                    fingerprint = source_fingerprint(config, project, source)
                    old = ledger.sources.get(source.source_id)
                    previous = old.cursor if old and old.fingerprint == fingerprint else None
                    if old and previous is None:
                        changed_sources.add(source.source_id)
                    try:
                        provider = (providers or {}).get(source.kind) or provider_for(source.kind)
                        result = provider.collect_source(req, previous)
                        if (req.source.collection_mode == "snapshot"
                                and (old is None or not old.cursor.inventory)):
                            ledger.pending_analysis = [
                                eid for eid in ledger.pending_analysis
                                if not (store.read_evidence(eid).source_id == source.source_id
                                        and store.read_evidence(eid).origin == "git_commit")
                            ]
                        result.cursor.fingerprint = fingerprint
                        ledger.sources[source.source_id] = SourceState(
                            cursor=result.cursor, coverage=result.coverage, fingerprint=fingerprint)
                        changed_paths.update((source.source_id, path)
                                             for path in result.invalidated_paths)
                        if "history_reconciled" in result.warnings:
                            changed_sources.add(source.source_id)
                            if source.source_id not in ledger.reconciliation_sources:
                                ledger.reconciliation_sources.append(source.source_id)
                        for record in result.evidence:
                            store.put_evidence(record)
                            is_new = record.evidence_id not in ledger.evidence_ids
                            revisited = source.source_id in changed_sources or (
                                record.origin == "snapshot" and old is not None and
                                old.cursor.inventory.get(record.source_relative_path) !=
                                result.cursor.inventory.get(record.source_relative_path))
                            if is_new:
                                ledger.evidence_ids.append(record.evidence_id)
                                count += 1
                            if is_new or revisited:
                                if record.evidence_id not in ledger.pending_analysis:
                                    ledger.pending_analysis.append(record.evidence_id)
                                changed_paths.add((source.source_id, record.source_relative_path))
                    except Exception as error:  # noqa: BLE001 - isolate unsafe provider errors
                        code = error.code if isinstance(error, TracefolioError) else "source_failed"
                        ledger.sources[source.source_id] = SourceState(
                            cursor=previous or SourceCursor(), fingerprint=fingerprint,
                            errors=[code], coverage=Coverage(state="unavailable",
                                sources=[SourceCoverage(source_id=source.source_id,
                                    collection_state="unavailable", limitations=[code])]))
                active_sources = {s.source_id for s in project.sources}
                removed_sources = ledger.sources.keys() - active_sources
                changed_sources.update(removed_sources)
                ledger.sources = {k: v for k, v in ledger.sources.items() if k in active_sources}
                ledger.reconciliation_sources = [s for s in ledger.reconciliation_sources
                                                 if s in active_sources]
                if generation:
                    if ledger.route_fingerprint != project_fingerprint(config, project):
                        invalidated.update(w.work_id for w in generation.works)
                    for work in generation.works:
                        for ref in work_evidence_refs(work):
                            old_record = store.read_evidence(ref)
                            if (old_record.source_id in changed_sources
                                    or (old_record.source_id, old_record.source_relative_path)
                                    in changed_paths):
                                invalidated.add(work.work_id)
                ledger.invalidated_work_ids = sorted(invalidated)
                ledger.route_fingerprint = project_fingerprint(config, project)
                if _meaningful(ledger) != before:
                    ledger.collection_revision += 1
                    manifest = CollectionManifest(
                        collection_id=f"collection-{project_id}-{ledger.collection_revision}",
                        project_id=project_id, collection_revision=ledger.collection_revision,
                        evidence_ids=ledger.evidence_ids, coverage=ledger.coverage())
                    store._atomic_json(store._path("collections", manifest.collection_id + ".json"),
                                       manifest.model_dump(mode="json"))
                    store.save_state(project_id, ledger)
                collection_complete = all(s.coverage.state == "complete"
                                          for s in ledger.sources.values())
                reports.append({"project_id": project_id,
                                "status": "complete" if collection_complete else "partial",
                                "new_evidence": count})
        except TracefolioError as error:
            reports.append({"project_id": project_id, "status": "error",
                            "code": error.code, "new_evidence": 0})
    return CollectionReport(reports)

