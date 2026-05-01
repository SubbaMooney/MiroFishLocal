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


def create_llm_func(model: Optional[str] = None) -> Callable[..., Awaitable[str]]:
    """Liefert eine async LLM-Func im LightRAG-erwarteten Schema.

    LightRAG ruft die Func mit ``(prompt, system_prompt=..., history_messages=...,
    keyword_extraction=..., **kwargs)``. Das OpenAI-SDK ist sync; wir wrappen
    den Call ueber ``run_in_executor`` des aktuellen Event-Loops.
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
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
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
        vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
        if vecs.shape != (len(texts), embed_dim):
            raise RuntimeError(
                f"Embedding-Dimension passt nicht zu EMBED_DIM={embed_dim}: "
                f"Provider lieferte shape={vecs.shape} fuer {len(texts)} texte. "
                f"Bitte EMBED_DIM in .env an das Modell anpassen."
            )
        return vecs

    return EmbeddingFunc(embedding_dim=embed_dim, max_token_size=max_token_size, func=_embed)


async def create_rag(working_dir: str):
    """Initialisiert eine LightRAG-Instanz mit dem Pflicht-Init-Pattern.

    Pflicht-Invariante (siehe Modul-Docstring): Aufrufer MUSS sicherstellen,
    dass der hier verwendete Event-Loop fuer die gesamte Lebensdauer der
    Instanz weiterlaeuft — sonst bricht ``initialize_pipeline_status`` spaeter
    mit Loop-Binding-Fehlern weg. Im Produktivpfad uebernimmt das der
    ``RagManager``.
    """
    from lightrag import LightRAG
    from lightrag.kg.shared_storage import initialize_pipeline_status

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=create_llm_func(),
        embedding_func=create_embed_func(),
    )
    await rag.initialize_storages()
    await initialize_pipeline_status()
    return rag


__all__ = ["create_llm_func", "create_embed_func", "create_rag"]
