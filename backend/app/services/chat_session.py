"""
Server-seitige Chat-Session-Persistenz (Audit-Finding H4).

Vorher: Der Client schickte bei jedem ``POST /api/report/chat``-Aufruf
das gesamte ``chat_history``-Array selbst mit. Damit konnte ein
Angreifer (oder ein blosses Buggy-Frontend) eigene ``assistant``-Rolle
mit ``<tool_call>``-Markup einschleusen und so Tool-Aufrufe gegen die
Zep-Wissensbasis ausloesen.

Jetzt: Pro ``simulation_id`` haelt der Server die kanonische
Chat-Historie selbst, persistiert in einem JSON-File unter
``<UPLOAD_FOLDER>/chat_sessions/<simulation_id>.json``. Der Client
sendet nur noch ``message`` (User-String); der Server haengt die
neue User-Message und die Assistant-Antwort an, persistiert das, und
liefert die volle History read-only zurueck.

MiroFish ist Single-User -- es gibt keinen Concurrency-Bedarf, der
ueber Per-Session-File-Locks hinaus geht. Dieses Modul ist
absichtlich klein gehalten (kein DB-Layer, keine Threads).
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import Config
from ..utils.safe_id import safe_id, safe_path_under


# Erlaubte Roles in einer Chat-Session. ``system`` wird vom Server
# selbst gesetzt und nicht ueber API/Storage transportiert.
_ALLOWED_ROLES = {'user', 'assistant'}

# Defense-in-Depth gegen H2: aus User-Strings schneiden wir alle
# ``<tool_call>...</tool_call>``-Markups raus, bevor sie in die
# Historie wandern. Das verhindert, dass der LLM-Tool-Parser sie als
# echte Tool-Calls interpretiert. Wir matchen mit DOTALL, damit
# Multiline-Payloads erfasst werden.
_TOOL_CALL_RE = re.compile(r'<\s*tool_call\b[^>]*>.*?<\s*/\s*tool_call\s*>',
                           re.IGNORECASE | re.DOTALL)
# Auch der oeffnende Solo-Tag (ohne schliessenden) wird neutralisiert,
# damit ein Angreifer ihn nicht offen laesst, um nachfolgenden
# Assistant-Output in einen Tool-Call-Kontext zu ziehen.
_TOOL_CALL_OPEN_RE = re.compile(r'<\s*tool_call\b[^>]*>', re.IGNORECASE)
_TOOL_CALL_CLOSE_RE = re.compile(r'<\s*/\s*tool_call\s*>', re.IGNORECASE)

# Maximale Anzahl persistierter Messages pro Session. Bei Ueberschreitung
# wird vorne abgeschnitten. Verhindert unbeschraenktes Disk-Wachstum.
_MAX_HISTORY = 200

# Maximale Laenge einer einzelnen User-Message. LLM-Calls werden sonst
# sehr teuer; ausserdem ist das ein einfaches DoS-Schutz.
_MAX_MESSAGE_LEN = 8000


def sanitize_user_message(content: str) -> str:
    """Filtert Tool-Call-Markup aus einer User-Nachricht.

    - Entfernt komplette ``<tool_call>...</tool_call>``-Bloecke.
    - Neutralisiert offene/schliessende Solo-Tags (verhindert,
      dass eine offene Klammer den nachfolgenden Assistant-Output
      in einen Tool-Call-Kontext zieht).
    - Strippt Leerraum.
    - Erzwingt Maximal-Laenge.
    """
    if not isinstance(content, str):
        raise ValueError("message muss ein String sein")

    cleaned = _TOOL_CALL_RE.sub('', content)
    cleaned = _TOOL_CALL_OPEN_RE.sub('', cleaned)
    cleaned = _TOOL_CALL_CLOSE_RE.sub('', cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        raise ValueError("message darf nach Sanitization nicht leer sein")
    if len(cleaned) > _MAX_MESSAGE_LEN:
        cleaned = cleaned[:_MAX_MESSAGE_LEN]
    return cleaned


class ChatSessionStore:
    """JSON-File-basierter Storage fuer Chat-Sessions je ``simulation_id``.

    File-Layout::

        {
          "simulation_id": "sim_abc123",
          "messages": [
            {"role": "user", "content": "...", "ts": "2026-05-04T..."},
            {"role": "assistant", "content": "...", "ts": "2026-05-04T..."},
            ...
          ]
        }

    Concurrency: ein Modul-globales ``threading.RLock`` reicht fuer
    Single-User. Bei Bedarf koennen wir spaeter auf Per-Session-Locks
    umstellen, ohne die API zu aendern.
    """

    _LOCK = threading.RLock()

    @classmethod
    def _sessions_dir(cls) -> str:
        path = os.path.join(Config.UPLOAD_FOLDER, 'chat_sessions')
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def _session_path(cls, simulation_id: str) -> str:
        """Validierter Pfad zur Session-Datei.

        Nutzt ``safe_id`` + ``safe_path_under`` (analog zu allen anderen
        ID-basierten Pfaden seit C6) und appendet ``.json``.
        """
        safe_id(simulation_id, prefix='sim')
        base = os.path.abspath(cls._sessions_dir())
        # Wir reichen ``simulation_id + '.json'`` als einzelne Komponente,
        # damit safe_path_under den realpath-Anker setzt.
        return safe_path_under(base, f"{simulation_id}.json")

    @classmethod
    def load(cls, simulation_id: str) -> List[Dict[str, str]]:
        """Liefert die persistierte Message-Liste, oder ``[]``."""
        with cls._LOCK:
            path = cls._session_path(simulation_id)
            if not os.path.exists(path):
                return []
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                return []
            messages = data.get('messages', [])
            if not isinstance(messages, list):
                return []
            # Nochmal hart gegen kaputte Persistenz absichern.
            valid = []
            for m in messages:
                if not isinstance(m, dict):
                    continue
                role = m.get('role')
                content = m.get('content')
                if role in _ALLOWED_ROLES and isinstance(content, str):
                    valid.append({
                        'role': role,
                        'content': content,
                        'ts': m.get('ts', ''),
                    })
            return valid

    @classmethod
    def append(
        cls,
        simulation_id: str,
        role: str,
        content: str,
    ) -> Dict[str, str]:
        """Haengt eine Nachricht an und persistiert.

        Returns:
            Das eingefuegte Message-Dict.

        Raises:
            ValueError: Bei ungueltiger Role oder leerem Content.
        """
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"role muss user oder assistant sein, war: {role}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content darf nicht leer sein")

        msg = {
            'role': role,
            'content': content,
            'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

        with cls._LOCK:
            path = cls._session_path(simulation_id)
            messages = cls.load(simulation_id)
            messages.append(msg)
            # Cap.
            if len(messages) > _MAX_HISTORY:
                messages = messages[-_MAX_HISTORY:]
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(
                    {'simulation_id': simulation_id, 'messages': messages},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, path)

        return msg

    @classmethod
    def reset(cls, simulation_id: str) -> None:
        """Loescht die Session (z. B. fuer Tests oder explizites
        ``Clear chat``-Feature)."""
        with cls._LOCK:
            path = cls._session_path(simulation_id)
            if os.path.exists(path):
                os.remove(path)
