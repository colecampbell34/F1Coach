from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch

from f1coach.cli import main
from f1coach.coach import CompletedLap
from f1coach.dashboard import STATIC_DIR, TelemetryRuntime


class TelemetryRuntimeTests(unittest.TestCase):
    def test_pause_resume_preserves_current_state(self) -> None:
        runtime = TelemetryRuntime()
        runtime.coach.notices.append("existing data")

        paused = runtime.pause()
        resumed = runtime.resume()

        self.assertTrue(paused["paused"])
        self.assertFalse(resumed["paused"])
        self.assertIn("existing data", resumed["notices"])

    def test_new_session_clears_paused_packet_counts(self) -> None:
        runtime = TelemetryRuntime()
        runtime.paused_packet_counts["lap_data"] = 3

        state = runtime.start_new_session()

        self.assertEqual(state["pausedPackets"], {})
        self.assertIn("Manual new session started.", state["notices"])

    def test_set_driving_goal_updates_snapshot(self) -> None:
        runtime = TelemetryRuntime()

        state = runtime.set_driving_goal("race")

        self.assertEqual(state["drivingGoal"], "race")
        self.assertIn("Coaching goal set to race pace.", state["notices"])

    def test_sector_theoretical_best_updates_snapshot(self) -> None:
        runtime = TelemetryRuntime()
        runtime.packet_counts["lap_data"] = 1
        lap = CompletedLap(1, 83_900, 27_000, 28_000, False, [])
        runtime.coach.clean_laps = [lap]
        runtime.coach.completed_laps = [lap]

        state = runtime.snapshot()

        self.assertEqual(state["reference"]["name"], "Theoretical best")
        self.assertEqual(state["reference"]["source"], "sector-theoretical")
        self.assertEqual(state["reference"]["lapTimeMs"], 83_900)
        self.assertEqual(state["reference"]["samples"], [])
        self.assertTrue(any(item["title"] == "Time-only target active" for item in state["diagnostics"]))

    def test_snapshot_exposes_waiting_diagnostics_before_packets(self) -> None:
        runtime = TelemetryRuntime()

        state = runtime.snapshot()

        self.assertEqual(state["diagnostics"][0]["title"], "Waiting for UDP packets")

    def test_snapshot_exposes_missing_packet_diagnostics(self) -> None:
        runtime = TelemetryRuntime()
        runtime.packet_counts["lap_data"] = 3

        state = runtime.snapshot()

        titles = {item["title"] for item in state["diagnostics"]}
        self.assertIn("Missing car telemetry packets", titles)
        self.assertIn("No completed lap yet", titles)

    def test_cli_reports_http_port_bind_failure(self) -> None:
        fake_sock = Mock()
        with (
            patch("f1coach.cli.open_udp_socket", return_value=fake_sock),
            patch("f1coach.cli.serve_dashboard", side_effect=OSError("address already in use")),
            patch("sys.stderr", new=io.StringIO()),
        ):
            exit_code = main(
                [
                    "dashboard",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--http-host",
                    "127.0.0.1",
                    "--http-port",
                    "8765",
                ]
            )

        self.assertEqual(exit_code, 2)
        fake_sock.close.assert_called_once()

    def test_dashboard_static_assets_are_present(self) -> None:
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn("F1Coach Garage Review", index)
        self.assertIn("Delta Circuit", index)
        self.assertIn("diagnostics", index)
        self.assertIn(".appShell", css)
        self.assertIn("function renderReviewHero", js)
        self.assertIn("function renderDiagnostics", js)


if __name__ == "__main__":
    unittest.main()
