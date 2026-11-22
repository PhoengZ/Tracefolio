from typing import Literal

from pydantic import Field, model_validator

from .common import Artifact, EnvRef, Id, StrictModel


class Limits(StrictModel):
    max_commits_per_run: int = Field(default=200, ge=1, le=10000)
    max_diff_bytes_per_commit: int = Field(default=524288, ge=1, le=16777216)
    max_text_file_bytes: int = Field(default=131072, ge=1, le=16777216)
    max_notebook_bytes: int = Field(default=8388608, ge=1, le=33554432)
    max_packet_bytes: int = Field(default=2097152, ge=512, le=33554432)
    max_files_per_source: int = Field(default=2000, ge=1, le=100000)
    max_total_source_bytes: int = Field(default=16777216, ge=1, le=134217728)


class Owner(StrictModel):
    author_emails_env: EnvRef | None = None
    github_user_id_env: EnvRef | None = None


class Privacy(StrictModel):
    redact_values_env: EnvRef | None = None


class WorkUnit(StrictModel):
    work_id: Id
    context: str = Field(min_length=1)
    include_paths: list[str] = Field(min_length=1)


class SourceRoute(StrictModel):
    source_id: Id
    kind: Literal["local_git", "local_directory", "github_public"]
    collection_mode: Literal["commits", "snapshot"] = "commits"
    root_env: EnvRef | None = None
    repository_env: EnvRef | None = None
    ref: str | None = None
    include_paths: list[str] = Field(default_factory=lambda: ["README.md"])
    exclude_paths: list[str] = Field(default_factory=list)
    work_units: list[WorkUnit] = Field(default_factory=list)

    @model_validator(mode="after")
    def binding_shape(self):
        if self.kind == "github_public":
            if not self.repository_env or self.root_env:
                raise ValueError("GitHub requires only a repository environment reference")
        elif not self.root_env or self.repository_env:
            raise ValueError("Local sources require only a root environment reference")
        if self.kind == "local_directory" and self.ref is not None:
            raise ValueError("A directory snapshot has no Git ref")
        if self.ref and (self.ref.startswith("-") or any(c in self.ref for c in "\r\n\0")):
            raise ValueError("Unsafe ref")
        ids = [w.work_id for w in self.work_units]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate work unit")
        for pattern in self.include_paths + self.exclude_paths + [
            g for w in self.work_units for g in w.include_paths
        ]:
            if (not pattern or pattern.startswith(("/", "\\")) or "\\" in pattern
                    or ":" in pattern or ".." in pattern.split("/") or "\0" in pattern):
                raise ValueError("Patterns must be safe relative POSIX paths")
        return self


class ProjectRoute(StrictModel):
    project_id: Id
    title: str = Field(min_length=1)
    context: str = Field(min_length=1)
    enabled: bool = True
    sources: list[SourceRoute] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self):
        ids = [s.source_id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source")
        contexts = {}
        for source in self.sources:
            for work in source.work_units:
                if work.work_id in contexts and contexts[work.work_id] != work.context:
                    raise ValueError("Conflicting shared work context")
                contexts[work.work_id] = work.context
        return self


class RouteRegistry(Artifact):
    state_dir_env: EnvRef = "TRACEFOLIO_STATE_DIR"
    owner: Owner = Field(default_factory=Owner)
    privacy: Privacy = Field(default_factory=Privacy)
    limits: Limits = Field(default_factory=Limits)
    projects: list[ProjectRoute]

    @model_validator(mode="after")
    def unique_projects(self):
        ids = [p.project_id for p in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate project")
        return self

