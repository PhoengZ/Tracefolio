"""Two-layer authorship attribution engine and read-only audit workflow."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .contracts.audit import (
    AuthorshipAuditReport,
    AuthorshipIdentitySummary,
    ClaimAuthorshipAudit,
    ComponentAuthorship,
    ProjectAuthorshipAudit,
    SourceAuthorshipAudit,
    WorkAuthorshipAudit,
)
from .errors import TracefolioError
from .privacy import path_allowed, sanitize_text
from .private_config import validate_export_path
from .state import StateStore, effective_works, work_evidence_refs

_COAUTHOR_RE = re.compile(
    r"^\s*Co-authored-by:\s*(.+?)\s*<([^>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_AI_EMAIL_PATTERNS = (
    re.compile(r"(?:^|@)(?:noreply\.)?(?:anthropic|openai)\.com$", re.IGNORECASE),
    re.compile(r"^copilot@github\.com$", re.IGNORECASE),
    re.compile(
        r"(?:copilot|claude|codex|gemini|cursor|devin|swe-agent).*@users\.noreply\.github\.com$",
        re.IGNORECASE,
    ),
    re.compile(r"\[bot\]@users\.noreply\.github\.com$", re.IGNORECASE),
    re.compile(r"^gemini(?:[-_.a-z0-9]*)@google\.com$", re.IGNORECASE),
)

_AI_NAME_PATTERNS = (
    re.compile(
        r"\b(?:claude\s+(?:code|ai)|anthropic\s+claude|openai\s+codex|codex\s+cli|"
        r"gemini\s+(?:cli|code\s+assist)|google\s+gemini|github\s+copilot)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\[bot\]$", re.IGNORECASE),
)


def component_key(rel_path: str | None) -> str:
    if not rel_path:
        return "(root)"
    clean = rel_path.strip("/")
    return clean.split("/", 1)[0] if "/" in clean else clean


def _is_ai_assistant_trailer(name: str, email: str) -> bool:
    clean_name = name.strip()
    clean_email = email.strip()
    if any(pattern.search(clean_email) for pattern in _AI_EMAIL_PATTERNS):
        return True
    return any(pattern.search(clean_name) for pattern in _AI_NAME_PATTERNS)


def parse_coauthor_trailers(message: str, owner_emails: frozenset[str]) -> tuple[bool, bool]:
    """Return (has_human_coauthor, has_ai_tool_trailer).

    Recognized AI-assistant trailers represent tool usage rather than human collaboration.
    """
    has_human = False
    has_ai = False
    for match in _COAUTHOR_RE.finditer(message or ""):
        name, email = match.group(1).strip(), match.group(2).strip()
        if _is_ai_assistant_trailer(name, email):
            has_ai = True
            continue
        if email.casefold() not in owner_emails:
            has_human = True
    return has_human, has_ai


def classify_commit_attribution(
    email: str,
    *,
    parent_count: int,
    message: str,
    owner_emails: frozenset[str],
    github_author_id: int | None = None,
    owner_github_id: int | None = None,
) -> tuple[str, bool, bool]:
    """Classify a single commit into EvidenceRecord attribution.

    Returns (attribution, has_human_coauthor, has_ai_tool_trailer) where attribution is
    one of: "self", "shared", "other", "unverified".
    """
    has_human, has_ai = parse_coauthor_trailers(message, owner_emails)
    if parent_count > 1:
        return "unverified", has_human, has_ai

    clean_email = (email or "").strip().casefold()
    if owner_github_id is not None and github_author_id is not None:
        is_owner = github_author_id == owner_github_id
        identity_known = True
    elif owner_emails:
        is_owner = bool(clean_email and clean_email in owner_emails)
        identity_known = True
    else:
        is_owner = False
        identity_known = False

    if not identity_known:
        return "unverified", has_human, has_ai
    if is_owner:
        return ("shared" if has_human else "self"), has_human, has_ai
    return "other", has_human, has_ai


def _audit_commit_label(
    attribution: str,
    *,
    is_merge: bool,
    source_kind: str,
) -> str:
    if is_merge:
        return "merge_context"
    if attribution == "self":
        return "self"
    if attribution == "shared":
        return "shared"
    if attribution == "other":
        return "other_account" if source_kind == "github_public" else "unmatched_local_author"
    return "unverified"


@dataclass
class CommitMeta:
    sha: str
    attribution: str
    audit_label: str
    is_merge: bool
    has_human_coauthor: bool
    has_ai_tool: bool
    subject: str


@dataclass
class PathStats:
    self_commits: int = 0
    other_commits: int = 0
    merge_commits: int = 0
    human_coauthored_commits: int = 0
    ai_assisted_commits: int = 0
    self_lines_added: int = 0
    self_lines_deleted: int = 0
    other_lines_added: int = 0
    other_lines_deleted: int = 0


@dataclass
class SourceAuthorshipIndex:
    source_id: str
    kind: str
    pinned_revision: str | None
    path_attributions: dict[str, str] = field(default_factory=dict)
    path_audit_labels: dict[str, str] = field(default_factory=dict)
    commit_meta: dict[str, CommitMeta] = field(default_factory=dict)
    components: list[ComponentAuthorship] = field(default_factory=list)
    derived_owner_emails: set[str] = field(default_factory=set)
    self_commits: int = 0
    other_or_unmatched_commits: int = 0
    merge_context_commits: int = 0
    unmatched_local_author_count: int = 0
    limitations: list[str] = field(default_factory=list)

    def attribution_for_path(self, rel_path: str | None) -> str:
        if not rel_path:
            return "unverified"
        return self.path_attributions.get(rel_path, "unverified")

    def audit_label_for_path(self, rel_path: str | None) -> str:
        if not rel_path:
            return "unverified"
        if self.kind == "local_directory":
            return "unverifiable_non_git"
        return self.path_audit_labels.get(rel_path, "unverified")


def _component_verdict(
    *,
    kind: str,
    self_commits: int,
    other_commits: int,
    merge_commits: int,
    human_coauthored: int,
    has_identity: bool,
) -> str:
    if kind == "local_directory":
        return "unverifiable_non_git"
    if not has_identity:
        return "unverified"
    if self_commits > 0 and other_commits == 0 and human_coauthored == 0:
        return "self_only"
    if self_commits > 0:
        return "shared_with_others"
    if other_commits > 0:
        return "other_only" if kind == "github_public" else "unmatched_local_only"
    if merge_commits > 0:
        return "merge_context_only"
    return "unverified"


def _source_assessment(index: SourceAuthorshipIndex, *, has_identity: bool) -> str:
    if index.kind == "local_directory":
        return "unverifiable_non_git"
    if not has_identity:
        return "unverified"
    has_human_co = any(c.human_coauthored_commits > 0 for c in index.components)
    if index.self_commits > 0 and index.other_or_unmatched_commits == 0 and not has_human_co:
        return "self_only"
    if index.self_commits > 0:
        return "mixed_or_shared"
    if index.other_or_unmatched_commits > 0:
        return "other_only" if index.kind == "github_public" else "unmatched_local_only"
    if index.merge_context_commits > 0:
        return "merge_context_only"
    return "unverified"


def _safe_subject(message: str, policy) -> str:
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    if not first:
        return ""
    return sanitize_text(first[:160], policy).text


def index_local_git_paths(
    req,
    revision: str,
    git_fn,
    *,
    owner_emails_override: frozenset[str] | None = None,
    tree_paths: list[str] | None = None,
) -> SourceAuthorshipIndex:
    """Build a complete per-path and per-component authorship index in a single git log pass."""
    owner_emails = req.owner_emails if owner_emails_override is None else owner_emails_override
    index = SourceAuthorshipIndex(
        source_id=req.source.source_id,
        kind="local_git",
        pinned_revision=revision,
    )
    if tree_paths is None:
        raw_tree = git_fn(req, "ls-tree", "-r", "-z", revision, optional=True)
        scoped_tree: list[str] = []
        if raw_tree:
            for entry in raw_tree.decode("utf-8", "replace").split("\0"):
                if not entry or "\t" not in entry:
                    continue
                _, path = entry.split("\t", 1)
                if path_allowed(path, req.source.include_paths, req.source.exclude_paths):
                    scoped_tree.append(path)
        tree_paths = sorted(set(scoped_tree))

    raw_log = git_fn(
        req,
        "log",
        "--numstat",
        "--no-renames",
        "--format=%x1e%H%x1f%ae%x1f%P%x1f%B%x1d",
        revision,
        optional=True,
    )
    if raw_log is None:
        index.limitations.append("git_history_unavailable")
        return index

    path_stats: dict[str, PathStats] = defaultdict(PathStats)
    comp_commits: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"self": set(), "other": set(), "merge": set(), "human_co": set(), "ai": set()}
    )
    comp_self_files: dict[str, set[str]] = defaultdict(set)
    comp_subjects: dict[str, list[str]] = defaultdict(list)
    comp_lines: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    unmatched_emails: set[str] = set()

    decoded = raw_log.decode("utf-8", "replace")
    for chunk in decoded.split("\x1e"):
        if not chunk.strip() or "\x1d" not in chunk:
            continue
        header, stats_block = chunk.split("\x1d", 1)
        fields = header.split("\x1f", 3)
        if len(fields) < 4:
            continue
        sha, email, parents_line, message = fields
        sha = sha.strip()
        email_clean = email.strip().casefold()
        parents = parents_line.split()
        is_merge = len(parents) > 1
        attr, has_human, has_ai = classify_commit_attribution(
            email_clean,
            parent_count=len(parents),
            message=message,
            owner_emails=owner_emails,
        )
        label = _audit_commit_label(attr, is_merge=is_merge, source_kind="local_git")
        subject = _safe_subject(message, req.policy)
        index.commit_meta[sha] = CommitMeta(
            sha=sha,
            attribution=attr,
            audit_label=label,
            is_merge=is_merge,
            has_human_coauthor=has_human,
            has_ai_tool=has_ai,
            subject=subject,
        )
        if is_merge:
            index.merge_context_commits += 1
            continue
        if attr in {"self", "shared"}:
            index.self_commits += 1
        elif attr == "other":
            index.other_or_unmatched_commits += 1
            if email_clean:
                unmatched_emails.add(email_clean)

        touched_components: set[str] = set()
        for line in stats_block.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added_s, deleted_s, rel_path = parts
            if not path_allowed(rel_path, req.source.include_paths, req.source.exclude_paths):
                continue
            added = int(added_s) if added_s.isdigit() else 0
            deleted = int(deleted_s) if deleted_s.isdigit() else 0
            ps = path_stats[rel_path]
            comp = component_key(rel_path)
            touched_components.add(comp)
            if attr in {"self", "shared"}:
                ps.self_commits += 1
                ps.self_lines_added += added
                ps.self_lines_deleted += deleted
                comp_self_files[comp].add(rel_path)
                comp_lines[comp][0] += added
                comp_lines[comp][1] += deleted
                if has_human:
                    ps.human_coauthored_commits += 1
                if has_ai:
                    ps.ai_assisted_commits += 1
            elif attr == "other":
                ps.other_commits += 1
                ps.other_lines_added += added
                ps.other_lines_deleted += deleted
                comp_lines[comp][2] += added
                comp_lines[comp][3] += deleted

        for comp in touched_components:
            bucket = comp_commits[comp]
            if attr in {"self", "shared"}:
                bucket["self"].add(sha)
                if has_human:
                    bucket["human_co"].add(sha)
                if has_ai:
                    bucket["ai"].add(sha)
                if subject and subject not in comp_subjects[comp] and len(comp_subjects[comp]) < 3:
                    comp_subjects[comp].append(subject)
            elif attr == "other":
                bucket["other"].add(sha)

    index.unmatched_local_author_count = len(unmatched_emails)
    has_identity = bool(owner_emails)
    all_paths = sorted(set(tree_paths) | set(path_stats.keys()))
    for rel_path in all_paths:
        ps = path_stats.get(rel_path)
        if not has_identity or ps is None:
            index.path_attributions[rel_path] = "unverified"
            index.path_audit_labels[rel_path] = "unverified"
            continue
        if ps.self_commits > 0 and ps.other_commits == 0 and ps.human_coauthored_commits == 0:
            index.path_attributions[rel_path] = "self"
            index.path_audit_labels[rel_path] = "self"
        elif ps.self_commits > 0:
            index.path_attributions[rel_path] = "shared"
            index.path_audit_labels[rel_path] = "shared"
        elif ps.other_commits > 0:
            index.path_attributions[rel_path] = "other"
            index.path_audit_labels[rel_path] = "unmatched_local_author"
        else:
            index.path_attributions[rel_path] = "unverified"
            index.path_audit_labels[rel_path] = "unverified"

    head_by_comp: dict[str, set[str]] = defaultdict(set)
    for rel_path in tree_paths:
        head_by_comp[component_key(rel_path)].add(rel_path)

    all_comps = sorted(set(head_by_comp.keys()) | set(comp_commits.keys()))
    for comp in all_comps:
        bucket = comp_commits[comp]
        lines = comp_lines[comp]
        self_c = len(bucket["self"])
        other_c = len(bucket["other"])
        merge_c = len(bucket["merge"])
        human_co = len(bucket["human_co"])
        ai_c = len(bucket["ai"])
        head_files = head_by_comp.get(comp, set())
        touched_in_head = len(comp_self_files.get(comp, set()) & head_files) if head_files else len(
            comp_self_files.get(comp, set())
        )
        verdict = _component_verdict(
            kind="local_git",
            self_commits=self_c,
            other_commits=other_c,
            merge_commits=merge_c,
            human_coauthored=human_co,
            has_identity=has_identity,
        )
        index.components.append(
            ComponentAuthorship(
                component=sanitize_text(comp, req.policy).text,
                verdict=verdict,
                self_commits=self_c,
                other_or_unmatched_commits=other_c,
                merge_context_commits=merge_c,
                human_coauthored_commits=human_co,
                ai_assisted_commits=ai_c,
                self_touched_files_count=touched_in_head,
                total_head_files_count=len(head_files),
                self_lines_added=lines[0],
                self_lines_deleted=lines[1],
                other_lines_added=lines[2],
                other_lines_deleted=lines[3],
                sample_self_subjects=comp_subjects[comp],
            )
        )
    return index


def _record_github_commit(
    item: dict,
    *,
    owner_emails: frozenset[str],
    owner_github_id: int | None,
    policy,
    derived_emails: set[str],
) -> CommitMeta:
    sha = item.get("sha", "")
    parents = item.get("parents") or []
    is_merge = len(parents) > 1
    commit_obj = item.get("commit") or {}
    author_obj = item.get("author") or {}
    gh_author_id = author_obj.get("id") if isinstance(author_obj, dict) else None
    email = ((commit_obj.get("author") or {}).get("email") or "").strip().casefold()
    message = commit_obj.get("message") or ""

    if not is_merge and owner_github_id is not None and gh_author_id == owner_github_id and email:
        derived_emails.add(email)

    attr, has_human, has_ai = classify_commit_attribution(
        email,
        parent_count=len(parents),
        message=message,
        owner_emails=owner_emails,
        github_author_id=gh_author_id,
        owner_github_id=owner_github_id,
    )
    label = _audit_commit_label(attr, is_merge=is_merge, source_kind="github_public")
    return CommitMeta(
        sha=sha,
        attribution=attr,
        audit_label=label,
        is_merge=is_merge,
        has_human_coauthor=has_human,
        has_ai_tool=has_ai,
        subject=_safe_subject(message, policy),
    )


def index_github_public_paths(
    provider,
    req,
    revision: str,
    tree_paths: list[str],
    *,
    owner_github_id: int | None = None,
    owner_github_login: str | None = None,
    owner_emails_override: frozenset[str] | None = None,
    max_history_pages: int = 5,
    max_component_pages: int = 2,
    max_owner_detail_commits: int = 60,
    detailed_components: bool = True,
) -> SourceAuthorshipIndex:
    """Build a fast metadata-only per-path and per-component index for a public GitHub repo."""
    owner_emails = req.owner_emails if owner_emails_override is None else owner_emails_override
    eff_github_id = req.owner_github_id if owner_github_id is None else owner_github_id
    eff_login = req.owner_github_login if owner_github_login is None else owner_github_login
    has_identity = bool(owner_emails or eff_github_id is not None)

    index = SourceAuthorshipIndex(
        source_id=req.source.source_id,
        kind="github_public",
        pinned_revision=revision,
    )
    if eff_github_id is not None and eff_login:
        index.derived_owner_emails.add(f"{eff_github_id}+{eff_login}@users.noreply.github.com".casefold())

    if not has_identity:
        for p in tree_paths:
            index.path_attributions[p] = "unverified"
            index.path_audit_labels[p] = "unverified"
        return index

    prefix, _ = provider._repository(req)
    history_bounded = False
    try:
        for page in range(1, max_history_pages + 1):
            resp = provider._get(
                req,
                prefix + "/commits",
                {"sha": revision, "per_page": "100", "page": str(page)},
            )
            items = resp.data if isinstance(resp.data, list) else []
            for item in items:
                if not isinstance(item, dict) or not item.get("sha"):
                    continue
                meta = _record_github_commit(
                    item,
                    owner_emails=owner_emails,
                    owner_github_id=eff_github_id,
                    policy=req.policy,
                    derived_emails=index.derived_owner_emails,
                )
                if meta.sha not in index.commit_meta:
                    index.commit_meta[meta.sha] = meta
                    if meta.is_merge:
                        index.merge_context_commits += 1
                    elif meta.attribution in {"self", "shared"}:
                        index.self_commits += 1
                    elif meta.attribution == "other":
                        index.other_or_unmatched_commits += 1
            has_next = 'rel="next"' in resp.headers.get("link", "")
            if not has_next or not items:
                break
            if page == max_history_pages:
                history_bounded = True
                index.limitations.append("github_history_bounded")
    except TracefolioError as error:
        index.limitations.append(error.code)
        for p in tree_paths:
            index.path_attributions[p] = "unverified"
            index.path_audit_labels[p] = "unverified"
        return index

    head_by_comp: dict[str, list[str]] = defaultdict(list)
    for rel_path in sorted(set(tree_paths)):
        head_by_comp[component_key(rel_path)].append(rel_path)

    any_human_co = any(m.has_human_coauthor for m in index.commit_meta.values() if not m.is_merge)
    solo_owner_repo = (
        not history_bounded
        and index.self_commits > 0
        and index.other_or_unmatched_commits == 0
    )
    other_only_repo = (
        not history_bounded
        and index.self_commits == 0
        and index.other_or_unmatched_commits > 0
    )

    if solo_owner_repo:
        attr_value = "shared" if any_human_co else "self"
        verdict = "shared_with_others" if any_human_co else "self_only"
        sample_subjects = []
        for meta in index.commit_meta.values():
            if (meta.attribution in {"self", "shared"} and meta.subject
                    and meta.subject not in sample_subjects and len(sample_subjects) < 3):
                sample_subjects.append(meta.subject)
        for comp, files in sorted(head_by_comp.items()):
            for f in files:
                index.path_attributions[f] = attr_value
                index.path_audit_labels[f] = attr_value
            if detailed_components:
                index.components.append(
                    ComponentAuthorship(
                        component=sanitize_text(comp, req.policy).text,
                        verdict=verdict,
                        self_commits=index.self_commits,
                        other_or_unmatched_commits=0,
                        merge_context_commits=index.merge_context_commits,
                        human_coauthored_commits=sum(
                            1 for m in index.commit_meta.values()
                            if not m.is_merge and m.has_human_coauthor
                        ),
                        ai_assisted_commits=sum(
                            1 for m in index.commit_meta.values()
                            if not m.is_merge and m.has_ai_tool
                        ),
                        self_touched_files_count=len(files),
                        total_head_files_count=len(files),
                        sample_self_subjects=sample_subjects,
                    )
                )
        return index

    if other_only_repo:
        for comp, files in sorted(head_by_comp.items()):
            for f in files:
                index.path_attributions[f] = "other"
                index.path_audit_labels[f] = "other_account"
            if detailed_components:
                index.components.append(
                    ComponentAuthorship(
                        component=sanitize_text(comp, req.policy).text,
                        verdict="other_only",
                        self_commits=0,
                        other_or_unmatched_commits=index.other_or_unmatched_commits,
                        merge_context_commits=index.merge_context_commits,
                        self_touched_files_count=0,
                        total_head_files_count=len(files),
                    )
                )
        return index

    # Mixed / multi-author or bounded-history repository: inspect per top-level component.
    comp_owner_shas: dict[str, set[str]] = defaultdict(set)
    comp_verdicts: dict[str, str] = {}
    comp_stats_map: dict[str, dict] = {}
    api_interrupted = False

    for comp, files in sorted(head_by_comp.items()):
        self_shas: set[str] = set()
        other_shas: set[str] = set()
        merge_shas: set[str] = set()
        human_co_shas: set[str] = set()
        ai_shas: set[str] = set()
        subjects: list[str] = []
        comp_bounded = False
        try:
            for page in range(1, max_component_pages + 1):
                resp = provider._get(
                    req,
                    prefix + "/commits",
                    {"sha": revision, "path": comp, "per_page": "100", "page": str(page)},
                )
                items = resp.data if isinstance(resp.data, list) else []
                for item in items:
                    if not isinstance(item, dict) or not item.get("sha"):
                        continue
                    meta = _record_github_commit(
                        item,
                        owner_emails=owner_emails,
                        owner_github_id=eff_github_id,
                        policy=req.policy,
                        derived_emails=index.derived_owner_emails,
                    )
                    index.commit_meta.setdefault(meta.sha, meta)
                    if meta.is_merge:
                        merge_shas.add(meta.sha)
                    elif meta.attribution in {"self", "shared"}:
                        self_shas.add(meta.sha)
                        if meta.has_human_coauthor:
                            human_co_shas.add(meta.sha)
                        if meta.has_ai_tool:
                            ai_shas.add(meta.sha)
                        if meta.subject and meta.subject not in subjects and len(subjects) < 3:
                            subjects.append(meta.subject)
                    elif meta.attribution == "other":
                        other_shas.add(meta.sha)
                if 'rel="next"' not in resp.headers.get("link", "") or not items:
                    break
                if page == max_component_pages:
                    comp_bounded = True
        except TracefolioError as error:
            index.limitations.append(error.code)
            api_interrupted = True
            comp_bounded = True

        if comp_bounded and not api_interrupted:
            index.limitations.append("github_component_history_bounded")

        if api_interrupted and not self_shas and not other_shas:
            verdict = "unverified"
        else:
            verdict = _component_verdict(
                kind="github_public",
                self_commits=len(self_shas),
                other_commits=len(other_shas),
                merge_commits=len(merge_shas),
                human_coauthored=len(human_co_shas),
                has_identity=has_identity,
            )
            if comp_bounded and verdict == "other_only":
                verdict = "unverified"

        comp_verdicts[comp] = verdict
        comp_owner_shas[comp] = self_shas
        comp_stats_map[comp] = {
            "self": len(self_shas),
            "other": len(other_shas),
            "merge": len(merge_shas),
            "human_co": len(human_co_shas),
            "ai": len(ai_shas),
            "subjects": subjects,
            "files": files,
            "bounded": comp_bounded,
        }

    # Resolve exact file-level owner touches only for commits inside mixed components.
    mixed_owner_shas = sorted({
        sha
        for comp, verdict in comp_verdicts.items()
        if verdict == "shared_with_others"
        for sha in comp_owner_shas[comp]
    })
    self_touched_paths: set[str] = set()
    detail_bounded = False
    if len(mixed_owner_shas) > max_owner_detail_commits:
        detail_bounded = True
        index.limitations.append("owner_commit_detail_bounded")
        mixed_owner_shas = mixed_owner_shas[:max_owner_detail_commits]

    if not api_interrupted:
        for sha in mixed_owner_shas:
            try:
                detail = provider._get(req, prefix + "/commits/" + sha, {"per_page": "100", "page": "1"})
                data = detail.data if isinstance(detail.data, dict) else {}
                for file_item in data.get("files") or []:
                    fname = file_item.get("filename") if isinstance(file_item, dict) else None
                    if fname and path_allowed(fname, req.source.include_paths, req.source.exclude_paths):
                        self_touched_paths.add(fname)
            except TracefolioError as error:
                index.limitations.append(error.code)
                detail_bounded = True
                break

    for comp, stats in sorted(comp_stats_map.items()):
        verdict = comp_verdicts[comp]
        files = stats["files"]
        touched_count = 0
        for f in files:
            if verdict == "self_only":
                index.path_attributions[f] = "self"
                index.path_audit_labels[f] = "self"
                touched_count += 1
            elif verdict == "other_only":
                index.path_attributions[f] = "other"
                index.path_audit_labels[f] = "other_account"
            elif verdict == "shared_with_others":
                if f in self_touched_paths:
                    index.path_attributions[f] = "shared"
                    index.path_audit_labels[f] = "shared"
                    touched_count += 1
                elif not detail_bounded and not stats["bounded"]:
                    index.path_attributions[f] = "other"
                    index.path_audit_labels[f] = "other_account"
                else:
                    index.path_attributions[f] = "unverified"
                    index.path_audit_labels[f] = "unverified"
            elif verdict == "merge_context_only":
                index.path_attributions[f] = "unverified"
                index.path_audit_labels[f] = "merge_context"
            else:
                index.path_attributions[f] = "unverified"
                index.path_audit_labels[f] = "unverified"

        if detailed_components:
            index.components.append(
                ComponentAuthorship(
                    component=sanitize_text(comp, req.policy).text,
                    verdict=verdict,
                    self_commits=stats["self"],
                    other_or_unmatched_commits=stats["other"],
                    merge_context_commits=stats["merge"],
                    human_coauthored_commits=stats["human_co"],
                    ai_assisted_commits=stats["ai"],
                    self_touched_files_count=touched_count,
                    total_head_files_count=len(files),
                    sample_self_subjects=stats["subjects"],
                )
            )

    index.limitations = sorted(set(index.limitations))
    return index


def _derive_support_verdict(commit_counts: Counter, path_counts: Counter) -> str:
    combined = Counter(commit_counts)
    combined.update(path_counts)
    if not combined:
        return "unverified"
    has_self = combined.get("self", 0) > 0
    has_shared = combined.get("shared", 0) > 0
    has_other = combined.get("other_account", 0) > 0
    has_unmatched = combined.get("unmatched_local_author", 0) > 0
    has_merge = combined.get("merge_context", 0) > 0
    has_non_git = combined.get("unverifiable_non_git", 0) > 0

    if has_self and not (has_shared or has_other or has_unmatched):
        return "self_supported"
    if has_self or has_shared:
        return "shared_supported"
    if has_other:
        return "other_authored_only"
    if has_unmatched:
        return "unmatched_local_only"
    if has_merge:
        return "merge_context_only"
    if has_non_git:
        return "unverifiable_non_git"
    return "unverified"


def _project_assessment(sources: list[SourceAuthorshipAudit]) -> str:
    assessments = {s.assessment for s in sources}
    if not assessments:
        return "unverified"
    if assessments == {"unverifiable_non_git"}:
        return "unverifiable_non_git"
    if assessments == {"unavailable"}:
        return "unavailable"
    has_self = any(a in {"self_only", "mixed_or_shared"} for a in assessments)
    has_other = any(a in {"other_only", "mixed_or_shared", "unmatched_local_only"} for a in assessments)
    if has_self and not has_other:
        return "self_only"
    if has_self:
        return "mixed_or_shared"
    if "other_only" in assessments:
        return "other_only"
    if "unmatched_local_only" in assessments:
        return "unmatched_local_only"
    if "merge_context_only" in assessments:
        return "merge_context_only"
    if "unverifiable_non_git" in assessments:
        return "unverifiable_non_git"
    return "unverified"


def _github_tree_paths(provider, req, revision: str) -> tuple[list[str], list[str]]:
    prefix, _ = provider._repository(req)
    tree_data = provider._get(req, prefix + "/git/trees/" + revision, {"recursive": "1"}).data
    limitations: list[str] = []
    if not isinstance(tree_data, dict) or not isinstance(tree_data.get("tree"), list):
        raise TracefolioError("github_invalid_json", exit_code=3)
    if tree_data.get("truncated"):
        limitations.append("github_tree_truncated")
    paths: list[str] = []
    for node in tree_data["tree"]:
        if not isinstance(node, dict) or node.get("type") != "blob":
            continue
        rel = node.get("path", "")
        if rel and path_allowed(rel, req.source.include_paths, req.source.exclude_paths):
            paths.append(rel)
    return sorted(set(paths)), limitations


def audit_projects(
    store: StateStore,
    registry,
    *,
    github_transport=None,
    github_user_override: tuple[int | None, str | None] | None = None,
) -> AuthorshipAuditReport:
    """Run read-only two-layer authorship audit (Layer 1 components + Layer 2 works/claims)."""
    from .sources.github_public import GhCliClient, GitHubPublicProvider
    from .sources.local_git import git as local_git_exec

    config = store.config
    has_github_sources = any(
        p.enabled and any(s.kind == "github_public" for s in p.sources)
        for p in registry.projects
    )
    report_limitations: list[str] = [
        "Repository ownership and committer identity do not establish authorship.",
        "Merge commits are recorded separately as merge_context.",
        "Recognized AI-assistant Co-authored-by trailers represent tool usage, not human collaboration.",
        "Local unmatched authors may include additional owner emails not yet linked to GitHub.",
        "Semantic work records remain immutable and unreviewed; this audit does not mutate state.",
    ]

    gh_user_id: int | None = config.github_user_id
    gh_login: str | None = None
    gh_authenticated = False
    gh_client = github_transport or GhCliClient()

    if github_user_override is not None:
        override_id, override_login = github_user_override
        if override_id is not None:
            gh_user_id = override_id
            gh_login = override_login
            gh_authenticated = True
    elif (has_github_sources or gh_user_id is None) and hasattr(gh_client, "get_authenticated_user"):
        try:
            user_data = gh_client.get_authenticated_user()
            if isinstance(user_data, dict) and isinstance(user_data.get("id"), int):
                if gh_user_id is None or gh_user_id == user_data["id"]:
                    gh_user_id = user_data["id"]
                    gh_login = user_data.get("login")
                    gh_authenticated = True
                else:
                    report_limitations.append("github_authenticated_user_mismatch")
        except TracefolioError:
            if has_github_sources:
                report_limitations.append("github_auth_unavailable")

    derived_emails: set[str] = set()
    if gh_user_id is not None and gh_login:
        derived_emails.add(f"{gh_user_id}+{gh_login}@users.noreply.github.com".casefold())

    source_indices: dict[tuple[str, str], SourceAuthorshipIndex] = {}
    project_ledgers = {}
    project_errors: dict[str, list[str]] = defaultdict(list)

    # Pass 1: Index all github_public sources first so derived owner emails feed into local_git.
    for project in registry.projects:
        if not project.enabled:
            continue
        try:
            ledger = store.read_state(project.project_id)
            project_ledgers[project.project_id] = ledger
        except TracefolioError as error:
            project_errors[project.project_id].append(error.code)
            continue
        for source in project.sources:
            if source.kind != "github_public":
                continue
            state = ledger.sources.get(source.source_id)
            revision = state.cursor.head if state and state.cursor.head else None
            if not revision:
                source_indices[(project.project_id, source.source_id)] = SourceAuthorshipIndex(
                    source_id=source.source_id,
                    kind="github_public",
                    pinned_revision=None,
                    limitations=["source_not_collected"],
                )
                continue
            try:
                req = store.source_request(project.project_id, source.source_id)
                provider = GitHubPublicProvider(gh_client)
                tree_paths, tree_limits = _github_tree_paths(provider, req, revision)
                idx = index_github_public_paths(
                    provider,
                    req,
                    revision,
                    tree_paths,
                    owner_github_id=gh_user_id,
                    owner_github_login=gh_login,
                    owner_emails_override=config.author_emails,
                )
                idx.limitations = sorted(set(idx.limitations + tree_limits))
                derived_emails.update(idx.derived_owner_emails)
                source_indices[(project.project_id, source.source_id)] = idx
            except TracefolioError as error:
                source_indices[(project.project_id, source.source_id)] = SourceAuthorshipIndex(
                    source_id=source.source_id,
                    kind="github_public",
                    pinned_revision=revision,
                    limitations=[error.code],
                )

    effective_emails = frozenset(config.author_emails | derived_emails)

    # Pass 2: Index local_git and local_directory sources using effective_emails.
    for project in registry.projects:
        if not project.enabled or project.project_id not in project_ledgers:
            continue
        ledger = project_ledgers[project.project_id]
        for source in project.sources:
            if source.kind == "github_public":
                continue
            state = ledger.sources.get(source.source_id)
            revision = state.cursor.head if state and state.cursor.head else None
            if source.kind == "local_directory":
                inv_paths = sorted(state.cursor.inventory.keys()) if state else []
                by_comp: dict[str, int] = Counter(component_key(p) for p in inv_paths)
                idx = SourceAuthorshipIndex(
                    source_id=source.source_id,
                    kind="local_directory",
                    pinned_revision=revision,
                    path_attributions={p: "unverified" for p in inv_paths},
                    path_audit_labels={p: "unverifiable_non_git" for p in inv_paths},
                    components=[
                        ComponentAuthorship(
                            component=sanitize_text(comp, config.policy).text,
                            verdict="unverifiable_non_git",
                            total_head_files_count=count,
                        )
                        for comp, count in sorted(by_comp.items())
                    ],
                    limitations=["non_git_directory"],
                )
                source_indices[(project.project_id, source.source_id)] = idx
                continue
            if not revision:
                source_indices[(project.project_id, source.source_id)] = SourceAuthorshipIndex(
                    source_id=source.source_id,
                    kind="local_git",
                    pinned_revision=None,
                    limitations=["source_not_collected"],
                )
                continue
            try:
                req = store.source_request(project.project_id, source.source_id)
                idx = index_local_git_paths(
                    req,
                    revision,
                    local_git_exec,
                    owner_emails_override=effective_emails,
                )
                source_indices[(project.project_id, source.source_id)] = idx
            except TracefolioError as error:
                source_indices[(project.project_id, source.source_id)] = SourceAuthorshipIndex(
                    source_id=source.source_id,
                    kind="local_git",
                    pinned_revision=revision,
                    limitations=[error.code],
                )

    # Pass 3 (Layer 2): Cross-check works and claims against Layer 1 indices.
    has_any_identity = bool(effective_emails or gh_user_id is not None)
    project_audits: list[ProjectAuthorshipAudit] = []

    for project in registry.projects:
        if not project.enabled:
            project_audits.append(
                ProjectAuthorshipAudit(
                    project_id=project.project_id,
                    status="disabled",
                    assessment="disabled",
                )
            )
            continue
        if project.project_id in project_errors:
            project_audits.append(
                ProjectAuthorshipAudit(
                    project_id=project.project_id,
                    status="error",
                    assessment="unavailable",
                    errors=project_errors[project.project_id],
                )
            )
            continue

        ledger = project_ledgers[project.project_id]
        source_audits: list[SourceAuthorshipAudit] = []
        for source in project.sources:
            idx = source_indices.get((project.project_id, source.source_id))
            if idx is None:
                continue
            if not idx.pinned_revision:
                assessment = "unverifiable_non_git" if source.kind == "local_directory" else "unavailable"
            elif any(
                lim in {"github_not_public", "github_unavailable", "git_read_failed", "github_not_found"}
                for lim in idx.limitations
            ) and not idx.components and idx.self_commits == 0 and idx.other_or_unmatched_commits == 0:
                assessment = "unavailable"
            else:
                assessment = _source_assessment(idx, has_identity=has_any_identity)
            source_audits.append(
                SourceAuthorshipAudit(
                    source_id=source.source_id,
                    kind=source.kind,
                    pinned_revision=idx.pinned_revision,
                    assessment=assessment,
                    self_commits=idx.self_commits,
                    other_or_unmatched_commits=idx.other_or_unmatched_commits,
                    merge_context_commits=idx.merge_context_commits,
                    unmatched_local_author_count=idx.unmatched_local_author_count,
                    components=idx.components,
                    limitations=sorted(set(idx.limitations)),
                )
            )

        generation = store.current(project.project_id, ledger)
        works = effective_works(generation, ledger)
        ev_cache = {}
        current_project_id = project.project_id

        def _get_evidence(eid: str, cache=ev_cache):
            if eid not in cache:
                cache[eid] = store.read_evidence(eid)
            return cache[eid]

        def _classify_evidence_ref(
            eid: str,
            proj_id: str = current_project_id,
            cache=ev_cache,
        ) -> tuple[str, str, str | None]:
            rec = _get_evidence(eid, cache)
            idx = source_indices.get((proj_id, rec.source_id))
            comp = component_key(rec.source_relative_path) if rec.source_relative_path else None
            if idx is None:
                return "path", "unverified", comp
            if rec.origin == "git_commit":
                meta = idx.commit_meta.get(rec.revision)
                if meta is None and idx.kind == "github_public" and gh_user_id is not None:
                    try:
                        req = store.source_request(proj_id, rec.source_id)
                        provider = GitHubPublicProvider(gh_client)
                        prefix, _ = provider._repository(req)
                        data = provider._get(req, prefix + "/commits/" + rec.revision).data
                        if isinstance(data, dict) and data.get("sha"):
                            meta = _record_github_commit(
                                data,
                                owner_emails=effective_emails,
                                owner_github_id=gh_user_id,
                                policy=config.policy,
                                derived_emails=derived_emails,
                            )
                            idx.commit_meta[rec.revision] = meta
                    except TracefolioError:
                        meta = None
                if meta is not None:
                    return "commit", meta.audit_label, comp
                return "commit", "unverified", comp
            return "path", idx.audit_label_for_path(rec.source_relative_path), comp

        work_audits: list[WorkAuthorshipAudit] = []
        for work in works:
            all_refs = sorted(work_evidence_refs(work))
            work_commit_shas: dict[tuple[str, str], str] = {}
            work_path_counts: Counter = Counter()
            work_components: set[str] = set()
            for eid in all_refs:
                try:
                    kind_tag, label, comp = _classify_evidence_ref(eid)
                    rec = _get_evidence(eid)
                    if comp:
                        work_components.add(sanitize_text(comp, config.policy).text)
                    if kind_tag == "commit":
                        work_commit_shas[(rec.source_id, rec.revision)] = label
                    else:
                        work_path_counts[label] += 1
                except TracefolioError:
                    work_path_counts["unverified"] += 1

            work_commit_counts: Counter = Counter(work_commit_shas.values())
            claim_audits: list[ClaimAuthorshipAudit] = []
            for claim in work.claims:
                c_commits: dict[tuple[str, str], str] = {}
                c_paths: Counter = Counter()
                for eid in claim.evidence_refs:
                    try:
                        kind_tag, label, _ = _classify_evidence_ref(eid)
                        rec = _get_evidence(eid)
                        if kind_tag == "commit":
                            c_commits[(rec.source_id, rec.revision)] = label
                        else:
                            c_paths[label] += 1
                    except TracefolioError:
                        c_paths["unverified"] += 1
                c_commit_counts = Counter(c_commits.values())
                claim_audits.append(
                    ClaimAuthorshipAudit(
                        claim_id=claim.claim_id,
                        statement=sanitize_text(claim.statement, config.policy).text,
                        support_verdict=_derive_support_verdict(c_commit_counts, c_paths),
                        commit_support_counts=dict(sorted(c_commit_counts.items())),
                        path_support_counts=dict(sorted(c_paths.items())),
                    )
                )

            work_audits.append(
                WorkAuthorshipAudit(
                    work_id=work.work_id,
                    title=sanitize_text(work.title, config.policy).text,
                    assessment=_derive_support_verdict(work_commit_counts, work_path_counts),
                    referenced_commit_counts=dict(sorted(work_commit_counts.items())),
                    referenced_path_counts=dict(sorted(work_path_counts.items())),
                    referenced_components=sorted(work_components),
                    claims=claim_audits,
                    semantic_contribution_status_unchanged=work.contribution,
                )
            )

        status = "pending" if not any(s.pinned_revision for s in source_audits) else "audited"
        project_audits.append(
            ProjectAuthorshipAudit(
                project_id=project.project_id,
                status=status,
                assessment=_project_assessment(source_audits),
                sources=source_audits,
                works=work_audits,
            )
        )

    methods: list[str] = []
    if gh_user_id is not None:
        methods.append("GitHub commit author.id compared against verified numeric GitHub user ID")
    if config.author_emails:
        methods.append("Git author email matched against configured private author_emails_env")
    if derived_emails:
        methods.append(
            "Local Git author email matched against emails observed on owner-matched public GitHub commits"
        )

    return AuthorshipAuditReport(
        scope=(
            "Two-layer authorship audit: Layer 1 per-component/per-path non-merge commit metadata "
            "at pinned HEAD revisions, and Layer 2 cross-check of work records and claims."
        ),
        identity=AuthorshipIdentitySummary(
            github_authenticated=gh_authenticated,
            github_user_id_configured=config.github_user_id is not None,
            configured_email_count=len(config.author_emails),
            derived_email_count=len(derived_emails - config.author_emails),
            effective_email_count=len(effective_emails),
            match_methods=methods,
        ),
        limitations=sorted(set(report_limitations)),
        configured_project_count=len(registry.projects),
        projects=project_audits,
    )


def export_audit(store: StateStore, report: AuthorshipAuditReport, path) -> None:
    target = validate_export_path(store.config, Path(path))
    export_root = store.root.resolve() / "exports"
    if not target.is_relative_to(export_root) or target == export_root:
        raise TracefolioError(
            "unsafe_export",
            "Export must be inside the private state's exports directory.",
        )
    with store.lock("export-writer"):
        store._atomic_json(target, report.model_dump(mode="json"))
