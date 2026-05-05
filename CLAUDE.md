# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**MiroFish** ist eine AGPL-3.0 lizenzierte Multi-Agent Simulations-Engine. Aus "Seed"-Eingaben (PDF/MD/TXT-Berichte oder Stories) wird eine GraphRAG-Wissensbasis aufgebaut, daraus Agent-Personas generiert und auf simulierten Twitter/Reddit-Plattformen (über das `camel-oasis` Framework) parallel laufen gelassen, um Verlauf und Reaktionen vorherzusagen. Sprachoberfläche EN/ZH.

## Tech Stack

- **Backend**: Python 3.11–3.12, Flask 3, `uv` als Paketmanager, OpenAI-SDK-kompatible LLM-API, Zep Cloud (Memory Graph), `camel-oasis` (Simulation), PyMuPDF.
- **Frontend**: Vue 3 + Vite 7, Vue Router, vue-i18n, Axios, D3.
- **Locales**: Geteilt zwischen Frontend und (potentiell) Backend in `/locales/{en,zh,languages}.json`. Vite Alias `@locales` zeigt auf `../locales`.

## Common Commands

Alle aus dem Repo-Root, sofern nicht anders angegeben.

```bash
# Erstinstallation (Node + Python via uv)
npm run setup:all

# Dev-Server: Backend (5001) + Frontend (3000) parallel via concurrently
npm run dev

# Einzeln
npm run backend     # uv run python run.py
npm run frontend    # vite --host
npm run build       # Production-Build des Frontends

# Docker (komponiert aus root .env)
docker compose up -d
```

Tests (Backend): pytest ist als optional-dependency definiert, aber **es existieren keine `tests/`-Verzeichnisse**. `backend/scripts/test_profile_format.py` ist ein Standalone-Smoke-Test, kein pytest-Suite. Vor dem Schreiben neuer Tests prüfen, ob ein test-Verzeichnis angelegt werden soll und mit `uv run pytest <path>` laufen lassen.

Frontend hat keinen Test-Runner konfiguriert. `npm run preview` dient nur zur lokalen Vorschau eines Builds.

## Required Environment

`.env` im Repo-Root (von `backend/app/config.py` geladen — **nicht** in `backend/`):

| Variable | Pflicht | Zweck |
|---|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_NAME` | ja | Haupt-LLM, beliebiger OpenAI-SDK-kompatibler Endpoint (provider-agnostisch) |
| `LLM_BOOST_*` | optional | "Boost"-LLM für Speed-kritische Calls — **wenn ungenutzt, Variablen ganz entfernen, nicht leer lassen** |
| `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | optional | Server-Binding (Default `0.0.0.0:5001`, Debug an) |
| `OASIS_DEFAULT_MAX_ROUNDS` | optional | Default-Rundenzahl pro Simulation (Default 10) |
| `REPORT_AGENT_MAX_TOOL_CALLS` / `REPORT_AGENT_MAX_REFLECTION_ROUNDS` / `REPORT_AGENT_TEMPERATURE` | optional | Tuning des Report-Agents |

`Config.validate()` erzwingt `LLM_API_KEY`, `SECRET_KEY` und `MIROFISH_API_KEY` beim Start; Fehlen führt zu `sys.exit(1)`. (Zep wurde 2026-05-03 vollständig durch LightRAG abgelöst.)

## Architecture (Big Picture)

Das System ist eine 5-Stufen-Pipeline. Frontend-Komponenten (`Step1GraphBuild` … `Step5Interaction`) entsprechen direkt den Backend-Phasen.

```
Seed (PDF/MD/TXT) ──► [1 Graph Build] ──► [2 Env Setup] ──► [3 Simulation] ──► [4 Report] ──► [5 Interaction]
                       │                    │                │                  │              │
                       │                    │                │                  │              └─ Chat mit Agents/ReportAgent
                       │                    │                │                  └─ ReportAgent + Tools
                       │                    │                └─ Twitter/Reddit Subprozesse, IPC
                       │                    └─ Persona Generation, Agent Configs
                       └─ Text Processor, Ontology, Zep GraphRAG Aufbau
```

### Backend-Topologie (`backend/app/`)

- **Flask-App-Factory** (`app/__init__.py`): Registriert drei Blueprints unter `/api/graph`, `/api/simulation`, `/api/report`. Health-Endpoint unter `/health`. CORS offen für `/api/*`. JSON `ensure_ascii=False` für CJK-Output. Registriert `SimulationRunner.register_cleanup()` als atexit-Hook — **das ist kritisch**: Simulationen laufen als Subprozesse und müssen beim Server-Shutdown getötet werden.
- **API-Schicht** (`app/api/`): Dünn, route-only — `graph.py`, `simulation.py`, `report.py`. Business-Logik gehört nach `services/`.
- **Services** (`app/services/`):
  - `graph_builder.py`, `text_processor.py`, `ontology_generator.py` — Stufe 1 (Seed-Verarbeitung & GraphRAG).
  - `oasis_profile_generator.py`, `simulation_config_generator.py` — Stufe 2 (Persona & Konfig).
  - `simulation_runner.py`, `simulation_manager.py`, `simulation_ipc.py` — Stufe 3 (Subprozess-Lifecycle, IPC zwischen Flask und den `scripts/run_*_simulation.py` Workern).
  - `report_agent.py` — Stufe 4 (LLM-Agent mit Tool-Calls).
  - `rag_manager.py`, `lightrag_factory.py`, `entity_reader.py`, `lightrag_tools.py`, `graph_memory_updater.py` — LightRAG-basierter GraphRAG-Layer (lokal, OpenAI-SDK-kompatibler Provider, Phase-1-5 Migration komplett seit 2026-05-03).
- **Utils** (`app/utils/`): `llm_client.py` (OpenAI-SDK-Wrapper), `file_parser.py` (PDF/Text-Extraktion mit Encoding-Detection), `logger.py`, `retry.py`, `locale.py`.
- **Models** (`app/models/`): Daten-Klassen für `project` und `task`.
- **Scripts** (`backend/scripts/`): **Werden als eigene Prozesse vom Backend gestartet**, nicht direkt manuell aufrufen. `run_parallel_simulation.py` (62k LOC) orchestriert Twitter+Reddit; `run_twitter_simulation.py` und `run_reddit_simulation.py` sind die plattform-spezifischen Worker. `action_logger.py` schreibt Telemetrie pro Runde.
- **Uploads** (`backend/uploads/`): Persistenter Storage für Seed-Files und `simulations/` (per Run getrennt). Im Docker-Compose als Volume gemountet.

### Frontend-Topologie (`frontend/src/`)

- **Views** = Routen-Container, **Components** = Wiederverwendbare UI-Bausteine. Die `Step1`–`Step5`-Komponenten bilden die Pipeline ab; `GraphPanel.vue` rendert die GraphRAG-Visualisierung (D3).
- **API-Layer** (`src/api/`): Axios-Wrapper pro Backend-Blueprint (`graph.js`, `simulation.js`, `report.js`, gemeinsame `index.js`). Vite-Dev-Proxy leitet `/api` → `http://localhost:5001`.
- **i18n**: Sprachen werden aus `/locales/*.json` (Repo-Root) gezogen, nicht aus `frontend/src/i18n/` allein. Bei neuen Strings beide Locales (`en.json`, `zh.json`) aktuell halten.
- **Store** (`src/store/pendingUpload.js`): Sehr kleines, custom State-Modul — kein Pinia/Vuex.

## Wichtige Konventionen

- **Sprache im Code**: Kommentare und Log-Messages sind überwiegend Chinesisch. Das ist beabsichtigt — bei Änderungen den Stil beibehalten und nicht "auf Englisch übersetzen".
- **CJK-Encoding**: `run.py` rekonfiguriert stdout für UTF-8 unter Windows; Flask hat `ensure_ascii=False`. Bei Logging/JSON-Output **nicht** durch eigene `json.dumps(..., ensure_ascii=True)` Calls untergraben.
- **LLM-Konfig**: Immer über `Config` (aus `app.config`) lesen, nicht direkt `os.environ`. Boost-LLM ist optional und darf nur verwendet werden, wenn alle drei Variablen gesetzt sind.
- **Subprozess-Disziplin**: Alle Simulations-Worker laufen via `SimulationRunner`. Niemals `subprocess.Popen` direkt einbauen — sonst werden sie beim Shutdown nicht gekillt.
- **OASIS Action Sets**: `Config.OASIS_TWITTER_ACTIONS` und `OASIS_REDDIT_ACTIONS` sind die einzige Source of Truth für plattformfähige Aktionen. Worker-Scripts sollten diese Listen importieren statt hartzukodieren.
- **File Uploads**: 50 MB Limit, Whitelist `{pdf, md, txt, markdown}` (siehe `Config.ALLOWED_EXTENSIONS`).

## Documentation Files

Per User-Standard gilt: Bei jeder neuen oder geänderten `.md` parallel ein HTML-Pendant unter `docs/HTML/` mit aktueller Mermaid-Unterstützung erzeugen, samt Update der Index-Übersicht. Keine Emoticons in `.md`-Dateien.
