"""Les tests unitaires sont hors ligne : tout accès réseau non simulé échoue."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return  # tests d'intégration : accès réseau réel volontaire

    def blocked(*args, **kwargs):
        raise ConnectionError("Accès réseau interdit dans les tests unitaires.")

    from teamtrader import data
    monkeypatch.setattr(data, "_get_json", blocked)
    monkeypatch.setattr(data.yf, "Ticker", blocked)
    monkeypatch.setenv("TEAMTRADER_DB", os.devnull)
