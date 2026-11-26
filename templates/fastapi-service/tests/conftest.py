import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def call():
    """Calls the service through the framework-neutral dispatcher with a fresh store."""
    from app import store
    importlib.reload(store)
    import app.routes as routes
    importlib.reload(routes)
    from app.http import dispatch

    def _call(method, path, headers=None, body=None):
        return dispatch(routes.ROUTES, method, path, headers or {}, body)
    return _call
