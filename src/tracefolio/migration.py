"""Verified private-state copy and atomic binding cutover. No source acquisition."""

import base64
import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .contracts.evidence import CollectionManifest
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
from .sources.base import digest
from .state import StateStore, logical_id, work_evidence_refs

CONTROL = {'.migration.json', 'retired.json'}


def reject_tree_links(root):
    reject_links(root)
    def unreadable(error):
        raise TracefolioError('migration_unreadable', exit_code=4) from None
    for current, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in [*directories, *files]:
            reject_links(Path(current) / name)


def file_manifest(root: Path) -> dict:
    """Hash only durable regular files; never follow a link, including in excluded dirs."""
    reject_links(root)
    files = {}
    if not root.exists():
        return files
    def unreadable(error):
        raise TracefolioError('migration_unreadable', exit_code=4) from None
    for current, directories, names in os.walk(root, followlinks=False, onerror=unreadable):
        current = Path(current)
        for name in [*directories, *names]:
            reject_links(current / name)
        if current == root:
            directories[:] = [d for d in directories if d != 'locks']
        for name in names:
            relative = (current / name).relative_to(root)
            if current == root and name in CONTROL:
                continue
            if (relative.parts[0] in {'evidence', 'collections', 'projects', 'packets'} and
                    re.fullmatch(r'.+\.json\.tmp-[0-9a-f]{32}', name)):
                continue
            path = current / name
            if not path.is_file():
                raise TracefolioError('unsafe_root')
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(files.items()))


def validate_contents(store):
    """Check immutable evidence, packets, generations and all referenced records."""
    checked = set()
    packets = {}
    collections = {}
    policy = store._read_json(store.root / 'policy.json')
    if policy != {'privacy_policy': store.config.policy.epoch}:
        raise TracefolioError('privacy_policy_changed')
    def check_evidence(ref):
        if ref not in checked:
            store.read_evidence(ref)
            checked.add(ref)
    def check_packet(packet_id):
        if packet_id not in packets:
            packet = store.read_packet(logical_id(packet_id))
            try:
                validate_packet(packet)
            except TracefolioError:
                raw = store._read_json(store.root / 'packets' / packet_id / 'packet.json')
                missing = set(type(packet).model_fields) - set(raw)
                raw_digest = digest({k: v for k, v in raw.items()
                                     if k not in {'packet_id', 'packet_digest'}})
                if (not missing or not missing <= {'semantic_map', 'evidence_aliases'} or
                        raw_digest != raw['packet_digest'] or
                        packet_id != 'packet-' + raw_digest[:24]):
                    raise TracefolioError('packet_integrity') from None
            for ref in packet.allowed_evidence_ids:
                check_evidence(ref)
            packets[packet_id] = packet.project_id
        return packets[packet_id]
    def check_collection(collection_id):
        if collection_id not in collections:
            collection = CollectionManifest.model_validate(store._read_json(
                store.root / 'collections' / (logical_id(collection_id) + '.json')))
            if collection.collection_id != collection_id:
                raise TracefolioError('state_integrity')
            for ref in collection.evidence_ids:
                check_evidence(ref)
            collections[collection_id] = collection.project_id
        return collections[collection_id]
    for path in (store.root / 'evidence').glob('*.json'):
        # read_evidence performs canonical hash validation.
        check_evidence(path.stem)
    for path in (store.root / 'packets').glob('*/packet.json'):
        check_packet(path.parent.name)
    for path in (store.root / 'collections').glob('*.json'):
        check_collection(path.stem)
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
            for packet_id in work.provenance.packet_ids:
                if check_packet(packet_id) != project.project_id:
                    raise TracefolioError('state_integrity')
            for collection_id in work.provenance.collection_ids:
                if check_collection(collection_id) != project.project_id:
                    raise TracefolioError('state_integrity')
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
    private_parent = (path.parent / '.tracefolio-local'
                      if path.name == 'bindings.local.json' else path.parent)
    if private_parent.exists():
        reject_tree_links(private_parent)
    secure_private_root(private_parent)
    temp = private_parent / (path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
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


@contextmanager
def migration_guard(parent):
    """An OS-released exclusive lock serializes migrators and recovery owners."""
    if parent.exists():
        reject_tree_links(parent)
    secure_private_root(parent)
    with (parent / 'migration.lock').open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise TracefolioError('migration_owner_running', exit_code=3) from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_owned(path, token):
    if path.exists() and json.loads(path.read_bytes()).get('token') == token:
        path.unlink()


def _copy_and_activate(config, checkout, bindings_path, destination, journal, journal_path):
    store = StateStore(config)
    token = journal['token']
    with ExitStack() as stack:
        ids = {p.project_id for p in config.registry.projects}
        ids.add('export-writer')
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
        _remove_owned(destination / 'retired.json', token)
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


def _migrate_state(config, checkout: Path, bindings_path: Path, *, dry_run=False) -> dict:
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
    try:
        store._atomic_json(journal_path, journal)
        _barrier(store, journal)
        return _copy_and_activate(config, checkout, bindings_path, destination, journal, journal_path)
    except Exception as error:
        atomic_bytes(bindings_path, base64.b64decode(journal['binding_backup']))
        _remove_owned(store.root / '.migration.json', journal['token'])
        _remove_owned(store.root / 'retired.json', journal['token'])
        if isinstance(error, TracefolioError):
            raise
        raise TracefolioError('migration_failed', exit_code=4) from None


def migrate_state(config, checkout: Path, bindings_path: Path, *, dry_run=False) -> dict:
    if dry_run:
        return _migrate_state(config, checkout, bindings_path, dry_run=True)
    checkout, bindings_path, destination = _locations(checkout, bindings_path)
    with migration_guard(destination.parent):
        return _migrate_state(config, checkout, bindings_path)


def _owner_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = wintypes.DWORD()
            try:
                return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
            finally:
                kernel.CloseHandle(handle)
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _recover_migration(config, checkout, bindings_path, *, mode):
    checkout, bindings_path, destination = _locations(checkout, bindings_path)
    journal_path = destination.parent / 'migration.json'
    reject_links(journal_path)
    journal = json.loads(journal_path.read_bytes())
    if (journal['destination'] != str(destination) or journal['bindings'] != str(bindings_path)
            or mode not in {'resume', 'rollback'} or
            not re.fullmatch('[0-9a-f]{32}', journal['token'])):
        raise TracefolioError('migration_binding_mismatch')
    if _owner_alive(journal['pid']):
        raise TracefolioError('migration_owner_running', exit_code=3)
    original = base64.b64decode(journal['binding_backup'], validate=True)
    source_config = bound_config(json.loads(original))
    if (str(source_config.state_root) != journal['source'] or
            config.config_paths != source_config.config_paths or
            config.state_root not in {source_config.state_root, destination}):
        raise TracefolioError('migration_binding_mismatch')
    store = StateStore(source_config)
    barrier = store.root / '.migration.json'
    if journal['phase'] == 'activated' and not barrier.exists():
        raise TracefolioError('migration_already_completed', exit_code=3)
    if barrier.exists() and store._read_json(barrier).get('token') != journal['token']:
        raise TracefolioError('state_migrating', exit_code=3)
    journal['pid'] = os.getpid()
    store._atomic_json(journal_path, journal)
    if not barrier.exists():
        _barrier(store, journal)
    # Hard-killed Python processes cannot run lock-directory finally blocks.
    for lock in (store.root / 'locks').glob('*'):
        reject_links(lock)
        owner = lock / 'owner.json'
        if (owner.exists() and store._read_json(owner).get('token') == journal['token']
                and {p.name for p in lock.iterdir()} == {'owner.json'}):
            owner.unlink()
            lock.rmdir()
    if mode == 'rollback':
        with ExitStack() as locks:
            if destination.exists():
                from dataclasses import replace
                target_store = StateStore(replace(source_config, state_root=destination))
                _barrier(target_store, journal)
                try:
                    ids = {p.name for p in (destination / 'projects').glob('*') if p.is_dir()}
                    ids.add('export-writer')
                    for pid in sorted(ids):
                        locks.enter_context(target_store.lock(pid, migration_token=journal['token']))
                    if file_manifest(destination) != journal['manifest']:
                        raise TracefolioError('migration_destination_changed')
                    atomic_bytes(bindings_path, original)
                    target_store._atomic_json(destination / 'retired.json',
                                              {'token': journal['token']})
                finally:
                    _remove_owned(destination / '.migration.json', journal['token'])
            else:
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


def recover_migration(config, checkout, bindings_path, *, mode):
    checkout, bindings_path, destination = _locations(checkout, bindings_path)
    with migration_guard(destination.parent):
        return _recover_migration(config, checkout, bindings_path, mode=mode)
