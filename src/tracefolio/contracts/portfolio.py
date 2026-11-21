from typing import Literal

from pydantic import Field

from .common import Artifact, Coverage, Id, StrictModel, Utc, utc_now
from .knowledge import ProjectRecord, WorkRecord


class SafeError(StrictModel):
    code: str


class ActivityWindow(StrictModel):
    first_activity_at: Utc | None = None
    last_activity_at: Utc | None = None
    sources: list[Literal[
        "git_commit_dates", "github_repo_and_head_dates", "filesystem_mtime"
    ]] = Field(default_factory=list)


class PortfolioEntry(StrictModel):
    project_id: Id
    status: Literal["ready", "partial", "pending", "error", "disabled"]
    generation_id: Id | None = None
    project: ProjectRecord | None = None
    works: list[WorkRecord] = Field(default_factory=list)
    activity_window: ActivityWindow | None = None
    coverage: Coverage | None = None
    errors: list[SafeError] = Field(default_factory=list)


class PortfolioSnapshot(Artifact):
    generated_at: Utc = Field(default_factory=utc_now)
    configured_project_count: int = Field(ge=0)
    entries: list[PortfolioEntry]

