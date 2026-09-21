"""Headless regression coverage for the interface, without hardware writes."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, Input, Label, RichLog, Select, Static
from textual.worker import WorkerCancelled

import app as app_module
import intel_controls
from app import (
    ApplyConfirmation,
    ApplyPlan,
    LEGION_FEATURES,
    LENOVO_ATTRIBUTES,
    LegionToggle,
    NumberInput,
    Telemetry,
    ToggleRow,
    TunnerApp,
    TuningValue,
    ValueRow,
    cpu_boost_text,
    cpu_clock_values,
    cpu_groups,
    cpu_temperature_from_sensors,
    describe_cpu_policies,
    intel_values,
    nvidia_clock_values,
    nvidia_power_limit_available,
    probe_documented_values,
    probe_legion_toggles,
    read_telemetry,
)


PROFILES = ["quiet", "balanced", "performance", "custom"]
LEGION_CLI = "/usr/bin/legion_cli"


def headless_app(stack, *, settings=(), cpu=(), nvidia=(), intel=(), legion=(), saved=None,
                 power_available=False, sudo=True, profile="balanced", poll=False):
    """Build a TunnerApp with every hardware probe patched out.

    `saved`, when given, is written to a temporary last-values.json that the
    app reads from and writes to, so Restore can be exercised end to end.
    Telemetry polling is stubbed out unless `poll` is set, in which case the
    test must patch `app.read_telemetry` itself. `legion` toggles are served
    by a fake legion_cli at /usr/bin/legion_cli; none means it is not installed.
    """
    stack.enter_context(patch("app.load_values", return_value=list(settings)))
    stack.enter_context(patch(
        "app.probe_legion_toggles",
        return_value=(LEGION_CLI if legion else None, list(legion)),
    ))
    stack.enter_context(patch("app.cpu_groups", return_value={}))
    stack.enter_context(patch("app.cpu_clock_values", return_value=list(cpu)))
    stack.enter_context(patch("app.nvidia_clock_values", return_value=list(nvidia)))
    stack.enter_context(patch("app.intel_values", return_value=list(intel)))
    stack.enter_context(patch("app.nvidia_power_limit_available", return_value=power_available))
    stack.enter_context(patch("app.sudo_ready", return_value=sudo))
    stack.enter_context(patch("app.turbo_state", return_value=True))
    if not poll:
        stack.enter_context(patch.object(TunnerApp, "refresh_telemetry"))
    directory = Path(stack.enter_context(TemporaryDirectory()))
    (directory / "platform_profile_choices").write_text(" ".join(PROFILES) + "\n")
    (directory / "platform_profile").write_text(profile + "\n")
    stack.enter_context(patch("app.PLATFORM_PROFILE", directory / "platform_profile"))
    if saved is None:
        stack.enter_context(patch("app.write_last_values"))
    else:
        path = directory / "last-values.json"
        path.write_text(json.dumps(saved))
        stack.enter_context(patch("app.LAST_VALUES", path))
    return TunnerApp()


async def settle(app, pilot):
    """Let the startup probe (and any apply) worker finish, then the UI catch up.

    An exclusive worker superseded while still running raises WorkerCancelled
    from wait_for_complete; that is normal, so wait again for the rest.
    """
    await pilot.pause()
    for _ in range(200):
        try:
            await app.workers.wait_for_complete()
            break
        except WorkerCancelled:
            await asyncio.sleep(0.05)
    await pilot.pause()


def log_text(app):
    return "\n".join(line.text for line in app.query_one("#apply-log", RichLog).lines)


def activity_text(app):
    return str(app.query_one("#activity").render())


def unavailable_intel_rows():
    return [
        TuningValue(
            key.replace('-', ' '), None, low, high, unit, step=step,
            unavailable_reason='Intel config unavailable or unsupported',
        )
        for key, (low, high, unit, step) in intel_controls.SPECS.items()
    ]


def clock_row(value=3000, live=None):
    setting = TuningValue("CPU P-core maximum frequency", value, 800, 5400, "MHz", 100)
    setting.live = value if live is None else live
    return setting


def legion_toggle(feature, live=False, unavailable=None):
    name, _, description = next(entry for entry in LEGION_FEATURES if entry[1] == feature)
    return LegionToggle(name, feature, description, live=live, unavailable_reason=unavailable)


def legion_cli_status(feature, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([LEGION_CLI, f"{feature}-status"], returncode, stdout, stderr)


class LegionProbeTest(unittest.TestCase):
    def test_reads_every_feature_through_legion_cli_status(self):
        # legion_cli prints the Python bool, a notice line first for hybrid
        # mode, its own "not available" text for a missing sysfs node, and
        # exits non-zero with stderr when the user may not read the node.
        replies = {
            "fan-unlock": legion_cli_status("fan-unlock", stdout="True\n"),
            "hybrid-mode": legion_cli_status("hybrid-mode", stdout="This is the current state.\nFalse\n"),
            "batteryconservation": legion_cli_status(
                "batteryconservation", 246,
                stdout="Command not available because feature is not available or kernel module is not loaded.\n",
            ),
            "fnlock": legion_cli_status("fnlock", 1, stderr="Error: [Errno 13] Permission denied\n"),
        }

        def fake_run(arguments, *, access, **kwargs):
            self.assertEqual(access, "read")
            self.assertEqual(arguments[0], LEGION_CLI)
            feature = arguments[1].removesuffix("-status")
            self.assertNotEqual(feature, arguments[1])
            return replies.get(feature, legion_cli_status(feature, stdout="False\n"))

        with patch("app.shutil.which", return_value=LEGION_CLI), patch("app.run_command", side_effect=fake_run):
            legion_cli, toggles = probe_legion_toggles()

        self.assertEqual(legion_cli, LEGION_CLI)
        self.assertEqual([toggle.feature for toggle in toggles], [feature for _, feature, _ in LEGION_FEATURES])
        by_feature = {toggle.feature: toggle for toggle in toggles}
        self.assertEqual((by_feature["fan-unlock"].live, by_feature["fan-unlock"].unavailable_reason), (True, None))
        self.assertEqual((by_feature["hybrid-mode"].live, by_feature["hybrid-mode"].unavailable_reason), (False, None))
        self.assertEqual(by_feature["touchpad"].live, False)
        conservation = by_feature["batteryconservation"]
        self.assertEqual((conservation.live, conservation.unavailable_reason), (None, "Feature unavailable"))
        self.assertIn("kernel module", conservation.detail)
        # A read the user may not make says nothing about the root write, so
        # the row stays editable with its state unknown.
        fnlock = by_feature["fnlock"]
        self.assertEqual((fnlock.live, fnlock.unavailable_reason, fnlock.read_failure), (None, None, "Status read failed"))
        self.assertIn("Permission denied", fnlock.detail)
        self.assertEqual(fnlock.state_text, "Status read failed")
        self.assertTrue(fnlock.modified("on"))

    def test_missing_legion_cli_marks_every_toggle_unavailable_without_running_anything(self):
        with patch("app.shutil.which", return_value=None), patch("app.run_command") as run:
            legion_cli, toggles = probe_legion_toggles()

        self.assertIsNone(legion_cli)
        self.assertEqual(len(toggles), len(LEGION_FEATURES))
        self.assertTrue(all(toggle.unavailable_reason == "legion_cli not installed" for toggle in toggles))
        run.assert_not_called()

    def test_a_failed_launch_is_a_failed_read(self):
        with patch("app.shutil.which", return_value=LEGION_CLI), patch("app.run_command", side_effect=OSError("boom")):
            _, toggles = probe_legion_toggles()

        self.assertEqual((toggles[0].unavailable_reason, toggles[0].read_failure), (None, "Status read failed"))
        self.assertEqual(toggles[0].detail, "boom")

    def test_a_programming_error_in_the_reader_is_not_a_failed_read(self):
        # Only a launch, timeout or decode problem is "Status read failed";
        # a bug in the reader must not hide behind ten failed rows.
        with patch("app.shutil.which", return_value=LEGION_CLI), \
                patch("app.run_command", side_effect=TypeError("unexpected keyword argument")):
            with self.assertRaises(TypeError):
                probe_legion_toggles()

    def test_one_undecodable_reply_fails_only_that_read(self):
        # subprocess raises UnicodeDecodeError, a ValueError, for output the
        # locale cannot decode; an optional tool's oddity must not sink the
        # whole hardware probe.
        def fake_run(arguments, *, access, **kwargs):
            if arguments[1] == "touchpad-status":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return legion_cli_status(arguments[1].removesuffix("-status"), stdout="True\n")

        with patch("app.shutil.which", return_value=LEGION_CLI), patch("app.run_command", side_effect=fake_run):
            legion_cli, toggles = probe_legion_toggles()

        self.assertEqual(legion_cli, LEGION_CLI)
        by_feature = {toggle.feature: toggle for toggle in toggles}
        self.assertEqual(by_feature["touchpad"].read_failure, "Status read failed")
        self.assertIn("can't decode byte 0xff", by_feature["touchpad"].detail)
        self.assertTrue(all(toggle.live for feature, toggle in by_feature.items() if feature != "touchpad"))


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

    def test_multi_step_change_walks_supported_clocks(self):
        setting = TuningValue("NVIDIA core clock maximum", 1200, 405, 1410, "MHz", choices=(405, 1200, 1410))

        setting.change(10)
        self.assertEqual(setting.value, 1410)
        setting.change(-10)
        self.assertEqual(setting.value, 405)
        setting.value = 1300
        self.assertEqual(setting.nearest_choice(setting.value), 1200)

    def test_stepping_from_a_typed_value_reaches_the_adjacent_clock_first(self):
        setting = TuningValue("NVIDIA core clock maximum", 1300, 405, 1410, "MHz", choices=(405, 1200, 1410))
        setting.change(-1)
        self.assertEqual(setting.value, 1200)

        setting.value = 1100
        setting.change(1)
        self.assertEqual(setting.value, 1200)

        setting.value = 1100
        setting.change(2)
        self.assertEqual(setting.value, 1410)
        setting.value = 1300
        setting.change(-5)
        self.assertEqual(setting.value, 405)

    @patch("app.nvidia_output")
    def test_idle_clocks_below_the_lowest_step_start_at_that_step(self, output):
        output.side_effect = [
            "210, 300\n",
            "Memory : 9001 MHz\n    Graphics : 1410 MHz\nMemory : 405 MHz\n    Graphics : 405 MHz\n",
        ]
        settings = {setting.key: setting for setting in nvidia_clock_values()}
        self.assertEqual(settings["nvidia-core-clock-minimum"].value, 405)
        self.assertEqual(settings["nvidia-memory-clock-minimum"].value, 405)

    def test_power_availability_comes_from_the_probed_row(self):
        power = TuningValue("NVIDIA power ceiling", 150, 5, 175, "W")
        self.assertTrue(nvidia_power_limit_available([power]))
        power.value, power.unavailable_reason = None, "Live NVIDIA power limit unavailable"
        self.assertFalse(nvidia_power_limit_available([power]))
        self.assertFalse(nvidia_power_limit_available([]))


class CpuGroupTest(unittest.TestCase):
    def sysfs(self, stack, bases, *, rated=None, base_files=True, floor=800):
        root = Path(stack.enter_context(TemporaryDirectory()))
        for index, base in enumerate(bases):
            policy = root / f"policy{index}"
            policy.mkdir()
            if base_files:
                (policy / "base_frequency").write_text(f"{base}000\n")
            (policy / "cpuinfo_min_freq").write_text(f"{floor}000\n")
            (policy / "cpuinfo_max_freq").write_text(f"{(rated or {}).get(base, base * 2)}000\n")
            (policy / "scaling_min_freq").write_text(f"{floor}000\n")
            (policy / "scaling_max_freq").write_text(f"{base}000\n")
            (policy / "scaling_cur_freq").write_text(f"{base}000\n")
        stack.enter_context(patch("app.CPUFREQ_ROOT", root))
        return root

    def test_hybrid_cpu_groups_by_base_frequency_without_a_model_check(self):
        with ExitStack() as stack:
            self.sysfs(stack, [2200, 1600, 2200, 1600], rated={2200: 5400, 1600: 3900})
            groups = cpu_groups()
            settings = {setting.key: setting for setting in cpu_clock_values(groups)}
            policies, average = describe_cpu_policies(groups)
            boost = cpu_boost_text(groups)

        self.assertEqual(sorted(groups), ["e", "p"])
        self.assertEqual(average, 1900)
        self.assertEqual(len(groups["p"]), 2)
        self.assertEqual(
            (settings["cpu-p-core-maximum-frequency"].minimum, settings["cpu-p-core-maximum-frequency"].maximum),
            (800, 5400),
        )
        self.assertEqual(settings["cpu-e-core-maximum-frequency"].maximum, 3900)
        self.assertEqual(settings["cpu-e-core-minimum-frequency"].value, 800)
        self.assertIn("P-cores: 800–2200 MHz", policies)
        self.assertIn("P: up to 5400", boost)

    def test_uniform_cpu_is_one_group_named_all(self):
        with ExitStack() as stack:
            self.sysfs(stack, [3000, 3000])
            groups = cpu_groups()
            names = [setting.name for setting in cpu_clock_values(groups)]

        self.assertEqual(list(groups), ["all"])
        self.assertEqual(names, ["CPU all-core minimum frequency", "CPU all-core maximum frequency"])

    def test_fixed_frequency_policy_is_still_writable(self):
        # A VM or locked policy reports cpuinfo_min_freq == cpuinfo_max_freq;
        # writing that single value back is valid, not "limits unavailable".
        with ExitStack() as stack:
            self.sysfs(stack, [2400], rated={2400: 2400}, floor=2400)
            settings = {setting.key: setting for setting in cpu_clock_values(cpu_groups())}

        for key in ("cpu-all-core-minimum-frequency", "cpu-all-core-maximum-frequency"):
            self.assertEqual(settings[key].value, 2400, key)
            self.assertIsNone(settings[key].unavailable_reason, key)
            self.assertEqual((settings[key].minimum, settings[key].maximum), (2400, 2400))

    def test_driver_without_base_frequency_is_one_group_even_with_ranked_maxima(self):
        # amd-pstate reports a slightly higher cpuinfo_max_freq on preferred
        # cores; that is a ranking, not a core type, so no P/E split.
        with ExitStack() as stack:
            self.sysfs(stack, [3000, 2900], base_files=False)
            groups = cpu_groups()
            self.assertEqual(list(groups), ["all"])
            self.assertEqual(len(groups["all"]), 2)
            self.assertEqual([s.name for s in cpu_clock_values(groups)],
                             ["CPU all-core minimum frequency", "CPU all-core maximum frequency"])

    def test_no_policies_means_no_groups(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(TemporaryDirectory()))
            stack.enter_context(patch("app.CPUFREQ_ROOT", root / "missing"))
            self.assertEqual(cpu_groups(), {})
            self.assertEqual(cpu_clock_values({}), [])


class ApplyCommandTest(unittest.TestCase):
    @staticmethod
    def app(settings, *, profile="balanced", power_available=False, groups=None):
        app = TunnerApp()
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
        app.cpu_groups = groups or {}
        return app

    @patch("app.turbo_state", return_value=True)
    def test_custom_profile_rejects_unavailable_lenovo_value(self, _turbo):
        settings = [
            TuningValue(key.replace("-", " "), 50, 1, 200, "W")
            for key in LENOVO_ATTRIBUTES
        ]
        settings[0].value = None
        app = self.app(settings, profile="custom")

        with self.assertRaisesRegex(ValueError, "lenovo-cpu-cross-load-limit unavailable"):
            app.apply_commands()

    @patch("app.turbo_state", return_value=True)
    def test_power_apply_rejects_unavailable_live_value(self, _turbo):
        setting = TuningValue("NVIDIA power ceiling", None, 5, 175, "W")
        app = self.app([setting], power_available=True)

        with self.assertRaisesRegex(ValueError, "NVIDIA power limit unavailable"):
            app.apply_commands()

    @patch("app.turbo_state", return_value=True)
    def test_missing_controls_are_reported_not_key_errors(self, _turbo):
        # A control disappears entirely when its documentation row fails to
        # parse; that must be a planning error, not a crash.
        app = self.app([], power_available=True)
        with self.assertRaisesRegex(ValueError, "NVIDIA power limit unavailable"):
            app.apply_commands()

        app = self.app([], profile="custom")
        with self.assertRaisesRegex(ValueError, "lenovo-cpu-cross-load-limit unavailable"):
            app.apply_commands()

        app = self.app([])
        app.options["intel-mode"] = "undervolt"
        with self.assertRaisesRegex(ValueError, "intel-pl1-sustained-power must be an integer"):
            app.apply_commands()

        app = self.app([])
        app.options["core-mode"] = "locked"
        with self.assertRaisesRegex(ValueError, "GPU core clock range invalid or unavailable"):
            app.apply_commands()

    @patch("app.turbo_state", return_value=True)
    def test_missing_cpu_controls_are_reported(self, _turbo):
        app = self.app([], groups={"p": [Path("/policy0")]})
        with self.assertRaisesRegex(ValueError, "P-core frequency range unavailable or invalid"):
            app.apply_commands()

    @patch("app.cpu_group_limits", return_value=(800, 2200, 5400))
    @patch("app.turbo_state", return_value=True)
    def test_cpu_writes_use_driver_limits_and_cap_at_base_without_turbo(self, _turbo, _limits):
        settings = [
            TuningValue("CPU P-core minimum frequency", 800, 800, 5400, "MHz", 100),
            TuningValue("CPU P-core maximum frequency", 4000, 800, 5400, "MHz", 100),
        ]
        app = self.app(settings, groups={"p": [Path("/policy0")]})

        plan = app.apply_commands()
        writes = [(arguments[-1], text) for arguments, text in plan.commands if "tee" in arguments]
        self.assertIn(("/policy0/scaling_max_freq", "4000000\n"), writes)
        self.assertEqual(writes[-3], ("/policy0/scaling_min_freq", "800000\n"))
        self.assertEqual(
            plan.values,
            {"cpu-p-core-minimum-frequency": 800, "cpu-p-core-maximum-frequency": 4000},
        )

        app.options["turbo"] = "off"
        with self.assertRaisesRegex(ValueError, "800–2200 MHz with Turbo off"):
            app.apply_commands()

    @patch("app.cpu_group_limits", return_value=(800, 2200, 5400))
    @patch("app.turbo_state", return_value=None)
    def test_without_intel_pstate_the_turbo_write_is_skipped_not_fatal(self, _turbo, _limits):
        settings = [
            TuningValue("CPU P-core minimum frequency", 800, 800, 5400, "MHz", 100),
            TuningValue("CPU P-core maximum frequency", 5000, 800, 5400, "MHz", 100),
            TuningValue("NVIDIA power ceiling", 150, 5, 175, "W"),
        ]
        app = self.app(settings, groups={"p": [Path("/policy0")]}, power_available=True)
        app.options["turbo"] = "off"  # the disabled selector's default

        plan = app.apply_commands()
        targets = [arguments[-1] for arguments, _ in plan.commands if "tee" in arguments]
        self.assertNotIn(str(app_module.NO_TURBO), targets)
        self.assertIn("/policy0/scaling_max_freq", targets)
        self.assertTrue(any("nvidia-smi" in arguments for arguments, _ in plan.commands))
        self.assertEqual(plan.values["cpu-p-core-maximum-frequency"], 5000)

    @patch("app.cpu_group_limits", return_value=(800, 2200, 5400))
    @patch("app.turbo_state", return_value=True)
    def test_each_command_carries_the_previews_it_makes_live(self, _turbo, _limits):
        settings = [TuningValue(key.replace("-", " "), 50, 1, 200, "W") for key in LENOVO_ATTRIBUTES]
        settings += [
            TuningValue("CPU P-core minimum frequency", 800, 800, 5400, "MHz", 100),
            TuningValue("CPU P-core maximum frequency", 4000, 800, 5400, "MHz", 100),
            TuningValue("NVIDIA power ceiling", 150, 5, 175, "W"),
            TuningValue("NVIDIA core clock minimum", 405, 405, 1410, "MHz"),
            TuningValue("NVIDIA core clock maximum", 1410, 405, 1410, "MHz"),
        ]
        app = self.app(settings, profile="custom", power_available=True,
                       groups={"p": [Path("/policy0"), Path("/policy1")]})
        app.options["core-mode"] = "locked"

        plan = app.apply_commands()
        self.assertEqual(len(plan.carried), len(plan.commands))
        carried = {}
        for values in plan.carried:
            carried.update(values)
        self.assertEqual(carried, plan.values)
        by_command = {tuple(arguments): values for (arguments, _), values in zip(plan.commands, plan.carried)}
        self.assertEqual(by_command[("sudo", "-n", "tee", str(app_module.PLATFORM_PROFILE))], {})
        self.assertEqual(
            by_command[("sudo", "-n", "tee", str(app_module.LENOVO_ATTRIBUTE_ROOT / "cpu_temp" / "current_value"))],
            {"lenovo-cpu-temperature-target": 50},
        )
        self.assertEqual(by_command[("sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150")], {"nvidia-power-ceiling": 150})
        self.assertEqual(
            by_command[("sudo", "-n", "nvidia-smi", "-i", "0", "-lgc", "405,1410")],
            {"nvidia-core-clock-minimum": 405, "nvidia-core-clock-maximum": 1410},
        )
        # A policy group is live only once its last policy holds the range.
        cpu_writes = [values for (arguments, _), values in zip(plan.commands, plan.carried) if "/policy" in arguments[-1]]
        self.assertEqual(cpu_writes[:-1], [{}] * (len(cpu_writes) - 1))
        self.assertEqual(cpu_writes[-1], {"cpu-p-core-minimum-frequency": 800, "cpu-p-core-maximum-frequency": 4000})

    @patch("app.turbo_state", return_value=True)
    def test_legion_toggles_run_legion_cli_only_for_enable_or_disable(self, _turbo):
        app = self.app([])
        app.legion_cli = LEGION_CLI
        app.legion_toggles = [
            legion_toggle("fan-unlock", live=False),
            legion_toggle("maximumfanspeed", live=True),
            legion_toggle("touchpad", live=None, unavailable="Feature unavailable"),
            legion_toggle("fnlock", live=False),
        ]
        app.options.update({
            "legion-fan-unlock": "on",
            "legion-maximumfanspeed": "off",
            "legion-touchpad": "on",  # restored choice for a feature this firmware lacks
            "legion-fnlock": "keep",
        })

        plan = app.apply_commands()
        legion_commands = [arguments for arguments, _ in plan.commands if LEGION_CLI in arguments]
        self.assertEqual(legion_commands, [
            ["sudo", "-n", LEGION_CLI, "fan-unlock-enable"],
            ["sudo", "-n", LEGION_CLI, "maximumfanspeed-disable"],
        ])
        self.assertEqual(plan.toggles, {"legion-fan-unlock": True, "legion-maximumfanspeed": False})
        # The toggles run last, so what a partial Apply asked of them follows
        # from how many commands ran.
        self.assertEqual(plan.written_toggles(len(plan.commands)), plan.toggles)
        self.assertEqual(plan.written_toggles(len(plan.commands) - 1), {"legion-fan-unlock": True})
        self.assertEqual(plan.written_toggles(len(plan.commands) - 2), {})
        self.assertEqual(plan.written_toggles(0), {})
        # The profile write comes first so a profile switch cannot undo a fan toggle.
        self.assertLess(
            next(index for index, (arguments, _) in enumerate(plan.commands) if "tee" in arguments),
            next(index for index, (arguments, _) in enumerate(plan.commands) if LEGION_CLI in arguments),
        )

        app.legion_cli = None
        self.assertEqual(app.apply_commands().toggles, {})

    @patch("app.turbo_state", return_value=True)
    def test_a_toggle_whose_status_read_failed_is_still_written(self, _turbo):
        app = self.app([])
        app.legion_cli = LEGION_CLI
        toggle = legion_toggle("fnlock", live=None)
        toggle.read_failure = "Status read failed"
        app.legion_toggles = [toggle]
        app.options["legion-fnlock"] = "on"

        plan = app.apply_commands()
        self.assertEqual(plan.toggles, {"legion-fnlock": True})
        self.assertIn(["sudo", "-n", LEGION_CLI, "fnlock-enable"], [arguments for arguments, _ in plan.commands])

    @patch("app.turbo_state", return_value=True)
    def test_enabling_both_battery_modes_is_rejected_before_the_dialog(self, _turbo):
        # Rapid charging turns conservation off, so the second write would
        # silently undo the first; the plan is refused like a PL1 above PL2.
        app = self.app([])
        app.legion_cli = LEGION_CLI
        app.legion_toggles = [
            legion_toggle("batteryconservation", live=False),
            legion_toggle("rapid-charging", live=False),
        ]
        app.options.update({"legion-batteryconservation": "on", "legion-rapid-charging": "on"})

        with self.assertRaisesRegex(ValueError, "Only one of Battery conservation and Rapid charging can be enabled"):
            app.apply_commands()

        # Enabling one while disabling the other is what the tool expects.
        app.options["legion-batteryconservation"] = "off"
        self.assertEqual(app.apply_commands().toggles, {"legion-batteryconservation": False, "legion-rapid-charging": True})

    @patch("app.turbo_state", return_value=True)
    def test_enabling_one_battery_mode_while_the_other_is_kept_on_is_rejected(self, _turbo):
        # Keep promises to leave a feature alone, yet enabling rapid charging
        # would turn conservation off behind the user's back; they are asked
        # to choose Disable for it, so the plan and the dialog show the flip.
        app = self.app([])
        app.legion_cli = LEGION_CLI
        app.legion_toggles = [
            legion_toggle("batteryconservation", live=True),
            legion_toggle("rapid-charging", live=False),
        ]
        app.options.update({"legion-batteryconservation": "keep", "legion-rapid-charging": "on"})

        with self.assertRaisesRegex(
            ValueError,
            "Enabling Rapid charging would turn Battery conservation off: "
            "set Battery conservation to Disable rather than Keep current",
        ):
            app.apply_commands()

        app.options["legion-batteryconservation"] = "off"
        self.assertEqual(app.apply_commands().toggles, {"legion-batteryconservation": False, "legion-rapid-charging": True})

        # A partner that is off, or whose state is unknown, is not in the way.
        app.options["legion-batteryconservation"] = "keep"
        for live in (False, None):
            app.legion_toggles[0].live = live
            self.assertEqual(app.apply_commands().toggles, {"legion-rapid-charging": True})

    @patch("app.turbo_state", return_value=True)
    def test_plan_records_only_the_previews_the_modes_write(self, _turbo):
        # The plan's values are what a successful Apply promotes to "live", so
        # a gated or unwritable row must never appear in them.
        settings = [
            TuningValue(key.replace("-", " "), 50, 1, 200, "W")
            for key in LENOVO_ATTRIBUTES
        ]
        settings.append(TuningValue("NVIDIA power ceiling", 150, 150, 150, "W"))
        settings.append(TuningValue("NVIDIA core clock maximum", 1410, 405, 1410, "MHz"))
        settings.append(TuningValue("NVIDIA core clock minimum", 405, 405, 1410, "MHz"))

        app = self.app(settings, profile="balanced", power_available=False)
        plan = app.apply_commands()
        self.assertEqual(plan.values, {})
        self.assertFalse(any("nvidia-smi" in arguments for arguments, _ in plan.commands))

        app = self.app(settings, profile="custom", power_available=True)
        app.options["core-mode"] = "locked"
        plan = app.apply_commands()
        self.assertEqual(
            set(plan.values),
            set(LENOVO_ATTRIBUTES) | {"nvidia-power-ceiling", "nvidia-core-clock-minimum", "nvidia-core-clock-maximum"},
        )
        self.assertEqual(plan.values["nvidia-power-ceiling"], 150)


class LayoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_loading_indicator_gives_way_to_the_plan(self):
        with ExitStack() as stack:
            app = headless_app(stack, settings=[clock_row()])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertFalse(app.query("#loading"))
                self.assertTrue(app.plan_ready)
                self.assertEqual(len(app.query(ValueRow)), 1)
                self.assertEqual(app.options["profile"], "balanced")

    async def test_apply_log_shows_executed_command(self):
        command = ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        with ExitStack() as stack:
            app = headless_app(stack)
            run_command = stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.apply_confirmed(ApplyPlan([(command, None)]))
                await settle(app, pilot)
                output = log_text(app)

        run_command.assert_called_once()
        self.assertIn("APPLIED", output)
        self.assertIn("sudo -n nvidia-smi -i 0 -pl 150", output)

    async def test_confirmation_lists_the_plan_and_escape_cancels(self):
        command = ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch.object(TunnerApp, "apply_commands", return_value=ApplyPlan([(command, None)])))
            run_command = stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.action_apply()
                await settle(app, pilot)

                self.assertIsInstance(app.screen, ApplyConfirmation)
                dialog = "\n".join(str(static.render()) for static in app.screen.query(Static))
                self.assertIn("sudo -n nvidia-smi -i 0 -pl 150", dialog)
                self.assertIn("Apply 1 command(s)", dialog)
                self.assertIs(app.screen.focused, app.screen.query_one("#cancel", Button))

                # A second Apply while the dialog is open must not stack another.
                dialog_screen = app.screen
                app.action_apply()
                await settle(app, pilot)
                self.assertIs(app.screen, dialog_screen)

                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ApplyConfirmation)
                run_command.assert_not_called()

                app.action_apply()
                await settle(app, pilot)
                self.assertTrue(await pilot.click("#confirm"))
                await settle(app, pilot)
                output = log_text(app)

        run_command.assert_called_once()
        self.assertIn("APPLIED", output)

    async def test_planning_failure_is_reported_without_a_dialog(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch.object(
                TunnerApp, "apply_commands", side_effect=ValueError("Lenovo profile unavailable"),
            ))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.action_apply()
                await pilot.pause()
                output = log_text(app)
                self.assertNotIsInstance(app.screen, ApplyConfirmation)

        self.assertIn("FAILED", output)
        self.assertIn("Lenovo profile unavailable", output)

    async def test_confirmation_warns_when_sudo_is_not_authenticated(self):
        with ExitStack() as stack:
            app = headless_app(stack, sudo=False)
            stack.enter_context(patch.object(TunnerApp, "apply_commands", return_value=ApplyPlan()))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                notice = app.query_one("#notice", Static)
                self.assertTrue(notice.has_class("warning"))
                self.assertIn("sudo -v", str(notice.render()))

                app.action_apply()
                await settle(app, pilot)
                self.assertIn("sudo -v", str(app.screen.query_one("#confirm-warning", Static).render()))

    async def test_sudo_check_runs_off_the_event_loop(self):
        # A stalled sudo must not freeze rendering and input before the dialog.
        gate = threading.Event()
        threads = []

        def slow_sudo():
            threads.append(threading.current_thread())
            gate.wait(10)
            return True

        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch("app.sudo_ready", side_effect=slow_sudo))
            stack.enter_context(patch.object(TunnerApp, "apply_commands", return_value=ApplyPlan()))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.action_apply()
                for _ in range(50):
                    await pilot.pause()
                    if threads:
                        break
                # The loop kept turning while the check was pending.
                self.assertNotIsInstance(app.screen, ApplyConfirmation)
                self.assertIsNot(threads[-1], threading.main_thread())
                gate.set()
                await settle(app, pilot)
                self.assertIsInstance(app.screen, ApplyConfirmation)

    async def test_apply_disables_actions_until_the_worker_finishes(self):
        command = ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        gate = threading.Event()

        def slow_run(arguments, **kwargs):
            gate.wait(10)
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch("app.run_command", side_effect=slow_run))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.apply_confirmed(ApplyPlan([(command, None)]))
                # Flagged before the worker's first message, so nothing can
                # slip in between confirmation and the first command.
                self.assertTrue(app.applying)
                self.assertTrue(app.query_one("#apply", Button).disabled)
                self.assertTrue(app.query_one("#restore", Button).disabled)
                self.assertIn("Applying", activity_text(app))

                app.action_apply()
                app.apply_confirmed(ApplyPlan([(command, None)]))
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ApplyConfirmation)

                gate.set()
                await settle(app, pilot)
                self.assertFalse(app.applying)
                self.assertFalse(app.query_one("#apply", Button).disabled)
                self.assertIn("Applied successfully", activity_text(app))
                self.assertEqual(len(log_text(app).splitlines()), 1)

    async def test_memory_clock_can_be_entered_directly(self):
        setting = TuningValue("NVIDIA memory clock maximum", 405, 405, 7001, "MHz")
        with ExitStack() as stack:
            app = headless_app(stack, nvidia=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
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

    async def test_every_row_accepts_typed_values_and_snaps_to_supported_clocks(self):
        core = TuningValue("NVIDIA core clock maximum", 1200, 405, 1410, "MHz", choices=(405, 1200, 1410))
        with ExitStack() as stack:
            app = headless_app(stack, settings=[clock_row()], nvidia=[core])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.query_one("#core-mode", Select).value = "locked"
                await pilot.pause()
                rows = {row.setting.key: row for row in app.query(ValueRow)}

                field = rows["cpu-p-core-maximum-frequency"].query_one("#value", Input)
                field.focus()
                await pilot.pause()
                field.value = "4000"
                await pilot.pause()
                self.assertEqual(rows["cpu-p-core-maximum-frequency"].setting.value, 4000)

                field = rows["nvidia-core-clock-maximum"].query_one("#value", Input)
                field.focus()
                await pilot.pause()
                field.value = "1300"
                await pilot.pause()
                self.assertEqual(core.value, 1300)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(core.value, 1200)
                self.assertEqual(field.value, "1200")

    async def test_arrow_keys_step_the_focused_row(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                row.query_one("#value", Input).focus()
                await pilot.pause()

                await pilot.press("up")
                self.assertEqual(setting.value, 3100)
                await pilot.press("shift+down")
                self.assertEqual(setting.value, 2100)
                await pilot.press("down")
                self.assertEqual(setting.value, 2000)
                self.assertEqual(row.query_one("#value", Input).value, "2000")

    def test_passthrough_keys_are_app_bindings(self):
        # NumberInput lets these letters reach the app; a binding renamed or
        # removed in one place must be noticed in the other.
        bound = {binding.key for binding in TunnerApp.BINDINGS}
        self.assertTrue(NumberInput.PASSTHROUGH_KEYS <= bound, NumberInput.PASSTHROUGH_KEYS - bound)

    async def test_tab_leaves_a_value_field(self):
        floor = TuningValue("CPU P-core minimum frequency", 800, 800, 5400, "MHz", 100)
        with ExitStack() as stack:
            app = headless_app(stack, cpu=[floor, clock_row(3000)])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                fields = [row.query_one("#value", Input) for row in app.query(ValueRow)]
                fields[0].focus()
                await pilot.pause()

                await pilot.press("tab")
                await pilot.pause()
                self.assertIs(app.focused, fields[1])
                self.assertEqual(fields[0].value, "800")
                await pilot.press("shift+tab")
                await pilot.pause()
                self.assertIs(app.focused, fields[0])

    async def test_letter_bindings_work_while_a_value_field_is_focused(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            stack.enter_context(patch.object(TunnerApp, "apply_commands", return_value=ApplyPlan()))
            async with app.run_test(size=(80, 24)) as pilot:
                await settle(app, pilot)
                field = app.query_one(ValueRow).query_one("#value", Input)
                field.focus()
                await pilot.pause()
                self.assertFalse(app.query_one("#status-rail").display)

                await pilot.press("t")
                await pilot.pause()
                self.assertTrue(app.query_one("#status-rail").display)
                self.assertFalse(app.query_one("#tuning-plan").display)
                self.assertEqual(field.value, "3000")

                await pilot.press("t")
                await pilot.pause()
                self.assertTrue(app.query_one("#tuning-plan").display)

                await pilot.press("a")
                await settle(app, pilot)
                self.assertEqual(field.value, "3000")
                self.assertIsInstance(app.screen, ApplyConfirmation)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ApplyConfirmation)

                # A stray letter must neither reach a destructive binding nor the field.
                restore = stack.enter_context(patch.object(TunnerApp, "restore_saved"))
                stress = stack.enter_context(patch.object(TunnerApp, "toggle_stress"))
                field.focus()
                await pilot.pause()
                for key in ("q", "r", "c", "g", "v"):
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertTrue(app.is_running, key)
                self.assertEqual(field.value, "3000")
                restore.assert_not_called()
                stress.assert_not_called()

    async def test_status_strip_appears_when_the_rail_is_hidden(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            async with app.run_test(size=(140, 30)) as pilot:
                await settle(app, pilot)
                app.telemetry = Telemetry(cpu_temperature=61.0, gpu_temperature=55.0, profile="balanced")
                app.refresh_status_rail()
                self.assertTrue(app.query_one("#status-rail").display)
                self.assertFalse(app.query_one("#status-strip").display)

                app.action_toggle_rail()
                await pilot.pause()
                self.assertFalse(app.query_one("#status-rail").display)
                strip = app.query_one("#status-strip", Static)
                self.assertTrue(strip.display)
                self.assertIn("CPU 61°C · GPU 55°C", str(strip.render()))

                # The strip reports the hardware, not the plan: a selected but
                # unapplied profile must not appear beside live readings.
                app.query_one("#profile", Select).value = "custom"
                await pilot.pause()
                self.assertIn("profile balanced", str(strip.render()))
                self.assertNotIn("custom", str(strip.render()))

    async def test_edited_marker_and_revert(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                name = row.query_one(".setting-name", Label)
                self.assertNotIn("●", str(name.render()))

                row.action_step(1)
                await pilot.pause()
                self.assertEqual(setting.value, 3100)
                self.assertIn("●", str(name.render()))
                self.assertTrue(name.has_class("modified"))
                limits = str(app.query_one("#limit-readout", Static).render())
                self.assertIn("was", limits)

                app.action_revert()
                await pilot.pause()
                self.assertEqual(setting.value, 3000)
                self.assertNotIn("●", str(name.render()))
                self.assertIn("Reverted 1 value", activity_text(app))

    async def test_disabled_rows_say_which_mode_enables_them(self):
        lenovo = TuningValue("Lenovo CPU sustained limit", 70, 30, 140, "W")
        with ExitStack() as stack:
            app = headless_app(stack, settings=[lenovo])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                self.assertTrue(row.disabled)
                self.assertIn("Needs Custom profile", str(row.query_one("#range", Static).render()))

                app.query_one("#profile", Select).value = "custom"
                await pilot.pause()
                self.assertFalse(row.disabled)
                self.assertIn("30–140 W", str(row.query_one("#range", Static).render()))

                # An edit made while enabled is still an edit once the gate closes.
                row.action_step(1)
                app.query_one("#profile", Select).value = "balanced"
                await pilot.pause()
                self.assertTrue(row.disabled)
                self.assertTrue(lenovo.modified)
                app.action_revert()
                await pilot.pause()
                self.assertEqual(lenovo.value, 70)
                self.assertIn("Reverted 1 value", activity_text(app))

    async def test_legion_toggle_shows_live_state_and_marks_a_planned_change(self):
        toggles = [
            legion_toggle("fan-unlock", live=False),
            legion_toggle("touchpad", unavailable="Feature unavailable"),
        ]
        toggles[1].detail = "Command not available because feature is not available or kernel module is not loaded."
        with ExitStack() as stack:
            app = headless_app(stack, legion=toggles)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                fan_unlock, touchpad = app.query(ToggleRow).results()
                self.assertIn("live: off", str(fan_unlock.query_one("#range", Static).render()))
                self.assertNotIn("●", str(fan_unlock.query_one(".setting-name", Label).render()))
                self.assertFalse(fan_unlock.query_one(Select).disabled)
                self.assertTrue(touchpad.query_one(Select).disabled)
                self.assertIn("Feature unavailable", str(touchpad.query_one("#range", Static).render()))
                self.assertIn("kernel module", touchpad.query_one("#range", Static).tooltip)
                self.assertIn("no toggles planned", str(app.query_one("#limit-readout", Static).render()))

                fan_unlock.query_one(Select).value = "on"
                await pilot.pause()
                self.assertEqual(app.options["legion-fan-unlock"], "on")
                self.assertIn("●", str(fan_unlock.query_one(".setting-name", Label).render()))
                self.assertIn("Fan unlock on", str(app.query_one("#limit-readout", Static).render()))
                self.assertIn("Changed just now", activity_text(app))

                # Choosing the state the hardware already has is not an edit.
                fan_unlock.query_one(Select).value = "off"
                await pilot.pause()
                self.assertNotIn("●", str(fan_unlock.query_one(".setting-name", Label).render()))
                self.assertIn("Fan unlock off", str(app.query_one("#limit-readout", Static).render()))

    async def test_a_failed_status_read_leaves_the_row_editable(self):
        toggle = legion_toggle("fnlock", live=None)
        toggle.read_failure, toggle.detail = "Status read failed", "Error: [Errno 13] Permission denied"
        missing = legion_toggle("touchpad", live=None, unavailable="Feature unavailable")
        with ExitStack() as stack:
            app = headless_app(stack, legion=[toggle, missing])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row, missing_row = app.query(ToggleRow)
                self.assertEqual(str(row.query_one("#range", Static).render()), "Status read failed")
                self.assertIn("Permission denied", row.query_one("#range", Static).tooltip)
                self.assertFalse(row.query_one(Select).disabled)
                # The unknown state is flagged, not drawn in the disabled
                # colour a missing feature gets: this row can still be written.
                self.assertTrue(row.query_one("#range", Static).has_class("unknown"))
                self.assertFalse(row.query_one("#range", Static).has_class("hint"))
                self.assertTrue(missing_row.query_one("#range", Static).has_class("hint"))
                self.assertFalse(missing_row.query_one("#range", Static).has_class("unknown"))
                self.assertTrue(missing_row.query_one(Select).disabled)

                row.query_one(Select).value = "on"
                await pilot.pause()
                self.assertIn("●", str(row.query_one(".setting-name", Label).render()))
                self.assertIn("Fn lock on", str(app.query_one("#limit-readout", Static).render()))

    async def test_legion_section_is_absent_without_toggles(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertEqual(len(app.query(ToggleRow)), 0)
                self.assertNotIn("Legion", str(app.query_one("#limit-readout", Static).render()))

    async def test_restore_brings_back_legion_choices_and_apply_rereads_them(self):
        toggle = legion_toggle("fan-unlock", live=False)
        saved = {"options": {"legion-fan-unlock": "on", "legion-touchpad": "off"}}
        with ExitStack() as stack:
            app = headless_app(stack, legion=[toggle], saved=saved)
            run_command = stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.restore_saved()
                await pilot.pause()
                row = app.query_one(ToggleRow)
                self.assertEqual(row.query_one(Select).value, "on")
                self.assertEqual(app.options["legion-fan-unlock"], "on")
                self.assertTrue(toggle.modified("on"))

                app.action_apply()
                await settle(app, pilot)
                self.assertIsInstance(app.screen, ApplyConfirmation)
                self.assertEqual(app.screen.plan.toggles, {"legion-fan-unlock": True})
                self.assertIn(f"{LEGION_CLI} fan-unlock-enable", str(app.screen.query_one("#confirm-commands Static").render()))

                # After the write legion_cli is asked again rather than trusted.
                reread = legion_toggle("fan-unlock", live=True)
                stack.enter_context(patch("app.probe_legion_toggles", return_value=(LEGION_CLI, [reread])))
                self.assertTrue(await pilot.click("#confirm"))
                await settle(app, pilot)
                self.assertIn("Applied successfully", activity_text(app))
                self.assertEqual(toggle.live, True)
                self.assertNotIn("●", str(row.query_one(".setting-name", Label).render()))
                self.assertIn("live: on", str(row.query_one("#range", Static).render()))
            # The choice is saved with the other options; the stray touchpad one was ignored.
            options = json.loads(app_module.LAST_VALUES.read_text())["options"]
        self.assertEqual(options["legion-fan-unlock"], "on")
        self.assertEqual(options["legion-touchpad"], "keep")
        writes = [call.args[0] for call in run_command.call_args_list if call.kwargs.get("access") == "write"]
        self.assertIn(["sudo", "-n", LEGION_CLI, "fan-unlock-enable"], writes)

    async def test_an_apply_that_ran_nothing_does_not_reread_legion_states(self):
        # Nothing ran, so nothing changed: ten legion_cli reads are spared.
        toggle = legion_toggle("fan-unlock", live=True)
        command = ["sudo", "-n", "tee", "/dev/null"]
        with ExitStack() as stack:
            app = headless_app(stack, legion=[toggle])
            stack.enter_context(patch("app.run_command", side_effect=subprocess.CalledProcessError(1, command)))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                probe = stack.enter_context(patch("app.probe_legion_toggles"))
                app.apply_confirmed(ApplyPlan([(command, None)]))
                await settle(app, pilot)
                self.assertIn("Apply failed", activity_text(app))
                probe.assert_not_called()
                self.assertEqual(toggle.live, True)

    async def test_a_write_the_firmware_did_not_keep_is_reported_after_the_read_back(self):
        # The read-back is checked against what the Apply asked for: a
        # toggle that still reads the old state is called out in the log and
        # a toast, except hybrid mode, which only changes after a reboot.
        fan_unlock, hybrid = legion_toggle("fan-unlock", live=False), legion_toggle("hybrid-mode", live=False)
        commands = [
            (["sudo", "-n", LEGION_CLI, "fan-unlock-enable"], None),
            (["sudo", "-n", LEGION_CLI, "hybrid-mode-enable"], None),
        ]
        with ExitStack() as stack:
            app = headless_app(stack, legion=[fan_unlock, hybrid])
            stack.enter_context(patch("app.run_command"))
            notify = stack.enter_context(patch.object(app_module.App, "notify"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                reread = [legion_toggle("fan-unlock", live=False), legion_toggle("hybrid-mode", live=False)]
                stack.enter_context(patch("app.probe_legion_toggles", return_value=(LEGION_CLI, reread)))
                app.apply_confirmed(ApplyPlan(
                    commands, toggles={"legion-fan-unlock": True, "legion-hybrid-mode": True},
                ))
                await settle(app, pilot)
                self.assertIn("Applied successfully", activity_text(app))
                log = log_text(app)
                self.assertIn("READBACK Fan unlock: asked on, reads off; the write did not take", log)
                self.assertIn("READBACK Hybrid mode: asked on, reads off; it takes effect after a reboot", log)
        toasts = [call.args[0] for call in notify.call_args_list]
        self.assertIn("Fan unlock did not take; see the apply log", toasts)
        self.assertFalse(any("Hybrid mode" in toast for toast in toasts))

    async def test_every_apply_rereads_legion_states_without_touching_typed_text(self):
        # A profile switch can reset fan state, so an Apply that ran no
        # legion_cli command still asks for the states again; the read-back
        # lands later and must leave a half-typed value alone.
        toggle = legion_toggle("maximumfanspeed", live=True)
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting], legion=[toggle])
            stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ToggleRow)
                self.assertIn("live: on", str(row.query_one("#range", Static).render()))
                self.assertEqual(app.query_one("#profile", Select).value, "balanced")

                reread = legion_toggle("maximumfanspeed", live=False)
                probe = stack.enter_context(patch("app.probe_legion_toggles", return_value=(LEGION_CLI, [reread])))
                app.action_apply()
                await settle(app, pilot)
                self.assertEqual(app.screen.plan.toggles, {})
                self.assertTrue(await pilot.click("#confirm"))
                await settle(app, pilot)
                self.assertIn("Applied successfully", activity_text(app))
                probe.assert_called_once()
                self.assertEqual(toggle.live, False)
                self.assertIn("live: off", str(row.query_one("#range", Static).render()))

                field = app.query_one(ValueRow).query_one("#value", Input)
                field.focus()
                await pilot.pause()
                field.value = ""
                await pilot.press("3")
                await pilot.pause()
                self.assertEqual((field.value, setting.value), ("3", None))
                app.apply_legion_states(LEGION_CLI, [legion_toggle("maximumfanspeed", live=True)])
                await pilot.pause()
                self.assertEqual(field.value, "3")
                self.assertIn("live: on", str(row.query_one("#range", Static).render()))

    async def test_finishing_an_apply_leaves_a_half_typed_value_alone(self):
        # The commands run in the background, so their end lands whenever
        # they finish; promoting the plan's values to live must mark the
        # rows without rewriting a field that holds a value still being typed.
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                field = row.query_one("#value", Input)
                field.focus()
                await pilot.pause()
                field.value = ""
                await pilot.press("3")
                await pilot.pause()
                self.assertEqual((field.value, setting.value), ("3", None))

                app.finish_apply(None, ApplyPlan(values={setting.key: 3000}))
                await pilot.pause()
                self.assertEqual((field.value, setting.value, setting.live), ("3", None, 3000))
                self.assertIn("●", str(row.query_one(".setting-name", Label).render()))
                self.assertIn("Applied successfully", activity_text(app))

    async def test_startup_selects_do_not_refresh_every_row_again(self):
        # Every Select posts Changed for its initial value at mount; those
        # are not edits, so the rows and rail are refreshed once, by the probe.
        toggles = [legion_toggle(feature, live=False) for _, feature, _ in LEGION_FEATURES]
        with ExitStack() as stack:
            app = headless_app(stack, settings=[clock_row(3000)], legion=toggles)
            rail = stack.enter_context(patch.object(
                TunnerApp, "refresh_status_rail", autospec=True, side_effect=TunnerApp.refresh_status_rail,
            ))
            rows = stack.enter_context(patch.object(
                TunnerApp, "update_row_disabled_state", autospec=True, side_effect=TunnerApp.update_row_disabled_state,
            ))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertEqual(len(app.query(ToggleRow)), len(LEGION_FEATURES))
                self.assertEqual((rows.call_count, rail.call_count), (1, 1))

                app.query_one(ToggleRow).query_one(Select).value = "on"
                await pilot.pause()
                self.assertEqual((rows.call_count, rail.call_count), (2, 2))

    async def test_startup_neither_marks_a_change_nor_overwrites_the_saved_file(self):
        # Input posts Changed for its initial text at mount; that is not an
        # edit, so Restore must still find the previous session's preview.
        setting = clock_row(3000)
        saved = {"values": {"cpu-p-core-maximum-frequency": 4500}, "options": {"intel-mode": "undervolt"}}
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting], saved=saved)
            stack.enter_context(patch("app.SAVE_DELAY", 0.05))
            stack.enter_context(patch("app.intel_controls.live_control_state", return_value=({}, {})))
            path = app_module.LAST_VALUES
            async with app.run_test() as pilot:
                await settle(app, pilot)
                await asyncio.sleep(0.3)
                await pilot.pause()
                self.assertIsNone(app.save_timer)
                self.assertIsNone(app.last_change)
                self.assertIn("No preview change", activity_text(app))
                self.assertEqual(json.loads(path.read_text())["values"], {"cpu-p-core-maximum-frequency": 4500})

                # A save pending from a fresh edit must not pre-empt Restore either.
                stack.enter_context(patch("app.SAVE_DELAY", 30))
                app.query_one(ValueRow).action_step(1)
                await pilot.pause()
                self.assertIsNotNone(app.save_timer)
                app.restore_saved()
                await pilot.pause()
                self.assertEqual(setting.value, 4500)
                self.assertEqual(app.options["intel-mode"], "undervolt")
                self.assertIsNone(app.save_timer)

    async def test_a_live_reading_outside_the_range_survives_mount(self):
        setting = TuningValue("NVIDIA core clock minimum", 210, 405, 1410, "MHz", choices=(405, 1200, 1410))
        setting.live = 210
        with ExitStack() as stack:
            app = headless_app(stack, nvidia=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                self.assertEqual(setting.value, 210)
                self.assertEqual(row.query_one("#value", Input).value, "210")
                self.assertNotIn("●", str(row.query_one(".setting-name", Label).render()))
                self.assertIsNone(app.last_change)

    async def test_a_failed_save_is_reported_and_the_app_keeps_running(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            stack.enter_context(patch("app.write_last_values", side_effect=PermissionError("read-only")))
            stack.enter_context(patch("app.SAVE_DELAY", 0.05))
            notify = stack.enter_context(patch.object(TunnerApp, "notify"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.query_one(ValueRow).action_step(1)
                await asyncio.sleep(0.3)
                await pilot.pause()
                self.assertTrue(app.is_running)
                self.assertIn("Could not save last-values.json", activity_text(app))

                app.apply_confirmed(ApplyPlan([(["sudo", "-n", "true"], None)], {setting.key: 3100}))
                await settle(app, pilot)
                self.assertTrue(app.is_running)
                self.assertEqual(setting.live, 3100)
                self.assertIn("Could not save last-values.json", activity_text(app))
        messages = [call.args[0] for call in notify.call_args_list]
        self.assertIn("Applied to hardware", messages)
        self.assertTrue(any("Could not save" in message for message in messages))

    async def test_a_partial_apply_promotes_only_what_ran(self):
        first, second = clock_row(3000), TuningValue("NVIDIA power ceiling", 150, 5, 175, "W")
        ok, bad = ["sudo", "-n", "true"], ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        failure = subprocess.CalledProcessError(1, bad, stderr="GPU is lost\n")

        def run(arguments, **kwargs):
            if arguments == bad:
                raise failure
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

        with ExitStack() as stack:
            app = headless_app(stack, settings=[first, second])
            stack.enter_context(patch("app.run_command", side_effect=run))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                # Both rows edited away from their readings (the probe made
                # the starting values live).
                first.value, second.live = 3100, 175
                for row in app.query(ValueRow):
                    row.refresh_value()
                plan = ApplyPlan(
                    [(ok, None), (bad, None)],
                    {first.key: 3100, second.key: 150},
                    carried=[{first.key: 3100}, {second.key: 150}],
                )
                app.apply_confirmed(plan)
                await settle(app, pilot)
                self.assertIn("Apply failed", activity_text(app))
                self.assertEqual(first.live, 3100)
                self.assertFalse(first.modified)
                self.assertEqual(second.live, 175)
                self.assertTrue(second.modified)
                names = [str(row.query_one(".setting-name", Label).render()) for row in app.query(ValueRow)]
                self.assertNotIn("●", names[0])
                self.assertIn("●", names[1])

    async def test_command_output_is_shown_verbatim_not_as_markup(self):
        command = ["sudo", "-n", "python", "intel_controls.py"]
        failure = subprocess.CalledProcessError(1, command, stderr="Intel apply failed ([E] Model not supported)\n")
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch("app.run_command", side_effect=failure))
            notify = stack.enter_context(patch.object(app_module.App, "notify"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.apply_confirmed(ApplyPlan([(command, None)]))
                await settle(app, pilot)
                self.assertIn("[E] Model not supported", activity_text(app))
        # The base method is mocked, so the override's super() call reaches it unbound.
        failure_toast = next(call for call in notify.call_args_list if "Apply failed" in call.args[0])
        self.assertIn("[E] Model not supported", failure_toast.args[0])
        self.assertFalse(failure_toast.kwargs["markup"])

    async def test_unavailable_memory_clock_stays_disabled_in_locked_mode(self):
        setting = TuningValue(
            "NVIDIA memory clock maximum",
            None,
            0,
            0,
            "MHz",
            unavailable_reason="NVIDIA clock query failed",
        )
        with ExitStack() as stack:
            app = headless_app(stack, nvidia=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.query_one("#memory-mode", Select).value = "locked"
                await pilot.pause()

                row = app.query_one(ValueRow)
                self.assertTrue(row.disabled)
                self.assertTrue(row.query_one("#value", Input).disabled)

    async def test_live_intel_mode_populates_values_without_config(self):
        with ExitStack() as stack:
            app = headless_app(stack, intel=unavailable_intel_rows())
            stack.enter_context(patch(
                "app.intel_controls.live_control_state",
                return_value=(intel_controls.LIVE_DEFAULTS, {}),
            ))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.query_one('#intel-mode', Select).value = 'undervolt'
                await pilot.pause()

                self.assertEqual(
                    {setting.key: setting.value for setting in app.settings},
                    intel_controls.LIVE_DEFAULTS,
                )
                self.assertFalse(any(setting.modified for setting in app.settings))

    async def test_failed_stress_resets_button_and_reports_error(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                process = Mock()
                process.poll.return_value = 1
                reader, writer = os.pipe()
                os.write(writer, b"unable to load libcublas.so.12\n")
                os.close(writer)
                process.stderr = os.fdopen(reader, "r")
                app.stress_processes["gpu"] = process
                app.query_one("#stress-gpu", Button).label = "Stop GPU stress"

                with patch.object(app, "notify") as notify:
                    self.assertTrue(app.refresh_stress_processes())

                self.assertNotIn("gpu", app.stress_processes)
                self.assertTrue(process.stderr.closed)
                self.assertEqual(str(app.query_one("#stress-gpu", Button).label), "Stress GPU")
                self.assertIn("libcublas.so.12", activity_text(app))
                notify.assert_called_once_with(
                    "GPU stress failed: unable to load libcublas.so.12",
                    severity="error",
                    timeout=10,
                )
                # The failure must survive the periodic activity refresh.
                app.refresh_activity()
                self.assertIn("libcublas.so.12", activity_text(app))
                self.assertTrue(app.query_one("#activity").has_class("error"))

    async def test_restore_fills_intel_rows_in_undervolt_mode(self):
        saved = {
            "options": {"intel-mode": "undervolt"},
            "values": {
                "intel-pl1-sustained-power": 60,
                "intel-pl2-burst-power": 90,
                "intel-thermal-offset": -15,
            },
        }
        with ExitStack() as stack:
            app = headless_app(stack, intel=unavailable_intel_rows(), saved=saved)
            stack.enter_context(patch(
                "app.intel_controls.live_control_state",
                return_value=(intel_controls.LIVE_DEFAULTS, {"intel-pl2-time-window": "RAPL reads 9 ms; shown clamped to 1"}),
            ))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.restore_saved()
                await pilot.pause()
                await pilot.pause()

                rows = {row.setting.key: row for row in app.query(ValueRow)}
                values = {key: row.setting.value for key, row in rows.items()}
                self.assertEqual(app.query_one("#intel-mode", Select).value, "undervolt")
                # Saved values win; live readings fill what was not saved.
                self.assertEqual(values["intel-pl1-sustained-power"], 60)
                self.assertEqual(values["intel-pl2-burst-power"], 90)
                self.assertEqual(values["intel-thermal-offset"], -15)
                self.assertEqual(values["intel-pl1-time-window"], intel_controls.LIVE_DEFAULTS["intel-pl1-time-window"])
                for key, row in rows.items():
                    self.assertFalse(row.disabled, key)
                    self.assertIsNone(row.setting.unavailable_reason, key)
                    self.assertFalse(row.query_one("#up", Button).disabled, key)
                    self.assertFalse(row.query_one("#down", Button).disabled, key)
                self.assertIn(
                    "RAPL reads 9 ms",
                    str(rows["intel-pl2-time-window"].query_one("#range", Static).render()),
                )
                self.assertIn("Restored 3 saved", activity_text(app))

    async def test_restore_keeps_saved_memory_clock_in_unavailable_row(self):
        setting = TuningValue(
            "NVIDIA memory clock maximum", None, 0, 0, "MHz",
            unavailable_reason="NVIDIA clock query failed",
        )
        saved = {"options": {"memory-mode": "locked"}, "values": {"nvidia-memory-clock-maximum": 6123}}
        with ExitStack() as stack:
            app = headless_app(stack, nvidia=[setting], saved=saved)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.restore_saved()
                await pilot.pause()
                await pilot.pause()

                row = app.query_one(ValueRow)
                self.assertEqual(setting.value, 6123)
                self.assertEqual(row.query_one("#value", Input).value, "6123")
                self.assertFalse(row.disabled)
                self.assertFalse(row.query_one("#value", Input).disabled)
                self.assertEqual(app.options["memory-mode"], "locked")

    async def test_edits_are_saved_after_a_quiet_moment_and_on_exit(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting], saved={"values": {}})
            stack.enter_context(patch("app.SAVE_DELAY", 30))  # only the exit flush may write
            path = app_module.LAST_VALUES  # the temporary file headless_app patched in
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.query_one(ValueRow).action_step(1)
                await pilot.pause()
                self.assertIsNotNone(app.save_timer)
                self.assertEqual(json.loads(path.read_text())["values"], {})
            saved = json.loads(path.read_text())["values"]

        self.assertEqual(saved["cpu-p-core-maximum-frequency"], 3100)

    async def test_apply_log_shows_stderr_of_failed_command(self):
        command = ["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", "150"]
        failure = subprocess.CalledProcessError(1, command, stderr="sudo: a password is required\n")
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch("app.run_command", side_effect=failure))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.apply_confirmed(ApplyPlan([(command, None)]))
                await settle(app, pilot)
                output = log_text(app)
                activity = activity_text(app)

        self.assertIn("FAILED", output)
        self.assertIn("sudo: a password is required", output)
        self.assertIn("sudo: a password is required", activity)

    async def test_apply_log_shows_helper_output_and_skips_tee_echo(self):
        commands = [
            (["sudo", "-n", "tee", "/sys/firmware/acpi/platform_profile"], "custom\n"),
            (["sudo", "-n", "python", "intel_controls.py"], "{}"),
        ]
        outputs = ["custom\n", "Backup: /etc/intel-undervolt.conf.tunner-1.bak\nIntel settings applied\n"]
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch(
                "app.run_command",
                side_effect=[subprocess.CompletedProcess(c[0], 0, stdout=o, stderr="") for c, o in zip(commands, outputs)],
            ))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.apply_confirmed(ApplyPlan(commands))
                await settle(app, pilot)
                lines = [line.text for line in app.query_one("#apply-log", RichLog).lines]

        self.assertEqual(sum("custom" in line and "APPLIED" not in line for line in lines), 0)
        self.assertTrue(any("Backup: /etc/intel-undervolt.conf.tunner-1.bak" in line for line in lines))
        self.assertTrue(any("Intel settings applied" in line for line in lines))

    async def test_successful_apply_marks_rows_as_live(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                row.action_step(1)
                await pilot.pause()
                self.assertTrue(setting.modified)

                app.apply_confirmed(ApplyPlan([(["sudo", "-n", "true"], None)], {setting.key: 3100}))
                await settle(app, pilot)
                self.assertFalse(setting.modified)
                self.assertEqual(setting.live, 3100)
                self.assertNotIn("●", str(row.query_one(".setting-name", Label).render()))

    async def test_apply_promotes_only_the_values_it_wrote(self):
        # An edit typed while the commands run was never sent, so it must
        # stay marked as edited rather than be recorded as the live reading.
        setting = clock_row(3000)
        gate = threading.Event()

        def slow_run(arguments, **kwargs):
            gate.wait(10)
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            stack.enter_context(patch("app.run_command", side_effect=slow_run))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                row.action_step(1)
                await pilot.pause()
                app.apply_confirmed(ApplyPlan([(["sudo", "-n", "true"], None)], {setting.key: 3100}))
                await pilot.pause()
                row.action_step(1)
                await pilot.pause()
                self.assertEqual(setting.value, 3200)

                gate.set()
                await settle(app, pilot)
                self.assertEqual(setting.live, 3100)
                self.assertTrue(setting.modified)
                self.assertIn("●", str(row.query_one(".setting-name", Label).render()))

    async def test_restored_power_ceiling_is_not_written_or_marked_live_when_unavailable(self):
        setting = TuningValue(
            "NVIDIA power ceiling", None, 5, 175, "W",
            unavailable_reason="Live NVIDIA power limit unavailable",
        )
        saved = {"values": {"nvidia-power-ceiling": 150}}
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting], saved=saved, power_available=False)
            run_command = stack.enter_context(patch("app.run_command"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.restore_saved()
                await pilot.pause()
                row = app.query_one(ValueRow)
                self.assertEqual(setting.value, 150)
                self.assertTrue(row.disabled)
                self.assertIn("power range unavailable", str(row.query_one("#range", Static).render()))

                app.action_apply()
                await settle(app, pilot)
                self.assertIsInstance(app.screen, ApplyConfirmation)
                self.assertNotIn("nvidia-power-ceiling", app.screen.plan.values)
                self.assertTrue(await pilot.click("#confirm"))
                await settle(app, pilot)

                self.assertIn("Applied successfully", activity_text(app))
                self.assertIsNone(setting.live)
                self.assertTrue(setting.modified)
                self.assertIn("●", str(row.query_one(".setting-name", Label).render()))
        self.assertFalse(any("nvidia-smi" in call.args[0] for call in run_command.call_args_list))

    async def test_unexpected_apply_error_is_logged_not_fatal(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch.object(TunnerApp, "apply_commands", side_effect=KeyError("nvidia-power-ceiling")))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.action_apply()
                await pilot.pause()
                output = log_text(app)
                self.assertIn("KeyError: 'nvidia-power-ceiling'", output)
                self.assertIn("Apply failed", activity_text(app))
                self.assertTrue(app.is_running)

    async def test_activity_message_survives_refresh_until_something_newer(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.show_activity("● CPU stress started; press again to stop")
                for _ in range(3):
                    app.refresh_activity()
                self.assertIn("CPU stress started", activity_text(app))

                app.last_change = datetime.now()
                app.refresh_activity()
                self.assertIn("Changed just now", activity_text(app))

                app.last_change = app.last_apply = datetime.now()
                app.refresh_activity()
                self.assertIn("Applied successfully", activity_text(app))

    async def test_value_row_buttons_do_not_reach_the_app_handler(self):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            async with app.run_test() as pilot:
                await settle(app, pilot)
                row = app.query_one(ValueRow)
                row.scroll_visible(immediate=True)
                await pilot.pause()
                with patch.object(TunnerApp, "on_button_pressed") as handler:
                    self.assertTrue(await pilot.click(row.query_one("#up", Button)))
                    await pilot.pause()
                self.assertEqual(setting.value, 3100)
                handler.assert_not_called()

    async def test_successful_apply_remains_visible_in_activity(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            async with app.run_test() as pilot:
                await settle(app, pilot)
                app.last_change = app.last_apply = datetime.now() - timedelta(seconds=6)
                app.refresh_activity()

                self.assertIn("Applied successfully", activity_text(app))

    async def test_probe_failure_is_reported_without_blaming_sudo(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch("app.probe_system", side_effect=OSError("no sysfs")))
            notify = stack.enter_context(patch.object(TunnerApp, "notify"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertTrue(app.plan_ready)
                self.assertIn("Hardware probe failed: no sysfs", activity_text(app))
                # sudo was never checked, so the notice must not say it failed.
                notice = app.query_one("#notice", Static)
                self.assertFalse(notice.has_class("warning"))
                self.assertNotIn("sudo -v", str(notice.render()))
        notify.assert_called_once()
        self.assertIn("no sysfs", notify.call_args.args[0])

    async def test_plan_build_failure_replaces_the_spinner(self):
        with ExitStack() as stack:
            app = headless_app(stack)
            stack.enter_context(patch.object(TunnerApp, "finish_probe", side_effect=RuntimeError("bad widget")))
            notify = stack.enter_context(patch.object(TunnerApp, "notify"))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertFalse(app.query("#loading"))
                self.assertFalse(app.plan_ready)
                self.assertIn("bad widget", str(app.query_one("#probe-failure", Static).render()))
                self.assertTrue(app.is_running)
        notify.assert_called_once()
        self.assertIn("bad widget", notify.call_args.args[0])

    async def test_a_slow_telemetry_poll_still_lands_and_ticks_do_not_pile_up(self):
        gate = threading.Event()
        started = threading.Event()
        polls = []

        def read(groups):
            polls.append(groups)
            if len(polls) == 2:
                started.set()
                gate.wait(10)  # the poll that outlives the next tick
                return Telemetry(cpu_temperature=50.0)
            return Telemetry(cpu_temperature=40.0)

        with ExitStack() as stack:
            app = headless_app(stack, poll=True)
            stack.enter_context(patch("app.read_telemetry", side_effect=read))
            async with app.run_test() as pilot:
                await settle(app, pilot)
                self.assertEqual(app.telemetry.cpu_temperature, 40.0)

                app.refresh_telemetry()
                await asyncio.to_thread(started.wait, 10)
                # Ticks while the slow poll runs are skipped, not stacked or cancelled.
                app.refresh_telemetry()
                app.refresh_telemetry()
                await pilot.pause()
                self.assertEqual(len(polls), 2)

                gate.set()
                await settle(app, pilot)
                self.assertEqual(app.telemetry.cpu_temperature, 50.0)
                self.assertIn(50.0, app.history["cpu_temperature"])
                self.assertIn("Rated boost unavailable", app.telemetry.cpu_boost)

    async def test_controls_remain_visible_after_resize(self):
        await self.check_resize_layout(confirmation_open=False)

    async def test_controls_remain_visible_after_resize_with_confirmation(self):
        await self.check_resize_layout(confirmation_open=True)

    async def check_resize_layout(self, *, confirmation_open):
        setting = clock_row(3000)
        with ExitStack() as stack:
            app = headless_app(stack, settings=[setting])
            async with app.run_test(size=(80, 24)) as pilot:
                await settle(app, pilot)
                for width in (80, 60, 119, 120, 140, 60, 140, 80):
                    if confirmation_open:
                        app.push_screen(ApplyConfirmation(ApplyPlan()), app.apply_confirmed)
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
                    self.assertEqual(app.query_one("#status-strip").display, width < 120)
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


class StressProcessTest(unittest.TestCase):
    def test_stop_stress_reaps_the_process_group_and_closes_stderr(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; sys.stderr.write('ready\\n'); sys.stderr.flush(); time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            TunnerApp.stop_stress(process)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stderr.closed)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class TelemetryFormatTest(unittest.TestCase):
    @patch("app.run_command", side_effect=OSError)
    @patch("app.nvidia_output", return_value="61, 120.50, 45, 1500, 8000, 175.00, 2280, 9001, 12, 0x0000000000000004\n")
    def test_gpu_utilization_is_a_whole_percentage(self, _output, _run):
        telemetry = read_telemetry({})

        self.assertEqual(telemetry.gpu_utilization, 45)
        self.assertIsInstance(telemetry.gpu_utilization, int)
        self.assertEqual(telemetry.gpu_power, 120.5)
        self.assertEqual(telemetry.gpu_reasons, "Power cap")

    def test_cpu_temperature_comes_from_the_package_sensor(self):
        report = {
            "nvme-pci-0100": {"Composite": {"temp1_input": 35.0}},
            "coretemp-isa-0000": {"Adapter": "ISA adapter", "Package id 0": {"temp1_input": 61.0, "temp1_max": 100.0}},
        }
        self.assertEqual(cpu_temperature_from_sensors(report), 61.0)
        self.assertIsNone(cpu_temperature_from_sensors({"chip": {"Composite": {"temp1_input": 35.0}}}))
        self.assertIsNone(cpu_temperature_from_sensors(["not", "a", "report"]))

    def test_cpu_temperature_ignores_fans_and_prefers_the_package_sensor(self):
        # A motherboard chip can list a "CPU Fan" and a generic "CPUTIN"
        # before coretemp; neither the RPM nor the board sensor should win.
        report = {
            "nct6775-isa-0290": {
                "CPU Fan": {"fan1_input": 2400.0},
                "CPUTIN": {"temp2_input": 48.0},
            },
            "coretemp-isa-0000": {"Package id 0": {"temp1_input": 61.0}},
        }
        self.assertEqual(cpu_temperature_from_sensors(report), 61.0)
        self.assertIsNone(cpu_temperature_from_sensors({"chip": {"CPU Fan": {"fan1_input": 2400.0}}}))
        self.assertEqual(cpu_temperature_from_sensors({"chip": {"CPUTIN": {"temp2_input": 48.0}}}), 48.0)
        self.assertEqual(
            cpu_temperature_from_sensors({"k10temp-pci-00c3": {"Tctl": {"temp1_input": 57.5}}}), 57.5
        )

    def test_intel_rows_keep_upper_case_limit_names(self):
        with patch("intel_controls.Path.read_text", side_effect=OSError):
            names = [setting.name for setting in intel_values()]

        self.assertEqual(
            names,
            [
                "Intel PL1 sustained power",
                "Intel PL1 time window",
                "Intel PL2 burst power",
                "Intel PL2 time window",
                "Intel thermal offset",
            ],
        )

    def test_intel_config_beyond_the_spec_is_clamped_with_a_note(self):
        # Firmware's "unlimited" 4095.875 W rounds to 4096, one past the MSR
        # bound. The note quotes the file's own text and unit (seconds for a
        # window), so the number can be found in the config.
        config = "power package 4095.875/2 45/200\ntjoffset -10\n"
        with patch("intel_controls.Path.read_text", return_value=config):
            settings = {setting.key: setting for setting in intel_values()}

        burst = settings["intel-pl2-burst-power"]
        self.assertEqual(burst.value, 4095)
        self.assertEqual(burst.note, "Config reads 4095.875 W; shown clamped to 4095 W")
        self.assertIsNone(burst.unavailable_reason)
        window = settings["intel-pl1-time-window"]
        self.assertEqual(window.value, 128000)
        self.assertEqual(window.note, "Config reads 200 s; shown clamped to 128000 ms")
        self.assertIsNone(settings["intel-pl1-sustained-power"].note)

    def test_intel_config_with_a_non_finite_token_is_unavailable_not_fatal(self):
        # float() accepts "inf"; one such token must mark the Intel rows
        # unavailable like any other malformed config, not abort the probe
        # and blank the whole plan.
        config = "power package inf/2 45/28\ntjoffset -10\n"
        with patch("intel_controls.Path.read_text", return_value=config):
            settings = intel_values()

        self.assertEqual(len(settings), len(intel_controls.SPECS))
        self.assertTrue(all(setting.value is None for setting in settings))
        self.assertTrue(all(setting.unavailable_reason for setting in settings))


if __name__ == "__main__":
    unittest.main()
