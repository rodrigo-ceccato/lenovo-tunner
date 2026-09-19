"""A display-only interface for the adjustable values in lenovo-tuning.md.

At launch, it reads CPU policy files and NVIDIA's read-only clock queries to
seed the preview controls. It never writes to hardware or runs a tuning command.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, Static


DOCUMENT = Path(__file__).with_name("lenovo-tuning.md")
LAST_VALUES = Path(__file__).with_name("last-values.json")
RANGE_PATTERN = re.compile(
    r"allowed range:\s*(?P<minimum>\d+)\s*[–-]\s*(?P<maximum>\d+)\s*(?P<unit>[^. `|]+)",
    re.IGNORECASE,
)


@dataclass
class TuningValue:
    """One scalar setting documented as having a writable range."""

    name: str
    value: int | None
    minimum: int
    maximum: int
    unit: str
    step: int = 1
    choices: tuple[int, ...] = ()
    unavailable_reason: str | None = None

    def change(self, amount: int) -> None:
        if self.value is None:
            return
        if self.choices:
            index = min(
                range(len(self.choices)),
                key=lambda candidate: abs(self.choices[candidate] - self.value),
            )
            self.value = self.choices[
                max(0, min(len(self.choices) - 1, index + (1 if amount > 0 else -1)))
            ]
            return
        self.value = max(
            self.minimum, min(self.maximum, self.value + (self.step if amount > 0 else -self.step))
        )

    def can_change(self, amount: int) -> bool:
        if self.value is None:
            return False
        if self.choices:
            return self.value != (self.choices[-1] if amount > 0 else self.choices[0])
        return self.value != (self.maximum if amount > 0 else self.minimum)

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


def current_value(description: str, command: str) -> int | None:
    """Find the display value documented in a table row, without executing it."""
    ceiling = re.search(r"Current ceiling:\s*(\d+)", description, re.IGNORECASE)
    if ceiling:
        return int(ceiling.group(1))

    printf_value = re.search(r"printf\s+.*?'\s+(\d+)\s+", command)
    if printf_value:
        return int(printf_value.group(1))

    nvidia_limit = re.search(r"\s-pl\s+(\d+)", command)
    if nvidia_limit:
        return int(nvidia_limit.group(1))
    return None


def load_values(document: Path = DOCUMENT) -> list[TuningValue]:
    """Extract all scalar 'allowed range' rows from the Markdown reference."""
    values: list[TuningValue] = []
    for line in document.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line[1:-1].split(" | ", 2)]
        if len(cells) != 3:
            continue
        name, description, command = cells
        match = RANGE_PATTERN.search(description)
        value = current_value(description, command)
        if match and value is not None:
            values.append(
                TuningValue(
                    name=name,
                    value=value,
                    minimum=int(match["minimum"]),
                    maximum=int(match["maximum"]),
                    unit=match["unit"],
                )
            )
    return values


def policy_values(filename: str) -> list[int]:
    """Read a CPU policy attribute from every policy available to this system."""
    return [
        int(path.read_text(encoding="utf-8").strip()) // 1_000
        for path in Path("/sys/devices/system/cpu/cpufreq").glob(f"policy*/{filename}")
    ]


def cpu_clock_values() -> list[TuningValue]:
    """Return the common safe CPU range and its live policy-limit starting values."""
    try:
        current_minimums = policy_values("scaling_min_freq")
        current_maximums = policy_values("scaling_max_freq")
        if not all((current_minimums, current_maximums)):
            raise OSError("CPU frequency policies are unavailable")
        # 3.9 GHz is the i9-13900HX E-core maximum, so it is the conservative
        # upper bound for a limit intended to cover every CPU policy. The live
        # cpuinfo maximum can be lower while Turbo is disabled, hence it is not
        # used as the preview range.
        minimum, maximum = 800, 3_900
        return [
            TuningValue(
                "CPU minimum frequency",
                max(current_minimums),
                minimum,
                maximum,
                "MHz",
                step=100,
            ),
            TuningValue(
                "CPU maximum frequency",
                min(current_maximums),
                minimum,
                maximum,
                "MHz",
                step=100,
            ),
        ]
    except (OSError, ValueError):
        # i9-13900HX's E-core maximum is 3.9 GHz; this common range is safe for
        # every policy. These are only offline preview defaults.
        return [
            TuningValue("CPU minimum frequency", 800, 800, 3_900, "MHz", step=100),
            TuningValue("CPU maximum frequency", 3_000, 800, 3_900, "MHz", step=100),
        ]


def nvidia_output(arguments: list[str]) -> str:
    """Run a read-only NVIDIA query; no shell or setting-changing option is used."""
    return subprocess.run(
        ["nvidia-smi", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout


def supported_nvidia_clocks(report: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Extract unique memory and graphics MHz values from SUPPORTED_CLOCKS."""
    memory = tuple(sorted({int(value) for value in re.findall(r"Memory\s*:\s*(\d+)\s*MHz", report)}))
    graphics = tuple(
        sorted({int(value) for value in re.findall(r"Graphics\s*:\s*(\d+)\s*MHz", report)})
    )
    return graphics, memory


def nvidia_gpu_detected() -> bool:
    """Detect an NVIDIA GPU without treating clock-query failure as no hardware."""
    return any(Path("/proc/driver/nvidia/gpus").glob("*/information"))


def nvidia_clock_values() -> list[TuningValue]:
    """Build GPU lock previews from the driver-reported current/supported clocks."""
    try:
        current = nvidia_output(
            [
                "--query-gpu=clocks.current.graphics,clocks.current.memory",
                "--format=csv,noheader,nounits",
            ]
        ).splitlines()[0]
        graphics_current, memory_current = (int(value.strip()) for value in current.split(","))
        graphics, memory = supported_nvidia_clocks(nvidia_output(["-q", "-d", "SUPPORTED_CLOCKS"]))
        if not graphics or not memory:
            raise ValueError("No supported clocks reported")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        unavailable = (
            "NVIDIA clock query failed (GPU detected)"
            if nvidia_gpu_detected()
            else "NVIDIA GPU not detected"
        )
        return [
            TuningValue("NVIDIA core clock minimum", None, 0, 0, "MHz", unavailable_reason=unavailable),
            TuningValue("NVIDIA core clock maximum", None, 0, 0, "MHz", unavailable_reason=unavailable),
            TuningValue("NVIDIA memory clock minimum", None, 0, 0, "MHz", unavailable_reason=unavailable),
            TuningValue("NVIDIA memory clock maximum", None, 0, 0, "MHz", unavailable_reason=unavailable),
        ]

    def clock(name: str, current_value: int, supported: tuple[int, ...]) -> TuningValue:
        return TuningValue(
            name,
            current_value,
            supported[0],
            supported[-1],
            "MHz",
            choices=supported,
        )

    return [
        clock("NVIDIA core clock minimum", graphics_current, graphics),
        clock("NVIDIA core clock maximum", graphics_current, graphics),
        clock("NVIDIA memory clock minimum", memory_current, memory),
        clock("NVIDIA memory clock maximum", memory_current, memory),
    ]


def nvidia_power_limit_available() -> bool:
    """Return whether NVIDIA reports a numeric, writable-range power limit."""
    try:
        limits = nvidia_output(
            [
                "-i",
                "0",
                "--query-gpu=power.min_limit,power.max_limit",
                "--format=csv,noheader,nounits",
            ]
        ).splitlines()[0]
        minimum, maximum = (float(value.strip()) for value in limits.split(","))
        return minimum >= 0 and maximum >= minimum
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False


def read_telemetry() -> tuple[str, str]:
    """Read CPU and GPU telemetry without changing any hardware setting."""
    cpu = "CPU: unavailable"
    gpu = "GPU: unavailable"
    try:
        report = json.loads(
            subprocess.run(
                ["sensors", "-j"], check=True, capture_output=True, text=True, timeout=3
            ).stdout
        )
        for chip in report.values():
            if not isinstance(chip, dict):
                continue
            for label, readings in chip.items():
                if not isinstance(readings, dict) or not any(
                    term in label.lower() for term in ("package", "tctl", "cpu")
                ):
                    continue
                temperature = next(
                    (value for key, value in readings.items() if key.endswith("_input")),
                    None,
                )
                if isinstance(temperature, (int, float)):
                    cpu = f"CPU: {temperature:.0f}°C"
                    raise StopIteration
    except StopIteration:
        pass
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    try:
        output = nvidia_output(
            [
                "--query-gpu=temperature.gpu,power.draw,utilization.gpu",
                "--format=csv,noheader",
            ]
        ).splitlines()[0]
        temperature, power, utilization = (part.strip() for part in output.split(","))
        gpu = f"GPU: {temperature} · {power} · {utilization}"
    except (OSError, subprocess.SubprocessError, IndexError):
        if nvidia_gpu_detected():
            gpu = "GPU: clock telemetry read failed"
    return cpu, gpu


def load_last_values() -> dict[str, int]:
    """Load only integer values from a previously saved preview state."""
    try:
        payload = json.loads(LAST_VALUES.read_text(encoding="utf-8"))
        values = payload.get("values", {})
        return {key: value for key, value in values.items() if isinstance(value, int)}
    except (OSError, ValueError, AttributeError):
        return {}


def write_last_values(settings: list[TuningValue], applied: bool = False) -> None:
    """Persist preview values; this never applies a hardware setting."""
    payload: dict[str, object] = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "values": {setting.key: setting.value for setting in settings if setting.value is not None},
    }
    if applied:
        payload["applied_at"] = payload["updated_at"]
    else:
        try:
            old_payload = json.loads(LAST_VALUES.read_text(encoding="utf-8"))
            if isinstance(old_payload.get("applied_at"), str):
                payload["applied_at"] = old_payload["applied_at"]
        except (OSError, ValueError, AttributeError):
            pass
    LAST_VALUES.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ValueRow(Horizontal):
    """A compact display-only increment/decrement control."""

    class Changed(Message):
        def __init__(self, value_row: "ValueRow") -> None:
            self.value_row = value_row
            super().__init__()

    def __init__(self, setting: TuningValue) -> None:
        super().__init__(classes="value-row")
        self.setting = setting

    def compose(self) -> ComposeResult:
        yield Label(self.setting.name, classes="setting-name")
        yield Static("", id="range", classes="range")
        yield Button("−", id="down", classes="step")
        yield Static("", id="value", classes="value")
        yield Button("+", id="up", classes="step")

    def on_mount(self) -> None:
        self.refresh_value()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.setting.change(-1 if event.button.id == "down" else 1)
        self.refresh_value()
        self.post_message(self.Changed(self))

    def refresh_value(self) -> None:
        range_text = self.setting.unavailable_reason or (
            f"{self.setting.minimum}–{self.setting.maximum} {self.setting.unit}"
        )
        value_text = (
            "unavailable"
            if self.setting.value is None
            else f"{self.setting.value} {self.setting.unit}"
        )
        self.query_one("#range", Static).update(range_text)
        self.query_one("#value", Static).update(value_text)
        self.query_one("#down", Button).disabled = not self.setting.can_change(-1)
        self.query_one("#up", Button).disabled = not self.setting.can_change(1)


class ApplyConfirmation(ModalScreen[bool]):
    """Require a second, explicit action before modifying live hardware."""

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static("Apply these preview values to live hardware?", id="confirm-text")
            with Horizontal(classes="confirm-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Apply now", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class TunnerApp(App[None]):
    """Minimal tuner with explicit confirmation before any hardware write."""

    TITLE = "Tunner"
    CSS = """
    Screen { background: #101113; color: #e7e9ed; }
    Header { background: #101113; color: #e7e9ed; }
    Footer { background: #101113; color: #8e949f; }
    #content { width: 92%; max-width: 78; height: 1fr; margin: 1 2; }
    #notice { color: #8e949f; margin-bottom: 1; }
    #telemetry { color: #b8bec9; margin-bottom: 1; }
    #activity { color: #6ee7a8; margin-bottom: 1; }
    .value-row { height: 3; align: center middle; border-bottom: solid #292c32; }
    .setting-name { width: 1fr; }
    .range { width: 22; color: #8e949f; text-align: right; }
    .value { width: 13; text-align: center; color: #fbfbfc; }
    .step { min-width: 5; width: 5; background: #24272d; border: none; }
    .step:focus { background: #3d4655; }
    #actions { height: 3; margin-top: 1; }
    #actions Button { margin-right: 1; }
    ApplyConfirmation { align: center middle; background: #00000099; }
    #confirm-dialog { width: 52; height: auto; padding: 1 2; background: #1b1e24; border: solid #4b5563; }
    #confirm-text { margin-bottom: 1; }
    .confirm-actions { height: 3; }
    .confirm-actions Button { margin-right: 1; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_values() + cpu_clock_values() + nvidia_clock_values()
        self.nvidia_power_limit_available = nvidia_power_limit_available()
        self.last_change: datetime | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="content"):
            yield Static(
                "Preview values persist locally. Apply changes live hardware only after confirmation.",
                id="notice",
            )
            yield Static("", id="telemetry")
            yield Static("", id="activity")
            for setting in self.settings:
                yield ValueRow(setting)
            with Horizontal(id="actions"):
                yield Button("Restore saved", id="restore")
                yield Button("Apply", id="apply", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_telemetry()
        self.refresh_activity()
        self.set_interval(2, self.refresh_telemetry)
        self.set_interval(1, self.refresh_activity)

    def on_value_row_changed(self, event: ValueRow.Changed) -> None:
        self.last_change = datetime.now()
        write_last_values(self.settings)
        self.refresh_activity()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "restore":
            self.restore_saved()
        elif event.button.id == "apply":
            self.push_screen(ApplyConfirmation(), self.apply_confirmed)

    def refresh_telemetry(self) -> None:
        cpu, gpu = read_telemetry()
        self.query_one("#telemetry", Static).update(f"{cpu}    {gpu}")

    def refresh_activity(self) -> None:
        activity = self.query_one("#activity", Static)
        if self.last_change is None:
            activity.styles.color = "#8e949f"
            activity.update("○ No preview change in this session")
            return
        age = datetime.now() - self.last_change
        if age < timedelta(seconds=5):
            activity.styles.color = "#6ee7a8"
            activity.update("● Changed just now — saved to last-values.json")
        else:
            activity.styles.color = "#8e949f"
            activity.update(f"● Last preview change {int(age.total_seconds())}s ago")

    def restore_saved(self) -> None:
        saved = load_last_values()
        restored = 0
        for setting in self.settings:
            value = saved.get(setting.key)
            if value is None:
                continue
            if setting.value is None:
                # The stored value remains usable for an Apply operation even
                # when live NVIDIA telemetry is temporarily inaccessible.
                setting.value = value
                setting.minimum = value
                setting.maximum = value
                setting.unavailable_reason = "saved value; live clock query failed"
            else:
                setting.value = max(setting.minimum, min(setting.maximum, value))
            row = next(
                row for row in self.query(ValueRow) if row.setting.key == setting.key
            )
            row.refresh_value()
            restored += 1
        self.last_change = datetime.now()
        activity = self.query_one("#activity", Static)
        activity.styles.color = "#6ee7a8"
        activity.update(
            f"● Restored {restored} saved preview value(s); press Apply to set hardware"
        )

    def apply_confirmed(self, approved: bool | None) -> None:
        if not approved:
            return
        try:
            commands = self.apply_commands()
            for arguments, input_text in commands:
                subprocess.run(
                    arguments,
                    input=input_text,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self.query_one("#activity", Static).update(
                f"● Apply failed: {error}. Run 'sudo -v' in the terminal, then retry."
            )
            return
        write_last_values(self.settings, applied=True)
        self.last_change = datetime.now()
        activity = self.query_one("#activity", Static)
        activity.styles.color = "#6ee7a8"
        activity.update("● Applied just now — saved to last-values.json")
        self.refresh_telemetry()

    def apply_commands(self) -> list[tuple[list[str], str | None]]:
        """Build only the documented tuning commands from the preview values."""
        values = {setting.key: setting.value for setting in self.settings}

        def required(key: str) -> int:
            value = values[key]
            if value is None:
                raise ValueError(f"{key} is unavailable")
            return value

        cpu_minimum = required("cpu-minimum-frequency")
        cpu_maximum = required("cpu-maximum-frequency")
        if cpu_minimum > cpu_maximum:
            raise ValueError("CPU minimum frequency exceeds maximum frequency")

        commands: list[tuple[list[str], str | None]] = [
            (["sudo", "-n", "tee", "/sys/firmware/acpi/platform_profile"], "custom\n"),
            (
                ["sudo", "-n", "tee", "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_cpu_cl/current_value"],
                f"{required('lenovo-cpu-cross-load-limit')}\n",
            ),
            (
                ["sudo", "-n", "tee", "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_pl1_spl/current_value"],
                f"{required('lenovo-cpu-sustained-limit')}\n",
            ),
            (
                ["sudo", "-n", "tee", "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_pl2_sppt/current_value"],
                f"{required('lenovo-cpu-burst-limit')}\n",
            ),
            (
                ["sudo", "-n", "tee", "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/cpu_temp/current_value"],
                f"{required('lenovo-cpu-temperature-target')}\n",
            ),
            (["sudo", "-n", "cpupower", "frequency-set", "--min", f"{cpu_minimum}MHz"], None),
            (["sudo", "-n", "cpupower", "frequency-set", "--max", f"{cpu_maximum}MHz"], None),
        ]

        gpu_keys = (
            "nvidia-core-clock-minimum",
            "nvidia-core-clock-maximum",
            "nvidia-memory-clock-minimum",
            "nvidia-memory-clock-maximum",
        )
        # Power limiting and clock locking have independent NVIDIA support.
        # Only write a power limit after its own capability query succeeds.
        if self.nvidia_power_limit_available:
            commands.append(
                (
                    [
                        "sudo",
                        "-n",
                        "nvidia-smi",
                        "-i",
                        "0",
                        "-pl",
                        str(required("nvidia-power-ceiling")),
                    ],
                    None,
                )
            )

        if all(values[key] is not None for key in gpu_keys):
            core_minimum, core_maximum, memory_minimum, memory_maximum = (
                required(key) for key in gpu_keys
            )
            if core_minimum > core_maximum or memory_minimum > memory_maximum:
                raise ValueError("GPU clock minimum exceeds maximum")
            commands.extend(
                [
                    (
                        ["sudo", "-n", "nvidia-smi", "-i", "0", "-lgc", f"{core_minimum},{core_maximum}"],
                        None,
                    ),
                    (
                        ["sudo", "-n", "nvidia-smi", "-i", "0", "-lmc", f"{memory_minimum},{memory_maximum}"],
                        None,
                    ),
                ]
            )
        return commands


if __name__ == "__main__":
    TunnerApp().run()
