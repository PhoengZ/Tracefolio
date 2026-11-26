import json
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from .collection import valid_project_fingerprints
from .contracts.review import (
    ReviewApplyProjectResult,
    ReviewApplySummary,
    ReviewFeedbackItem,
    SkippedReviewItem,
)
from .errors import TracefolioError
from .private_config import validate_feedback_path
from .state import StateStore


def _parse_feedback_items(raw_text: str) -> list[ReviewFeedbackItem]:
    trimmed = raw_text.strip()
    if not trimmed:
        raise TracefolioError("invalid_feedback")
    raw_items: list[object] = []
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        for line in trimmed.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_items.append(json.loads(stripped))
            except ValueError:
                raise TracefolioError("invalid_feedback") from None
    else:
        if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            raw_items = list(parsed["items"])
        elif isinstance(parsed, list):
            raw_items = list(parsed)
        elif isinstance(parsed, dict):
            raw_items = [parsed]
        else:
            raise TracefolioError("invalid_feedback")

    if not raw_items:
        raise TracefolioError("invalid_feedback")

    deduped: dict[tuple[str, str], ReviewFeedbackItem] = {}
    for entry in raw_items:
        try:
            item = ReviewFeedbackItem.model_validate(entry)
        except ValidationError:
            raise TracefolioError("invalid_feedback") from None
        deduped[(item.project_id, item.work_id)] = item
    return list(deduped.values())


def apply_review_feedback(store: StateStore, feedback_path: Path) -> ReviewApplySummary:
    path = validate_feedback_path(store.config, Path(feedback_path))
    try:
        limit = store.config.registry.limits.max_packet_bytes
        if path.stat().st_size > limit:
            raise TracefolioError("feedback_budget")
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        raise TracefolioError("invalid_feedback") from None

    items = _parse_feedback_items(raw_text)
    grouped: dict[str, list[ReviewFeedbackItem]] = defaultdict(list)
    for item in items:
        grouped[item.project_id].append(item)

    routes = {p.project_id: p for p in store.config.registry.projects}
    project_results: list[ReviewApplyProjectResult] = []
    skipped: list[SkippedReviewItem] = []
    total_updated = 0
    total_unchanged = 0

    for project_id in sorted(grouped):
        project_items = grouped[project_id]
        route = routes.get(project_id)
        if route is None:
            for item in project_items:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="project_unknown", verdict=item.verdict,
                ))
            project_results.append(ReviewApplyProjectResult(
                project_id=project_id, status="skipped",
            ))
            continue
        if not route.enabled:
            for item in project_items:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="project_disabled", verdict=item.verdict,
                ))
            project_results.append(ReviewApplyProjectResult(
                project_id=project_id, status="skipped",
            ))
            continue

        ledger = store.read_state(project_id)
        generation = store.current(project_id, ledger)
        if generation is None:
            for item in project_items:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="no_generation", verdict=item.verdict,
                ))
            project_results.append(ReviewApplyProjectResult(
                project_id=project_id, status="skipped",
            ))
            continue

        if ledger.route_fingerprint not in valid_project_fingerprints(store.config, route):
            for item in project_items:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="route_changed", verdict=item.verdict,
                ))
            project_results.append(ReviewApplyProjectResult(
                project_id=project_id, status="skipped",
                generation_id=generation.generation_id,
                project_review_status=generation.project.review_status,
            ))
            continue

        works_by_id = {w.work_id: w.model_copy(deep=True) for w in generation.works}
        updated_ids: list[str] = []
        unchanged_ids: list[str] = []

        for item in project_items:
            if item.verdict != "correct":
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="verdict_not_correct", verdict=item.verdict,
                ))
                continue
            work = works_by_id.get(item.work_id)
            if work is None:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="work_not_found", verdict=item.verdict,
                ))
                continue
            if work.work_id in ledger.invalidated_work_ids:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="work_invalidated", verdict=item.verdict,
                ))
                continue
            if item.record_revision is not None and item.record_revision != work.record_revision:
                skipped.append(SkippedReviewItem(
                    project_id=item.project_id, work_id=item.work_id,
                    reason="revision_mismatch", verdict=item.verdict,
                ))
                continue
            if work.review_status == "user_reviewed":
                unchanged_ids.append(work.work_id)
                continue
            work.review_status = "user_reviewed"
            updated_ids.append(work.work_id)

        all_reviewed = bool(works_by_id) and all(
            w.review_status == "user_reviewed" for w in works_by_id.values()
        )
        next_project_review = "user_reviewed" if all_reviewed else "unreviewed"
        project_changed = generation.project.review_status != next_project_review

        total_updated += len(updated_ids)
        total_unchanged += len(unchanged_ids)

        if not updated_ids and not project_changed:
            project_results.append(ReviewApplyProjectResult(
                project_id=project_id,
                status="noop",
                generation_id=generation.generation_id,
                unchanged_work_ids=sorted(unchanged_ids),
                project_review_status=generation.project.review_status,
            ))
            continue

        next_project = generation.project.model_copy(
            update={"review_status": next_project_review}
        )
        pub = store.publish(
            project_id,
            generation.generation_id,
            next_project,
            [works_by_id[key] for key in sorted(works_by_id)],
            ledger,
        )
        project_results.append(ReviewApplyProjectResult(
            project_id=project_id,
            status=pub.status,
            generation_id=pub.generation_id,
            updated_work_ids=sorted(updated_ids),
            unchanged_work_ids=sorted(unchanged_ids),
            project_review_status=next_project_review,
        ))

    return ReviewApplySummary(
        processed_item_count=len(items),
        updated_work_count=total_updated,
        unchanged_work_count=total_unchanged,
        skipped_count=len(skipped),
        projects=project_results,
        skipped=skipped,
    )
