# Spike-Report — LightRAG Phase 0 (Mock-Spike)

Datum: 2026-04-30 - Branch: `spike/lightrag-phase0-mock` - Worktree-HEAD: `df5d74b` (Plan-Stand) - LightRAG-Version: `1.4.15`

Verdikt: GREEN. 7 von 7 Annahmen aus dem Migrationsplan validiert ohne einen einzigen echten LLM-API-Call. Aufwand: ca. 2 h Engineering. Kosten: 0 USD.

WICHTIG: Es gibt einen substantiellen Befund am `RagManager`-Skeleton aus `docs/MIGRATION-ZEP-TO-LIGHTRAG.md` Sektion "Code-Skeleton". Dieser ist im Spike-Code bereits korrigiert und muss in den produktiven Code von Phase 1 mit der Korrektur übernommen werden. Details siehe Sektion "Befund am RagManager-Skeleton".

## Was ist ein "Mock-Spike"?

Wir validieren die im Plan definierten kritischen Annahmen, indem wir LightRAG mit voll gemockten LLM- und Embedding-Funktionen laufen lassen. Die Mocks erzeugen deterministische Synthetic-Outputs im Format, das LightRAG erwartet (Entity-Extraction-Records mit Tuple-Delimitern, JSON für Keyword-Extraction, freier String für Query). Es findet kein einziger echter Network-Call statt.

Der Spike zeigt damit Code-Bereitschaft (Setup, API-Verträge, Threading-Modell, Storage-Persistenz). Er macht keine Aussage über LLM-Cost oder Output-Qualität (siehe Sektion "Cost/Performance — vertagt").

## Mock-Spike — was wurde validiert

| ID | Annahme aus dem Plan | Wie getestet | Ergebnis |
|---|---|---|---|
| A1 | Pflicht-Init-Pattern (`initialize_storages` + `initialize_pipeline_status`) funktioniert ohne `KeyError: history_messages` oder `AttributeError: __aenter__`. | LightRAG-Instanz frisch anlegen, beide Init-Calls, dann `ainsert` mit Demo-Text. Alles im langlebigen RagManager-Loop, nicht in temporärem `asyncio.run`. | PASS |
| A2 | RagManager-Singleton mit dediziertem Event-Loop-Thread und Sync-API funktioniert threadsafe. | Zwei sequentielle `mgr.insert(...)` aus dem Hauptthread, danach `mgr.get_all_nodes(...)` aus einem zweiten OS-Thread. | PASS (`nodes_count=3` nach 2 Inserts) |
| A3 | Multi-Project-Isolation via `working_dir`. Insert in A bleibt unsichtbar in B. | Zwei separate `graph_id`s mit jeweils eigenem `working_dir` gleichzeitig betreiben. | PASS (12 Dateien in jedem `working_dir`, getrennte Pfade) |
| A4 | NetworkX-Graph-Zugriff für strukturierte Reads (für `get_all_nodes` / `get_all_edges` Migration). | `rag.chunk_entity_relation_graph.get_all_nodes()` und `.get_all_edges()` als async Calls. | PASS — siehe Korrektur-Hinweis unten |
| A5 | Per-Graph `asyncio.Lock` verhindert NanoVectorDB-Race. | 4 parallele OS-Threads inserten gleichzeitig in denselben Graph; Lock muss serialisieren. | PASS (kein Crash, finale Konsistenz `nodes=3, edges=2`) |
| A6 | Storage-Persistenz: nach Insert liegt das erwartete File-Layout im `working_dir`. | Nach Insert die Files im `working_dir` listen und gegen erwartetes Pattern prüfen. | PASS — siehe Korrektur-Hinweis unten |
| A7 | `shutil.rmtree(working_dir)` als Delete-Pfad ist sauber. | Insert, dann `mgr.delete(graph_id)`, dann prüfen dass Verzeichnis und Manager-Instanz weg sind. | PASS |

LLM-Mock-Calls bei kompletter Suite: 24 - Embedding-Mock-Calls: 72 - Wallclock: ca. 0,5 s

### Korrekturhinweise zum Migrationsplan

Drei kleine Detail-Korrekturen am Plan, die der Spike aufgedeckt hat:

1. **A4 — Plan-Text vs. echte LightRAG-API**: Im Plan steht `rag.chunk_entity_relation_graph.nodes()` / `.edges()`. Das ist die direkte NetworkX-Iteration auf dem Graph-Objekt. Der echte Vertrag von LightRAG (`BaseGraphStorage`-Interface, Implementierung `lightrag/kg/networkx_impl.py:511-538`) ist:

   ```python
   nodes = await rag.chunk_entity_relation_graph.get_all_nodes()  # async
   edges = await rag.chunk_entity_relation_graph.get_all_edges()  # async
   ```

   Beide liefern `list[dict]` mit Properties wie `id`, `entity_type`, `description`, `created_at`, `file_path` (Knoten) bzw. `source`, `target`, `keywords`, `description` (Edges). Die direkte `.nodes()`/`.edges()`-Form ist als private NetworkX-API verfügbar, aber unter `_graph` versteckt — die supportierte API ist async. Für die Phase-3-Migration in `entity_reader.py` muss daher per `RagManager` gewrappt werden, wie im Spike-Skript vorgemacht.

2. **A6 — File-Endung NanoVectorDB**: Im Plan steht `vdb_*.pkl`. NanoVectorDB persistiert tatsächlich als JSON: `vdb_entities.json`, `vdb_relationships.json`, `vdb_chunks.json`. Das tatsächliche Layout im `working_dir` (12 Dateien pro Graph):
   - `graph_chunk_entity_relation.graphml`
   - `kv_store_doc_status.json`, `kv_store_full_docs.json`, `kv_store_text_chunks.json`
   - `kv_store_full_entities.json`, `kv_store_full_relations.json`
   - `kv_store_entity_chunks.json`, `kv_store_relation_chunks.json`
   - `kv_store_llm_response_cache.json`
   - `vdb_entities.json`, `vdb_relationships.json`, `vdb_chunks.json`

3. **A1/A2 — Loop-Bindung von `initialize_pipeline_status()`**: siehe nächste Sektion (kritischer Befund).

## Befund am RagManager-Skeleton

Beim ersten Spike-Run (vor der Korrektur im Test-Code) ist Folgendes passiert: A1 lief in einem `asyncio.run(_run())`-Loop, A2..A7 liefen anschließend im `RagManager`-Loop-Thread. Resultat: A2..A7 brachen alle mit folgendem Fehler ab:

```
RuntimeError: <asyncio.locks.Lock object at 0x...> is bound to a different event loop
  File "lightrag/kg/shared_storage.py", line 170, in __aenter__
    await self._lock.acquire()
```

Root cause: `initialize_pipeline_status()` aus `lightrag.kg.shared_storage` legt einen prozessweiten globalen `pipeline_status_lock` an und bindet ihn an den **aktuellen** `asyncio`-Loop. Der Lock ist Modul-State (über alle LightRAG-Instanzen geteilt). Wird `initialize_pipeline_status()` zuerst in Loop X aufgerufen und Loop X danach beendet, schlagen alle nachfolgenden LightRAG-Calls aus Loop Y fehl, weil Python-asyncio-Locks fest an ihren Erst-Loop gebunden sind.

Konkrete Konsequenzen für Phase 1:

- Im `RagManager`-Skeleton (`docs/MIGRATION-ZEP-TO-LIGHTRAG.md` Zeile 183-242) ist `await initialize_pipeline_status()` korrekt im `_get_or_create`-Coroutine aufgerufen, läuft also automatisch im RagManager-Loop. Das ist im Plan richtig dokumentiert. **Aber**: Wenn jemand in Tests, im Startup, in CLI-Skripten oder in einem REPL `await initialize_pipeline_status()` im Hauptthread mit eigenem `asyncio.run` aufruft (z.B. zum Smoke-Test), dann ist der globale Lock danach an den toten Loop gebunden, und der Produktiv-Pfad ist tot.

- Pflicht-Regel für Phase 1: **`initialize_pipeline_status()` darf im gesamten Prozess nur in einem einzigen, langlebigen Event-Loop aufgerufen werden — dem `RagManager`-Loop.** Das gehört in `backend/app/services/rag_manager.py` als Doc-String und in den Phase-1-PR als Lint-Regel (z.B. via Grep-Check).

- Empfehlung für die Test-Suite (Phase 1 pytest-Smoke): Der Smoke-Test darf nicht direkt `await initialize_pipeline_status()` aufrufen, sondern muss über den `RagManager` gehen. Der Spike-Code zeigt dieses Pattern in `test_1_init_pattern(mgr, ...)`.

Diese Erkenntnis war ohne Mock-Spike nicht trivial zu sehen — die Doku des LightRAG-Repos warnt zwar vor "Init nicht vergessen" (`KeyError: history_messages`), aber der Loop-Bindings-Fallstrick taucht in Issues erst bei mehrthread-Setups auf (z.B. Issue #2527 zu Multi-Tenancy).

## Cost/Performance — vertagt

Folgende Punkte aus Phase 0 des Migrationsplans wurden NICHT validiert, weil sie ohne echte API-Calls nicht messbar sind:

| Frage | Warum vertagt | Wann nachholbar |
|---|---|---|
| LLM-Calls pro 10 MB PDF | Mock-LLM liefert sofort, sagt nichts über echte Provider-Costs aus | Sobald `LLM_API_KEY`/`LLM_BASE_URL` für den gewählten LLM-Provider verfügbar |
| Wallclock-Time für Indexierung 10 MB PDF | Ohne LLM- und Embedding-Latenz nicht aussagekräftig | siehe oben |
| Output-Qualität von `aquery(mode="local"/"global"/"hybrid")` | Mock-LLM gibt fixe Strings zurück | siehe oben |
| Embedding-Kompatibilität (Default 1024-dim, OpenAI-SDK-Format) | Mock-Embedding ist Hash-basiert, nicht das echte Modell | sobald `EMBED_API_KEY` für den gewählten Embedding-Provider verfügbar |
| Abbruchkriterium "LLM-Calls pro 10 MB > 10.000" | nicht messbar im Mock | siehe oben |
| Abbruchkriterium "Wallclock > 30 Min für 10 MB" | nicht messbar im Mock | siehe oben |

Schätzung des Echt-Spike-Aufwands für die Cost-Validierung:

- 1 MB Beispiel-Text (kleiner als die im Plan vorgesehenen 5-10 MB) erzeugt mit einem mid-tier Cloud-LLM ungefähr 200-500 LLM-Calls (Entity-Extraction + Merging + Summarization), Kosten in der Größenordnung 0,02-0,10 USD bei typischen Tarifen. Wallclock-Schätzung: 2-5 Min. Konkrete Zahlen sind provider-abhängig und werden im Echt-Spike gemessen.
- Damit lässt sich ein vorsichtiger Echt-Spike als Folgeschritt für ca. 0,05 USD durchführen, ohne sofort die volle 5-10 MB Variante zu fahren.

## RagManager-Skeleton — produktionsreif?

Status: produktionsreif für Phase 1, aber mit folgenden Pflichtergänzungen gegenüber dem Plan-Text:

- Doc-Comment-Block im Modulkopf, der die Loop-Bindung von `initialize_pipeline_status()` erklärt und das einzig-zulässige Aufrufmuster (über `RagManager`) festschreibt.
- `shutdown()`-Methode hinzufügen (im Plan-Skeleton fehlend) — sauberer Stop des Loop-Threads für Tests und für Flask-Reload.
- `finalize_storages()` der LightRAG-Instanz vor dem `shutil.rmtree(...)` aufrufen, damit keine offenen Datei-Handles in NanoVectorDB hängen. Im Spike-Skript bereits eingebaut, im Plan-Text nicht.
- `get_all_nodes(graph_id)` und `get_all_edges(graph_id)` als sync-API-Convenience-Wrapper für Phase-3-Caller (`entity_reader.py`-Migration). Im Plan-Text nicht enthalten, im Spike-Skript bereits drin.
- Insert-Lock pro Graph (`asyncio.Lock`) wie im Plan-Skeleton — im Spike validiert (A5).

Die Singleton-Lifecycle-Frage (wer ruft `RagManager.shutdown()` in Flask?) ist offen: vermutlich via `atexit`-Hook in `app/__init__.py`, analog zur bereits bestehenden `SimulationRunner.register_cleanup()`-Registrierung. Das ist Phase-1-Engineering-Arbeit, nicht Spike-Scope.

## Was die Mocks wirklich validieren — ehrliche Einschätzung

Repräsentativ und belastbar:

- Mock-LLM liefert echtes Entity-Extraction-Format (Tuple-Delimiter `<|#|>`, `<|COMPLETE|>`-Marker). LightRAG parst das ohne Fehler und legt Knoten und Edges korrekt an. Das stützt: Pflicht-Init, Storage-Persistenz, Multi-Project-Isolation, Per-Graph-Lock, Delete-Pfad, NetworkX-Reads.
- Mock-Embedding liefert deterministische 1024-dim L2-normalisierte Vektoren. NanoVectorDB akzeptiert sie ohne Dimensions-Fehler und persistiert die Indizes.
- Threading-Modell (Loop-Thread + `run_coroutine_threadsafe` + Per-Graph-Lock) ist 1:1 wie im Produktiv-Pfad gemeint und wird unter echtem Multi-Thread-Stress getestet.

Fragwürdig — was Mocks NICHT zeigen:

- Echtes Embedding-API-Schema des gewählten Providers (Body-Format, Auth-Header, Rate-Limits, Token-Window-Limits) — der Mock umgeht das komplett. Vor Phase 2 ist eine Validierung mit dem realen Embedding-Modell zwingend.
- Echte LightRAG-LLM-Prompt-Robustheit gegen Provider-Output-Drift (manche LLMs liefern Markdown-Wrapper um die Tuple-Records, manche verbinden Records mit Tuple-Delimiter statt Newline — siehe `lightrag/operate.py:970-1004` mit dem `fix_tuple_delimiter_corruption`-Workaround). Wie oft der Workaround in der Praxis greift, lässt sich nur mit echten Calls beobachten und ist provider-/modell-abhängig.
- Cost-Profile pro Insert-Typ: 24 Mock-LLM-Calls für 9 winzige Inserts ergeben hochgerechnet ca. 200-300 Mock-Calls pro 1 MB Text. Wie sich das auf echte LightRAG-Default-Konfig (`max_parallel_insert=4`, `entity_extract_max_gleaning`, etc.) skaliert, ist eine Funktion des echten LLM-Verhaltens.
- Output-Qualität von `aquery` ist im Mock-Setup nicht aussagekräftig — der Mock gibt einen Fixed-String zurück.

## Empfehlung für die nächsten Schritte

1. **Sofort**: Mock-Spike-Befunde in den Migrationsplan einarbeiten (3 Korrekturen, siehe oben), v.a. die Loop-Bindings-Pflicht für `initialize_pipeline_status()`. Das ist eine kleine Plan-Aktualisierung, kein eigener PR.

2. **Vor Phase 1-Start**: Kleiner Echt-Spike mit ca. 1 MB Beispiel-Text aus `backend/uploads/` und echten LLM-Keys (beliebiger OpenAI-SDK-kompatibler Provider). Geschätzte Kosten 0,02-0,10 USD bei typischen Cloud-LLM-Tarifen, 0 USD bei lokalem LLM (Ollama). Validiert: Embedding-Kompatibilität (provider-abhängige Dim, Default 1024), echte LLM-Output-Robustheit, Wallclock-Zeit pro 1 MB. Liefert die fehlenden Zahlen für die Phase-0-Abbruchkriterien (10.000-Calls-Limit, 30-Min-Limit). Das Spike-Skript ist dafür bereits fertig (`backend/scripts/lightrag_real_spike.py`) — provider-agnostisch via OpenAI-SDK + Cost-Cap-Guard.

3. **Nach Echt-Spike grün**: Phase 1 starten (`feat(rag): introduce RagManager and migrate graph_builder`). Das Spike-Skript-Skeleton kann fast 1:1 nach `backend/app/services/rag_manager.py` übernommen werden, mit den oben aufgezählten Ergänzungen.

4. **Phase 2 ohne weiteren Echt-Spike**: nicht empfohlen. Phase 2 ist die Indexing-Migration, und ohne valide Cost-Daten aus Schritt 2 fliegt Phase 2 blind ins LLM-Cost-Risiko (Plan-Risiko Nummer 1 mit Wahrscheinlichkeit "hoch" und Auswirkung "hoch"). Schritt 2 ist Pflicht-Vorbedingung für Phase 2.

5. **Risiko Multi-Tenancy parallel**: Issue #2527 im LightRAG-Repo deutet an, dass NanoVectorDB unter `working_dir`-Isolation und gleichzeitiger Concurrent-Last in seltenen Fällen Datenverlust haben kann. Im Mock-Spike (A3 + A5) keine Auffälligkeit, aber Last-Test mit echten Embeddings im Produktiv-Stack wäre eine Phase-5-Smoke-Test-Erweiterung wert.

## Reproduzierbarkeit / How to run

Voraussetzungen:

- Python 3.13 (Spike läuft auch unter 3.11/3.12, in dieser Session war 3.13.7 verfügbar).
- `lightrag-hku>=1.4.10,<1.5` (Spike validiert auf `1.4.15`).

Setup (im Worktree-Root):

```bash
# Variante mit uv (empfohlen)
uv venv backend/scripts/.spike-venv
uv pip install --python backend/scripts/.spike-venv/bin/python "lightrag-hku>=1.4.10,<1.5"

# Variante mit stdlib
python3 -m venv backend/scripts/.spike-venv
backend/scripts/.spike-venv/bin/pip install "lightrag-hku>=1.4.10,<1.5"

# Run
backend/scripts/.spike-venv/bin/python backend/scripts/lightrag_mock_spike.py \
    --working-dir-base /tmp/lightrag_spike \
    --report-json /tmp/spike-report.json
```

Erwarteter Exit-Code 0 bei VERDICT=GREEN, 0 bei YELLOW (>=5 PASS), 1 bei RED.

Bei Setup-Fehlschlag (z.B. wegen Python-Version-Mismatch oder fehlendem `wheel`-Cache):

- Python-Version per `python3 --version` prüfen, ggf. `pyenv local 3.12.x`.
- `uv pip install --reinstall --no-cache "lightrag-hku>=1.4.10,<1.5"` als Reset.
- Bei Apple-Silicon mit `numpy>=2.x` und `nano-vectordb`-ABI-Mismatch: Wheels-Cache leeren und neu installieren.

In dieser Session wurde das venv per stdlib `python -m venv` angelegt, weil die Sandbox-Konfig `uv venv` blockiert hat. Beide Varianten sind funktional äquivalent.

## Anhang: Test-Run JSON-Report

Der vollständige JSON-Report aus dem Spike-Run liegt unter `/tmp/spike-report.json`. Auszug der wichtigsten Felder:

```json
{
  "spike": "lightrag-mock-spike",
  "lightrag_version": "1.4.15",
  "duration_s": 0.5,
  "llm_calls_total": 24,
  "embedding_calls_total": 72,
  "verdict": "GREEN",
  "results": [
    {"name": "A1: Pflicht-Init-Pattern", "status": "PASS"},
    {"name": "A2: RagManager Sync→Async-Bridge", "status": "PASS"},
    {"name": "A3: Multi-Project-Isolation", "status": "PASS"},
    {"name": "A4: NetworkX-Graph-Reads", "status": "PASS"},
    {"name": "A5: Per-Graph asyncio.Lock", "status": "PASS"},
    {"name": "A6: Storage-Persistenz", "status": "PASS"},
    {"name": "A7: Delete-Pfad shutil.rmtree", "status": "PASS"}
  ]
}
```

## Verweise

- Migrationsplan: `docs/MIGRATION-ZEP-TO-LIGHTRAG.md`
- Spike-Skript: `backend/scripts/lightrag_mock_spike.py`
- LightRAG Source-Refs: `lightrag/lightrag.py:706` (`chunk_entity_relation_graph` Init), `lightrag/kg/networkx_impl.py:511-538` (`get_all_nodes`/`get_all_edges`), `lightrag/kg/shared_storage.py:170` (Lock-Stelle des Loop-Bindings-Bugs).
- Issue zur Multi-Tenancy: `https://github.com/HKUDS/LightRAG/issues/2527`
