"""Journaux techniques : durées des analyses, erreurs des agents et des sources.

Niveau réglable avec TEAMTRADER_LOG_LEVEL (INFO par défaut) ; fichier optionnel avec
TEAMTRADER_LOG_FILE (sinon la console, visible dans les logs de Streamlit Cloud).
"""

from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)s %(name)s : %(message)s"


def setup() -> None:
    root = logging.getLogger("teamtrader")
    if root.handlers:
        return  # déjà configuré (Streamlit réexécute le script à chaque interaction)
    root.setLevel(os.environ.get("TEAMTRADER_LOG_LEVEL", "INFO").upper())
    path = os.environ.get("TEAMTRADER_LOG_FILE")
    handler = logging.FileHandler(path, encoding="utf-8") if path else logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.propagate = False
