import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from ..contracts.common import Coverage, SourceCoverage
from ..contracts.evidence import CollectionResult, ContextResult, EvidenceRecord, SourceCursor
from ..contracts.routes import Limits, SourceRoute
from ..privacy import PrivacyPolicy, sanitize_text


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(repr=False)
class SourceRequest:
    project_id: str
    source: SourceRoute
    locator: str
    limits: Limits
    policy: PrivacyPolicy
    owner_emails: frozenset[str] = field(default_factory=frozenset)


@dataclass(repr=False)
class ContextRequest:
    source_request: SourceRequest
    revision: str
    path: str
    packet_id: str = ""


class SourceProvider(Protocol):
    def collect_source(self, request: SourceRequest,
                       previous: SourceCursor | None) -> CollectionResult: ...
    def read_context(self, request: ContextRequest) -> ContextResult: ...


def coverage(request: SourceRequest, revision: str | None, complete: bool,
             limitations=()) -> Coverage:
    state = "complete" if complete else "partial"
    return Coverage(state=state, sources=[SourceCoverage(
        source_id=request.source.source_id, revision=revision, collection_state=state,
        limitations=sorted(set(limitations)),
    )])


def evidence(request: SourceRequest, revision: str, path: str | None, payload: str,
             *, origin="snapshot", attribution="unverified", completeness="complete",
             normalization=None, cells=None
             ) -> EvidenceRecord:
    clean = sanitize_text(payload, request.policy)
    safe_path = sanitize_text(path, request.policy).text if path else None
    body = {"project_id": request.project_id, "source_id": request.source.source_id,
            "origin": origin, "revision": revision, "source_relative_path": safe_path,
            "payload": clean.text, "attribution": attribution, "completeness": completeness,
            "normalization": normalization, "cells": cells or []}
    record = EvidenceRecord(evidence_id="pending", content_digest="pending",
                            redactions=clean.redactions, **body)
    hashed = digest(record.model_dump(mode="json", exclude={"evidence_id", "content_digest"}))
    record.evidence_id = "ev-" + hashed
    record.content_digest = hashed
    return record

