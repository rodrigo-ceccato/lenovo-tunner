"""Headless regression coverage for terminal resizing without hardware writes."""

import unittest
from unittest.mock import patch

from textual.widgets import Button

from app import ApplyConfirmation, TunnerApp, TuningValue, ValueRow


class LayoutTest(unittest.IsolatedAsyncioTestCase):
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
