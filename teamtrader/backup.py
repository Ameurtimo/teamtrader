"""Sauvegarde / restauration du fichier SQLite (historique, paper trading, réglages)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile

KNOWN_TABLES = {"decisions", "paper_positions", "calendar_forecasts", "settings", "ai_usage"}


def restore(content: bytes, path: str) -> tuple[bool, str]:
    """Remplace la base par `content` après vérification. L'ancienne base est conservée
    à côté, avec le suffixe « .avant-restauration »."""
    if not content.startswith(b"SQLite format 3\x00"):
        return False, "Ce fichier n'est pas une base SQLite."
    fd, tmp = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        con = sqlite3.connect(tmp)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "La base est corrompue."
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        if not tables & KNOWN_TABLES:
            return False, "Cette base ne contient aucune table TeamTrader."
        if os.path.exists(path):
            shutil.copyfile(path, path + ".avant-restauration")
        shutil.copyfile(tmp, path)
        return True, f"Base restaurée ({', '.join(sorted(tables & KNOWN_TABLES))}). " \
                     "L'ancienne base est conservée en « .avant-restauration »."
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
