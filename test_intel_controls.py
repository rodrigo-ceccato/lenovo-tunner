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


if __name__ == '__main__':
    unittest.main()
