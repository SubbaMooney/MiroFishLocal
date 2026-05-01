# Audit camel-oasis und camel-ai: Externe Netzwerk-Calls

Datum: 2026-05-01
Scope: `camel-oasis 0.2.5` und `camel-ai 0.2.78` im venv unter
`/Users/moberauer/src/MiroFishLocal/backend/.venv/lib/python3.12/site-packages/`
Auftrag: Werden zur Laufzeit andere externe Endpoints kontaktiert als der
konfigurierte LLM-Endpoint?
Methodik: Read-only grep aller `*.py` auf direkte HTTP-Clients, Sockets,
HuggingFace-Hub-Pulls, Telemetrie/Tracking und hardcoded URLs. Anschließend
Abgleich, welche Module MiroFish via
`backend/scripts/run_*_simulation.py` und `backend/app/` tatsächlich importiert.

## TL;DR

Verdikt: **DEFANGED**. Beide Libs enthalten Egress-Quellen, aber die
gefährlichsten Pfade (camel-Toolkits mit Drittanbieter-APIs, AgentOps-Tracking,
Discord-Bot, HuggingFace-Datasets) werden von MiroFish nicht importiert. Eine
einzige reale Egress-Quelle bleibt: oasis lädt für `DefaultPlatformType.TWITTER`
beim ersten Lauf zwei HuggingFace-Modelle (`Twitter/twhin-bert-base`,
`paraphrase-MiniLM-L6-v2`). Mit zwei Env-Variablen oder einem Pre-Cache
deaktivierbar; Reddit-Pfad ist sauber.

## Findings

| # | Lib   | Datei:Zeile                                | Kategorie         | Default-aktiv? | Deaktivierbar | Trifft MiroFish? |
|---|-------|--------------------------------------------|-------------------|----------------|---------------|------------------|
| 1 | oasis | `social_platform/recsys.py:68`             | Hub-Pull          | Ja, wenn TWITTER-Env genutzt | Ja, via `HF_HUB_OFFLINE=1` + Pre-Cache | JA (Twitter-Run) |
| 2 | oasis | `social_platform/recsys.py:78`             | Hub-Pull          | Ja, wenn TWITTER-Env genutzt | Ja, via `HF_HUB_OFFLINE=1` + Pre-Cache | JA (Twitter-Run) |
| 3 | oasis | `social_platform/recsys.py:87`             | Hub-Pull (`paraphrase-MiniLM-L6-v2`) | Nur wenn `recsys_type="twitter"` (RecsysType.TWITTER) gesetzt wäre | Ja, nicht aktivieren oder offline | NEIN (MiroFish nutzt twhin-bert, nicht TWITTER) |
| 4 | camel | `utils/commons.py:598`                     | AgentOps-Tracking | NEIN (`AGENTOPS_API_KEY` muss gesetzt sein) | Variable nicht setzen | NEIN |
| 5 | camel | diverse `models/{cohere,samba,reka,mistral}.py` | AgentOps-Tracking | NEIN          | dito          | NEIN (Modelle ungenutzt) |
| 6 | camel | `utils/commons.py:126`                     | HF-Datasets-Pull  | nur via `download_task_zip` (camel.benchmarks) | nicht importieren | NEIN |
| 7 | camel | `toolkits/*.py` (twitter, linkedin, zapier, discord, wechat, dingtalk, whatsapp, pubmed, semantic_scholar, wolfram, jina, meshy, mineru, search, web_deploy, pptx, audio, image) | Direkte 3rd-Party-API-Calls | nur on import | nicht importieren | NEIN (`camel.toolkits` wird in MiroFish nicht importiert) |
| 8 | camel | `bots/discord/discord_app.py:171/201/229`  | Discord-API       | nur on import | nicht importieren | NEIN |
| 9 | camel | `runtimes/{docker,remote_http}_runtime.py` | localhost-Probe + remote-Runtime POST | nur on import | nicht importieren | NEIN |
| 10| camel | `models/sglang_model.py:449`               | sglang-Probe      | nur on import | nicht importieren | NEIN |
| 11| camel | `models/samba_model.py:412/643`            | SambaNova httpx   | nur on import | nicht importieren | NEIN |
| 12| camel | `models/gemini_model.py:99` Default-Endpoint `https://generativelanguage.googleapis.com/...` | Default-Endpoint | nur on import | über `Config.LLM_BASE_URL` überschreibbar | NEIN |
| 13| camel | `models/anthropic_model.py:122` Default-Endpoint `https://api.anthropic.com/v1/` | Default-Endpoint | nur on import | über `Config.LLM_BASE_URL` überschreibbar | NEIN |
| 14| camel | `embeddings/{jina,vlm}_embedding.py`       | API + HF-Pull     | nur on import | nicht importieren | NEIN |
| 15| camel | `loaders/{mineru,jina_url_reader,chunkr_reader}.py` | 3rd-Party-API | nur on import | nicht importieren | NEIN |
| 16| camel | `datahubs/huggingface.py`                  | HF-Hub-Auth/Pull  | nur on import | nicht importieren | NEIN |
| 17| camel | `benchmarks/{gaia,nexus,apibench}.py`      | HF snapshot_download | nur on import | nicht importieren | NEIN |
| 18| oasis | `social_agent/agent_graph.py:28`           | Neo4j-Bolt-Driver | nur wenn `Neo4jConfig` gesetzt | Default `igraph` (in-memory) | NEIN |

Keine Treffer für `posthog`, `segment`, `mixpanel`, `sentry_sdk`, `langsmith`,
`opentelemetry`, `wandb`, `mlflow`, `weave`, `datadog`, `newrelic` in `oasis/`
oder `camel/`. Keine `socket`-Verbindungen außer einem lokalen
`SO_REUSEADDR`-Probe in `camel/utils/commons.py:179` (kein Egress).

## Was MiroFish konkret aus den Libs importiert

Nachgewiesen via grep über `backend/scripts/` und `backend/app/`:

- aus `oasis`: `make`, `DefaultPlatformType`, `LLMAction`, `ManualAction`,
  `generate_*_agent_graph`, `SocialAgent`, `AgentGraph`, `print_db_contents`.
- aus `camel`: ausschließlich `camel.models.ModelFactory` und
  `camel.types.ModelPlatformType`. **Keine** Toolkits, Bots, Runtimes,
  Embeddings, Benchmarks, Loaders, Datahubs.

Folge: Die in der Tabelle gelb/rot markierten camel-Module sind tot, weil sie
nie geladen werden. Egress-Risiko aus camel-ai = praktisch null, solange
niemand neue Imports einführt.

## Aktiver Egress-Pfad: oasis Twitter-Recsys

Der Twitter-Run von MiroFish ruft (Stand
`backend/scripts/run_twitter_simulation.py:587` und
`run_parallel_simulation.py:1137`):

```
oasis.make(platform=oasis.DefaultPlatformType.TWITTER, ...)
```

Das instanziiert in `oasis/environment/env.py:78` ein `Platform` mit
`recsys_type="twhin-bert"`. Beim ersten Recsys-Refresh führt
`oasis/social_platform/recsys.py:64-79` aus:

```python
AutoTokenizer.from_pretrained("Twitter/twhin-bert-base", model_max_length=512)
AutoModel.from_pretrained("Twitter/twhin-bert-base").to(device)
```

`transformers` versucht dabei standardmäßig `huggingface.co/...`. Das ist der
einzige bestätigte Egress an einen Nicht-LLM-Endpoint, der in einem MiroFish-
Run unter Default-Config tatsächlich passiert. Beim Reddit-Run gibt es ihn
nicht (`recsys_type="reddit"` → keine Modell-Loads).

## Verdikt und Fix-Schritte

Verdikt: **DEFANGED**. Die Lib ist im MiroFish-Kontext akzeptabel zu behalten,
braucht aber drei sehr konkrete Härtungen.

Empfohlene Maßnahmen für MiroFish (jeweils unkritisch und reversibel):

1. **Modelle pre-cachen, dann offline gehen**: Einmal mit Internet
   `huggingface_hub.snapshot_download("Twitter/twhin-bert-base")` und
   `SentenceTransformer("paraphrase-MiniLM-L6-v2")` ausführen, Cache in das
   Image / das Volume persistieren, danach in Container-Env setzen:
   - `HF_HUB_OFFLINE=1`
   - `TRANSFORMERS_OFFLINE=1`
   - `HF_DATASETS_OFFLINE=1`
   Damit ist auch ein versehentlicher zukünftiger Hub-Call hard-blockiert.

2. **AgentOps explizit verbieten**: In `backend/app/config.py` einen Hard-Check
   einbauen, der `AGENTOPS_API_KEY` aus `os.environ` löscht, falls gesetzt.
   Andernfalls würde jede zukünftige Variable den Tracker scharf schalten.

3. **Import-Whitelist via Test**: Ein winziger Smoke-Test, der
   `sys.modules` nach dem Import von `oasis` und `camel.models` prüft und
   fehlschlägt, wenn `camel.toolkits`, `camel.bots`, `camel.runtimes`,
   `camel.embeddings`, `camel.benchmarks`, `camel.loaders`, `camel.datahubs`,
   `agentops`, `posthog`, `langsmith`, `wandb` oder `sentry_sdk` geladen
   wurden. Verhindert künftige stille Erweiterungen.

Optional, falls die Egress-Linie maximal hart sein soll:

4. **Recsys auf RANDOM für Twitter**: oasis lässt einen Custom-`Platform` mit
   `recsys_type="random"` zu (siehe `oasis/social_platform/platform.py:336`).
   Das eliminiert auch den lokalen Modell-Load, kostet aber Recsys-Qualität.
5. **Egress-Firewall im Container**: docker-compose mit `--network` auf einen
   Bridge ohne Default-Route, dazu allowlist-Proxy nur auf den LLM-Endpoint.

## Quellen-Tabelle (Roh-grep, gekürzt)

```
oasis/social_platform/recsys.py:68: AutoTokenizer.from_pretrained("Twitter/twhin-bert-base", ...)
oasis/social_platform/recsys.py:78: AutoModel.from_pretrained("Twitter/twhin-bert-base").to(device)
oasis/social_platform/recsys.py:87: SentenceTransformer("paraphrase-MiniLM-L6-v2", cache_folder="./models")
oasis/environment/env.py:81:        recsys_type="twhin-bert"
oasis/social_agent/agent_graph.py:28: GraphDatabase.driver(...)  # nur wenn neo4j_config gesetzt
camel/utils/commons.py:126:           requests.get("https://huggingface.co/datasets/camel-ai/...") # benchmarks-only
camel/utils/commons.py:598:           if os.getenv("AGENTOPS_API_KEY") ...     # opt-in
camel/models/anthropic_model.py:122:  default base_url "https://api.anthropic.com/v1/"
camel/models/gemini_model.py:99:      default base_url "https://generativelanguage.googleapis.com/..."
camel/toolkits/*:                     diverse 3rd-Party-APIs, alle on-import-only
camel/bots/discord/*:                 Discord-API
camel/runtimes/*:                     localhost + remote-runtime
camel/embeddings/*:                   Jina/HF
camel/benchmarks/*:                   HF snapshot_download
```

Keiner der oben gelisteten camel-Module wird von MiroFish importiert
(`grep -rE "from camel" backend/scripts backend/app` ergibt nur
`camel.models.ModelFactory` und `camel.types.ModelPlatformType`).
