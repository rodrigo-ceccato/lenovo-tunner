"""Command rendering and persistent audit-log coverage."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import command_log


class CommandLogTest(unittest.TestCase):
    def record_sample(self, log_reads):
        with TemporaryDirectory() as directory:
            log_file = Path(directory) / "log.txt"
            with patch("command_log.LOG_FILE", log_file), patch("command_log.LOG_READS", log_reads):
                command_log.record_command(["nvidia-smi", "-q"], "read")
                command_log.record_command(
                    ["sudo", "-n", "tee", "/sys/example"], "write", "150\n"
                )
                command_log.record_command(["sudo", "-n", "true"], "exec")
            return log_file.read_text().splitlines()

    def test_records_read_and_write_commands_when_reads_are_enabled(self):
        lines = self.record_sample(log_reads=True)

        self.assertIn("[READ] nvidia-smi -q", lines[0])
        self.assertIn("[WRITE] printf %b", lines[1])
        self.assertIn("sudo -n tee /sys/example", lines[1])
        self.assertIn("[EXEC] sudo -n true", lines[2])
        self.assertEqual(len(lines), 3)

    def test_skips_reads_by_default_but_keeps_writes_and_launches(self):
        lines = self.record_sample(log_reads=False)

        self.assertEqual(len(lines), 2)
        self.assertIn("[WRITE]", lines[0])
        self.assertIn("[EXEC]", lines[1])

    def test_returns_rendered_command_even_when_not_logged(self):
        with patch("command_log.LOG_READS", False):
            rendered = command_log.record_command(["nvidia-smi", "-q"], "read")

        self.assertEqual(rendered, "nvidia-smi -q")


if __name__ == "__main__":
    unittest.main()
