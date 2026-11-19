import json
import subprocess

import pytest
from test_portfolio_cli import publish_demo

from tracefolio.errors import TracefolioError
from tracefolio.portfolio import export_portfolio, summarize_portfolio
from tracefolio.private_config import load_config


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


@pytest.fixture
def local_checkout(knowledge_workspace, tmp_path, monkeypatch):
    _cfg, source, config_path = knowledge_workspace
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    git(checkout, 'init', '-q')
    checkout.joinpath('.gitignore').write_text('/.tracefolio-local/\n/bindings.local.json\n')
    monkeypatch.setenv('TRACEFOLIO_LOCAL_STATE_CHECKOUT', str(checkout))
    monkeypatch.setenv('TRACEFOLIO_STATE_DIR', str(checkout / '.tracefolio-local' / 'state'))
    return checkout, source, config_path


def test_opted_in_local_state_and_export(local_checkout):
    checkout, _, path = local_checkout
    cfg = load_config(path)
    store = publish_demo(cfg, path.parent)
    export_portfolio(store, summarize_portfolio(store, cfg.registry),
                     store.root / 'exports' / 'portfolio.json')
    assert (store.root / 'exports' / 'portfolio.json').exists()
    for target in [checkout / 'public.json', store.root / 'evidence' / 'export.json']:
        with pytest.raises(TracefolioError, match='unsafe_export'):
            export_portfolio(store, summarize_portfolio(store, cfg.registry), target)


@pytest.mark.parametrize('case', ['not_ignored', 'tracked', 'wrong_root', 'not_git_top'])
def test_local_state_rejects_unsafe_git_locations(local_checkout, monkeypatch, case):
    checkout, _, path = local_checkout
    if case == 'not_ignored':
        checkout.joinpath('.gitignore').write_text('')
    elif case == 'tracked':
        state = checkout / '.tracefolio-local' / 'state'
        state.mkdir(parents=True)
        state.joinpath('secret.json').write_text('{}')
        git(checkout, 'add', '-f', '.tracefolio-local/state/secret.json')
    elif case == 'wrong_root':
        monkeypatch.setenv('TRACEFOLIO_STATE_DIR', str(checkout / 'other'))
    else:
        nested = checkout / 'nested'
        nested.mkdir()
        monkeypatch.setenv('TRACEFOLIO_LOCAL_STATE_CHECKOUT', str(nested))
    with pytest.raises(TracefolioError, match='unsafe_root'):
        load_config(path)


def test_local_state_rejects_source_overlap(local_checkout, monkeypatch):
    checkout, _, path = local_checkout
    monkeypatch.setenv('TRACEFOLIO_DEMO_ROOT', str(checkout))
    with pytest.raises(TracefolioError, match='unsafe_root'):
        load_config(path)


def test_local_state_is_excluded_from_acquisition(local_checkout):
    from tracefolio.privacy import mandatory_allowed
    assert not mandatory_allowed('.tracefolio-local/state/projects/demo/CURRENT')


def test_local_state_rejects_link_escape(local_checkout, tmp_path):
    checkout, _, path = local_checkout
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = checkout / '.tracefolio-local'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, check=False)
        if result.returncode:
            pytest.skip('Cannot create link on this platform')
    with pytest.raises(TracefolioError, match='unsafe_root'):
        load_config(path)


def test_local_state_binding_is_not_a_literal_registry_field(local_checkout):
    _, _, path = local_checkout
    assert 'local_state_checkout' not in json.loads(path.read_text())
