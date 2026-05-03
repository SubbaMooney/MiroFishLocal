"""
Ollama-Smoke-Check fuer MiroFish.

Validiert, dass die produktive ``lightrag_factory`` ohne Code-Aenderung
gegen einen lokalen Ollama-Daemon laeuft (OpenAI-SDK-kompatibler Endpoint
auf ``/v1``).

Setup (einmalig):
    ollama pull qwen2.5:7b              # oder ein anderes LLM
    ollama pull nomic-embed-text        # oder ein anderes Embedding-Modell
    ollama serve                        # falls nicht schon als Service

Usage (Defaults targeten qwen2.5:7b + nomic-embed-text):
    cd backend
    uv run python scripts/check_ollama.py
    uv run python scripts/check_ollama.py --llm-model gemma3:27b --embed-model nomic-embed-text --embed-dim 768

Wichtig: Das Script setzt seine Shell-Vars BEVOR ``app.config`` importiert
wird, und blockiert ``dotenv.load_dotenv`` — sonst wuerde die im Repo-Root
liegende ``.env`` (i.d.R. mit OpenAI-Werten gefuellt) die Ollama-Werte
ueberschreiben (Config nutzt ``load_dotenv(override=True)``).

Exit-Code 0 = OK, 1 = Fehler.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def probe_daemon(base_url: str = "http://localhost:11434") -> dict[str, Any]:
    """Liefert die Liste verfuegbarer Modelle, raised bei Verbindungsfehler."""
    url = f"{base_url}/api/tags"
    with urllib.request.urlopen(url, timeout=3) as r:
        data = json.loads(r.read())
    models = [m.get("name", "") for m in data.get("models", [])]
    return {"models": models, "count": len(models)}


def _bootstrap_env(args: argparse.Namespace) -> None:
    """Setzt os.environ + neutralisiert dotenv-Auto-Load.

    Muss VOR dem Import von ``app.config`` laufen — sonst frisst dessen
    ``load_dotenv(override=True)`` unsere Werte und liest die Repo-``.env``.
    """
    base = args.base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    os.environ.setdefault("SECRET_KEY", "ollama-smoke-dummy")
    os.environ["LLM_API_KEY"] = "ollama"
    os.environ["LLM_BASE_URL"] = base
    os.environ["LLM_MODEL_NAME"] = args.llm_model
    os.environ["EMBED_API_KEY"] = "ollama"
    os.environ["EMBED_BASE_URL"] = base
    os.environ["EMBED_MODEL_NAME"] = args.embed_model
    os.environ["EMBED_DIM"] = str(args.embed_dim)

    import dotenv  # type: ignore[import]
    dotenv.load_dotenv = lambda *a, **kw: True  # neutralisiert Config-Auto-Load


async def test_llm_roundtrip() -> str:
    from app.services.lightrag_factory import create_llm_func

    llm = create_llm_func()
    return await llm(
        prompt="Antworte ausschliesslich mit dem Wort 'PONG' und sonst nichts.",
        system_prompt="Du bist ein Test-Bot.",
    )


async def test_embedding() -> tuple[int, int]:
    from app.services.lightrag_factory import create_embed_func

    embed = create_embed_func()
    vecs = await embed.func(["Hallo Welt", "Lorem ipsum"])
    return len(vecs), len(vecs[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama-Smoke-Check fuer MiroFish")
    parser.add_argument("--base-url", default="http://localhost:11434",
                        help="Ollama-Daemon (ohne /v1; default: http://localhost:11434)")
    parser.add_argument("--llm-model", default="qwen2.5:7b",
                        help="LLM-Modellname (default: qwen2.5:7b)")
    parser.add_argument("--embed-model", default="nomic-embed-text",
                        help="Embedding-Modellname (default: nomic-embed-text)")
    parser.add_argument("--embed-dim", type=int, default=768,
                        help="Embedding-Dimension (nomic-embed-text=768, embeddinggemma=768)")
    args = parser.parse_args()

    print("=" * 70)
    print("Ollama-Smoke-Check fuer MiroFish lightrag_factory")
    print("=" * 70)

    # Step 1: Daemon erreichbar?
    try:
        info = probe_daemon(args.base_url)
        print(f"[PASS] Daemon erreichbar, {info['count']} Modelle: {info['models'][:5]}")
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print(f"[FAIL] Daemon nicht erreichbar: {e}")
        print("       Tipp: 'ollama serve' starten oder Service-Status pruefen.")
        return 1

    # Step 2: Env-Bootstrap (vor Config-Import)
    _bootstrap_env(args)

    from app.config import Config
    print(f"[INFO] LLM:    {Config.LLM_MODEL_NAME} via {Config.LLM_BASE_URL}")
    print(f"[INFO] Embed:  {Config.EMBED_MODEL_NAME} via {Config.EMBED_BASE_URL} (dim={Config.EMBED_DIM})")

    # Step 3: LLM-Roundtrip
    try:
        answer = asyncio.run(test_llm_roundtrip())
        print(f"[PASS] LLM-Roundtrip: '{answer.strip()[:60]}'")
    except Exception as e:
        print(f"[FAIL] LLM-Roundtrip: {type(e).__name__}: {e}")
        print(f"       Tipp: Modell vorhanden? 'ollama pull {args.llm_model}'")
        return 1

    # Step 4: Embedding-Format + Dimension
    try:
        n, dim = asyncio.run(test_embedding())
        print(f"[PASS] Embedding: {n} Vektoren, {dim} Dimensionen")
        if dim != args.embed_dim:
            print(f"[WARN] Dimension {dim} != --embed-dim={args.embed_dim} — bitte Argument anpassen.")
            return 1
    except Exception as e:
        print(f"[FAIL] Embedding: {type(e).__name__}: {e}")
        print(f"       Tipp: Modell vorhanden? 'ollama pull {args.embed_model}'")
        return 1

    print()
    print("=" * 70)
    print("ERGEBNIS: Ollama-Setup funktioniert mit der produktiven Factory.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
