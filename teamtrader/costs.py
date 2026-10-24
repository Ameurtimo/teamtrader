"""Consommation et coût estimé des appels à Claude.

Les prix sont ceux de l'API Anthropic (dollars par million de tokens) au moment de
l'écriture ; vérifie-les sur https://platform.claude.com/docs/en/about-claude/pricing
et ajuste ce tableau si besoin. Le coût affiché est une estimation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

# modèle : (entrée, sortie, lecture de cache) en $ / million de tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_FACTOR = 1.25   # écriture de cache (durée 5 min) = 1,25 × le prix d'entrée
WEB_SEARCH_PRICE = 10.0 / 1000  # 10 $ pour 1 000 recherches

# Budget mensuel en dollars (0 = pas de limite). Au-delà, les analyses sont bloquées.
MONTHLY_BUDGET_USD = float(os.environ.get("TEAMTRADER_MONTHLY_BUDGET_USD", "0"))


def _price(model: str) -> tuple[float, float, float]:
    for name, p in PRICES.items():
        if model.startswith(name):
            return p
    return PRICES["claude-opus-5-5"]  # modèle inconnu : on estime au tarif Opus


def usage_of(response, label: str) -> dict:
    """Résumé de consommation d'une réponse de l'API (tolérant aux champs absents)."""
    u = getattr(response, "usage", None)

    def n(attr):
        v = getattr(u, attr, 0) if u is not None else 0
        return int(v or 0)

    server = getattr(u, "server_tool_use", None) if u is not None else None
    searches = int(getattr(server, "web_search_requests", 0) or 0) if server is not None else 0
    model = str(getattr(response, "model", "") or "")
    p_in, p_out, p_cache = _price(model)
    inp, out = n("input_tokens"), n("output_tokens")
    c_read, c_write = n("cache_read_input_tokens"), n("cache_creation_input_tokens")
    cost = (inp * p_in + out * p_out + c_read * p_cache + c_write * p_in * CACHE_WRITE_FACTOR) / 1e6
    cost += searches * WEB_SEARCH_PRICE
    return {
        "label": label, "model": model, "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": c_read, "cache_write_tokens": c_write, "web_searches": searches,
        "cost_usd": round(cost, 6),
    }


def total(usages: list[dict]) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "web_searches", "cost_usd")
    return {k: sum(u.get(k, 0) for u in usages) for k in keys}


# ------------------------------------------------------------------ journal des dépenses

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    web_searches INTEGER,
    cost_usd REAL NOT NULL
)
"""


def record(con, kind: str, usages: list[dict]) -> None:
    con.execute(SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT INTO ai_usage (created_at, kind, label, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, web_searches, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(now, kind, u["label"], u["model"], u["input_tokens"], u["output_tokens"],
          u["cache_read_tokens"], u["cache_write_tokens"], u["web_searches"], u["cost_usd"])
         for u in usages],
    )
    con.commit()


def month_spend(con, now: datetime | None = None) -> float:
    con.execute(SCHEMA)
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE created_at >= ?",
                      (start,)).fetchone()
    return float(row[0])


def budget_status(con) -> tuple[bool, float]:
    """(analyse autorisée ?, dépense du mois)."""
    spent = month_spend(con)
    return (MONTHLY_BUDGET_USD <= 0 or spent < MONTHLY_BUDGET_USD), spent


def summary_by_kind(con) -> list[tuple]:
    con.execute(SCHEMA)
    return con.execute(
        "SELECT substr(created_at, 1, 7) AS mois, kind, COUNT(*), SUM(web_searches), "
        "SUM(cost_usd) FROM ai_usage GROUP BY mois, kind ORDER BY mois DESC, kind"
    ).fetchall()
