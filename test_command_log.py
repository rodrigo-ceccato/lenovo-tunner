"""Command rendering and persistent audit-log coverage."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import command_log


class CommandLogTest(unittest.TestCase):
    def test_records_read_and_write_commands(self):
        with TemporaryDirectory() as directory:
            log_file = Path(directory) / "log.txt"
            with patch("command_log.LOG_FILE", log_file):
                command_log.record_command(["nvidia-smi", "-q"], "read")
                command_log.record_command(
                    ["sudo", "-n", "tee", "/sys/example"], "write", "150\n"
                )
            lines = log_file.read_text().splitlines()

        self.assertIn("[READ] nvidia-smi -q", lines[0])
        self.assertIn("[WRITE] printf %b", lines[1])
        self.assertEqual(len(lines), 2)
        self.assertIn("sudo -n tee /sys/example", lines[1])


if __name__ == "__main__":
    unittest.main()
