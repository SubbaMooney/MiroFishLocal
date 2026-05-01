"""
Pre-Cache der HuggingFace-Modelle, die oasis zur Laufzeit ueber
``AutoTokenizer.from_pretrained``/``AutoModel.from_pretrained`` zieht.

Hintergrund (siehe docs/AUDIT-CAMEL-OASIS-EGRESS.md):
  - oasis.DefaultPlatformType.TWITTER triggert beim Start einen Pull von
    ``Twitter/twhin-bert-base`` (Tokenizer + Model, ca. 350 MB) aus
    huggingface.co. Das ist der einzige verbleibende Egress-Pfad ausserhalb
    des konfigurierten LLM-Providers.
  - Mit ``HF_HUB_OFFLINE=1`` (Default seit commit f15015a) scheitert dieser
    Pull mit klarer Fehlermeldung. Damit Twitter-Simulationen weiterhin
    laufen, muessen die Modelle ein einziges Mal hier vorher gecached werden.
  - Reddit-Pfad braucht keinen HF-Pull (im Audit verifiziert).

Aufruf:
    cd backend && uv run python scripts/precache_hf_models.py [--force]

Optional:
    --cache-dir PATH   eigenes HF-Cache-Verzeichnis (default: ~/.cache/huggingface)
    --extra-models     zusaetzlich sentence-transformers/paraphrase-MiniLM-L6-v2
                       (defensiv, falls camel-ai-Embeddings doch geladen werden)

Nach erfolgreichem Lauf koennen Twitter-Simulationen mit ``HF_HUB_OFFLINE=1``
ohne weiteren Egress laufen — die Modelle werden aus dem lokalen Cache geladen.
"""

from __future__ import annotations

# WICHTIG: HF_HUB_OFFLINE explizit ausschalten BEVOR transformers importiert
# wird. Config setzt das per Default auf 1; dieses Skript muss aktiv online
# sein, um die Modelle einmalig zu ziehen.
import os

os.environ["HF_HUB_OFFLINE"] = "0"
os.environ.pop("TRANSFORMERS_OFFLINE", None)

import argparse
import sys
import time
from pathlib import Path
from typing import Iterable

# Modelle, die oasis zur Laufzeit braucht (siehe Audit-Bericht):
#   - Twitter/twhin-bert-base: oasis/social_platform/recsys.py:68/78
TWHIN_BERT = "Twitter/twhin-bert-base"

# Defensiv mit-cachen (nicht zwingend, aber im Audit als Empfehlung gelistet):
#   - sentence-transformers/paraphrase-MiniLM-L6-v2 fuer eventuelle camel-ai
#     embedding-pfade, die im aktuellen Backend zwar nicht aktiv sind, aber
#     ohne Pre-Cache spaeter unsichtbar Egress erzeugen wuerden.
EXTRA_MODELS = [
    "sentence-transformers/paraphrase-MiniLM-L6-v2",
]


def _print_header(msg: str) -> None:
    line = "=" * 70
    print(f"\n{line}\n{msg}\n{line}")


def _cache_via_transformers(model_id: str, force: bool) -> None:
    """Laedt tokenizer + model wie oasis es tut (snapshot-basiert)."""
    from transformers import AutoModel, AutoTokenizer

    print(f"  Lade Tokenizer fuer {model_id} ...", flush=True)
    t0 = time.perf_counter()
    AutoTokenizer.from_pretrained(model_id, force_download=force)
    print(f"    OK ({time.perf_counter() - t0:.1f}s)")

    print(f"  Lade Model fuer {model_id} ...", flush=True)
    t0 = time.perf_counter()
    AutoModel.from_pretrained(model_id, force_download=force)
    print(f"    OK ({time.perf_counter() - t0:.1f}s)")


def _verify_offline_load(model_id: str) -> bool:
    """Reload mit HF_HUB_OFFLINE=1 — beweist, dass der Cache reicht."""
    # Wir tunneln das ueber einen subprocess-aufruf, damit der HF_HUB_OFFLINE-
    # Toggle nicht im current-process-state haengen bleibt.
    import subprocess

    code = (
        "import os; os.environ['HF_HUB_OFFLINE']='1';"
        "from transformers import AutoTokenizer, AutoModel;"
        f"AutoTokenizer.from_pretrained('{model_id}');"
        f"AutoModel.from_pretrained('{model_id}');"
        "print('OFFLINE_OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0 and "OFFLINE_OK" in result.stdout:
        return True
    print(f"  WARN: offline-verify fuer {model_id} fehlgeschlagen")
    if result.stderr:
        print(f"  stderr: {result.stderr[-500:]}")
    return False


def _pre_cache(models: Iterable[str], force: bool, verify: bool) -> int:
    """Cached jedes Modell. Liefert Exit-Code (0 = OK)."""
    failed: list[str] = []
    for model_id in models:
        _print_header(f"Pre-cache: {model_id}")
        try:
            _cache_via_transformers(model_id, force=force)
            if verify and not _verify_offline_load(model_id):
                failed.append(model_id)
        except Exception as e:
            print(f"  FEHLER beim cachen von {model_id}: {e}")
            failed.append(model_id)

    print()
    print("=" * 70)
    if failed:
        print(f"FEHLGESCHLAGEN: {len(failed)} Modell(e) konnten nicht gecached werden:")
        for m in failed:
            print(f"  - {m}")
        return 1
    print(f"OK: alle {len(list(models))} Modell(e) erfolgreich gecached.")
    print("Twitter-Simulationen koennen jetzt mit HF_HUB_OFFLINE=1 laufen.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HuggingFace-Modelle fuer oasis Twitter-Simulation pre-cachen")
    parser.add_argument("--force", action="store_true", help="Re-download auch wenn Cache existiert")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Eigenes HF-Cache-Verzeichnis")
    parser.add_argument("--extra-models", action="store_true", help="Zusaetzlich sentence-transformers/paraphrase-MiniLM-L6-v2 cachen")
    parser.add_argument("--no-verify", action="store_true", help="Offline-Reload-Verifikation ueberspringen")
    args = parser.parse_args()

    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(args.cache_dir.resolve())
        print(f"[INFO] HF_HOME = {os.environ['HF_HOME']}")

    models = [TWHIN_BERT]
    if args.extra_models:
        models.extend(EXTRA_MODELS)

    return _pre_cache(models, force=args.force, verify=not args.no_verify)


if __name__ == "__main__":
    sys.exit(main())
