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


class Config:
    """Flask配置类"""

    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'

    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False

    # LLM配置（统一使用OpenAI格式）
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'gpt-4o-mini')

    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')

    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}

    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小

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
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY 未配置")
        if not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY 未配置")
        # 错误时附带 .env 加载来源，方便定位
        if errors:
            errors.append(f"配置加载来源: {cls.env_source()}")
            errors.append(f"期望 .env 路径: {ENV_PATH_PROJECT_ROOT}")
        return errors

