import copy

import pytest
from pydantic import ValidationError

from tracefolio.contracts.evidence import CollectionManifest, EvidenceRecord
from tracefolio.contracts.knowledge import ProjectRecord, WorkRecord
from tracefolio.contracts.packets import AnalysisProposal, InvestigationPacket
from tracefolio.contracts.portfolio import PortfolioSnapshot
from tracefolio.contracts.routes import RouteRegistry


@pytest.mark.parametrize("field", ["root", "url", "token"])
def test_routes_reject_literal_locator_fields(route_payload, field):
    route_payload["projects"][0]["sources"][0][field] = "/synthetic/private/root"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)


def test_duplicate_ids_rejected(route_payload):
    route_payload["projects"].append(copy.deepcopy(route_payload["projects"][0]))
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)


def test_unknown_fields_rejected(route_payload):
    route_payload["projects"][0]["sources"][0]["execute"] = "do-not-run"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)


def test_all_contract_models_have_schema_version():
    models = (
        RouteRegistry,
        EvidenceRecord,
        CollectionManifest,
        InvestigationPacket,
        AnalysisProposal,
        ProjectRecord,
        WorkRecord,
        PortfolioSnapshot,
    )
    for model in models:
        schema = model.model_json_schema()
        assert schema["properties"]["schema_version"]["const"] == "1.0.0", model.__name__


def test_utc_timestamps_and_enums(example_records):
    _, work = example_records
    work["generated_at"] = "2026-11-17T00:00:00+07:00"
    with pytest.raises(ValidationError):
        WorkRecord.model_validate(work)
    work["generated_at"] = "2026-11-17T00:00:00Z"
    work["review_status"] = "approved-by-model"
    with pytest.raises(ValidationError):
        WorkRecord.model_validate(work)


def test_project_and_work_examples_validate(example_records):
    for cls, data in zip((ProjectRecord, WorkRecord), example_records):
        assert cls.model_validate(data).language == "en"


def test_unsupported_version_and_source_kinds_fail(route_payload):
    route_payload["schema_version"] = "99.0.0"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)
    route_payload["schema_version"] = "1.0.0"
    route_payload["projects"][0]["sources"][0]["kind"] = "execute-script"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)


def test_source_binding_is_exclusive(route_payload):
    route_payload["projects"][0]["sources"][0]["repository_env"] = "TRACEFOLIO_REPO"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(route_payload)

