from typing import Literal

from pydantic import Field

from .common import Artifact, Id, StrictModel, Utc, utc_now

ComponentVerdict = Literal[
    "self_only",
    "shared_with_others",
    "other_only",
    "unmatched_local_only",
    "merge_context_only",
    "unverifiable_non_git",
    "unverified",
]

SourceAssessment = Literal[
    "self_only",
    "mixed_or_shared",
    "other_only",
    "unmatched_local_only",
    "merge_context_only",
    "unverifiable_non_git",
    "unverified",
    "unavailable",
]

SupportVerdict = Literal[
    "self_supported",
    "shared_supported",
    "minor_touch_only",
    "other_authored_only",
    "unmatched_local_only",
    "merge_context_only",
    "unverifiable_non_git",
    "unverified",
]


class AuthorshipIdentitySummary(StrictModel):
    github_authenticated: bool = False
    github_user_id_configured: bool = False
    configured_email_count: int = Field(default=0, ge=0)
    derived_email_count: int = Field(default=0, ge=0)
    effective_email_count: int = Field(default=0, ge=0)
    match_methods: list[str] = Field(default_factory=list)


class ComponentAuthorship(StrictModel):
    component: str
    verdict: ComponentVerdict
    self_commits: int = Field(default=0, ge=0)
    other_or_unmatched_commits: int = Field(default=0, ge=0)
    merge_context_commits: int = Field(default=0, ge=0)
    human_coauthored_commits: int = Field(default=0, ge=0)
    ai_assisted_commits: int = Field(default=0, ge=0)
    self_touched_files_count: int = Field(default=0, ge=0)
    total_head_files_count: int = Field(default=0, ge=0)
    self_lines_added: int | None = None
    self_lines_deleted: int | None = None
    other_lines_added: int | None = None
    other_lines_deleted: int | None = None
    sample_self_subjects: list[str] = Field(default_factory=list)


class SourceAuthorshipAudit(StrictModel):
    source_id: Id
    kind: Literal["local_git", "local_directory", "github_public"]
    pinned_revision: str | None = None
    assessment: SourceAssessment
    self_commits: int = Field(default=0, ge=0)
    other_or_unmatched_commits: int = Field(default=0, ge=0)
    merge_context_commits: int = Field(default=0, ge=0)
    unmatched_local_author_count: int = Field(default=0, ge=0)
    components: list[ComponentAuthorship] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ClaimAuthorshipAudit(StrictModel):
    claim_id: Id
    statement: str
    support_verdict: SupportVerdict
    self_share: float | None = Field(default=None, ge=0.0, le=1.0)
    commit_support_counts: dict[str, int] = Field(default_factory=dict)
    path_support_counts: dict[str, int] = Field(default_factory=dict)


class WorkAuthorshipAudit(StrictModel):
    work_id: Id
    title: str
    assessment: SupportVerdict
    self_share: float | None = Field(default=None, ge=0.0, le=1.0)
    referenced_commit_counts: dict[str, int] = Field(default_factory=dict)
    referenced_path_counts: dict[str, int] = Field(default_factory=dict)
    referenced_components: list[str] = Field(default_factory=list)
    claims: list[ClaimAuthorshipAudit] = Field(default_factory=list)
    semantic_contribution_status_unchanged: Literal[
        "self_authored", "collaborative", "unverified"
    ] = "unverified"


class ProjectAuthorshipAudit(StrictModel):
    project_id: Id
    status: Literal["audited", "disabled", "pending", "error"]
    assessment: Literal[
        "self_only",
        "mixed_or_shared",
        "other_only",
        "unmatched_local_only",
        "merge_context_only",
        "unverifiable_non_git",
        "unverified",
        "disabled",
        "unavailable",
    ]
    sources: list[SourceAuthorshipAudit] = Field(default_factory=list)
    works: list[WorkAuthorshipAudit] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AuthorshipAuditReport(Artifact):
    generated_at: Utc = Field(default_factory=utc_now)
    scope: str
    identity: AuthorshipIdentitySummary
    limitations: list[str] = Field(default_factory=list)
    configured_project_count: int = Field(ge=0)
    projects: list[ProjectAuthorshipAudit] = Field(default_factory=list)
