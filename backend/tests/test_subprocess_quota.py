"""
Tests fuer Audit-Finding H5 — Unkontrollierter Subprozess-Spawn ohne Quota.

Vorher: ``backend/app/services/simulation_runner.py:438-448`` startete bei
jedem ``POST /api/simulation/start`` einen ``subprocess.Popen`` ohne
Concurrency-Limit und ohne Resource-Limits. DoS- und Crypto-Mining-Vektor.

Fix:

1. ``Config.MAX_CONCURRENT_SIMULATIONS`` (default 2) — Concurrency-Limit.
   Das ``(N+1)``-te Start-Request wirft ``SimulationQuotaExceeded``, das
   die API in 429 uebersetzt.
2. ``preexec_fn`` setzt ``RLIMIT_AS`` (Adressraum) und ``RLIMIT_CPU``
   (CPU-Sekunden) auf POSIX (Darwin/Linux). Auf Windows graceful skip.
3. Wall-Clock-Watchdog: Subprozess wird nach
   ``Config.SIMULATION_MAX_WALL_SECONDS`` aggressiv gekillt.

Diese Tests verifizieren:

- ``_count_active_processes`` wirft Zombies/finished Prozesse aus.
- ``SimulationQuotaExceeded`` wird geworfen, sobald das Limit erreicht ist.
- ``_build_preexec_fn`` setzt RLIMIT_AS/RLIMIT_CPU mit den Config-Werten
  (POSIX). Auf Windows liefert sie ``None``.
- Der Wall-Clock-Watchdog killt einen Mock-Prozess nach Timeout.
- Die API antwortet mit 429 auf ``SimulationQuotaExceeded``.
"""

from __future__ import annotations

import sys
import time
import threading
from unittest.mock import MagicMock, patch

import pytest


_TEST_KEY = "x" * 64


def _Config():
    """Lazy-Loader fuer Config — andere Test-Module (test_resource_authz)
    clearen sys.modules['app.*'], wir muessen darauf reagieren statt im
    Modul-Header zu importieren. Tests rufen ``_Config()`` auf, wenn sie
    gegen das aktuell geladene Klassen-Objekt monkeypatchen wollen."""
    from app.config import Config as ConfigCls
    return ConfigCls


@pytest.fixture(autouse=True)
def _reset_runner_state():
    """Vor und nach jedem Test SimulationRunner-Klassen-Dicts leeren."""
    from app.services.simulation_runner import SimulationRunner

    SimulationRunner._processes.clear()
    SimulationRunner._run_states.clear()
    SimulationRunner._watchdog_threads.clear()
    yield
    SimulationRunner._processes.clear()
    SimulationRunner._run_states.clear()
    SimulationRunner._watchdog_threads.clear()


class TestActiveProcessCount:
    """``_count_active_processes`` filtert finished/zombied Prozesse heraus."""

    def test_no_processes_returns_zero(self):
        from app.services.simulation_runner import SimulationRunner
        assert SimulationRunner._count_active_processes() == 0

    def test_running_process_counts(self):
        from app.services.simulation_runner import SimulationRunner

        proc = MagicMock()
        proc.poll.return_value = None  # noch laufend
        SimulationRunner._processes["sim_aaaaaaaa11111111"] = proc

        assert SimulationRunner._count_active_processes() == 1

    def test_finished_process_does_not_count(self):
        from app.services.simulation_runner import SimulationRunner

        running = MagicMock()
        running.poll.return_value = None
        finished = MagicMock()
        finished.poll.return_value = 0  # exit code 0

        SimulationRunner._processes["sim_aaaaaaaa11111111"] = running
        SimulationRunner._processes["sim_bbbbbbbb22222222"] = finished

        assert SimulationRunner._count_active_processes() == 1

    def test_poll_oserror_does_not_crash(self):
        from app.services.simulation_runner import SimulationRunner

        crashing = MagicMock()
        crashing.poll.side_effect = OSError("permission denied")
        SimulationRunner._processes["sim_cccccccc33333333"] = crashing

        # Defensive: OSError wird gefressen, Counter bleibt bei 0.
        assert SimulationRunner._count_active_processes() == 0


class TestQuotaEnforcement:
    """``start_simulation`` wirft ``SimulationQuotaExceeded`` ueber dem Limit."""

    def test_quota_check_raises_when_limit_reached(self, monkeypatch):
        from app.services.simulation_runner import (
            SimulationRunner,
            SimulationQuotaExceeded,
        )

        monkeypatch.setattr(_Config(), "MAX_CONCURRENT_SIMULATIONS", 2, raising=False)

        # 2 laufende Prozesse simulieren.
        for sid in ("sim_aaaaaaaa11111111", "sim_bbbbbbbb22222222"):
            running = MagicMock()
            running.poll.return_value = None
            SimulationRunner._processes[sid] = running

        # ``get_run_state`` muss None zurueckgeben fuer die neue ID,
        # sonst greift die "laeuft schon"-ValueError-Pruefung davor.
        with patch.object(SimulationRunner, "get_run_state", return_value=None):
            with pytest.raises(SimulationQuotaExceeded) as exc:
                SimulationRunner.start_simulation(
                    simulation_id="sim_cccccccc33333333",
                    platform="parallel",
                )

        msg = str(exc.value)
        assert "Maximum 2" in msg
        assert "MAX_CONCURRENT_SIMULATIONS" in msg

    def test_quota_check_passes_when_below_limit(self, monkeypatch):
        from app.services.simulation_runner import (
            SimulationRunner,
            SimulationQuotaExceeded,
        )

        monkeypatch.setattr(_Config(), "MAX_CONCURRENT_SIMULATIONS", 5, raising=False)

        # Nur 1 laufender Prozess.
        running = MagicMock()
        running.poll.return_value = None
        SimulationRunner._processes["sim_aaaaaaaa11111111"] = running

        # Wir lassen den Aufruf weiterlaufen bis zur naechsten Validierung,
        # die mit ValueError ueber "Pfad/Konfig" scheitert. Das beweist,
        # dass der Quota-Check NICHT ausgeloest wurde.
        with patch.object(SimulationRunner, "get_run_state", return_value=None):
            with pytest.raises(ValueError):
                # ValueError aus safe_id oder fehlender Config — NICHT Quota.
                SimulationRunner.start_simulation(
                    simulation_id="sim_cccccccc33333333",
                    platform="parallel",
                )

    def test_quota_check_treats_zombies_as_inactive(self, monkeypatch):
        """Crashed Prozesse zaehlen NICHT zum Limit (Crash-Loop-Schutz)."""
        from app.services.simulation_runner import SimulationRunner

        monkeypatch.setattr(_Config(), "MAX_CONCURRENT_SIMULATIONS", 2, raising=False)

        # 2 ZOMBIES (poll() liefert exit code).
        for sid in ("sim_aaaaaaaa11111111", "sim_bbbbbbbb22222222"):
            zombie = MagicMock()
            zombie.poll.return_value = 1
            SimulationRunner._processes[sid] = zombie

        assert SimulationRunner._count_active_processes() == 0


class TestPreexecFn:
    """``_build_preexec_fn`` setzt rlimits korrekt (POSIX) oder skipt (Windows)."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
    def test_posix_preexec_fn_returns_callable(self, monkeypatch):
        from app.services.simulation_runner import _build_preexec_fn

        monkeypatch.setattr(_Config(), "SIMULATION_MAX_MEMORY_MB", 1024, raising=False)
        monkeypatch.setattr(_Config(), "SIMULATION_MAX_CPU_SECONDS", 60, raising=False)

        fn = _build_preexec_fn()
        assert fn is not None
        assert callable(fn)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
    def test_posix_preexec_fn_calls_setrlimit(self, monkeypatch):
        """Verifiziert, dass setrlimit mit den Config-Werten aufgerufen wird."""
        import resource

        from app.services import simulation_runner as runner_mod

        monkeypatch.setattr(_Config(), "SIMULATION_MAX_MEMORY_MB", 512, raising=False)
        monkeypatch.setattr(_Config(), "SIMULATION_MAX_CPU_SECONDS", 30, raising=False)

        calls = []

        def fake_setrlimit(which, limits):
            calls.append((which, limits))

        monkeypatch.setattr(resource, "setrlimit", fake_setrlimit)

        fn = runner_mod._build_preexec_fn()
        assert fn is not None
        fn()  # simuliert den Subprozess-preexec-Hook

        # Erwartet: zwei Aufrufe — RLIMIT_AS und RLIMIT_CPU.
        kinds = {c[0] for c in calls}
        assert resource.RLIMIT_AS in kinds
        assert resource.RLIMIT_CPU in kinds

        # Konkrete Werte pruefen.
        for kind, (soft, hard) in calls:
            if kind == resource.RLIMIT_AS:
                assert soft == hard == 512 * 1024 * 1024
            elif kind == resource.RLIMIT_CPU:
                assert soft == hard == 30


class TestWallClockWatchdog:
    """Watchdog killt Subprozesse nach Wall-Clock-Timeout."""

    def test_watchdog_kills_after_timeout(self, monkeypatch):
        import subprocess

        from app.services.simulation_runner import SimulationRunner

        monkeypatch.setattr(_Config(), "SIMULATION_MAX_WALL_SECONDS", 1, raising=False)

        proc = MagicMock()
        # process.wait(timeout=...) wirft TimeoutExpired -> Watchdog killt.
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1)
        proc.kill = MagicMock()

        SimulationRunner._start_wall_clock_watchdog("sim_aaaaaaaa11111111", proc)

        # Daemon-Thread laufen lassen.
        thread = SimulationRunner._watchdog_threads["sim_aaaaaaaa11111111"]
        thread.join(timeout=3)

        proc.kill.assert_called_once()

    def test_watchdog_disabled_when_timeout_zero(self, monkeypatch):
        from app.services.simulation_runner import SimulationRunner

        SimulationRunner._watchdog_threads.clear()
        monkeypatch.setattr(_Config(), "SIMULATION_MAX_WALL_SECONDS", 0, raising=False)

        proc = MagicMock()
        SimulationRunner._start_wall_clock_watchdog("sim_disabled1234abcd", proc)

        # Kein Thread registriert.
        assert "sim_disabled1234abcd" not in SimulationRunner._watchdog_threads

    def test_watchdog_does_not_kill_if_process_completes_in_time(self, monkeypatch):
        from app.services.simulation_runner import SimulationRunner

        monkeypatch.setattr(_Config(), "SIMULATION_MAX_WALL_SECONDS", 1, raising=False)

        proc = MagicMock()
        proc.wait.return_value = 0  # process beendet sich rechtzeitig
        proc.kill = MagicMock()

        SimulationRunner._start_wall_clock_watchdog("sim_aaaaaaaa11111111", proc)
        thread = SimulationRunner._watchdog_threads["sim_aaaaaaaa11111111"]
        thread.join(timeout=2)

        proc.kill.assert_not_called()


class TestApiQuota429:
    """API uebersetzt ``SimulationQuotaExceeded`` in 429."""

    @pytest.fixture
    def app_with_auth(self, monkeypatch):
        ConfigCls = _Config()
        monkeypatch.setattr(ConfigCls, "MIROFISH_API_KEY", _TEST_KEY, raising=False)
        monkeypatch.setattr(ConfigCls, "SECRET_KEY", "y" * 32, raising=False)
        monkeypatch.setattr(ConfigCls, "LLM_API_KEY", "test-key", raising=False)

        from app import create_app

        flask_app = create_app(ConfigCls)
        flask_app.config["TESTING"] = True
        flask_app.config["MIROFISH_API_KEY"] = _TEST_KEY
        return flask_app

    def test_start_returns_429_on_quota_exceeded(self, app_with_auth):
        from app.services.simulation_runner import SimulationQuotaExceeded

        client = app_with_auth.test_client()

        # SimulationManager.get_simulation muss truthy sein, damit der
        # Code-Pfad bis zum SimulationRunner.start_simulation kommt.
        fake_state = MagicMock()
        fake_state.status = "ready"

        with patch(
            "app.api.simulation.SimulationManager"
        ) as fake_manager_cls, patch(
            "app.api.simulation.SimulationRunner.start_simulation",
            side_effect=SimulationQuotaExceeded(
                "Maximum 2 gleichzeitige Simulationen erreicht (aktiv: 2). "
                "MAX_CONCURRENT_SIMULATIONS"
            ),
        ):
            fake_manager_cls.return_value.get_simulation.return_value = fake_state
            resp = client.post(
                "/api/simulation/start",
                json={"simulation_id": "sim_deadbeef12345678"},
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 429
        body = resp.get_json()
        assert body["success"] is False
        assert body.get("error_code") == "simulation_quota_exceeded"
        assert "Maximum" in body["error"]
