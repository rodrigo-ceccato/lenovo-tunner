"""Headless regression coverage for terminal resizing without hardware writes."""

from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Input, RichLog, Select

import intel_controls
from app import (
    ApplyConfirmation,
    LENOVO_ATTRIBUTES,
    TunnerApp,
    TuningValue,
    ValueRow,
    nvidia_clock_values,
    probe_documented_values,
)


class LiveDocumentedValueTest(unittest.TestCase):
    def settings(self):
        settings = [
            TuningValue(key.replace("-", " "), 1, 1, 200, "W")
            for key in LENOVO_ATTRIBUTES
        ]
        settings.append(TuningValue("NVIDIA power ceiling", 175, 5, 175, "W"))
        return settings

    @patch("app.nvidia_output", return_value="120.00, 10.00, 175.00\n")
    def test_probes_lenovo_and_nvidia_values(self, _output):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, attribute in enumerate(LENOVO_ATTRIBUTES.values(), start=1):
                path = root / attribute
                path.mkdir()
                (path / "current_value").write_text(str(index * 10))

            with patch("app.LENOVO_ATTRIBUTE_ROOT", root):
                settings = probe_documented_values(self.settings())

        values = {setting.key: setting.value for setting in settings}
        self.assertEqual(values["lenovo-cpu-cross-load-limit"], 10)
        self.assertEqual(values["lenovo-gpu-temperature-target"], 50)
        power = next(setting for setting in settings if setting.key == "nvidia-power-ceiling")
        self.assertEqual((power.value, power.minimum, power.maximum), (120, 10, 175))

    @patch("app.nvidia_output", side_effect=OSError)
    def test_marks_failed_live_reads_unavailable(self, _output):
        with TemporaryDirectory() as directory, patch("app.LENOVO_ATTRIBUTE_ROOT", Path(directory)):
            settings = probe_documented_values(self.settings())

        for setting in settings:
            self.assertIsNone(setting.value)
            self.assertIn("unavailable", setting.unavailable_reason.lower())


class NvidiaClockTest(unittest.TestCase):
    @patch("app.nvidia_output")
    def test_memory_clock_accepts_any_value_in_reported_range(self, output):
        output.side_effect = [
            "1410, 6001\n",
            """
            Memory : 9001 MHz
                Graphics : 1410 MHz
            Memory : 6001 MHz
                Graphics : 1200 MHz
            Memory : 405 MHz
                Graphics : 405 MHz
            """,
        ]

        settings = {setting.key: setting for setting in nvidia_clock_values()}
        maximum = settings["nvidia-memory-clock-maximum"]

        self.assertEqual((maximum.minimum, maximum.maximum), (405, 9001))
        self.assertEqual(maximum.choices, ())
        maximum.value = 6123
        self.assertTrue(maximum.can_change(-1))
        self.assertTrue(maximum.can_change(1))


class ApplyCommandTest(unittest.TestCase):
    @staticmethod
    def app(settings, *, profile="balanced", power_available=False):
        app = Mock()
        app.settings = settings
        app.options = {
            "intel-mode": "keep",
            "profile": profile,
            "turbo": "on",
            "core-mode": "keep",
            "memory-mode": "keep",
        }
        app.profiles = [profile]
        app.nvidia_power_limit_available = power_available
        return app

    @patch("app.cpu_groups", return_value={})
    @patch("app.turbo_state", return_value=True)
    def test_custom_profile_rejects_unavailable_lenovo_value(self, _turbo, _groups):
        settings = [
            TuningValue(key.replace("-", " "), 50, 1, 200, "W")
            for key in LENOVO_ATTRIBUTES
        ]
        settings[0].value = None
        app = self.app(settings, profile="custom")

        with self.assertRaisesRegex(ValueError, "lenovo-cpu-cross-load-limit unavailable"):
            TunnerApp.apply_commands(app)

    @patch("app.cpu_groups", return_value={})
    @patch("app.turbo_state", return_value=True)
    def test_power_apply_rejects_unavailable_live_value(self, _turbo, _groups):
        setting = TuningValue("NVIDIA power ceiling", None, 5, 175, "W")
        app = self.app([setting], power_available=True)

        with self.assertRaisesRegex(ValueError, "NVIDIA power limit unavailable"):
            TunnerApp.apply_commands(app)


class LayoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_log_shows_executed_command(self):
        command = ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch.object(TunnerApp, "apply_commands", return_value=[(command, None)]),
            patch("app.run_command") as run_command,
            patch("app.write_last_values"),
        ):
            app = TunnerApp()
            async with app.run_test() as pilot:
                app.apply_confirmed(True)
                await pilot.pause()
                output = "\n".join(
                    line.text for line in app.query_one("#apply-log", RichLog).lines
                )

        run_command.assert_called_once()
        self.assertIn("APPLIED", output)
        self.assertIn("sudo -n nvidia-smi -i 0 -pl 150", output)

    async def test_apply_log_shows_planning_failure(self):
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch.object(
                TunnerApp,
                "apply_commands",
                side_effect=ValueError("Lenovo profile unavailable"),
            ),
        ):
            app = TunnerApp()
            async with app.run_test() as pilot:
                app.apply_confirmed(True)
                await pilot.pause()
                output = "\n".join(
                    line.text for line in app.query_one("#apply-log", RichLog).lines
                )

        self.assertIn("FAILED", output)
        self.assertIn("Lenovo profile unavailable", output)

    async def test_memory_clock_can_be_entered_directly(self):
        setting = TuningValue("NVIDIA memory clock maximum", 405, 405, 7001, "MHz")
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[setting]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch("app.write_last_values"),
        ):
            app = TunnerApp()
            async with app.run_test() as pilot:
                app.query_one("#memory-mode", Select).value = "locked"
                await pilot.pause()
                value = app.query_one(ValueRow).query_one("#value", Input)
                value.value = "6123"
                await pilot.pause()

                self.assertEqual(setting.value, 6123)

                value.value = ""
                await pilot.pause()

                self.assertIsNone(setting.value)
                self.assertEqual(value.value, "")

    async def test_unavailable_memory_clock_stays_disabled_in_locked_mode(self):
        setting = TuningValue(
            "NVIDIA memory clock maximum",
            None,
            0,
            0,
            "MHz",
            unavailable_reason="NVIDIA clock query failed",
        )
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[setting]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch("app.write_last_values"),
        ):
            app = TunnerApp()
            async with app.run_test() as pilot:
                app.query_one("#memory-mode", Select).value = "locked"
                await pilot.pause()

                row = app.query_one(ValueRow)
                self.assertTrue(row.disabled)
                self.assertTrue(row.query_one("#value", Input).disabled)

    async def test_live_intel_mode_populates_values_without_config(self):
        unavailable = [
            TuningValue(
                key.replace('-', ' '), None, low, high, unit, step=step,
                unavailable_reason='Intel config unavailable or unsupported',
            )
            for key, (low, high, unit, step) in intel_controls.SPECS.items()
        ]
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=unavailable),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch("app.intel_controls.live_control_values", return_value=intel_controls.LIVE_DEFAULTS),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch("app.write_last_values"),
        ):
            app = TunnerApp()
            async with app.run_test() as pilot:
                app.query_one('#intel-mode', Select).value = 'undervolt'
                await pilot.pause()

                self.assertEqual(
                    {setting.key: setting.value for setting in app.settings},
                    intel_controls.LIVE_DEFAULTS,
                )

    async def test_failed_stress_resets_button_and_reports_error(self):
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
        ):
            app = TunnerApp()
            async with app.run_test():
                process = Mock()
                process.poll.return_value = 1
                process.stderr = StringIO("unable to load libcublas.so.12\n")
                app.stress_processes["gpu"] = process
                app.query_one("#stress-gpu", Button).label = "Stop GPU stress"

                with patch.object(app, "notify") as notify:
                    self.assertTrue(app.refresh_stress_processes())

                self.assertNotIn("gpu", app.stress_processes)
                self.assertEqual(str(app.query_one("#stress-gpu", Button).label), "Stress GPU")
                self.assertIn("libcublas.so.12", str(app.query_one("#activity").render()))
                notify.assert_called_once_with(
                    "GPU stress failed: unable to load libcublas.so.12",
                    severity="error",
                    timeout=10,
                )

    async def test_successful_apply_remains_visible_in_activity(self):
        with (
            patch("app.load_values", return_value=[]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
        ):
            app = TunnerApp()
            async with app.run_test():
                app.last_change = app.last_apply = datetime.now() - timedelta(seconds=6)
                app.refresh_activity()

                self.assertIn(
                    "Applied successfully", str(app.query_one("#activity").render())
                )

    async def test_controls_remain_visible_after_resize(self):
        await self.check_resize_layout(confirmation_open=False)

    async def test_controls_remain_visible_after_resize_with_confirmation(self):
        await self.check_resize_layout(confirmation_open=True)

    async def check_resize_layout(self, *, confirmation_open):
        setting = TuningValue("CPU P-core maximum frequency", 3000, 800, 5400, "MHz", 100)
        with (
            patch("app.load_values", return_value=[setting]),
            patch("app.cpu_clock_values", return_value=[]),
            patch("app.nvidia_clock_values", return_value=[]),
            patch("app.intel_values", return_value=[]),
            patch("app.nvidia_power_limit_available", return_value=False),
            patch.object(TunnerApp, "refresh_telemetry"),
            patch("app.write_last_values"),
        ):
            app = TunnerApp()
            async with app.run_test(size=(80, 24)) as pilot:
                for width in (80, 60, 119, 120, 140, 60, 140, 80):
                    if confirmation_open:
                        app.push_screen(ApplyConfirmation(), app.apply_confirmed)
                        await pilot.pause()
                    await pilot.resize_terminal(width, 24)
                    if confirmation_open:
                        self.assertTrue(await pilot.click("#cancel"))
                        await pilot.pause()
                    row = app.query_one(ValueRow)
                    row.scroll_visible(immediate=True)
                    await pilot.pause()
                    pane = app.query_one("#tuning-plan").scrollable_content_region
                    self.assertEqual(app.query_one("#status-rail").display, width >= 120)
                    self.assertEqual(row.query_one("#range").display, width >= 80)
                    self.assertGreaterEqual(row.query_one(".setting-name").size.width, 20)
                    for selector in ("#down", "#value", "#up"):
                        control = row.query_one(selector)
                        self.assertGreaterEqual(control.region.x, pane.x)
                        self.assertLessEqual(control.region.right, pane.right)
                    before = setting.value
                    self.assertTrue(await pilot.click(row.query_one("#up", Button)))
                    self.assertEqual(setting.value, before + setting.step)
                    self.assertTrue(await pilot.click(row.query_one("#down", Button)))
                    self.assertEqual(setting.value, before)


if __name__ == "__main__":
    unittest.main()
