"""
MiroFish Backend - Flask应用工厂
"""

import os
import warnings

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger
from .utils.error_response import format_error_response
from .utils.log_masking import mask_sensitive_fields


def create_app(config_class=Config):
    """Flask应用工厂函数"""
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 设置JSON编码：确保中文直接显示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，旧版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 设置日志
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子进程中打印启动信息（避免 debug 模式下打印两次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 启动中...")
        logger.info("=" * 50)
    
    # 启用CORS — C3-Fix: explizite Origin-Whitelist statt Wildcard.
    # Origins kommen aus Config.CORS_ALLOWED_ORIGINS (per ENV
    # CORS_ALLOWED_ORIGINS konfigurierbar). Validate() in config.py lehnt
    # leere Listen / '*' explizit ab.
    CORS(
        app,
        resources={r"/api/*": {"origins": Config.CORS_ALLOWED_ORIGINS}},
        supports_credentials=False,
    )

    # M5: Security-Headers via flask-talisman. Wird NACH CORS initialisiert,
    # damit CORS-Preflight (OPTIONS) eigene Header setzt, bevor Talisman
    # zusaetzliche Header anhaengt.
    # CSP ist bewusst dev-permissive ('unsafe-inline' fuer Vue-Inline-Styles,
    # 'self' fuer Fetch). In Produktion sollte CSP auf 'self' + spezifische
    # CDN-Hosts eingeengt werden.
    if app.config.get('SECURITY_HEADERS_ENABLED', True):
        from flask_talisman import Talisman
        Talisman(
            app,
            force_https=app.config.get('SECURITY_HEADERS_FORCE_HTTPS', False),
            strict_transport_security=app.config.get(
                'SECURITY_HEADERS_FORCE_HTTPS', False
            ),
            content_security_policy={
                'default-src': "'self'",
                'script-src': ["'self'", "'unsafe-inline'"],
                'style-src': ["'self'", "'unsafe-inline'"],
                'img-src': ["'self'", 'data:', 'blob:'],
                'connect-src': "'self'",
                'font-src': ["'self'", 'data:'],
                'object-src': "'none'",
                'frame-ancestors': "'none'",
            },
            referrer_policy='strict-origin-when-cross-origin',
            frame_options='DENY',
            session_cookie_secure=False,  # Single-User, kein Session-Cookie aktiv.
        )

    # M9 (CSRF) — Nicht zutreffend in aktuellem Setup:
    # Auth via X-API-Key-Header (siehe C1-Hook unten), keine Cookie-/
    # Session-Auth, kein Browser-CSRF-Vehikel. Falls jemals Cookie- oder
    # Session-basiertes Auth eingefuehrt wird, dieses Finding sofort
    # wieder oeffnen und flask-wtf CSRFProtect aktivieren.
    
    # 注册模拟进程清理函数（确保服务器关闭时终止所有模拟进程）
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("已注册模拟进程清理函数")

    # Auth-Middleware (C1): X-API-Key-Header gegen Config.MIROFISH_API_KEY
    # constant-time vergleichen. /health und CORS-Preflight (OPTIONS) sind
    # ausgenommen. Alle anderen Endpunkte (insbesondere /api/*) erfordern
    # einen gueltigen API-Key, sonst 401 mit format_error_response.
    import hmac
    from .utils.error_response import format_error_response

    _AUTH_EXEMPT_PATHS = frozenset({"/health"})

    @app.before_request
    def require_api_key():
        # CORS-Preflight muss ohne Auth durchgehen, damit der Browser
        # die eigentliche Request senden darf. Flask-CORS beantwortet
        # OPTIONS in seinem eigenen Hook — wir lassen ihn vor uns laufen.
        if request.method == "OPTIONS":
            return None
        if request.path in _AUTH_EXEMPT_PATHS:
            return None

        provided = request.headers.get("X-API-Key", "")
        expected = app.config.get("MIROFISH_API_KEY") or ""
        if not expected:
            # Server-Fehlkonfig — als Server-Fehler behandeln, aber
            # nicht im Klartext leaken.
            auth_logger = get_logger("mirofish.auth")
            auth_logger.error(
                "MIROFISH_API_KEY ist nicht konfiguriert — Anfragen "
                "werden bis zum Setzen des Keys mit 401 abgelehnt"
            )
            return format_error_response(
                PermissionError("auth not configured"), status=401
            )

        # hmac.compare_digest erwartet bytes oder str; beide muessen
        # nicht-leer sein, sonst False zurueck.
        if not hmac.compare_digest(provided, expected):
            return format_error_response(
                PermissionError("invalid or missing api key"), status=401
            )
        return None

    # 请求日志中间件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"请求: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            # PII-Masking: API-Keys, Tokens, Passwörter etc. werden vor
            # dem Logging durch '***' ersetzt. Die Original-Payload bleibt
            # für die Route unverändert (mask_sensitive_fields kopiert).
            body = request.get_json(silent=True)
            logger.debug(f"请求体: {mask_sensitive_fields(body)}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"响应: {response.status_code}")
        return response
    
    # H6: Rate-Limiter init. Muss VOR den Blueprints laufen, damit
    # die @limiter.limit-Dekorationen in den Routen tatsaechlich greifen.
    # Ist RATE_LIMIT_ENABLED=False, deaktivieren wir den Limiter komplett —
    # bequem fuer Tests, die viele Requests schicken.
    from .utils.rate_limit import limiter
    limiter.init_app(app)
    if not app.config.get('RATE_LIMIT_ENABLED', True):
        limiter.enabled = False

    # 429-Handler: Limiter wirft RateLimitExceeded; wir vereinheitlichen
    # die Antwort mit unserem error_response-Format (C5).
    from flask_limiter.errors import RateLimitExceeded

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit_exceeded(exc):
        # exc.description traegt die Limit-Beschreibung wie "5 per 1 minute".
        return format_error_response(
            RuntimeError(
                f"Rate-Limit ueberschritten: {getattr(exc, 'description', 'too many requests')}"
            ),
            status=429,
        )

    # 注册蓝图
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')

    # Globaler Error-Handler: fängt jede unerwartete Exception ab und
    # liefert eine einheitliche, im Produktivmodus stacktrace-freie
    # JSON-Antwort mit Request-ID. HTTPException (z. B. 404) wird hier
    # weitergereicht, damit Flask seine Standard-Behandlung übernimmt.
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)
    def handle_unexpected_exception(exc):
        if isinstance(exc, HTTPException):
            return exc
        return format_error_response(exc)
    
    # 健康检查
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish Backend'}
    
    if should_log_startup:
        logger.info("MiroFish Backend 启动完成")
    
    return app

