import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .contracts.routes import RouteRegistry
from .errors import TracefolioError
from .privacy import PrivacyPolicy

REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")


@dataclass(repr=False)
class ResolvedConfig:
    registry: RouteRegistry
    state_root: Path
    config_paths: list[Path]
    locators: dict[tuple[str, str], str] = field(repr=False)
    policy: PrivacyPolicy = field(repr=False)
    author_emails: frozenset[str] = field(default_factory=frozenset, repr=False)


def _binding(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise TracefolioError("missing_binding")
    return value


def _array(name: str | None, *, min_length=0) -> list[str]:
    if name is None:
        return []
    try:
        values = json.loads(_binding(name))
        if not isinstance(values, list) or any(
                not isinstance(v, str) or len(v) < min_length for v in values):
            raise ValueError
        return values
    except (ValueError, TypeError):
        raise TracefolioError("invalid_binding") from None


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise TracefolioError("unsafe_root")
    try:
        path = path.resolve()
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise TracefolioError("unsafe_root")
        return path
    except OSError:
        raise TracefolioError("unsafe_root") from None


def selected_config_paths() -> list[Path]:
    single = os.environ.get("TRACEFOLIO_CONFIG")
    multiple = os.environ.get("TRACEFOLIO_CONFIG_FILES")
    if bool(single) == bool(multiple):
        raise TracefolioError("config_selection")
    if single:
        return [_absolute(single)]
    values = _array("TRACEFOLIO_CONFIG_FILES")
    if not values or len(values) != len(set(values)):
        raise TracefolioError("config_selection")
    return [_absolute(v) for v in values]


def load_config(config_path: Path) -> ResolvedConfig:
    return load_config_set([config_path])


def load_config_set(config_paths: list[Path]) -> ResolvedConfig:
    if not config_paths:
        raise TracefolioError("config_selection")
    registries = []
    paths = []
    for path in config_paths:
        path = _absolute(str(path))
        try:
            registries.append(RouteRegistry.model_validate_json(path.read_text(encoding="utf-8")))
            paths.append(path)
        except (OSError, ValidationError):
            raise TracefolioError("invalid_config") from None
    first = registries[0]
    settings = first.model_dump(exclude={"projects"})
    projects = []
    seen = set()
    for registry in registries:
        if registry.model_dump(exclude={"projects"}) != settings:
            raise TracefolioError("settings_conflict")
        for project in registry.projects:
            if project.project_id in seen:
                raise TracefolioError("duplicate_project")
            seen.add(project.project_id)
            projects.append(project)
    registry = first.model_copy(update={"projects": projects})
    state = _absolute(_binding(registry.state_dir_env))
    checkout = Path(__file__).resolve().parents[2]
    if state.is_relative_to(checkout) or any(p.is_relative_to(checkout) for p in paths):
        raise TracefolioError("unsafe_root")
    locators = {}
    private_values = [str(state), *(str(p) for p in paths)]
    identities = _array(registry.owner.author_emails_env)
    private_values.extend(identities)
    extra_redactions = _array(registry.privacy.redact_values_env, min_length=4)
    private_values.extend(extra_redactions)
    source_roots = []
    for project in projects:
        if not project.enabled:
            continue
        for source in project.sources:
            if source.kind == "github_public":
                value = _binding(source.repository_env)
                if not REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
                    raise TracefolioError("invalid_repository")
            else:
                root = _absolute(_binding(source.root_env))
                if not root.is_dir() or root.is_relative_to(checkout):
                    raise TracefolioError("unsafe_root")
                source_roots.append(root)
                value = str(root)
            locators[(project.project_id, source.source_id)] = value
            private_values.extend((value, value.replace("\\", "/"), value.replace("/", "\\")))
    for root in source_roots:
        if (state.is_relative_to(root) or root.is_relative_to(state)
                or any(p.is_relative_to(root) for p in paths)):
            raise TracefolioError("unsafe_root")
    epoch = "redaction-v1-" + hashlib.sha256(
        json.dumps(sorted(extra_redactions)).encode()).hexdigest()
    policy = PrivacyPolicy(tuple(v for v in private_values if v), epoch)
    return ResolvedConfig(registry, state, paths, locators, policy,
                          frozenset(v.casefold() for v in identities))


def validate_export_path(config: ResolvedConfig, path: Path) -> Path:
    target = _absolute(str(path))
    checkout = Path(__file__).resolve().parents[2]
    roots = [Path(value) for key, value in config.locators.items()
             if any(p.project_id == key[0] and any(s.source_id == key[1] and
                    s.kind != "github_public" for s in p.sources)
                    for p in config.registry.projects)]
    if target.is_relative_to(checkout) or any(target.is_relative_to(r) for r in roots):
        raise TracefolioError("unsafe_export")
    return target

