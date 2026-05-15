from __future__ import annotations

import unittest

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

    def test_set_theoretical_best_updates_snapshot(self) -> None:
        runtime = TelemetryRuntime()

        state = runtime.set_theoretical_best(83_456)

        self.assertEqual(state["reference"]["name"], "Theoretical best")
        self.assertEqual(state["reference"]["source"], "manual-theoretical")
        self.assertEqual(state["reference"]["lapTimeMs"], 83_456)
        self.assertEqual(state["reference"]["samples"], [])

    def test_dashboard_static_assets_are_present(self) -> None:
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn("F1Coach Garage Review", index)
        self.assertIn("Delta Circuit", index)
        self.assertIn(".appShell", css)
        self.assertIn("function renderReviewHero", js)


if __name__ == "__main__":
    unittest.main()
