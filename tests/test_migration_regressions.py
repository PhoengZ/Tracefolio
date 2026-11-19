import json
import subprocess
import sys

import pytest
from test_local_state import git

from tracefolio.cli import main
from tracefolio.errors import TracefolioError
from tracefolio.migration import file_manifest, migrate_state, recover_migration
from tracefolio.portfolio import export_portfolio, summarize_portfolio
from tracefolio.state import StateStore


def test_manifest_preserves_control_like_export_names(migration_setup):
    _, store, _, _ = migration_setup
    for relative in ['exports/retired.json', 'exports/locks/report.json',
                     'proposals/report.tmp-custom.json']:
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
        assert relative in file_manifest(store.root)


@pytest.mark.parametrize('tampered', [False, True])
def test_migration_preserves_legacy_packet_digest(migration_setup, tampered):
    from tracefolio.sources.base import digest
    cfg, store, checkout, bindings = migration_setup
    packet_path = next((store.root / 'packets').glob('*/packet.json'))
    raw = json.loads(packet_path.read_bytes())
    old_id = raw['packet_id']
    raw.pop('semantic_map')
    raw.pop('evidence_aliases')
    raw['packet_digest'] = digest({k: v for k, v in raw.items()
                                   if k not in {'packet_id', 'packet_digest'}})
    raw['packet_id'] = 'packet-' + raw['packet_digest'][:24]
    legacy = store.root / 'packets' / raw['packet_id'] / 'packet.json'
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(raw))
    packet_path.unlink()
    packet_path.parent.rmdir()
    for path in (store.root / 'projects').glob('*/generations/*/works/*.json'):
        work = json.loads(path.read_bytes())
        work['provenance']['packet_ids'] = [raw['packet_id'] if p == old_id else p
                                          for p in work['provenance']['packet_ids']]
        path.write_text(json.dumps(work))
    before = legacy.read_bytes()
    if tampered:
        raw['project_context'] = 'Changed after digest creation'
        legacy.write_text(json.dumps(raw))
        with pytest.raises(TracefolioError, match='packet_integrity'):
            migrate_state(cfg, checkout, bindings, dry_run=True)
        return
    assert migrate_state(cfg, checkout, bindings, dry_run=True)['digest_match']
    assert legacy.read_bytes() == before


def test_manifest_raises_on_unreadable_subtree(tmp_path, monkeypatch):
    import os
    root = tmp_path / 'state'
    denied = root / 'denied'
    denied.mkdir(parents=True)
    scandir = os.scandir
    def unreadable(path):
        if str(path) == str(denied):
            raise PermissionError('synthetic unreadable subtree')
        return scandir(path)
    monkeypatch.setattr('os.scandir', unreadable)
    with pytest.raises((PermissionError, TracefolioError)):
        file_manifest(root)


@pytest.mark.parametrize('case', ['policy', 'packet', 'collection'])
def test_migration_rejects_invalid_policy_or_provenance(migration_setup, case):
    cfg, store, checkout, bindings = migration_setup
    if case == 'policy':
        store.root.joinpath('policy.json').write_text('{"privacy_policy":"wrong"}')
    else:
        generation = store.current('demo')
        path = store.root / 'projects' / 'demo' / 'generations' / generation.generation_id
        work_path = path / 'works' / 'work-a.json'
        work = json.loads(work_path.read_bytes())
        work['provenance'][case + '_ids'] = [case + '-missing']
        work_path.write_text(json.dumps(work))
    with pytest.raises(TracefolioError):
        migrate_state(cfg, checkout, bindings)


def test_ignore_probe_only_is_not_a_private_directory(migration_setup):
    cfg, _, checkout, bindings = migration_setup
    checkout.joinpath('.gitignore').write_text(
        '/.tracefolio-local/ignore-probe\n/bindings.local.json\n')
    with pytest.raises(TracefolioError, match='unsafe_root'):
        migrate_state(cfg, checkout, bindings, dry_run=True)


def test_saved_read_on_virgin_state_does_not_initialize(knowledge_workspace, monkeypatch, capsys):
    cfg, _, path = knowledge_workspace
    monkeypatch.delenv('TRACEFOLIO_CONFIG_FILES', raising=False)
    monkeypatch.setenv('TRACEFOLIO_CONFIG', str(path))
    for command in [['status', '--all'], ['summarize', '--all', '--view', 'brief']]:
        assert main(command) == 3
        capsys.readouterr()
        assert not cfg.state_root.exists()


def test_explicit_export_initializes_private_state(knowledge_workspace, monkeypatch, capsys):
    cfg, _, path = knowledge_workspace
    monkeypatch.delenv('TRACEFOLIO_CONFIG_FILES', raising=False)
    monkeypatch.setenv('TRACEFOLIO_CONFIG', str(path))
    target = cfg.state_root / 'exports' / 'empty.json'
    assert main(['summarize', '--all', '--out', str(target)]) == 3
    capsys.readouterr()
    assert target.exists()
    assert (cfg.state_root / 'policy.json').exists()


def test_completed_migration_cannot_rollback_new_destination(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    migrate_state(cfg, checkout, bindings)
    journal_path = checkout / '.tracefolio-local' / 'migration.json'
    journal = json.loads(journal_path.read_text())
    journal['pid'] = 0
    journal_path.write_text(json.dumps(journal))
    current = bindings.read_bytes()
    with pytest.raises(TracefolioError, match='migration_already_completed'):
        recover_migration(cfg, checkout, bindings, mode='rollback')
    assert bindings.read_bytes() == current
    assert (store.root / 'retired.json').exists()


def test_export_refuses_retired_source(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    snapshot = summarize_portfolio(store, cfg.registry)
    migrate_state(cfg, checkout, bindings)
    with pytest.raises(TracefolioError, match='state_retired'):
        export_portfolio(store, snapshot, store.root / 'exports' / 'stale.json')


def test_recovery_owners_are_serialized_by_os_lock(migration_setup):
    cfg, _, checkout, bindings = migration_setup
    code = '''import sys
from pathlib import Path
from tracefolio.migration import migration_guard
with migration_guard(Path(sys.argv[1])/'.tracefolio-local'):
 print('owned',flush=True)
 sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(checkout)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'owned'
        with pytest.raises(TracefolioError, match='migration_owner_running'):
            recover_migration(cfg, checkout, bindings, mode='rollback')
    finally:
        child.communicate('\n', timeout=20)


@pytest.mark.parametrize('mode', ['resume', 'rollback'])
def test_hard_kill_releases_owned_project_locks_via_recovery(migration_setup, mode):
    cfg, store, checkout, bindings = migration_setup
    code = '''import sys
from pathlib import Path
from tracefolio.private_config import load_config
import tracefolio.migration as m
def pause(*args, **kwargs):
 print('paused',flush=True)
 sys.stdin.readline()
m.activate_local_bindings=pause
m.migrate_state(load_config(Path(sys.argv[1])),Path(sys.argv[2]),Path(sys.argv[3]))
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(cfg.config_paths[0]),
                              str(checkout), str(bindings)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'paused'
        child.kill()
        child.communicate(timeout=20)
        result = recover_migration(cfg, checkout, bindings, mode=mode)
        assert result['status'] == ('migrated' if mode == 'resume' else 'rolled_back')
        assert not any((store.root / 'locks').iterdir())
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=20)


def test_binding_temporary_file_is_private_and_ignored(migration_setup, monkeypatch):
    from tracefolio.migration import atomic_bytes
    _, _, checkout, bindings = migration_setup
    import os
    replace = os.replace
    observed = []
    def inspect(source, target):
        observed.append(str(source))
        git(checkout, 'check-ignore', '--quiet', '--', str(source))
        return replace(source, target)
    monkeypatch.setattr('tracefolio.migration.os.replace', inspect)
    atomic_bytes(bindings, b'{}')
    assert observed


def test_local_state_accepts_proposals_in_its_own_namespace(migration_setup, monkeypatch):
    from test_proposals import proposal_for, write_proposal

    from tracefolio.collection import collect_projects
    from tracefolio.investigation import create_packet
    from tracefolio.private_config import load_config
    from tracefolio.proposals import apply_proposal
    cfg, _, checkout, bindings = migration_setup
    monkeypatch.setattr('tracefolio.private_config.__file__',
                        str(checkout / 'src' / 'tracefolio' / 'private_config.py'))
    migrate_state(cfg, checkout, bindings)
    for key, value in json.loads(bindings.read_text()).items():
        monkeypatch.setenv(key, value)
    local = load_config(cfg.config_paths[0])
    source = local.locators[('demo', 'code')]
    from pathlib import Path
    Path(source, 'one.py').write_text('checkpoint = 2\n')
    collect_projects(local, ['demo'])
    store = StateStore.open(local)
    packet = store.read_packet(create_packet(store, 'demo').packet_id)
    target = store.root / 'proposals' / 'update.json'
    target.parent.mkdir(exist_ok=True)
    write_proposal(target, proposal_for(packet))
    assert apply_proposal(store, target).status == 'published'
