"""
Admin/Diagnose-Endpoints. Auth wie alle /api/*-Routen via X-API-Key.
"""

from __future__ import annotations

from flask import jsonify

from . import admin_bp
from ..utils.token_tracker import tracker


@admin_bp.route('/tokens', methods=['GET'])
def get_token_usage():
    """Liefert kumulierten Token-/Cost-Snapshot seit Server-Start.

    Antwort-Schema (gekuerzt):
        {
          "success": true,
          "data": {
            "totals": {"calls": 321, "prompt_tokens": 423005,
                       "completion_tokens": 120970, "total_tokens": 543975,
                       "cost_usd": 0.1361},
            "by_model": [
              {"model": "gpt-4o-mini", "calls": 158,
               "prompt_tokens": ..., "completion_tokens": ...,
               "cost_usd": 0.1361, "by_purpose": [...]},
              ...
            ]
          }
        }

    Hinweis: Counter werden beim Server-Restart zurueckgesetzt — fuer
    historische Daten OpenAI-Dashboard nutzen.
    """
    return jsonify({
        "success": True,
        "data": tracker.snapshot(),
    })


@admin_bp.route('/tokens/reset', methods=['POST'])
def reset_token_usage():
    """Setzt den Counter zurueck (Debug/Test-Hilfe)."""
    tracker.reset()
    return jsonify({"success": True, "data": {"reset": True}})
