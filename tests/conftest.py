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
