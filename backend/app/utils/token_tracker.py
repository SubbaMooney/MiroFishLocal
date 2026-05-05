"""
In-Process Token- und Cost-Tracker.

Wird von ``LLMClient.chat()``, ``lightrag_factory._llm`` und
``lightrag_factory._embed`` mit ``response.usage`` gefuettert. Aggregiert
pro Modell + Purpose-Tag und liefert Snapshots fuer das Admin-Endpoint
``GET /api/admin/tokens``.

Designentscheidungen:
  * Singleton-Instanz ``tracker`` statt Klasse-Argument durch alle Aufrufer.
  * Thread-safe via ``threading.Lock`` — LightRAG laeuft in eigenem Loop,
    Persona/Config-Gen im Worker-Thread, beide reporten parallel.
  * Persistenz bewusst NICHT gebaut — der Counter wird beim Restart
    zurueckgesetzt; daily Cost ueber das OpenAI-Dashboard abfragen.
  * Cost-Tabelle ist hardcoded und Stand-2026 fuer den OpenAI-Default-Mix.
    Bei anderen Providern/Modellen waere eine Konfig sinnvoll, aber
    YAGNI bis das tatsaechlich gebraucht wird.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

# Stand 2026, USD pro 1 Million Tokens. Werte aus OpenAI-Pricing-Page.
# Bei unbekannten Modellen wird auf 0 gefallen und ein Warning geloggt.
_PRICE_PER_M_TOKENS: Dict[str, Dict[str, float]] = {
    # Completion-Modelle
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    # Embeddings (nur "input" — Embeddings haben kein Output)
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
}


def _resolve_price(model: str) -> Dict[str, float]:
    """Sucht Preis fuer das Modell; faellt auf 0 zurueck wenn unbekannt."""
    if model in _PRICE_PER_M_TOKENS:
        return _PRICE_PER_M_TOKENS[model]
    # Fuzzy-Match auf bekannte Familien (z.B. "gpt-4o-mini-2024-07-18").
    for known in _PRICE_PER_M_TOKENS:
        if model.startswith(known):
            return _PRICE_PER_M_TOKENS[known]
    return {"input": 0.0, "output": 0.0}


@dataclass
class _Bucket:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class _ModelStats:
    """Aggregat pro Modell-String. Aufgeschluesselt nach Purpose-Tag."""

    model: str
    by_purpose: Dict[str, _Bucket] = field(default_factory=dict)

    def add(
        self,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        b = self.by_purpose.setdefault(purpose, _Bucket())
        b.calls += 1
        b.prompt_tokens += prompt_tokens
        b.completion_tokens += completion_tokens


class TokenTracker:
    """Thread-safe Token-Counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: Dict[str, _ModelStats] = {}

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
        purpose: str = "unknown",
    ) -> None:
        """Buche einen LLM- oder Embedding-Call.

        Tolerant gegenueber None/fehlenden Werten — wenn der Provider keine
        ``usage`` zurueckliefert, ruft der Aufrufer das eben mit 0 auf, und
        der Counter taucht nicht auf.
        """
        if not model:
            return
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        if prompt_tokens == 0 and completion_tokens == 0:
            return
        with self._lock:
            stats = self._stats.setdefault(model, _ModelStats(model=model))
            stats.add(purpose, prompt_tokens, completion_tokens)

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()

    def snapshot(self) -> dict:
        """Strukturiertes Dict fuer JSON-Antwort."""
        with self._lock:
            total_calls = 0
            total_in = 0
            total_out = 0
            total_cost = 0.0
            by_model = []
            for model, stats in sorted(self._stats.items()):
                price = _resolve_price(model)
                m_calls = m_in = m_out = 0
                purposes = []
                for purpose, b in sorted(stats.by_purpose.items()):
                    m_calls += b.calls
                    m_in += b.prompt_tokens
                    m_out += b.completion_tokens
                    p_cost = (
                        b.prompt_tokens * price["input"] / 1_000_000
                        + b.completion_tokens * price["output"] / 1_000_000
                    )
                    purposes.append({
                        "purpose": purpose,
                        "calls": b.calls,
                        "prompt_tokens": b.prompt_tokens,
                        "completion_tokens": b.completion_tokens,
                        "total_tokens": b.total_tokens,
                        "cost_usd": round(p_cost, 6),
                    })
                m_cost = (
                    m_in * price["input"] / 1_000_000
                    + m_out * price["output"] / 1_000_000
                )
                by_model.append({
                    "model": model,
                    "calls": m_calls,
                    "prompt_tokens": m_in,
                    "completion_tokens": m_out,
                    "total_tokens": m_in + m_out,
                    "cost_usd": round(m_cost, 6),
                    "price_per_1m_input": price["input"],
                    "price_per_1m_output": price["output"],
                    "by_purpose": purposes,
                })
                total_calls += m_calls
                total_in += m_in
                total_out += m_out
                total_cost += m_cost
            return {
                "totals": {
                    "calls": total_calls,
                    "prompt_tokens": total_in,
                    "completion_tokens": total_out,
                    "total_tokens": total_in + total_out,
                    "cost_usd": round(total_cost, 6),
                },
                "by_model": by_model,
            }


tracker = TokenTracker()
