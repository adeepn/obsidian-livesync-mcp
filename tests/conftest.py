"""Configure import-time application construction without a live backend."""

import os
import pytest


def pytest_configure(config):
    patch = pytest.MonkeyPatch()
    for key, value in {
        'LIVESYNC_BACKEND': 'couchdb',
        'COUCHDB_USER': 'test-user',
        'COUCHDB_PASSWORD': 'test-password',
        'COUCHDB_DATABASE': 'test-vault',
        'MCP_API_KEY': 'test-bearer-key',
    }.items():
        if key not in os.environ:
            patch.setenv(key, value)
    config._app_env_patch = patch


def pytest_unconfigure(config):
    config._app_env_patch.undo()


@pytest.fixture(autouse=True)
def node_capability_scheduler_margin(monkeypatch):
    """Keep real Node startup out of semantic integration deadlines.

    F6 confirmation timed out in node --version under suite contention.
    Only the capability probe gets a generous test-only margin. Actual
    request deadlines and production configuration remain unchanged.
    """
    import selection_engine
    original = selection_engine.capability_version_check

    def capability_version_check(*, timeout=None):
        return original(timeout=max(60, timeout or selection_engine.TIMEOUT))

    monkeypatch.setattr(selection_engine, 'capability_version_check', capability_version_check)
