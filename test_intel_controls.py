"""Regression coverage for validated Intel limit commands."""

import unittest
from unittest.mock import patch

import intel_controls


class PythonUndervoltTest(unittest.TestCase):
    @patch('intel_controls.Path.read_text')
    def test_live_values_fall_back_without_persistent_config(self, read_text):
        read_text.side_effect = OSError

        values = intel_controls.live_control_values()

        self.assertEqual(values, intel_controls.LIVE_DEFAULTS)
        intel_controls.validate_values(values)

    def test_builds_live_limit_command_with_converted_units(self):
        values = {
            'intel-pl1-sustained-power': 45,
            'intel-pl1-time-window': 28000,
            'intel-pl2-burst-power': 55,
            'intel-pl2-time-window': 1500,
            'intel-thermal-offset': -10,
        }

        self.assertEqual(
            intel_controls.python_undervolt_command(values, '/project/python'),
            [
                'sudo', '-n', '/project/python', '-m', 'undervolt',
                '-p1', '45', '28',
                '-p2', '55', '1.5',
                '--temp', '90',
            ],
        )

    def test_rejects_pl1_above_pl2(self):
        values = {
            'intel-pl1-sustained-power': 56,
            'intel-pl1-time-window': 28000,
            'intel-pl2-burst-power': 55,
            'intel-pl2-time-window': 1500,
            'intel-thermal-offset': -10,
        }

        with self.assertRaisesRegex(ValueError, 'PL1 must not exceed PL2'):
            intel_controls.python_undervolt_command(values)

    def test_missing_value_is_reported_by_name(self):
        values = dict(intel_controls.LIVE_DEFAULTS)
        del values['intel-pl2-time-window']

        with self.assertRaisesRegex(ValueError, 'intel-pl2-time-window must be an integer'):
            intel_controls.validate_values(values)


class LiveControlStateTest(unittest.TestCase):
    def rapl(self, **readings):
        """Patch sysfs reads so each RAPL file returns the given raw value."""
        files = {
            'constraint_0_power_limit_uw': readings.get('pl1_uw'),
            'constraint_0_time_window_us': readings.get('pl1_us'),
            'constraint_1_power_limit_uw': readings.get('pl2_uw'),
            'constraint_1_time_window_us': readings.get('pl2_us'),
        }

        def read_text(path, *args, **kwargs):
            value = files.get(path.name)
            if value is None:
                raise OSError('unreadable')
            return f'{value}\n'

        return patch('intel_controls.Path.read_text', autospec=True, side_effect=read_text)

    def test_high_firmware_limits_are_live_values_not_defaults(self):
        # Lenovo Custom Mode allows PL2 up to 190 W; the old 157 W bound
        # silently replaced such readings with the 45/55 W defaults.
        with self.rapl(pl1_uw=140_000_000, pl1_us=28_000_000, pl2_uw=190_000_000, pl2_us=2_000_000):
            values, notes = intel_controls.live_control_state()

        self.assertEqual(values['intel-pl1-sustained-power'], 140)
        self.assertEqual(values['intel-pl2-burst-power'], 190)
        self.assertEqual(notes, {})

    def test_out_of_range_reading_is_clamped_with_a_note(self):
        # Firmware stores "unlimited" as 4095.875 W, which rounds past the bound.
        with self.rapl(pl1_uw=4_095_875_000, pl1_us=28_000_000, pl2_uw=55_000_000, pl2_us=200_000_000_000):
            values, notes = intel_controls.live_control_state({'intel-pl2-time-window': 2000})

        self.assertEqual(values['intel-pl1-sustained-power'], 4095)
        self.assertEqual(values['intel-pl2-time-window'], 128000)
        self.assertEqual(notes['intel-pl1-sustained-power'], 'RAPL reads 4096 W; shown clamped to 4095')
        self.assertEqual(notes['intel-pl2-time-window'], 'RAPL reads 200000000 ms; shown clamped to 128000')
        self.assertNotIn('intel-pl2-burst-power', notes)
        for key, value in values.items():
            self.assertTrue(intel_controls.in_spec(key, value), key)

    def test_inconsistent_live_pair_is_kept_for_the_user_to_fix(self):
        with self.rapl(pl1_uw=90_000_000, pl1_us=28_000_000, pl2_uw=60_000_000, pl2_us=2_000_000):
            values, notes = intel_controls.live_control_state()

        self.assertEqual((values['intel-pl1-sustained-power'], values['intel-pl2-burst-power']), (90, 60))
        self.assertEqual(notes, {})
        with self.assertRaisesRegex(ValueError, 'PL1 must not exceed PL2'):
            intel_controls.validate_values(values)

    def test_unreadable_rapl_uses_saved_preview_then_default(self):
        saved = {'intel-pl1-sustained-power': 70, 'intel-thermal-offset': -15, 'intel-pl2-time-window': 'bad'}
        with self.rapl():
            values, notes = intel_controls.live_control_state(saved)

        self.assertEqual(values['intel-pl1-sustained-power'], 70)
        self.assertEqual(values['intel-thermal-offset'], -15)
        self.assertEqual(values['intel-pl2-time-window'], intel_controls.LIVE_DEFAULTS['intel-pl2-time-window'])
        self.assertEqual(notes, {})


class ConfigRewriteTest(unittest.TestCase):
    CONFIG = (
        '# intel-undervolt configuration\n'
        'undervolt 0 \'CPU\' -100\n'
        '\n'
        '\n'
        'power package 55/2 45/28   # short then long\n'
        '\n'
        '\t tjoffset -10\n'
        '\n'
    )

    def test_config_readings_keep_the_file_text_and_units(self):
        self.assertEqual(
            intel_controls.config_readings(self.CONFIG),
            {
                'intel-pl2-burst-power': ('55', 'W'),
                'intel-pl2-time-window': ('2', 's'),
                'intel-pl1-sustained-power': ('45', 'W'),
                'intel-pl1-time-window': ('28', 's'),
                'intel-thermal-offset': ('-10', '°C'),
            },
        )

    def test_a_non_finite_token_is_a_value_error_like_any_other_bad_token(self):
        # float() accepts "inf" and "1e999", and round() of either raises
        # OverflowError, which no config reader expects; a startup probe
        # that let it through would blank the whole plan.
        for token in ("inf", "1e999", "1e308"):
            with self.assertRaisesRegex(ValueError, "not a finite number"):
                intel_controls.parse_config(f"power package 55/{token} 45/28\ntjoffset -10\n")

    def test_round_trip_keeps_blank_lines_and_other_directives(self):
        values = intel_controls.parse_config(self.CONFIG)
        self.assertEqual(
            values,
            {
                'intel-pl2-burst-power': 55,
                'intel-pl2-time-window': 2000,
                'intel-pl1-sustained-power': 45,
                'intel-pl1-time-window': 28000,
                'intel-thermal-offset': -10,
            },
        )

        rewritten = intel_controls.updated_config(self.CONFIG, {**values, 'intel-pl2-burst-power': 90, 'intel-thermal-offset': -5})

        self.assertEqual(
            rewritten,
            '# intel-undervolt configuration\n'
            'undervolt 0 \'CPU\' -100\n'
            '\n'
            '\n'
            'power package 90/2 45/28\n'
            '\n'
            'tjoffset -5\n'
            '\n',
        )
        self.assertEqual(intel_controls.parse_config(rewritten)['intel-pl2-burst-power'], 90)


class PrivilegedHelperTest(unittest.TestCase):
    def test_failed_apply_restores_config_and_reports_stderr(self):
        import subprocess
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        values = dict(intel_controls.LIVE_DEFAULTS)
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'intel-undervolt.conf'
            config.write_text(ConfigRewriteTest.CONFIG)
            failure = subprocess.CalledProcessError(1, ['intel-undervolt', 'apply'], stderr='msr: permission denied\n')
            with (
                patch('intel_controls.CONFIG', config),
                patch('intel_controls.subprocess.run', side_effect=failure) as run,
                patch('intel_controls.sys.stdin', StringIO(__import__('json').dumps(values))),
                __import__('contextlib').redirect_stdout(StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                intel_controls.main()

            self.assertIn('msr: permission denied', str(raised.exception))
            self.assertIn('config restored from', str(raised.exception))
            self.assertEqual(config.read_text(), ConfigRewriteTest.CONFIG)
            self.assertEqual(len(list(Path(directory).glob('*.tunner-*.bak'))), 1)
            run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
