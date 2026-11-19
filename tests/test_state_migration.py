import json
import os
import subprocess
import sys

import pytest

from tracefolio.collection import collect_projects
from tracefolio.errors import TracefolioError
from tracefolio.migration import migrate_state, recover_migration
from tracefolio.private_config import load_config
from tracefolio.state import StateStore


def durable(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and 'locks' not in p.relative_to(root).parts
            and p.name not in {'.migration.json', 'retired.json'}}


def test_migration_dry_run_does_not_change_state_or_bindings(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    before, binding = durable(store.root), bindings.read_bytes()
    result = migrate_state(cfg, checkout, bindings, dry_run=True)
    assert result['status'] == 'dry_run'
    assert not checkout.joinpath('.tracefolio-local', 'state').exists()
    assert durable(store.root) == before
    assert bindings.read_bytes() == binding


def test_verified_migration_preserves_records_and_blocks_stale_writers(migration_setup, monkeypatch):
    cfg, store, checkout, bindings = migration_setup
    before = durable(store.root)
    generation = store.current('demo').generation_id
    result = migrate_state(cfg, checkout, bindings)
    assert result['status'] == 'migrated' and result['digest_match'] is True
    destination = checkout / '.tracefolio-local' / 'state'
    assert durable(destination) == before == durable(store.root)
    data = json.loads(bindings.read_text())
    assert data['TRACEFOLIO_CONFIG'] == str(cfg.config_paths[0])
    for key, value in data.items():
        monkeypatch.setenv(key, value)
    new = load_config(cfg.config_paths[0])
    moved = StateStore.open(new)
    assert moved.current('demo').generation_id == generation
    assert collect_projects(new, ['demo']).projects[0]['new_evidence'] == 0
    assert moved.read_state('demo').pending_analysis == []
    with pytest.raises(TracefolioError, match='state_retired'), store.lock('demo'):
        pytest.fail('Stale writer acquired retired state')
    assert migrate_state(new, checkout, bindings)['status'] == 'noop'


def test_migration_rejects_busy_writer_and_occupied_destination(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    original = bindings.read_bytes()
    with store.lock('demo'), pytest.raises(TracefolioError, match='project_locked'):
        migrate_state(cfg, checkout, bindings)
    assert bindings.read_bytes() == original
    assert not (store.root / '.migration.json').exists()
    destination = checkout / '.tracefolio-local' / 'state'
    destination.mkdir(parents=True)
    destination.joinpath('occupied').write_text('keep')
    with pytest.raises(TracefolioError, match='migration_destination_exists'):
        migrate_state(cfg, checkout, bindings)
    assert destination.joinpath('occupied').read_text() == 'keep'


def test_migration_corrupt_evidence_keeps_original_active(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    original = bindings.read_bytes()
    eid = store.read_state('demo').evidence_ids[0]
    evidence = store.root / 'evidence' / (eid + '.json')
    data = json.loads(evidence.read_text())
    data['payload'] = 'tampered'
    evidence.write_text(json.dumps(data))
    with pytest.raises(TracefolioError, match='evidence_integrity'):
        migrate_state(cfg, checkout, bindings)
    assert bindings.read_bytes() == original
    assert not (store.root / 'retired.json').exists()


def test_activation_failure_rolls_back_binding_and_retirement(migration_setup, monkeypatch):
    cfg, store, checkout, bindings = migration_setup
    original = bindings.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('synthetic activation failure')
    monkeypatch.setattr('tracefolio.migration.activate_local_bindings', fail)
    with pytest.raises(TracefolioError, match='migration_failed'):
        migrate_state(cfg, checkout, bindings)
    assert bindings.read_bytes() == original
    with store.lock('demo'):
        assert not (store.root / 'retired.json').exists()


def test_interrupted_migration_can_resume_or_rollback(migration_setup, monkeypatch):
    cfg, store, checkout, bindings = migration_setup
    original = bindings.read_bytes()
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr('tracefolio.migration.activate_local_bindings', interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_state(cfg, checkout, bindings)
    journal = checkout / '.tracefolio-local' / 'migration.json'
    data = json.loads(journal.read_text())
    data['pid'] = 0  # synthetic stopped owner, never steal a live PID
    journal.write_text(json.dumps(data))
    result = recover_migration(cfg, checkout, bindings, mode='rollback')
    assert result['status'] == 'rolled_back'
    assert bindings.read_bytes() == original
    assert not (store.root / 'retired.json').exists()
    assert not (store.root / '.migration.json').exists()


def test_migration_never_follows_source_link(migration_setup, tmp_path):
    cfg, store, checkout, bindings = migration_setup
    outside = tmp_path / 'outside'
    outside.mkdir()
    outside.joinpath('secret').write_text('must not copy')
    link = store.root / 'outside-link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, check=False)
        if result.returncode:
            pytest.skip('Cannot create link')
    with pytest.raises(TracefolioError, match='unsafe_root'):
        migrate_state(cfg, checkout, bindings)


def test_barrier_blocks_normal_writer(migration_setup):
    _, store, _, _ = migration_setup
    store.root.joinpath('.migration.json').write_text(json.dumps({'pid': os.getpid()}))
    with pytest.raises(TracefolioError, match='state_migrating'), store.lock('demo'):
        pytest.fail('Normal writer ignored migration barrier')


def test_process_writer_blocks_migration(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    code = '''import sys
from tracefolio.private_config import load_config
from tracefolio.state import StateStore
from pathlib import Path
store=StateStore.open(load_config(Path(sys.argv[1])))
with store.lock('demo'):
 print('locked', flush=True)
 sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(cfg.config_paths[0])],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(TracefolioError, match='project_locked'):
            migrate_state(cfg, checkout, bindings)
    finally:
        child.communicate('\n', timeout=20)
    assert not (store.root / 'retired.json').exists()


def test_process_writer_refuses_barrier_and_retired_root(migration_setup):
    cfg, store, checkout, bindings = migration_setup
    code = '''import sys
from pathlib import Path
from tracefolio.private_config import load_config
from tracefolio.state import StateStore
from tracefolio.errors import TracefolioError
store=StateStore(load_config(Path(sys.argv[1])))
try:
 with store.lock('demo'): print('unexpected')
except TracefolioError as e: print(e.code)
'''
    barrier = store.root / '.migration.json'
    barrier.write_text(json.dumps({'token': 'other', 'pid': os.getpid()}))
    result = subprocess.run([sys.executable, '-c', code, str(cfg.config_paths[0])],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'state_migrating'
    barrier.unlink()
    migrate_state(cfg, checkout, bindings)
    result = subprocess.run([sys.executable, '-c', code, str(cfg.config_paths[0])],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'state_retired'


def test_interruption_after_activation_can_resume(migration_setup, monkeypatch):
    cfg, store, checkout, bindings = migration_setup
    original_write = StateStore._atomic_json
    def interrupt(self, path, data):
        if path.name == 'migration.json' and data.get('phase') == 'activated':
            raise KeyboardInterrupt
        return original_write(self, path, data)
    monkeypatch.setattr(StateStore, '_atomic_json', interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_state(cfg, checkout, bindings)
    monkeypatch.setattr(StateStore, '_atomic_json', original_write)
    journal_path = checkout / '.tracefolio-local' / 'migration.json'
    journal = json.loads(journal_path.read_text())
    journal['pid'] = 0
    journal_path.write_text(json.dumps(journal))
    assert recover_migration(cfg, checkout, bindings, mode='resume')['status'] == 'migrated'
    assert (store.root / 'retired.json').exists()


def test_changed_source_during_copy_aborts(migration_setup, monkeypatch):
    cfg, store, checkout, bindings = migration_setup
    original = bindings.read_bytes()
    import shutil
    copy = shutil.copyfile
    def changed(source, destination):
        result = copy(source, destination)
        store.root.joinpath('new-during-copy.json').write_text('{}')
        return result
    monkeypatch.setattr('tracefolio.migration.shutil.copyfile', changed)
    with pytest.raises(TracefolioError, match='migration_source_changed'):
        migrate_state(cfg, checkout, bindings)
    assert bindings.read_bytes() == original
    assert not (store.root / 'retired.json').exists()
