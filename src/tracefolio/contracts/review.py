from typing import Literal

from pydantic import Field

from .common import Id, StrictModel


class ReviewFeedbackItem(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: Id
    work_id: Id
    record_revision: int | None = Field(default=None, ge=1)
    verdict: Literal["correct", "not_my_work", "misunderstood", "needs_edit", "unreviewed"]
    is_correct: bool | None = None
    ownership_confirmed: bool | None = None
    reason: str = ""
    title: str | None = None
    original_contribution: str | None = None
    original_review_status: str | None = None
    reviewed_at: str | None = None
    audit_assessment: str | None = None
    risk_bucket: str | None = None


class SkippedReviewItem(StrictModel):
    project_id: Id
    work_id: Id
    reason: Literal[
        "project_unknown",
        "project_disabled",
        "no_generation",
        "route_changed",
        "work_not_found",
        "work_invalidated",
        "revision_mismatch",
        "verdict_not_correct",
    ]
    verdict: str


class ReviewApplyProjectResult(StrictModel):
    project_id: Id
    status: Literal["published", "noop", "skipped"]
    generation_id: Id | None = None
    updated_work_ids: list[Id] = Field(default_factory=list)
    unchanged_work_ids: list[Id] = Field(default_factory=list)
    project_review_status: Literal["unreviewed", "user_reviewed"] | None = None


class ReviewApplySummary(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    processed_item_count: int = Field(ge=0)
    updated_work_count: int = Field(ge=0)
    unchanged_work_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    projects: list[ReviewApplyProjectResult] = Field(default_factory=list)
    skipped: list[SkippedReviewItem] = Field(default_factory=list)
