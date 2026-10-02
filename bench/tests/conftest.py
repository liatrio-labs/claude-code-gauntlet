"""Benchmark-local pytest fixtures for adapter and real-poster tests."""

import pytest
from gauntlet.delivery import post

from tests.support.forge import install_forge_factory


@pytest.fixture
def forge_factory(monkeypatch, request):
    factory = install_forge_factory(monkeypatch, post)
    if request.instance is not None:
        request.instance.forge_factory = factory
    return factory
