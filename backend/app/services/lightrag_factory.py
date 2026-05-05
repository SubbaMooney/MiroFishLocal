"""
LightRAG Factory — Phase 1 Migration (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md).

Stellt provider-agnostische Factory-Funktionen bereit, mit denen LightRAG
gegen jeden OpenAI-SDK-kompatiblen Endpoint betrieben werden kann:

  - ``create_llm_func()``      — async LLM-Func fuer LightRAG.llm_model_func
  - ``create_embed_func()``    — EmbeddingFunc fuer LightRAG.embedding_func
  - ``create_rag(working_dir)``— vollstaendig initialisierte LightRAG-Instanz

WICHTIG zum Init-Pattern (im Mock-Spike validiert, siehe SPIKE-Report):
``initialize_pipeline_status()`` bindet einen prozessweiten Lock im Modul
``lightrag.kg.shared_storage`` an den AKTUELLEN Event-Loop. Wird ``create_rag``
in einem kurzlebigen ``asyncio.run`` aufgerufen, schlagen alle nachfolgenden
Calls aus einem anderen Loop mit ``RuntimeError: ... bound to a different
event loop`` fehl. Pflicht-Invariante: alle ``create_rag``-Aufrufe und alle
Folge-Operationen MUESSEN ueber denselben langlebigen Loop laufen — siehe
``RagManager`` (folgt in einem separaten Commit), der genau das durchsetzt.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from openai import OpenAI

from ..config import Config


def _get_llm_client() -> OpenAI:
    """OpenAI-SDK-Client gegen den konfigurierten LLM-Endpoint."""
    if not Config.LLM_API_KEY:
        raise ValueError(
            "LLM_API_KEY ist nicht gesetzt. LightRAG kann keine LLM-Calls "
            "absetzen — bitte .env pruefen."
        )
    return OpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL)


def _get_embed_client() -> OpenAI:
    """OpenAI-SDK-Client gegen den konfigurierten Embedding-Endpoint
    (faellt automatisch auf LLM_* zurueck)."""
    if not Config.EMBED_API_KEY:
        raise ValueError(
            "EMBED_API_KEY (oder LLM_API_KEY als Fallback) ist nicht gesetzt. "
            "LightRAG kann keine Embeddings erzeugen."
        )
    return OpenAI(api_key=Config.EMBED_API_KEY, base_url=Config.EMBED_BASE_URL)


def create_llm_func(
    model: Optional[str] = None,
    system_prompt_hint_provider: Optional[Callable[[], str]] = None,
) -> Callable[..., Awaitable[str]]:
    """Liefert eine async LLM-Func im LightRAG-erwarteten Schema.

    LightRAG ruft die Func mit ``(prompt, system_prompt=..., history_messages=...,
    keyword_extraction=..., **kwargs)``. Das OpenAI-SDK ist sync; wir wrappen
    den Call ueber ``run_in_executor`` des aktuellen Event-Loops.

    ``system_prompt_hint_provider`` (optional) wird bei JEDEM Call frisch
    aufgerufen und liefert einen Hint-String, der dem System-Prompt vorangestellt
    wird. So kann der Aufrufer die Ontologie nachtraeglich aendern, ohne die
    LLM-Func neu zu erstellen — Phase 2 Migration nutzt das fuer
    ``set_ontology``.
    """
    client = _get_llm_client()
    model_name = model or Config.LLM_MODEL_NAME
    if not model_name:
        raise ValueError(
            "LLM_MODEL_NAME ist nicht gesetzt — kein Default fuer "
            "provider-agnostische Konfiguration."
        )

    async def _llm(
        prompt: str,
        system_prompt: Optional[str] = None,
        history_messages: Optional[list] = None,
        keyword_extraction: bool = False,
        **kwargs: Any,
    ) -> str:
        # Hint frisch ziehen (mutable im Caller, z.B. RagManager.set_ontology).
        hint = system_prompt_hint_provider() if system_prompt_hint_provider else ""
        effective_system = (
            f"{hint}\n\n{system_prompt}" if hint and system_prompt
            else (hint or system_prompt or None)
        )

        messages: list[dict] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        if history_messages:
            messages.extend(history_messages)
        messages.append({"role": "user", "content": prompt})

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=kwargs.get("temperature", 0.0),
            ),
        )
        # Token-Tracker (Audit-Folge): LightRAG-Indexing macht zehntausende
        # LLM-Calls; ohne Tracking ist die Cost-Hochrechnung blind.
        try:
            from ..utils.token_tracker import tracker
            usage = getattr(resp, "usage", None)
            if usage is not None:
                tracker.record(
                    model=model_name,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    purpose="lightrag:keyword" if keyword_extraction else "lightrag:extract",
                )
        except Exception:  # noqa: BLE001
            pass
        return resp.choices[0].message.content or ""

    return _llm


def create_embed_func(
    model: Optional[str] = None,
    dim: Optional[int] = None,
    max_token_size: int = 8192,
):
    """Liefert ein ``lightrag.utils.EmbeddingFunc`` mit dim/max_token_size.

    Validiert die Embedding-Dimension nach dem ersten Call — wenn der Provider
    eine andere Dimension liefert als ``EMBED_DIM`` deklariert, wird
    ``RuntimeError`` geworfen (Konfigurationsfehler statt stiller Fehlsuche).
    """
    # Lazy-Import, damit das Modul ohne installiertes lightrag importierbar bleibt
    # (z.B. fuer Tests des Config-Pfads).
    from lightrag.utils import EmbeddingFunc

    client = _get_embed_client()
    model_name = model or Config.EMBED_MODEL_NAME
    embed_dim = dim or Config.EMBED_DIM
    if not model_name:
        raise ValueError(
            "EMBED_MODEL_NAME ist nicht gesetzt. LightRAG braucht ein "
            "Embedding-Modell — bitte in .env konfigurieren."
        )

    async def _embed(texts: list[str]) -> np.ndarray:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.embeddings.create(
                model=model_name, input=texts, encoding_format="float"
            ),
        )
        # Token-Tracker (Audit-Folge): Embedding-Calls haben prompt_tokens
        # in resp.usage; completion_tokens=0 (Embeddings haben kein Output).
        try:
            from ..utils.token_tracker import tracker
            usage = getattr(resp, "usage", None)
            if usage is not None:
                tracker.record(
                    model=model_name,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=0,
                    purpose="lightrag:embed",
                )
        except Exception:  # noqa: BLE001
            pass
        vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
        if vecs.shape != (len(texts), embed_dim):
            raise RuntimeError(
                f"Embedding-Dimension passt nicht zu EMBED_DIM={embed_dim}: "
                f"Provider lieferte shape={vecs.shape} fuer {len(texts)} texte. "
                f"Bitte EMBED_DIM in .env an das Modell anpassen."
            )
        return vecs

    return EmbeddingFunc(embedding_dim=embed_dim, max_token_size=max_token_size, func=_embed)


_PROMPTS_PATCHED = False


def _apply_prompts_optimization() -> None:
    """Idempotent: leert ``PROMPTS["entity_extraction_examples"]`` einmalig
    pro Prozess, wenn ``Config.LIGHTRAG_DROP_EXAMPLES`` aktiv ist.

    Hintergrund (siehe Phase-4.5-Recherche): Die Default-Examples machen
    ~2.5-3k Tokens pro Extraktions-Call aus — der groesste Einzelposten
    im Prompt-Overhead. Risiko: LLM-Output-Format-Stabilitaet kann leiden.
    Bei Quality-Issues `LIGHTRAG_DROP_EXAMPLES=false` setzen.

    Modul-Mutation ist process-wide; kein Sub-Set von Instanzen kann
    abweichen. Das ist OK fuer MiroFish (1 Prozess pro Service).
    """
    global _PROMPTS_PATCHED
    if _PROMPTS_PATCHED or not Config.LIGHTRAG_DROP_EXAMPLES:
        return
    from lightrag.prompt import PROMPTS
    PROMPTS["entity_extraction_examples"] = []
    _PROMPTS_PATCHED = True


async def create_rag(
    working_dir: str,
    system_prompt_hint_provider: Optional[Callable[[], str]] = None,
):
    """Initialisiert eine LightRAG-Instanz mit dem Pflicht-Init-Pattern.

    Pflicht-Invariante (siehe Modul-Docstring): Aufrufer MUSS sicherstellen,
    dass der hier verwendete Event-Loop fuer die gesamte Lebensdauer der
    Instanz weiterlaeuft — sonst bricht ``initialize_pipeline_status`` spaeter
    mit Loop-Binding-Fehlern weg. Im Produktivpfad uebernimmt das der
    ``RagManager``.

    ``system_prompt_hint_provider`` wird durchgereicht an ``create_llm_func``
    und ist die Schnittstelle fuer ``RagManager.set_ontology`` — Phase-2-Migration.

    Cost-Optimization-Knobs (Phase 4.5, siehe ``Config.LIGHTRAG_*``):
      - ``chunk_token_size``: groessere Chunks = weniger Calls
      - ``entity_extract_max_gleaning``: 0 = Single-Pass (Default-LightRAG: 1)
      - ``max_extract_input_tokens``: Safety-Cap fuer grosse Chunks
      - Examples-Drop: ueber ``_apply_prompts_optimization`` (process-wide)
    """
    from lightrag import LightRAG
    from lightrag.kg.shared_storage import initialize_pipeline_status

    _apply_prompts_optimization()

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=create_llm_func(
            system_prompt_hint_provider=system_prompt_hint_provider,
        ),
        embedding_func=create_embed_func(),
        chunk_token_size=Config.LIGHTRAG_CHUNK_TOKEN_SIZE,
        chunk_overlap_token_size=Config.LIGHTRAG_CHUNK_OVERLAP_TOKEN_SIZE,
        entity_extract_max_gleaning=Config.LIGHTRAG_MAX_GLEANING,
        max_extract_input_tokens=Config.LIGHTRAG_MAX_EXTRACT_INPUT_TOKENS,
    )
    await rag.initialize_storages()
    await initialize_pipeline_status()
    return rag


__all__ = ["create_llm_func", "create_embed_func", "create_rag"]
