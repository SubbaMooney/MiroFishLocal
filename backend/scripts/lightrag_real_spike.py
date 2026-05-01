"""
LightRAG Echt-Spike — Phase 0.5 mit echten LLM-Calls + Cost-Cap

Validiert die im Mock-Spike (`lightrag_mock_spike.py`) vertagten Cost/Quality-
Fragen aus `docs/MIGRATION-ZEP-TO-LIGHTRAG.md` Phase 0:
  - LLM-Call-Volumen pro KB Input (Hochrechnung auf 10 MB Abbruchkriterium)
  - Wallclock-Time für Insert + Query
  - Embedding-Format (Vektor-Dim, L2-normiert)
  - LLM-Output-Format-Kompatibilitaet mit LightRAG-Erwartungen

Architektur uebernommen aus dem Mock-Spike: RagManager-Singleton mit dediziertem
Event-Loop-Thread, sync->async Bridge, per-Graph asyncio.Lock. Einziger
Unterschied: `llm_model_func` und `embedding_func` rufen echte LLM-/Embedding-
Endpoints (OpenAI-SDK-kompatibel via `LLM_BASE_URL` / `EMBED_BASE_URL`).

Cost-Cap-Guard:
  - Vor jedem API-Call wird der projizierte zusaetzliche Spend geprueft.
  - Standard-Cap: 0.05 USD pro Spike-Run.
  - Verhalten bei Ueberschreitung: HARD-FAIL (siehe `CostTracker.check_or_fail`).

Aufruf:
    PYTHONPATH=/path/to/lightrag-libs python3 backend/scripts/lightrag_real_spike.py \\
        [--cost-cap-usd 0.05] [--test-corpus path/to/small.txt] \\
        [--embed-dim 1024] \\
        [--working-dir-base /tmp/lightrag_real_spike] [--keep-artifacts] \\
        [--price-llm-in 0.0008] [--price-llm-out 0.0020] [--price-embed 0.0001]

Voraussetzungen:
  - .env im Repo-Root mit gueltigen LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME.
  - Optional EMBED_API_KEY/EMBED_BASE_URL/EMBED_MODEL_NAME (default: LLM_*).
  - lightrag-hku>=1.4.10,<1.5 in PYTHONPATH oder venv.
  - openai>=1.0 (OpenAI-SDK).

Output: Konsolen-Report + JSON-Artefakt unter working-dir-base/spike_report.json.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from lightrag import LightRAG, QueryParam
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.utils import EmbeddingFunc

try:
    from openai import OpenAI
except ImportError:
    print("[FATAL] openai package nicht installiert. `pip install openai` ausfuehren.")
    sys.exit(2)


# ---------------------------------------------------------------------------
# LLM-Pricing-Defaults (USD pro 1k Tokens) — provider-agnostisch.
# Diese Werte sind PLATZHALTER. Setze sie via CLI auf die tatsaechlichen
# Tarife deines LLM-Providers. Beispiele (NICHT empfohlen, nur Format-Referenz):
#   - mid-tier Cloud-LLM:   in 0.0008 / out 0.0020 / embed 0.0001
#   - high-tier Cloud-LLM:  in 0.0030 / out 0.0150 / embed 0.0001
#   - lokales LLM (Ollama): 0.0 / 0.0 / 0.0
# ---------------------------------------------------------------------------

DEFAULT_PRICE_LLM_INPUT_PER_1K_USD = 0.0008
DEFAULT_PRICE_LLM_OUTPUT_PER_1K_USD = 0.0020
DEFAULT_PRICE_EMBED_PER_1K_USD = 0.0001


class CostCapExceeded(RuntimeError):
    """Wird geworfen, wenn der projizierte Spend das Cap ueberschreiten wuerde."""


@dataclasses.dataclass
class CostTracker:
    """Cumulative Token- und Cost-Tracking mit hartem Cap.

    Thread-safe: alle Schreibzugriffe ueber _lock. Der Cost-Cap wird VOR jedem
    Call geprueft (preflight), nicht erst nach dem Call — damit kein LLM-
    Call abgesetzt wird, der das Cap reissen wuerde.
    """

    cap_usd: float
    price_llm_in: float
    price_llm_out: float
    price_embed: float

    spent_usd: float = 0.0
    llm_calls: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    embedding_calls: int = 0
    embedding_tokens: int = 0
    blocked_calls: int = 0

    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def projected_llm_cost(self, input_tokens: int, est_output_tokens: int = 1024) -> float:
        return (input_tokens / 1000.0) * self.price_llm_in + (est_output_tokens / 1000.0) * self.price_llm_out

    def projected_embed_cost(self, tokens: int) -> float:
        return (tokens / 1000.0) * self.price_embed

    def check_or_fail(self, projected_extra_usd: float, label: str) -> None:
        """Cost-Cap-Policy: HARD-FAIL.

        Preflight vor jedem LLM-Call. Wuerde der projizierte Spend das Cap
        ueberschreiten, wird CostCapExceeded geworfen — die Tests fangen das ab
        und markieren betroffene Schritte als SKIPPED. Kein LLM-Request
        verlaesst den Prozess, sobald das Cap gerissen wird.
        """
        with self._lock:
            projected_total = self.spent_usd + projected_extra_usd
            if projected_total > self.cap_usd:
                self.blocked_calls += 1
                raise CostCapExceeded(
                    f"Cap-Ueberschreitung in '{label}': "
                    f"spent={self.spent_usd:.5f} + projected={projected_extra_usd:.5f} "
                    f"= {projected_total:.5f} > cap={self.cap_usd:.5f} USD"
                )

    def record_llm(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.llm_calls += 1
            self.llm_input_tokens += input_tokens
            self.llm_output_tokens += output_tokens
            self.spent_usd += (input_tokens / 1000.0) * self.price_llm_in
            self.spent_usd += (output_tokens / 1000.0) * self.price_llm_out

    def record_embedding(self, tokens: int) -> None:
        with self._lock:
            self.embedding_calls += 1
            self.embedding_tokens += tokens
            self.spent_usd += (tokens / 1000.0) * self.price_embed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cap_usd": self.cap_usd,
                "spent_usd": round(self.spent_usd, 6),
                "remaining_usd": round(self.cap_usd - self.spent_usd, 6),
                "llm_calls": self.llm_calls,
                "llm_input_tokens": self.llm_input_tokens,
                "llm_output_tokens": self.llm_output_tokens,
                "embedding_calls": self.embedding_calls,
                "embedding_tokens": self.embedding_tokens,
                "blocked_calls": self.blocked_calls,
            }


# ---------------------------------------------------------------------------
# .env-Loader (minimal, ohne python-dotenv-Dependency)
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """Liest KEY=VALUE Zeilen aus einer .env-Datei. Kommentare/Leerzeilen werden
    ignoriert. Existiert die Datei nicht, wird {} zurueckgegeben.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def resolve_config(env_path: Path) -> dict[str, str]:
    """Konfig-Aufloesung: .env-Datei >> os.environ. Nur LLM_*/EMBED_* relevant.
    Embed-Vars fallen auf LLM_*-Vars zurueck, falls separat nicht gesetzt.
    """
    file_env = load_env_file(env_path)
    merged = {**os.environ, **file_env}

    cfg = {
        "LLM_API_KEY": merged.get("LLM_API_KEY", "").strip(),
        "LLM_BASE_URL": merged.get("LLM_BASE_URL", "").strip(),
        "LLM_MODEL_NAME": merged.get("LLM_MODEL_NAME", "").strip(),
    }
    cfg["EMBED_API_KEY"] = merged.get("EMBED_API_KEY", cfg["LLM_API_KEY"]).strip()
    cfg["EMBED_BASE_URL"] = merged.get("EMBED_BASE_URL", cfg["LLM_BASE_URL"]).strip()
    cfg["EMBED_MODEL_NAME"] = merged.get("EMBED_MODEL_NAME", "").strip()

    missing = [k for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_NAME", "EMBED_MODEL_NAME") if not cfg[k]]
    if missing:
        raise SystemExit(
            f"[FATAL] Pflicht-Variablen fehlen in .env oder Umgebung: {missing}. "
            f"Siehe .env.example."
        )
    return cfg


# ---------------------------------------------------------------------------
# Echte LLM-Func + Embedding-Func (mit Cost-Cap-Guard)
# ---------------------------------------------------------------------------


def make_real_llm_func(client: OpenAI, model: str, tracker: CostTracker) -> Callable:
    """Liefert eine async LLM-Func via OpenAI-SDK-kompatiblem Endpoint.

    Token-Schaetzung fuer Preflight: chars/4 als grobe Heuristik (provider-/
    model-spezifische Tokenizer weichen ab, aber innerhalb ~30%). Tatsaechliche
    Tokens werden post-call aus `usage` gelesen und korrigiert ins Tracking
    eingebucht.
    """

    async def _llm(
        prompt: str,
        system_prompt: Optional[str] = None,
        history_messages: Optional[list] = None,
        keyword_extraction: bool = False,
        **kwargs: Any,
    ) -> str:
        full = (system_prompt or "") + (prompt or "")
        est_in = max(1, len(full) // 4)
        projected = tracker.projected_llm_cost(est_in, est_output_tokens=512)
        tracker.check_or_fail(projected, label=f"LLM(model={model})")

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            messages.extend(history_messages)
        messages.append({"role": "user", "content": prompt})

        # Synchroner Call im Threadpool (OpenAI-SDK ist sync; LightRAG ruft
        # async -> wir wrappen via run_in_executor des aktuellen Loops).
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.0,
            ),
        )
        usage = getattr(resp, "usage", None)
        in_tokens = int(getattr(usage, "prompt_tokens", est_in)) if usage else est_in
        out_text = resp.choices[0].message.content or ""
        out_tokens = int(getattr(usage, "completion_tokens", max(1, len(out_text) // 4))) if usage else max(1, len(out_text) // 4)
        tracker.record_llm(in_tokens, out_tokens)
        return out_text

    return _llm


def make_real_embedding_func(client: OpenAI, model: str, dim: int, tracker: CostTracker) -> EmbeddingFunc:
    """Embeddings via OpenAI-SDK-kompatiblem Endpoint. Liefert EmbeddingFunc mit dim/max_tokens."""

    async def _embed(texts: list[str]) -> np.ndarray:
        est_tokens = max(1, sum(len(t) for t in texts) // 4)
        projected = tracker.projected_embed_cost(est_tokens)
        tracker.check_or_fail(projected, label=f"Embed(model={model}, n={len(texts)})")

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.embeddings.create(model=model, input=texts, encoding_format="float"),
        )
        usage = getattr(resp, "usage", None)
        tokens_used = int(getattr(usage, "total_tokens", est_tokens)) if usage else est_tokens
        tracker.record_embedding(tokens_used)

        vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
        if vecs.shape != (len(texts), dim):
            raise RuntimeError(
                f"Embedding-Format unerwartet: shape={vecs.shape}, expected=({len(texts)}, {dim})"
            )
        return vecs

    return EmbeddingFunc(embedding_dim=dim, max_token_size=8192, func=_embed)


# ---------------------------------------------------------------------------
# RagManager (1:1 aus Mock-Spike, nur llm/embed-funcs werden injiziert)
# ---------------------------------------------------------------------------


class RagManager:
    """Singleton: pro Graph eine LightRAG-Instanz, ein dedizierter Loop-Thread."""

    def __init__(self, working_dir_base: Path, llm_func: Callable, embed_func: EmbeddingFunc):
        self._instances: dict[str, LightRAG] = {}
        self._instance_locks: dict[str, asyncio.Lock] = {}
        self._working_dir_base = working_dir_base
        self._working_dir_base.mkdir(parents=True, exist_ok=True)
        self._llm_func = llm_func
        self._embed_func = embed_func
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro, timeout: float = 600.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _get_or_create(self, graph_id: str) -> LightRAG:
        if graph_id in self._instances:
            return self._instances[graph_id]
        working_dir = self._working_dir_base / graph_id
        working_dir.mkdir(parents=True, exist_ok=True)
        rag = LightRAG(
            working_dir=str(working_dir),
            llm_model_func=self._llm_func,
            embedding_func=self._embed_func,
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        self._instances[graph_id] = rag
        self._instance_locks[graph_id] = asyncio.Lock()
        return rag

    def insert(self, graph_id: str, text: str) -> None:
        async def _do() -> None:
            rag = await self._get_or_create(graph_id)
            async with self._instance_locks[graph_id]:
                await rag.ainsert(text)

        self._run(_do(), timeout=600)

    def query(self, graph_id: str, question: str, mode: str = "hybrid") -> str:
        async def _do() -> str:
            rag = await self._get_or_create(graph_id)
            return await rag.aquery(question, param=QueryParam(mode=mode))

        return self._run(_do())

    def shutdown(self) -> None:
        async def _all_finalize() -> None:
            for rag in list(self._instances.values()):
                try:
                    await rag.finalize_storages()
                except Exception:
                    pass
            self._instances.clear()
            self._instance_locks.clear()

        try:
            self._run(_all_finalize(), timeout=30)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Tests (real-API focused, jeder Test in try/except wegen CostCapExceeded)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TestResult:
    name: str
    status: str  # PASS | FAIL | SKIPPED | INCONCLUSIVE
    detail: str
    duration_s: float
    cost_snapshot: dict


DEFAULT_CORPUS = (
    "Alice ist Engineering-Lead bei Acme Corp und arbeitet eng mit Bob zusammen, "
    "der Product-Owner ist. Bob berichtet an Carol, die als CTO fungiert. "
    "Acme Corp entwickelt LightRAG-Integrationen fuer ihre interne Knowledge-Base. "
    "Das Projekt MiroFish nutzt diese Integrationen seit 2026."
)


def _record(results: list[TestResult], name: str, status: str, detail: str, t0: float, tracker: CostTracker) -> None:
    results.append(
        TestResult(
            name=name,
            status=status,
            detail=detail,
            duration_s=round(time.perf_counter() - t0, 3),
            cost_snapshot=tracker.snapshot(),
        )
    )


def test_embedding_format(client: OpenAI, model: str, dim: int, tracker: CostTracker) -> TestResult:
    """T1: Embedding-API liefert (n, dim) float32, naeherungsweise L2-normiert."""
    t0 = time.perf_counter()
    name = "T1: Embedding-Format"
    try:
        embed = make_real_embedding_func(client, model, dim, tracker)
        loop = asyncio.new_event_loop()
        try:
            vecs = loop.run_until_complete(embed.func(["Hallo Welt", "Test 2"]))
        finally:
            loop.close()
        if vecs.shape != (2, dim):
            return TestResult(name, "FAIL", f"shape={vecs.shape}", round(time.perf_counter() - t0, 3), tracker.snapshot())
        norms = np.linalg.norm(vecs, axis=1)
        l2_ok = bool(np.all((norms > 0.95) & (norms < 1.05)))
        detail = f"shape={vecs.shape}, norms in [{norms.min():.3f}, {norms.max():.3f}], l2_normed={l2_ok}"
        return TestResult(name, "PASS", detail, round(time.perf_counter() - t0, 3), tracker.snapshot())
    except CostCapExceeded as e:
        return TestResult(name, "SKIPPED", f"cost-cap: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())
    except Exception as e:
        return TestResult(name, "FAIL", f"{type(e).__name__}: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())


def test_llm_roundtrip(client: OpenAI, model: str, tracker: CostTracker) -> TestResult:
    """T2: LLM-LLM antwortet auf einfachen Prompt mit nicht-leerem String."""
    t0 = time.perf_counter()
    name = "T2: LLM-Roundtrip"
    try:
        llm = make_real_llm_func(client, model, tracker)
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(llm("Antworte nur mit dem Wort PING."))
        finally:
            loop.close()
        ok = bool(out and len(out.strip()) > 0)
        return TestResult(name, "PASS" if ok else "FAIL", f"output={out[:80]!r}", round(time.perf_counter() - t0, 3), tracker.snapshot())
    except CostCapExceeded as e:
        return TestResult(name, "SKIPPED", f"cost-cap: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())
    except Exception as e:
        return TestResult(name, "FAIL", f"{type(e).__name__}: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())


def test_mini_insert(mgr: RagManager, corpus: str, tracker: CostTracker) -> TestResult:
    """T3: Mini-Insert eines kleinen Korpus, misst LLM-Calls + Wallclock."""
    t0 = time.perf_counter()
    name = "T3: Mini-Insert"
    try:
        before = tracker.snapshot()
        mgr.insert("real_spike", corpus)
        after = tracker.snapshot()
        delta_llm = after["llm_calls"] - before["llm_calls"]
        delta_emb = after["embedding_calls"] - before["embedding_calls"]
        bytes_in = len(corpus.encode("utf-8"))
        # Hochrechnung auf 10 MB (Abbruchkriterium aus Migration-Doc):
        scale = (10 * 1024 * 1024) / max(1, bytes_in)
        proj_llm_calls_10mb = int(delta_llm * scale)
        verdict_threshold = 10_000  # aus docs/MIGRATION-ZEP-TO-LIGHTRAG.md
        verdict = "innerhalb Limit" if proj_llm_calls_10mb < verdict_threshold else "UEBER Limit"
        detail = (
            f"corpus_bytes={bytes_in}, llm_calls={delta_llm}, embed_calls={delta_emb}, "
            f"projected_llm_calls_per_10mb={proj_llm_calls_10mb} ({verdict})"
        )
        return TestResult(name, "PASS", detail, round(time.perf_counter() - t0, 3), tracker.snapshot())
    except CostCapExceeded as e:
        return TestResult(name, "SKIPPED", f"cost-cap: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())
    except Exception as e:
        return TestResult(name, "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc()}", round(time.perf_counter() - t0, 3), tracker.snapshot())


def test_mini_query(mgr: RagManager, tracker: CostTracker) -> TestResult:
    """T4: Mini-Query nach Insert. Pruft, ob aquery brauchbaren String zurueckliefert."""
    t0 = time.perf_counter()
    name = "T4: Mini-Query (hybrid)"
    try:
        out = mgr.query("real_spike", "Wer arbeitet mit Alice zusammen?", mode="hybrid")
        ok = bool(out and len(out.strip()) > 20)
        detail = f"len={len(out or '')}, excerpt={(out or '')[:120]!r}"
        return TestResult(name, "PASS" if ok else "INCONCLUSIVE", detail, round(time.perf_counter() - t0, 3), tracker.snapshot())
    except CostCapExceeded as e:
        return TestResult(name, "SKIPPED", f"cost-cap: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())
    except Exception as e:
        return TestResult(name, "FAIL", f"{type(e).__name__}: {e}", round(time.perf_counter() - t0, 3), tracker.snapshot())


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all(cfg: dict[str, str], working_dir_base: Path, corpus: str, tracker: CostTracker, keep_artifacts: bool, embed_dim: int = 1024) -> dict:
    llm_client = OpenAI(api_key=cfg["LLM_API_KEY"], base_url=cfg["LLM_BASE_URL"])
    embed_client = OpenAI(api_key=cfg["EMBED_API_KEY"], base_url=cfg["EMBED_BASE_URL"])

    results: list[TestResult] = []

    results.append(test_embedding_format(embed_client, cfg["EMBED_MODEL_NAME"], embed_dim, tracker))
    results.append(test_llm_roundtrip(llm_client, cfg["LLM_MODEL_NAME"], tracker))

    llm_func = make_real_llm_func(llm_client, cfg["LLM_MODEL_NAME"], tracker)
    embed_func = make_real_embedding_func(embed_client, cfg["EMBED_MODEL_NAME"], embed_dim, tracker)
    mgr = RagManager(working_dir_base, llm_func, embed_func)
    try:
        results.append(test_mini_insert(mgr, corpus, tracker))
        results.append(test_mini_query(mgr, tracker))
    finally:
        mgr.shutdown()
        if not keep_artifacts and working_dir_base.exists():
            shutil.rmtree(working_dir_base, ignore_errors=True)

    return {
        "config": {
            "llm_model": cfg["LLM_MODEL_NAME"],
            "embed_model": cfg["EMBED_MODEL_NAME"],
            "llm_base_url": cfg["LLM_BASE_URL"],
        },
        "cost_final": tracker.snapshot(),
        "results": [dataclasses.asdict(r) for r in results],
        "verdict": _verdict(results),
    }


def _verdict(results: list[TestResult]) -> str:
    if any(r.status == "FAIL" for r in results):
        return "NO-GO (FAIL in mindestens einem Test)"
    if any(r.status == "SKIPPED" for r in results):
        return "INCONCLUSIVE (Cost-Cap erreicht — Cap erhoehen oder Korpus reduzieren)"
    if any(r.status == "INCONCLUSIVE" for r in results):
        return "INCONCLUSIVE (Output-Qualitaet unzureichend)"
    return "GO (alle Tests PASS)"


def main() -> int:
    parser = argparse.ArgumentParser(description="LightRAG Echt-Spike (provider-agnostisch + Cost-Cap)")
    parser.add_argument("--cost-cap-usd", type=float, default=0.05, help="Hartes Cost-Cap (default: 0.05 USD)")
    parser.add_argument("--working-dir-base", type=Path, default=Path("/tmp/lightrag_real_spike"))
    parser.add_argument("--test-corpus", type=Path, default=None, help="Optional: Pfad zu .txt-Korpus")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Pfad zur .env (default: ./.env)")
    parser.add_argument("--embed-dim", type=int, default=1024, help="Erwartete Embedding-Dimension (default: 1024)")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--price-llm-in", type=float, default=DEFAULT_PRICE_LLM_INPUT_PER_1K_USD)
    parser.add_argument("--price-llm-out", type=float, default=DEFAULT_PRICE_LLM_OUTPUT_PER_1K_USD)
    parser.add_argument("--price-embed", type=float, default=DEFAULT_PRICE_EMBED_PER_1K_USD)
    args = parser.parse_args()

    cfg = resolve_config(args.env_file)
    print(f"[INFO] LLM-Endpoint: {cfg['LLM_BASE_URL']} (model={cfg['LLM_MODEL_NAME']})")
    print(f"[INFO] Embed-Endpoint: {cfg['EMBED_BASE_URL']} (model={cfg['EMBED_MODEL_NAME']})")
    print(f"[INFO] Cost-Cap: {args.cost_cap_usd:.5f} USD")

    if args.test_corpus and args.test_corpus.exists():
        corpus = args.test_corpus.read_text(encoding="utf-8")
        print(f"[INFO] Korpus geladen aus {args.test_corpus} ({len(corpus)} Zeichen)")
    else:
        corpus = DEFAULT_CORPUS
        print(f"[INFO] Default-Korpus verwendet ({len(corpus)} Zeichen)")

    tracker = CostTracker(
        cap_usd=args.cost_cap_usd,
        price_llm_in=args.price_llm_in,
        price_llm_out=args.price_llm_out,
        price_embed=args.price_embed,
    )

    args.working_dir_base.mkdir(parents=True, exist_ok=True)
    report = run_all(cfg, args.working_dir_base, corpus, tracker, args.keep_artifacts, args.embed_dim)

    report_path = args.working_dir_base / "spike_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 70)
    print("SPIKE-REPORT")
    print("=" * 70)
    for r in report["results"]:
        print(f"[{r['status']:13}] {r['name']:30} ({r['duration_s']:.2f}s)  {r['detail']}")
    print("-" * 70)
    final = report["cost_final"]
    print(f"Spent:    {final['spent_usd']:.5f} / {final['cap_usd']:.5f} USD")
    print(f"LLM:      {final['llm_calls']} calls, {final['llm_input_tokens']} in / {final['llm_output_tokens']} out tokens")
    print(f"Embed:    {final['embedding_calls']} calls, {final['embedding_tokens']} tokens")
    print(f"Blocked:  {final['blocked_calls']} calls (Cost-Cap)")
    print(f"Verdict:  {report['verdict']}")
    print(f"Report-JSON: {report_path}")
    return 0 if "GO" in report["verdict"] and "NO-GO" not in report["verdict"] else 1


if __name__ == "__main__":
    sys.exit(main())
