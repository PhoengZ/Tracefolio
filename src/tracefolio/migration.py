"""Verified private-state copy and atomic binding cutover. No source acquisition."""

import base64
import hashlib
import json
import os
import shutil
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .contracts.evidence import EvidenceRecord
from .contracts.knowledge import ProjectRecord, WorkRecord
from .errors import TracefolioError
from .investigation import validate_packet
from .privacy import secure_private_root
from .private_config import (
    load_config_set,
    reject_links,
    selected_config_paths,
    validate_ignored_path,
    validate_state_location,
)
from .state import StateStore, logical_id, work_evidence_refs

CONTROL = {'.migration.json', 'retired.json'}


def file_manifest(root: Path) -> dict:
    """Hash only durable regular files; never follow a link, including in excluded dirs."""
    reject_links(root)
    files = {}
    if not root.exists():
        return files
    for current, directories, names in os.walk(root, followlinks=False):
        current = Path(current)
        for name in [*directories, *names]:
            reject_links(current / name)
        directories[:] = [d for d in directories if d != 'locks']
        for name in names:
            if name in CONTROL or '.tmp-' in name:
                continue
            path = current / name
            if not path.is_file():
                raise TracefolioError('unsafe_root')
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(files.items()))


def validate_contents(store):
    """Check immutable evidence, packets, generations and all referenced records."""
    checked = set()
    def check_evidence(ref):
        if ref not in checked:
            store.read_evidence(ref)
            checked.add(ref)
    for path in (store.root / 'evidence').glob('*.json'):
        # read_evidence performs canonical hash validation.
        EvidenceRecord.model_validate(store.read_evidence(path.stem).model_dump())
        checked.add(path.stem)
    for path in (store.root / 'packets').glob('*/packet.json'):
        packet = store.read_packet(path.parent.name)
        validate_packet(packet)
        for ref in packet.allowed_evidence_ids:
            check_evidence(ref)
    for path in (store.root / 'projects').glob('*/generations/*/project.json'):
        project = ProjectRecord.model_validate(store._read_json(path))
        for ref in project.evidence_ids:
            check_evidence(ref)
        for wid in project.work_ids:
            work = WorkRecord.model_validate(store._read_json(
                path.parent / 'works' / (logical_id(wid) + '.json')))
            if work.project_id != project.project_id or work.work_id != wid:
                raise TracefolioError('state_integrity')
            for ref in work_evidence_refs(work):
                check_evidence(ref)
    for project_dir in (store.root / 'projects').glob('*'):
        if project_dir.is_dir():
            ledger = store.read_state(project_dir.name)
            store.current(project_dir.name, ledger)
            for ref in [*ledger.evidence_ids, *ledger.pending_analysis]:
                check_evidence(ref)


@contextmanager
def binding_environment(data):
    previous = {k: v for k, v in os.environ.items() if k.startswith('TRACEFOLIO_')}
    try:
        for key in previous:
            del os.environ[key]
        os.environ.update(data)
        yield
    finally:
        for key in list(os.environ):
            if key.startswith('TRACEFOLIO_'):
                del os.environ[key]
        os.environ.update(previous)


def bound_config(data):
    if not isinstance(data, dict) or any(not k.startswith('TRACEFOLIO_') or
                                         not isinstance(v, str) for k, v in data.items()):
        raise TracefolioError('invalid_binding')
    with binding_environment(data):
        return load_config_set(selected_config_paths())


def atomic_bytes(path: Path, payload: bytes):
    reject_links(path)
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temp.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def activate_local_bindings(bindings_path, config, checkout, destination):
    original = bindings_path.read_bytes()
    data = json.loads(original)
    data[config.registry.state_dir_env] = str(destination)
    data['TRACEFOLIO_LOCAL_STATE_CHECKOUT'] = str(checkout)
    candidate = bound_config(data)
    if candidate.state_root != destination or candidate.config_paths != config.config_paths:
        raise TracefolioError('migration_binding_mismatch')
    try:
        atomic_bytes(bindings_path, (json.dumps(data, indent=2) + '\n').encode('utf-8'))
        if bound_config(json.loads(bindings_path.read_bytes())).state_root != destination:
            raise TracefolioError('migration_binding_mismatch')
    except BaseException:
        atomic_bytes(bindings_path, original)
        raise


def _locations(checkout, bindings_path):
    checkout, bindings_path = Path(checkout).absolute(), Path(bindings_path).absolute()
    reject_links(checkout)
    reject_links(bindings_path)
    destination = checkout / '.tracefolio-local' / 'state'
    validate_state_location(destination, checkout)
    if bindings_path != checkout / 'bindings.local.json':
        raise TracefolioError('migration_binding_mismatch')
    validate_ignored_path(checkout, bindings_path)
    return checkout, bindings_path, destination


def _barrier(store, journal):
    path = store.root / '.migration.json'
    try:
        with path.open('x', encoding='utf-8') as handle:
            json.dump({'token': journal['token'], 'pid': journal['pid']}, handle)
    except FileExistsError:
        raise TracefolioError('state_migrating', exit_code=3) from None


def _remove_owned(path, token):
    if path.exists() and json.loads(path.read_bytes()).get('token') == token:
        path.unlink()


def _copy_and_activate(config, checkout, bindings_path, destination, journal, journal_path):
    store = StateStore(config)
    token = journal['token']
    with ExitStack() as stack:
        ids = {p.project_id for p in config.registry.projects}
        ids.update(p.name for p in (store.root / 'projects').glob('*') if p.is_dir())
        for pid in sorted(ids):
            stack.enter_context(store.lock(pid, migration_token=token))
        manifest = file_manifest(store.root)
        if manifest != journal['manifest']:
            raise TracefolioError('migration_source_changed')
        validate_contents(store)
        staging = checkout / '.tracefolio-local' / ('staging-' + token)
        reject_links(staging)
        file_manifest(staging)
        secure_private_root(staging)
        if not destination.exists():
            for relative in manifest:
                source, target = store.root / relative, staging / relative
                reject_links(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            if file_manifest(staging) != manifest or file_manifest(store.root) != manifest:
                raise TracefolioError('migration_source_changed')
            staged_config = config.__class__(
                registry=config.registry, state_root=staging, config_paths=config.config_paths,
                locators=config.locators, policy=config.policy, author_emails=config.author_emails)
            validate_contents(StateStore(staged_config))
            staging.rename(destination)
        if file_manifest(destination) != manifest:
            raise TracefolioError('migration_destination_changed')
        journal['phase'] = 'verified'
        store._atomic_json(journal_path, journal)
        # New writers cannot target the old root after the barrier is released.
        store._atomic_json(store.root / 'retired.json', {'token': token})
        try:
            activate_local_bindings(bindings_path, config, checkout, destination)
        except Exception:
            _remove_owned(store.root / 'retired.json', token)
            raise
        journal['phase'] = 'activated'
        store._atomic_json(journal_path, journal)
    _remove_owned(store.root / '.migration.json', token)
    return {'status': 'migrated', 'file_count': len(manifest), 'digest_match': True}


def migrate_state(config, checkout: Path, bindings_path: Path, *, dry_run=False) -> dict:
    checkout, bindings_path, destination = _locations(checkout, bindings_path)
    reject_links(config.state_root)
    data = json.loads(bindings_path.read_bytes())
    selected = bound_config(data)
    if (selected.state_root != config.state_root or selected.config_paths != config.config_paths
            or selected.registry != config.registry or selected.locators != config.locators):
        raise TracefolioError('migration_binding_mismatch')
    manifest = file_manifest(config.state_root)
    store = StateStore(config)
    validate_contents(store)
    if config.state_root == destination:
        return {'status': 'noop', 'file_count': len(manifest), 'digest_match': True}
    if destination.exists():
        raise TracefolioError('migration_destination_exists')
    if dry_run:
        return {'status': 'dry_run', 'file_count': len(manifest), 'digest_match': True}
    parent = destination.parent
    secure_private_root(parent)
    journal_path = parent / 'migration.json'
    if journal_path.exists():
        raise TracefolioError('migration_recovery_required', exit_code=3)
    journal = {'token': uuid.uuid4().hex, 'pid': os.getpid(), 'phase': 'started',
               'source': str(config.state_root), 'destination': str(destination),
               'bindings': str(bindings_path), 'manifest': manifest,
               'binding_backup': base64.b64encode(bindings_path.read_bytes()).decode('ascii')}
    _barrier(store, journal)
    try:
        store._atomic_json(journal_path, journal)
        return _copy_and_activate(config, checkout, bindings_path, destination, journal, journal_path)
    except Exception as error:
        atomic_bytes(bindings_path, base64.b64decode(journal['binding_backup']))
        _remove_owned(store.root / '.migration.json', journal['token'])
        _remove_owned(store.root / 'retired.json', journal['token'])
        if isinstance(error, TracefolioError):
            raise
        raise TracefolioError('migration_failed', exit_code=4) from None


def _owner_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() != 87  # access denied is not dead
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def recover_migration(config, checkout, bindings_path, *, mode):
    checkout, bindings_path, destination = _locations(checkout, bindings_path)
    journal_path = destination.parent / 'migration.json'
    journal = json.loads(journal_path.read_bytes())
    if (journal['destination'] != str(destination) or journal['bindings'] != str(bindings_path)
            or mode not in {'resume', 'rollback'}):
        raise TracefolioError('migration_binding_mismatch')
    if _owner_alive(journal['pid']):
        raise TracefolioError('migration_owner_running', exit_code=3)
    original = base64.b64decode(journal['binding_backup'], validate=True)
    source_config = bound_config(json.loads(original))
    if str(source_config.state_root) != journal['source']:
        raise TracefolioError('migration_binding_mismatch')
    store = StateStore(source_config)
    barrier = store.root / '.migration.json'
    reject_links(journal_path)
    if barrier.exists() and store._read_json(barrier).get('token') != journal['token']:
        raise TracefolioError('state_migrating', exit_code=3)
    journal['pid'] = os.getpid()
    if not barrier.exists():
        _barrier(store, journal)
    if mode == 'rollback':
        atomic_bytes(bindings_path, original)
        _remove_owned(store.root / 'retired.json', journal['token'])
        _remove_owned(barrier, journal['token'])
        journal['phase'] = 'rolled_back'
        store._atomic_json(journal_path, journal)
        return {'status': 'rolled_back'}
    _remove_owned(store.root / 'retired.json', journal['token'])
    store._atomic_json(journal_path, journal)
    return _copy_and_activate(source_config, checkout, bindings_path, destination,
                              journal, journal_path)
