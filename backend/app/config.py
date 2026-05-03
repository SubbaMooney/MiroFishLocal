"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

import logging
import os
from dotenv import load_dotenv

# 模块级 logger（utils.logger 在此处尚未初始化，使用标准库 logging 即可）
_config_logger = logging.getLogger('mirofish.config')

# 候选 .env 路径（按优先级）
# 1) MiroFish/.env  —— 项目根目录（推荐）
# 2) MiroFish/backend/.env  —— 兜底，避免新人误放
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BACKEND_DIR, '..'))
ENV_PATH_PROJECT_ROOT = os.path.join(_PROJECT_ROOT, '.env')
ENV_PATH_BACKEND = os.path.join(_BACKEND_DIR, '.env')

# 实际加载来源（用于 validate() 的错误提示和启动日志）
_LOADED_ENV_PATH: str = ''
_LOADED_ENV_SOURCE: str = ''  # 'project-root' / 'backend-fallback' / 'os-env'


def _load_env_once() -> None:
    """加载 .env 一次，并记录使用的路径"""
    global _LOADED_ENV_PATH, _LOADED_ENV_SOURCE

    project_root_exists = os.path.exists(ENV_PATH_PROJECT_ROOT)
    backend_exists = os.path.exists(ENV_PATH_BACKEND)

    if project_root_exists:
        load_dotenv(ENV_PATH_PROJECT_ROOT, override=True)
        _LOADED_ENV_PATH = ENV_PATH_PROJECT_ROOT
        _LOADED_ENV_SOURCE = 'project-root'
        _config_logger.info(f"已加载 .env: {ENV_PATH_PROJECT_ROOT}")
        return

    if backend_exists:
        # 新人常见反射：把 .env 放到 backend/ 下
        # 静默接受但发出 WARN，提醒推荐位置
        load_dotenv(ENV_PATH_BACKEND, override=True)
        _LOADED_ENV_PATH = ENV_PATH_BACKEND
        _LOADED_ENV_SOURCE = 'backend-fallback'
        _config_logger.warning(
            f".env 位于 backend/ 下: {ENV_PATH_BACKEND} —— 推荐位置为项目根目录: "
            f"{ENV_PATH_PROJECT_ROOT}（已临时使用 backend/.env 作为兜底）"
        )
        return

    # 都不存在：依赖 OS 环境变量（生产环境/容器场景）
    load_dotenv(override=True)
    _LOADED_ENV_PATH = ''
    _LOADED_ENV_SOURCE = 'os-env'
    _config_logger.info(
        f"未找到 .env 文件（已尝试: {ENV_PATH_PROJECT_ROOT}, {ENV_PATH_BACKEND}），"
        "仅使用操作系统环境变量"
    )


# 模块导入时执行一次
_load_env_once()


def _harden_environment() -> None:
    """Defensive Umgebungs-Haerten gegen versteckten Egress.

    Siehe docs/AUDIT-CAMEL-OASIS-EGRESS.md (Verdikt: DEFANGED).

    1. AGENTOPS_API_KEY: camel-ai's AgentOps-Integration aktiviert sich
       automatisch sobald der Key in der Umgebung steht — egal aus welcher
       Quelle. Wir loeschen ihn proaktiv, damit MiroFish nie versehentlich
       Usage-Daten an agentops.ai schickt. User, die AgentOps explizit wollen,
       muessen den Key NACH dem Backend-Start setzen oder diesen Pop entfernen.

    2. HF_HUB_OFFLINE: oasis Twitter-Pfad versucht zur Laufzeit
       AutoTokenizer/AutoModel.from_pretrained("Twitter/twhin-bert-base") aus
       huggingface.co zu laden. Mit HF_HUB_OFFLINE=1 als Default scheitert ein
       erster Aufruf mit klarer Fehlermeldung, statt heimlich Modelle aus dem
       Netz zu ziehen. Der User muss vorher `python backend/scripts/precache_hf_models.py`
       laufen lassen — siehe Skript fuer Details. setdefault, damit User mit
       HF_HUB_OFFLINE=0 in der .env das Online-Verhalten zurueckholen kann.
    """
    os.environ.pop("AGENTOPS_API_KEY", None)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


_harden_environment()


class Config:
    """Flask配置类"""

    # Flask配置
    # SECRET_KEY MUSS aus der Umgebung kommen — kein Default-Fallback,
    # damit ein produktiver Start ohne explizit gesetzten Schlüssel scheitert (C4).
    SECRET_KEY = os.environ.get('SECRET_KEY')
    # FLASK_DEBUG defaultet auf False — Werkzeug-Debugger erlaubt sonst RCE (C2).
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'

    # API-Key fuer Auth-Middleware (C1). Wird per X-API-Key-Header
    # erwartet und mit hmac.compare_digest constant-time verglichen.
    # /health und CORS-Preflight-OPTIONS sind ausgenommen. validate()
    # erzwingt Mindest-Entropie (>=32 Zeichen).
    MIROFISH_API_KEY = os.environ.get('MIROFISH_API_KEY')

    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False

    # LLM配置（统一使用OpenAI格式）
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'gpt-4o-mini')

    # LightRAG / GraphRAG 配置 (Phase 1-5 Migration komplett — siehe
    # docs/MIGRATION-ZEP-TO-LIGHTRAG.md). Alle Werte sind optional; Pflicht
    # werden sie erst, wenn RagManager bzw. lightrag_factory aufgerufen wird.
    LIGHTRAG_WORKING_DIR_BASE = os.environ.get(
        'LIGHTRAG_WORKING_DIR_BASE',
        os.path.join(os.path.dirname(__file__), '../uploads/lightrag'),
    )
    # Embedding-Provider — falls nicht separat gesetzt, fällt auf LLM_* zurück.
    EMBED_API_KEY = os.environ.get('EMBED_API_KEY') or os.environ.get('LLM_API_KEY')
    EMBED_BASE_URL = os.environ.get(
        'EMBED_BASE_URL',
        os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1'),
    )
    EMBED_MODEL_NAME = os.environ.get('EMBED_MODEL_NAME')
    EMBED_DIM = int(os.environ.get('EMBED_DIM', '1024'))

    # LightRAG Cost-Optimization Knobs (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md
    # Phase 4.5 Optimization-Sprint). Defaults sind auf "kosten-optimiert"
    # gesetzt — basierend auf Echt-Spike 2026-05-03 (gpt-4o-mini lieferte
    # 74.898 Calls/10MB mit LightRAG-Defaults; mit diesen Werten ~10-20x weniger).
    LIGHTRAG_CHUNK_TOKEN_SIZE = int(os.environ.get('LIGHTRAG_CHUNK_TOKEN_SIZE', '5000'))
    LIGHTRAG_CHUNK_OVERLAP_TOKEN_SIZE = int(
        os.environ.get('LIGHTRAG_CHUNK_OVERLAP_TOKEN_SIZE', '200')
    )
    LIGHTRAG_MAX_GLEANING = int(os.environ.get('LIGHTRAG_MAX_GLEANING', '0'))
    LIGHTRAG_MAX_EXTRACT_INPUT_TOKENS = int(
        os.environ.get('LIGHTRAG_MAX_EXTRACT_INPUT_TOKENS', '8000')
    )
    # Few-Shot-Examples im Extraktions-Prompt rauswerfen (groesster
    # Token-Overhead, ~2.5-3k Tokens pro Call). Risiko: Output-Format-Stabilitaet —
    # bei Quality-Issues auf 'false' setzen.
    LIGHTRAG_DROP_EXAMPLES = os.environ.get('LIGHTRAG_DROP_EXAMPLES', 'true').lower() in ('1', 'true', 'yes')

    # Graph-Memory-Updater Throttling (Phase 4 Migration). Vorher hardcoded
    # in zep_graph_memory_updater (BATCH_SIZE=5, SEND_INTERVAL=0.5s) — bei
    # Zep war das billig, bei LightRAG entspricht jedes Insert einer vollen
    # LLM-Extraktion. Aggressive Defaults senken die Insert-Rate ~60x:
    #   - 0.5s/5  = 600 Activities/min/Plattform
    #   - 30s/50  =  100 Activities/min/Plattform (gepuffert in groesseren Batches)
    GRAPH_MEMORY_BATCH_SIZE = int(os.environ.get('GRAPH_MEMORY_BATCH_SIZE', '50'))
    GRAPH_MEMORY_SEND_INTERVAL = float(os.environ.get('GRAPH_MEMORY_SEND_INTERVAL', '30.0'))

    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}

    # 文本处理配置
    # 默认切块大小 — angepasst fuer LightRAG-Cost (siehe LIGHTRAG_CHUNK_TOKEN_SIZE
    # oben). Bei 5000 Zeichen MiroFish-Chunk wird LightRAG i.d.R. nicht
    # nochmal sub-chunken, d.h. 1 Extraktions-Call pro MiroFish-Chunk.
    DEFAULT_CHUNK_SIZE = int(os.environ.get('DEFAULT_CHUNK_SIZE', '5000'))
    DEFAULT_CHUNK_OVERLAP = int(os.environ.get('DEFAULT_CHUNK_OVERLAP', '200'))

    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')

    # OASIS平台可用动作配置（Single Source of Truth: app.oasis_actions）
    # Worker 脚本（backend/scripts/run_*_simulation.py）也从同一来源读取，
    # 防止配置漂移。详见 backend/app/oasis_actions.py 模块说明。
    # 该模块不属于 app.services 包，避免与 services/__init__.py 形成循环导入。
    from .oasis_actions import (
        TWITTER_ACTION_NAMES as _TWITTER_ACTION_NAMES,
        REDDIT_ACTION_NAMES as _REDDIT_ACTION_NAMES,
    )
    OASIS_TWITTER_ACTIONS = list(_TWITTER_ACTION_NAMES)
    OASIS_REDDIT_ACTIONS = list(_REDDIT_ACTION_NAMES)

    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))

    @classmethod
    def env_source(cls) -> str:
        """返回 .env 加载来源的可读描述（用于诊断）"""
        if _LOADED_ENV_SOURCE == 'project-root':
            return f"项目根目录 .env: {_LOADED_ENV_PATH}"
        if _LOADED_ENV_SOURCE == 'backend-fallback':
            return f"backend/.env (兜底): {_LOADED_ENV_PATH}"
        return f"操作系统环境变量（未找到 .env 文件，期望路径: {ENV_PATH_PROJECT_ROOT}）"

    @classmethod
    def validate(cls):
        """验证必要配置"""
        errors = []
        if not cls.SECRET_KEY:
            errors.append(
                "SECRET_KEY 未配置 (Pflichtfeld, kein Default mehr — bitte in .env setzen)"
            )
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY 未配置")
        # MIROFISH_API_KEY (C1) ist Pflicht und muss eine Mindest-Entropie
        # haben (>=32 Zeichen). Generieren via:
        #   python -c "import secrets; print(secrets.token_hex(32))"
        if not cls.MIROFISH_API_KEY:
            errors.append(
                "MIROFISH_API_KEY 未配置 (Pflicht fuer Auth-Middleware — "
                "bitte in .env setzen, z. B. python -c "
                "'import secrets; print(secrets.token_hex(32))')"
            )
        elif len(cls.MIROFISH_API_KEY) < 32:
            errors.append(
                "MIROFISH_API_KEY 太短 (mindestens 32 Zeichen — bitte "
                "ein zufaelliges Token mit ausreichend Entropie verwenden)"
            )
        # 错误时附带 .env 加载来源，方便定位
        if errors:
            errors.append(f"配置加载来源: {cls.env_source()}")
            errors.append(f"期望 .env 路径: {ENV_PATH_PROJECT_ROOT}")
        return errors

