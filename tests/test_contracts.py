import copy

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from tracefolio.contracts.knowledge import ProjectRecord, WorkRecord
from tracefolio.contracts.routes import RouteRegistry
from tracefolio.schema_export import schemas


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


def test_all_artifacts_have_version():
    for name, schema in schemas().items():
        assert schema["properties"]["schema_version"]["const"] == "1.0.0", name
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)


def test_utc_timestamps_and_enums(example_records):
    _, work = example_records
    work["generated_at"] = "2026-11-17T00:00:00+07:00"
    with pytest.raises(ValidationError):
        WorkRecord.model_validate(work)
    work["generated_at"] = "2026-11-17T00:00:00Z"
    work["review_status"] = "approved-by-model"
    with pytest.raises(ValidationError):
        WorkRecord.model_validate(work)


def test_project_and_work_validate_in_both_engines(example_records):
    for name, cls, data in zip(("project", "work"), (ProjectRecord, WorkRecord),
                               example_records):
        assert cls.model_validate(data).language == "en"
        Draft202012Validator(schemas()[name], format_checker=FormatChecker()).validate(data)


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

