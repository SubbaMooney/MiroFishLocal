"""
LLM客户端封装
统一使用OpenAI格式调用

Tier 3.1 (stateful-forging-kernighan):
    LLMClient ist stateless bzgl. der Aufruf-Argumente — alle Per-Call-Daten
    (messages, temperature, purpose, ...) kommen ueber Methoden-Parameter,
    nicht ueber Instanz-State. Damit ist ein Prozess-weiter Singleton sicher
    und ermoeglicht httpx-Connection-Pool-Reuse innerhalb des OpenAI-SDK,
    statt pro Konsument einen neuen TLS-Pool aufzubauen.

    Konsumenten sollten ``LLMClient.get_default()`` nutzen statt ``LLMClient()``
    aufzurufen. Der Konstruktor bleibt funktional fuer Test-/Override-Pfade
    (z.B. eigener model-Override oder explizite Mocks ueber DI).
"""

import json
import os
import re
import threading
from typing import Optional, Dict, Any, List
from openai import OpenAI

from ..config import Config


class LLMClient:
    """LLM客户端"""

    # Class-level Singleton-Caches fuer Default- und Boost-Instanz.
    # Double-Checked Locking via threading.Lock — nicht reentrant noetig.
    _default_instance: Optional["LLMClient"] = None
    _default_lock: threading.Lock = threading.Lock()

    _boost_instance: Optional["LLMClient"] = None
    _boost_lock: threading.Lock = threading.Lock()
    # Sentinel fuer "Boost wurde geprueft und ist nicht konfiguriert" —
    # vermeidet wiederholte env-Lookups + Logging in Hot-Paths.
    _boost_unavailable: bool = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")

        # L1 (Audit): expliziter Timeout + max_retries, sonst hangt der
        # Worker-Thread bis zum openai-Default 600s wenn der Provider
        # nicht antwortet.
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=Config.LLM_TIMEOUT_SECONDS,
            max_retries=Config.LLM_MAX_RETRIES,
        )

    # ------------------------------------------------------------------
    # Singleton-Accessor (Tier 3.1)
    # ------------------------------------------------------------------

    @classmethod
    def get_default(cls) -> "LLMClient":
        """Liefert die Prozess-weite Default-LLMClient-Instanz.

        Lazy-initialisiert beim ersten Aufruf, threadsafe via DCL. Wirft die
        Konstruktor-Exception (z.B. ``ValueError("LLM_API_KEY 未配置")``)
        durch, wenn die Config nicht valid ist.

        Wiederverwendet den selben httpx-Pool ueber ``self.client`` —
        spart TLS-Handshakes bei haeufigen LLM-Calls.
        """
        # Fast path ohne Lock-Akquisition.
        instance = cls._default_instance
        if instance is not None:
            return instance
        with cls._default_lock:
            # Re-check nach Lock-Akquisition (DCL).
            if cls._default_instance is None:
                cls._default_instance = cls()
            return cls._default_instance

    @classmethod
    def get_boost(cls) -> Optional["LLMClient"]:
        """Liefert eine Singleton-Instanz fuer den optionalen Boost-LLM.

        Boost-Vars werden direkt aus ``os.environ`` gelesen (nicht in Config
        gepflegt — siehe ``run_parallel_simulation.py``). Wenn nicht alle drei
        ``LLM_BOOST_API_KEY`` / ``LLM_BOOST_BASE_URL`` / ``LLM_BOOST_MODEL_NAME``
        gesetzt sind, gibt diese Methode ``None`` zurueck — Aufrufer koennen
        damit auf ``get_default()`` faellen.
        """
        # Fast path: bereits gebaut oder als unavailable markiert.
        instance = cls._boost_instance
        if instance is not None:
            return instance
        if cls._boost_unavailable:
            return None
        with cls._boost_lock:
            if cls._boost_instance is not None:
                return cls._boost_instance
            if cls._boost_unavailable:
                return None
            api_key = os.environ.get("LLM_BOOST_API_KEY", "").strip()
            base_url = os.environ.get("LLM_BOOST_BASE_URL", "").strip()
            model = os.environ.get("LLM_BOOST_MODEL_NAME", "").strip()
            if not (api_key and base_url and model):
                # Pruef-Ergebnis cachen — kein wiederholtes env-Lookup.
                cls._boost_unavailable = True
                return None
            cls._boost_instance = cls(
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
            return cls._boost_instance

    @classmethod
    def _reset_singletons_for_tests(cls) -> None:
        """Test-Helper: setzt Singleton-Cache zurueck (z.B. nach env-Patches).

        NICHT in Produktion aufrufen — bestehende Konsumenten halten weiter
        Referenzen auf die alte Instanz, dadurch entstehen mehrere Pools.
        """
        with cls._default_lock:
            cls._default_instance = None
        with cls._boost_lock:
            cls._boost_instance = None
            cls._boost_unavailable = False
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
        purpose: str = "llm:chat",
    ) -> str:
        """
        发送聊天请求

        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            purpose: Tag fuer den Token-Tracker (z.B. "persona:gen", "config:agent_batch").

        Returns:
            模型响应文本
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if response_format:
            kwargs["response_format"] = response_format

        response = self.client.chat.completions.create(**kwargs)
        # Token-Tracker (Audit-Folge): jede Response enthaelt response.usage.
        # Defensive — manche kompatible Provider liefern usage=None.
        try:
            from .token_tracker import tracker
            usage = getattr(response, "usage", None)
            if usage is not None:
                tracker.record(
                    model=self.model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    purpose=purpose,
                )
        except Exception:  # noqa: BLE001 — Tracking darf nie crashen.
            pass

        content = response.choices[0].message.content
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        # 清理markdown代码块标记
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"LLM返回的JSON格式无效: {cleaned_response}")

