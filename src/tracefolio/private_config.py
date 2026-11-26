import hashlib
import json
import os
import re
import subprocess
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
    local_state_checkout: Path | None = field(default=None, repr=False)
    github_user_id: int | None = field(default=None, repr=False)


def reject_links(path: Path) -> None:
    """Check existing ancestors before resolve() can hide a redirect."""
    for current in [path, *path.parents]:
        if current.is_symlink() or (current.exists() and (
                getattr(current, 'is_junction', lambda: False)() or
                getattr(current.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise TracefolioError('unsafe_root')


def git_output(checkout: Path, *args) -> bytes:
    try:
        result = subprocess.run(['git', '-C', str(checkout), *args], capture_output=True,
                                timeout=15, check=False)
        if result.returncode:
            raise TracefolioError('unsafe_root')
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise TracefolioError('unsafe_root') from None


def validate_ignored_path(checkout: Path, path: Path) -> None:
    reject_links(path)
    relative = path.relative_to(checkout).as_posix()
    probe = relative + '/' if not path.suffix else relative
    git_output(checkout, 'check-ignore', '--no-index', '--quiet', '--', probe)
    if git_output(checkout, 'ls-files', '--cached', '-z', '--', relative):
        raise TracefolioError('unsafe_root')


def validate_state_location(state: Path, checkout: Path | None) -> None:
    if checkout is None:
        return
    reject_links(checkout)
    reject_links(state)
    top = Path(git_output(checkout, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if top != checkout.resolve() or state != checkout / '.tracefolio-local' / 'state':
        raise TracefolioError('unsafe_root')
    validate_ignored_path(checkout, checkout / '.tracefolio-local')


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


def _optional_positive_int(name: str | None) -> int | None:
    if name is None:
        return None
    raw = _binding(name).strip()
    if not raw.isdigit():
        raise TracefolioError("invalid_binding")
    value = int(raw)
    if value <= 0:
        raise TracefolioError("invalid_binding")
    return value


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
    raw_state = Path(_binding(registry.state_dir_env))
    reject_links(raw_state)
    state = _absolute(str(raw_state))
    local_checkout = None
    if os.environ.get('TRACEFOLIO_LOCAL_STATE_CHECKOUT'):
        raw_checkout = Path(_binding('TRACEFOLIO_LOCAL_STATE_CHECKOUT'))
        reject_links(raw_checkout)
        local_checkout = _absolute(str(raw_checkout))
        validate_state_location(state, local_checkout)
    checkout = Path(__file__).resolve().parents[2]
    if ((state.is_relative_to(checkout) and local_checkout is None)
            or any(p.is_relative_to(checkout) or
                   (local_checkout and p.is_relative_to(local_checkout)) for p in paths)):
        raise TracefolioError("unsafe_root")
    locators = {}
    private_values = [str(state), *(str(p) for p in paths)]
    identities = _array(registry.owner.author_emails_env)
    github_user_id = _optional_positive_int(registry.owner.github_user_id_env)
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
    epoch = "redaction-v2-" + hashlib.sha256(
        json.dumps(sorted(extra_redactions)).encode()).hexdigest()
    policy = PrivacyPolicy(tuple(v for v in private_values if v), epoch)
    return ResolvedConfig(registry, state, paths, locators, policy,
                          frozenset(v.casefold() for v in identities), local_checkout,
                          github_user_id)


def validate_export_path(config: ResolvedConfig, path: Path) -> Path:
    target = _absolute(str(path))
    checkout = Path(__file__).resolve().parents[2]
    roots = [Path(value) for key, value in config.locators.items()
             if any(p.project_id == key[0] and any(s.source_id == key[1] and
                    s.kind != "github_public" for s in p.sources)
                    for p in config.registry.projects)]
    reject_links(Path(path))
    if config.local_state_checkout:
        validate_state_location(config.state_root, config.local_state_checkout)
    inside_exports = target.is_relative_to(config.state_root / 'exports')
    if ((target.is_relative_to(checkout) and not inside_exports)
            or any(target.is_relative_to(r) for r in roots)):
        raise TracefolioError("unsafe_export")
    return target


def validate_proposal_path(config: ResolvedConfig, path: Path) -> Path:
    reject_links(path)
    target = _absolute(str(path))
    if target.is_relative_to(config.state_root / 'proposals'):
        validate_state_location(config.state_root, config.local_state_checkout)
        return target
    return validate_export_path(config, path)


def validate_feedback_path(config: ResolvedConfig, path: Path) -> Path:
    reject_links(path)
    target = _absolute(str(path))
    for subdir in ('feedback', 'exports'):
        if target.is_relative_to(config.state_root / subdir):
            validate_state_location(config.state_root, config.local_state_checkout)
            return target
    return validate_export_path(config, path)

