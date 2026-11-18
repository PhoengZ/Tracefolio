import base64
import importlib
import io
import json

import pytest
from test_github_source import OLDER, SHA, req
from test_proposals import proposal_for, write_proposal

from tracefolio.collection import collect_projects
from tracefolio.contracts.routes import SourceRoute
from tracefolio.errors import TracefolioError
from tracefolio.investigation import create_packet, extend_packet
from tracefolio.portfolio import summarize_portfolio
from tracefolio.proposals import apply_proposal
from tracefolio.sources.base import ContextRequest
from tracefolio.sources.github_public import GitHubPublicProvider
from tracefolio.sources.github_transport import GitHubTransport
from tracefolio.state import StateStore


def bridge(replies=b"", *, output=None):
    module = importlib.import_module("tracefolio.sources.github_plugin_transport")
    return module.GitHubPluginTransport(
        input_stream=io.BytesIO(replies), output_stream=output or io.StringIO())


def reply(data=None, *, request_id="github-1", **extra):
    return (json.dumps({"request_id": request_id, "data": data, **extra},
                       ensure_ascii=False) + "\n").encode("utf-8")


def test_bridge_emits_fixed_get_and_reads_utf8_under_legacy_text_wrapper():
    stream = io.TextIOWrapper(io.BytesIO(reply({"text": "Thai \u0e44\u0e17\u0e22 and \u96ea"})),
                              encoding="cp1252")
    output = io.StringIO()
    transport = bridge(output=output)
    transport.input_stream = stream
    result = transport.get_json("/repos/example/demo/contents/main.py", {"ref": SHA})
    assert result.data == {"text": "Thai \u0e44\u0e17\u0e22 and \u96ea"}
    event = json.loads(output.getvalue())
    assert event == {"event": "github_request", "request_id": "github-1", "method": "GET",
                     "url": "https://api.github.com/repos/example/demo/contents/main.py?ref=" + SHA,
                     "max_response_bytes": 16777216}
    assert output.getvalue().isascii()
    assert result.byte_count > len("Thai \u0e44\u0e17\u0e22 and \u96ea")


@pytest.mark.parametrize("path", ["https://evil.invalid/x", "/repos/example/demo/../secret",
                                    "/repos/example/demo/%2e%2e/secret", "/repos/example/demo?x=y",
                                    "/repos/example/demo/contents/%2fetc", "/user/repos"])
def test_bridge_rejects_unsafe_path_before_emitting(path):
    output = io.StringIO()
    transport = bridge(output=output)
    with pytest.raises(TracefolioError, match="github_path_rejected"):
        transport.get_json(path)
    assert not output.getvalue()


def transport_with_recording_output(kind):
    output = io.StringIO()
    if kind == "plugin":
        return bridge(reply({}), output=output), lambda: json.loads(output.getvalue())["url"]
    transport = GitHubTransport()
    opened = []

    class Opener:
        def open(self, request, timeout):
            opened.append(request.full_url)
            response = io.BytesIO(b"{}")
            response.headers = {}
            return response

    transport.opener = Opener()
    return transport, lambda: opened[0]


@pytest.mark.parametrize("kind", ["anonymous", "plugin"])
@pytest.mark.parametrize("path", [
    "/repos/example/demo/commits/fix%2Fissue%2342",
    "/repos/example/demo/commits/release%25candidate",
    "/repos/example/demo/contents/report%3Ffinal%23v2%25.txt",
    "/repos/example/demo/contents/%E9%9B%AA%20report.txt",
])
def test_transports_preserve_safely_encoded_refs_and_filenames(kind, path):
    transport, requested_url = transport_with_recording_output(kind)
    assert transport.get_json(path).data == {}
    assert requested_url() == "https://api.github.com" + path


@pytest.mark.parametrize("kind", ["anonymous", "plugin"])
@pytest.mark.parametrize("suffix", ["%", "%2", "%zz", "%2g", "%ff", "%2e%2e/secret",
                                    "%252e%252e/secret", "%2fsecret", "%5csecret", "%00"])
def test_transports_still_reject_bad_escapes_and_encoded_traversal(kind, suffix):
    transport, _ = transport_with_recording_output(kind)
    with pytest.raises(TracefolioError, match="github_path_rejected"):
        transport.get_json("/repos/example/demo/contents/" + suffix)


@pytest.mark.parametrize(("wire", "code"), [
    (reply({}, request_id="wrong"), "github_plugin_protocol"),
    (b"PRIVATE_NON_JSON\n", "github_invalid_json"),
    (b"", "github_unavailable"),
    (reply("PRIVATE_SCALAR"), "github_invalid_json"),
    (b'{"request_id":"github-1","data":{}}', "github_plugin_protocol"),
    (b'{"request_id":"github-1","data":{"x":NaN}}\n', "github_invalid_json"),
    (b"\xff\n", "github_invalid_json"),
])
def test_bridge_rejects_bad_replies_without_disclosing_body(wire, code):
    with pytest.raises(TracefolioError) as caught:
        bridge(wire).get_json("/repos/example/demo")
    assert caught.value.code == code
    assert "PRIVATE" not in str(caught.value)


def test_bridge_bounds_wire_read_and_decoded_utf8_bytes():
    transport = bridge(reply({"x": "x" * 70}))
    transport.response_limit = 40
    with pytest.raises(TracefolioError, match="github_response_budget"):
        transport.get_json("/repos/example/demo")
    transport = bridge(b"x" * 70000 + b"\n")
    transport.response_limit = 40
    with pytest.raises(TracefolioError, match="github_response_budget"):
        transport.get_json("/repos/example/demo")


@pytest.mark.parametrize(("status", "code"), [(403, "github_rate_limited"),
    (429, "github_rate_limited"), (404, "github_not_found"), (500, "github_unavailable"),
    (302, "github_redirect_blocked")])
def test_bridge_maps_http_status_without_echoing_tool_failure(status, code):
    wire = reply(error={"status": status, "message": "PRIVATE_TOOL_FAILURE"},
                 headers={"Retry-After": "90000"})
    with pytest.raises(TracefolioError) as caught:
        bridge(wire).get_json("/repos/example/demo")
    assert caught.value.code == code
    assert "PRIVATE_TOOL_FAILURE" not in str(caught.value)
    if status in {403, 429}:
        assert caught.value.retry_after == 86400


@pytest.mark.parametrize(("path", "data"), [
    ("/repos/example/demo/commits", [{"sha": SHA}, {"sha": OLDER}]),
    ("/repos/example/demo/compare/a...b", {"commits": [{"sha": SHA}, {"sha": OLDER}]}),
    ("/repos/example/demo/commits/" + SHA, {"files": [{"filename": "a"}, {"filename": "b"}]}),
])
def test_headerless_full_pages_continue_and_empty_pages_stop(path, data):
    transport = bridge(reply(data) + reply([] if isinstance(data, list) else {},
                                           request_id="github-2"))
    first = transport.get_json(path, {"per_page": "2", "page": "1"})
    assert 'rel="next"' in first.headers["link"]
    second = transport.get_json(path, {"per_page": "2", "page": "2"})
    assert "link" not in second.headers


def test_explicit_link_headers_override_conservative_page_inference():
    result = bridge(reply([{}, {}], headers={"Link": '<ignored>; rel="last"'})).get_json(
        "/repos/example/demo/commits", {"per_page": "2"})
    assert result.headers["link"] == '<ignored>; rel="last"'
    result = bridge(reply([{}, {}])).get_json("/repos/example/demo/contents")
    assert "link" not in result.headers


def metadata(private=False):
    return {"private": private, "full_name": "example/demo", "default_branch": "main"}


def detail(patch="@@ -0,0 +1 @@\n+checkpoint = 1"):
    return {"sha": SHA, "commit": {"message": "vague update", "author": {"email":
            "author@example.invalid"}}, "parents": [],
            "files": [{"filename": "main.py", "status": "added", "patch": patch}]}


def sequence(*data):
    return b"".join(reply(item, request_id=f"github-{i}")
                    for i, item in enumerate(data, 1))


def test_plugin_provider_stops_on_private_visibility_and_has_no_fallback():
    output = io.StringIO()
    transport = bridge(sequence(metadata(True), {"sha": SHA}), output=output)
    with pytest.raises(TracefolioError, match="github_not_public"):
        GitHubPublicProvider(transport).collect_source(req(), None)
    assert len(output.getvalue().splitlines()) == 1


def test_plugin_provider_budget_is_forwarded_to_transport():
    request = req()
    request.limits.max_total_source_bytes = 128
    output = io.StringIO()
    with pytest.raises(TracefolioError, match="github_response_budget"):
        GitHubPublicProvider(bridge(reply({"padding": "a" * 200}), output=output)).collect_source(
            request, None)
    assert json.loads(output.getvalue())["max_response_bytes"] == 128


def test_plugin_rate_limit_preserves_retry_cursor_then_resumes():
    wire = sequence(metadata(), {"sha": SHA}, [{"sha": SHA}])
    wire += reply(request_id="github-4", error={"status": 429}, headers={"Retry-After": "60"})
    first = GitHubPublicProvider(bridge(wire)).collect_source(req(), None)
    assert first.cursor.pending == [SHA] and first.cursor.head is None
    assert "retry_after_seconds:60" in first.coverage.sources[0].limitations
    second = GitHubPublicProvider(bridge(sequence(metadata(), {"sha": SHA}, detail()))).collect_source(
        req(), first.cursor)
    assert second.cursor.head == SHA and second.coverage.state == "complete"


def test_real_provider_uses_headerless_commit_and_file_pagination():
    page = [{"sha": SHA}] * 100
    first = GitHubPublicProvider(bridge(sequence(metadata(), {"sha": SHA}, page))).collect_source(
        req(), None)
    assert first.cursor.page == 2 and not first.cursor.listed_complete
    many = detail()
    many["files"] = [{"filename": f"f{i}.py", "status": "added", "patch": "+x=1"}
                     for i in range(100)]
    last = detail()
    last["files"][0]["filename"] = "last.py"
    result = GitHubPublicProvider(bridge(sequence(metadata(), {"sha": SHA}, [], many, last))).collect_source(
        req(), first.cursor)
    assert result.coverage.state == "complete" and len(result.evidence) == 101
    assert result.cursor.head == SHA


def test_plugin_pipeline_sanitizes_before_publication_and_never_caches_raw(
        knowledge_workspace):
    cfg, _, private = knowledge_workspace
    cfg.registry.projects[0].sources = [SourceRoute(source_id="code", kind="github_public",
        repository_env="TRACEFOLIO_DEMO_REPO", include_paths=["*.py"])]
    cfg.locators[("demo", "code")] = "example/demo"
    wire = sequence(metadata(), {"sha": SHA}, [{"sha": SHA}],
                    detail('+email = "sensitive@example.invalid"\n+endpoint = "https://internal.invalid"'))
    report = collect_projects(cfg, ["demo"], providers={"github_public":
        GitHubPublicProvider(bridge(wire))})
    assert not report.partial
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    assert "sensitive@example.invalid" not in packet.evidence[0].payload
    assert packet.evidence[0].attribution == "unverified"
    proposal = private.parent / "plugin-proposal.json"
    write_proposal(proposal, proposal_for(packet))
    apply_proposal(store, proposal)
    assert summarize_portfolio(store, cfg.registry).entries[0].status == "ready"
    for path in cfg.state_root.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert b"sensitive@example.invalid" not in raw and b"https://internal.invalid" not in raw
            assert b'"private":false' not in raw and b'"default_branch"' not in raw


def test_scoped_context_can_inject_plugin_provider(knowledge_workspace):
    cfg, _, _ = knowledge_workspace
    cfg.registry.projects[0].sources = [SourceRoute(source_id="code", kind="github_public",
        repository_env="TRACEFOLIO_DEMO_REPO", include_paths=["*.py"])]
    cfg.locators[("demo", "code")] = "example/demo"
    collect_projects(cfg, ["demo"], providers={"github_public": GitHubPublicProvider(
        bridge(sequence(metadata(), {"sha": SHA}, [{"sha": SHA}], detail())))})
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    text = b"context = 2\n"
    provider = GitHubPublicProvider(bridge(sequence(metadata(), {"type": "file", "size": len(text),
        "encoding": "base64", "content": base64.b64encode(text).decode()})))
    result = extend_packet(store, ContextRequest(store.source_request("demo", "code"), SHA,
        "main.py", packet.packet_id), providers={"github_public": provider})
    assert any(e.payload == "context = 2\n" for e in store.read_packet(result.packet_id).evidence)


def test_cli_plugin_flag_selects_bridge_and_retains_normal_result(
        knowledge_workspace, monkeypatch, capsys):
    from tracefolio import cli
    cfg, _, _ = knowledge_workspace
    cfg.registry.projects[0].sources = [SourceRoute(source_id="code", kind="github_public",
        repository_env="TRACEFOLIO_DEMO_REPO", include_paths=["*.py"])]
    cfg.locators[("demo", "code")] = "example/demo"
    monkeypatch.setattr(cli, "load_config_set", lambda _: cfg)
    monkeypatch.setattr(cli, "selected_config_paths", list)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(
        sequence(metadata(), {"sha": SHA}, [{"sha": SHA}], detail())), encoding="cp1252"))
    assert cli.main(["collect", "--project", "demo", "--github-plugin"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len([x for x in lines if x.get("event") == "github_request"]) == 4
    assert lines[-1]["projects"][0]["status"] == "complete"
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    text = "context = '\u96ea'\n".encode()
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(sequence(metadata(), {
        "type": "file", "size": len(text), "encoding": "base64",
        "content": base64.b64encode(text).decode()})), encoding="cp1252"))
    assert cli.main(["context", "--packet", packet.packet_id, "--source", "code", "--revision",
                     SHA, "--path", "main.py", "--github-plugin"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len([x for x in lines if x.get("event") == "github_request"]) == 2
    assert lines[-1]["status"] == "created"
    assert any(e.payload == "context = '\u96ea'\n" for e in
               store.read_packet(lines[-1]["packet_id"]).evidence)
