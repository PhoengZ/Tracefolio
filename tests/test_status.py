import json

import pytest
from test_portfolio_cli import publish_demo

from tracefolio.cli import main
from tracefolio.collection import collect_projects
from tracefolio.contracts.routes import WorkUnit
from tracefolio.status import inspect_project


def test_saved_summary_is_reused_without_verified_authorship(knowledge_workspace):
    cfg, _, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    result = inspect_project(store, cfg.registry.projects[0])
    assert result['analysis_action'] == 'reuse'
    assert result['pending_evidence_count'] == 0
    assert result['reasons'] == []


def test_pending_and_route_drift_have_explicit_priority(knowledge_workspace):
    cfg, source, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    source.joinpath('one.py').write_text('checkpoint = 2\n')
    collect_projects(cfg, ['demo'])
    result = inspect_project(store, cfg.registry.projects[0])
    assert result['analysis_action'] == 'analyze'
    assert 'pending_evidence' in result['reasons']
    cfg.registry.projects[0].context = 'Changed interpretation scope.'
    result = inspect_project(store, cfg.registry.projects[0])
    assert result['analysis_action'] == 'blocked'
    assert result['reasons'][0] == 'collection_required'
    assert 'pending_evidence' in result['reasons']


def test_gap_with_no_backlog_does_not_schedule_reanalysis(knowledge_workspace):
    cfg, _, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    ledger = store.read_state('demo')
    ledger.sources['code'].coverage.state = 'partial'
    ledger.sources['code'].coverage.sources[0].collection_state = 'partial'
    ledger.sources['code'].coverage.sources[0].limitations = ['github_rate_limited']
    with store.lock('demo'):
        store.save_state('demo', ledger)
    result = inspect_project(store, cfg.registry.projects[0])
    assert result['analysis_action'] == 'blocked'
    assert result['pending_evidence_count'] == 0
    assert result['reasons'] == ['source_incomplete']
    assert result['generation_id'] is not None


def test_missing_disabled_and_missing_work(knowledge_workspace):
    cfg, _, path = knowledge_workspace
    from tracefolio.state import StateStore
    store = StateStore.open(cfg)
    route = cfg.registry.projects[0]
    assert inspect_project(store, route)['analysis_action'] == 'missing'
    assert inspect_project(store, route.model_copy(update={'enabled': False}))[
        'analysis_action'] == 'disabled'
    store = publish_demo(cfg, path.parent)
    route.sources[0].work_units = [WorkUnit(work_id='missing-work', context='Missing',
                                          include_paths=['missing.py'])]
    result = inspect_project(store, route)
    assert result['missing_work_ids'] == ['missing-work']
    assert 'configured_work_missing' in result['reasons']


@pytest.mark.parametrize('command', [['status', '--all'],
                                   ['summarize', '--all', '--view', 'brief']])
def test_read_commands_do_not_collect_or_write(knowledge_workspace, monkeypatch, capsys, command):
    cfg, _, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    monkeypatch.delenv('TRACEFOLIO_CONFIG_FILES', raising=False)
    monkeypatch.setenv('TRACEFOLIO_CONFIG', str(path))
    def forbidden(*args, **kwargs):
        pytest.fail('Read command invoked an acquisition or analysis mutation')
    for name in ['collect_projects', 'create_packet', 'apply_proposal']:
        monkeypatch.setattr('tracefolio.cli.' + name, forbidden)
    before = {p.relative_to(store.root): p.read_bytes()
              for p in store.root.rglob('*') if p.is_file()}
    assert main(command) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['entries'][0]['project_id'] == 'demo'
    after = {p.relative_to(store.root): p.read_bytes()
             for p in store.root.rglob('*') if p.is_file()}
    assert before == after


def test_brief_omits_provenance_but_export_stays_full(knowledge_workspace, monkeypatch, capsys):
    cfg, _, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    monkeypatch.delenv('TRACEFOLIO_CONFIG_FILES', raising=False)
    monkeypatch.setenv('TRACEFOLIO_CONFIG', str(path))
    target = store.root / 'exports' / 'full.json'
    assert main(['summarize', '--all', '--view', 'brief', '--out', str(target)]) == 0
    text = capsys.readouterr().out
    for forbidden in ['evidence_refs', 'evidence_ids', 'packet_ids', 'collection_ids', 'payload']:
        assert forbidden not in text
    full = json.loads(target.read_text())
    assert full['entries'][0]['works'][0]['claims'][0]['evidence_refs']
    assert json.loads(text)['entries'][0]['works'][0]['actions']


def test_status_rejects_unknown_project(knowledge_workspace, monkeypatch, capsys):
    _, _, path = knowledge_workspace
    monkeypatch.delenv('TRACEFOLIO_CONFIG_FILES', raising=False)
    monkeypatch.setenv('TRACEFOLIO_CONFIG', str(path))
    assert main(['status', '--project', 'not-configured']) == 2
    assert 'unknown_project' in capsys.readouterr().err


def test_brief_portfolio_exposes_compact_skills_without_evidence_refs():
    from tracefolio.contracts.knowledge import Skill, WorkRecord
    from tracefolio.contracts.portfolio import PortfolioEntry, PortfolioSnapshot
    from tracefolio.portfolio import brief_portfolio

    snap = PortfolioSnapshot(
        configured_project_count=1,
        entries=[PortfolioEntry(
            project_id='demo',
            status='ready',
            works=[
                WorkRecord(
                    project_id='demo', work_id='api', title='API', record_revision=1,
                    skills=[
                        Skill(name='Go', evidence_refs=['ev-1']),
                        Skill(name='gRPC', evidence_refs=['ev-2']),
                    ],
                ),
                WorkRecord(
                    project_id='demo', work_id='infra', title='Infra', record_revision=1,
                    skills=[
                        Skill(name='Docker', evidence_refs=['ev-3']),
                        Skill(name='gRPC', evidence_refs=['ev-4']),
                    ],
                ),
            ],
        )],
    )
    brief = brief_portfolio(snap)
    entry = brief['entries'][0]
    assert entry['skills'] == ['Docker', 'Go', 'gRPC']
    assert entry['works'][0]['skills'] == ['Go', 'gRPC']
    assert entry['works'][1]['skills'] == ['Docker', 'gRPC']
    assert 'evidence_refs' not in json.dumps(brief)
