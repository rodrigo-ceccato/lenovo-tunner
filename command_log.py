"""Append-only logging for external commands launched by Tunner."""

from datetime import datetime
from pathlib import Path
import shlex


LOG_FILE = Path(__file__).with_name("log.txt")


def render_command(arguments: list[str], input_text: str | None = None) -> str:
    """Render an argv invocation, including piped input used for sysfs writes."""
    command = shlex.join(str(argument) for argument in arguments)
    if input_text is None:
        return command
    escaped_input = input_text.encode("unicode_escape").decode("ascii")
    return f"printf %b {shlex.quote(escaped_input)} | {command}"


def record_command(
    arguments: list[str], access: str, input_text: str | None = None
) -> str:
    """Persist an attempted command and return its display representation."""
    rendered = render_command(arguments, input_text)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(f"{timestamp} [{access.upper()}] {rendered}\n")
    return rendered
