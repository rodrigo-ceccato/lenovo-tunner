"""Headless regression coverage for terminal resizing without hardware writes."""

from datetime import datetime, timedelta
from io import StringIO
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Select

import intel_controls
from app import ApplyConfirmation, TunnerApp, TuningValue, ValueRow, nvidia_clock_values


class NvidiaClockTest(unittest.TestCase):
    @patch("app.nvidia_output")
    def test_memory_clock_range_has_intermediate_values(self, output):
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

        self.assertIn(8000, maximum.choices)
        self.assertIn(9001, maximum.choices)
        self.assertGreater(len(maximum.choices), 3)


class LayoutTest(unittest.IsolatedAsyncioTestCase):
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
