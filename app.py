"""Preview hardware settings and apply them only after explicit confirmation."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json
import sys
import intel_controls
from command_log import record_command, render_command

from textual.app import App, ComposeResult
from textual.events import Resize
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.validation import Number
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static, Select


DOCUMENT = Path(__file__).with_name("lenovo-tuning.md")
LAST_VALUES = Path(__file__).with_name("last-values.json")
LENOVO_ATTRIBUTES = {
    "lenovo-cpu-cross-load-limit": "ppt_cpu_cl",
    "lenovo-cpu-sustained-limit": "ppt_pl1_spl",
    "lenovo-cpu-burst-limit": "ppt_pl2_sppt",
    "lenovo-cpu-temperature-target": "cpu_temp",
    "lenovo-gpu-temperature-target": "gpu_temp",
}
LENOVO_ATTRIBUTE_ROOT = Path("/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes")
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


@dataclass
class Telemetry:
    """The small, live snapshot shown alongside the editable plan."""

    cpu_temperature: float | None = None
    gpu_temperature: float | None = None
    gpu_power: float | None = None
    gpu_utilization: int | None = None
    gpu_clock: int | None = None
    gpu_memory_clock: int | None = None
    cpu_clock: int | None = None
    cpu_policy_minimum: int | None = None
    cpu_policy_maximum: int | None = None
    turbo_enabled: bool | None = None
    cpu_policies: str = "Policy readings unavailable"
    cpu_boost: str = "Rated boost unavailable"
    gpu_power_limit: float | None = None
    gpu_max_clock: float | None = None
    gpu_max_memory: float | None = None
    gpu_headroom: float | None = None
    gpu_reasons: str = "Unavailable"

    @staticmethod
    def bar(value: float | None, ceiling: float | None, width: int = 18) -> str:
        """A compact, dependable gauge that also works in a plain terminal."""
        if value is None or ceiling is None or ceiling <= 0:
            return "─" * width
        filled = max(0, min(width, round((value / ceiling) * width)))
        return "━" * filled + "─" * (width - filled)


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
    return probe_documented_values(values)


def policy_values(filename: str) -> list[int]:
    """Read a CPU policy attribute from every policy available to this system."""
    return [
        int(path.read_text(encoding="utf-8").strip()) // 1_000
        for path in Path("/sys/devices/system/cpu/cpufreq").glob(f"policy*/{filename}")
    ]


def turbo_state() -> bool | None:
    """Return the live Intel P-state turbo state, if this CPU exposes it."""
    try:
        # Intel exposes 0 for turbo allowed and 1 for turbo disabled.
        return Path("/sys/devices/system/cpu/intel_pstate/no_turbo").read_text(
            encoding="utf-8"
        ).strip() == "0"
    except OSError:
        return None




def cpu_groups() -> dict[str, list[Path]]:
    """Identify this machine's hybrid policies without assuming CPU numbering."""
    groups: dict[str, list[Path]] = {"p": [], "e": []}
    if "i9-13900HX" not in Path("/proc/cpuinfo").read_text():
        return groups
    for policy in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
        try:
            base = int((policy / "base_frequency").read_text())
            group = {2200000: "p", 1600000: "e"}.get(base)
            if group:
                groups[group].append(policy)
        except (OSError, ValueError):
            pass
    return groups


def intel_values():
    try:
        values = intel_controls.parse_config(intel_controls.CONFIG.read_text())
    except (OSError, ValueError):
        values = {}
    return [TuningValue(key.replace('-', ' ').replace('pl1', 'PL1').replace('pl2', 'PL2').capitalize(),
                        values.get(key), low, high, unit, step=step,
                        unavailable_reason='Intel config unavailable or unsupported' if key not in values else None)
            for key, (low, high, unit, step) in intel_controls.SPECS.items()]


def cpu_clock_values() -> list[TuningValue]:
    settings = []
    for group, policies in cpu_groups().items():
        for bound in ("minimum", "maximum"):
            try:
                readings = [int((p / f"scaling_{'min' if bound == 'minimum' else 'max'}_freq").read_text()) // 1000 for p in policies]
                current = (max(readings) if bound == "minimum" else min(readings)) if readings else None
            except (OSError, ValueError):
                current = None
            settings.append(TuningValue(f"CPU {group.upper()}-core {bound} frequency", current, 800,
                                        5400 if group == "p" else 3900, "MHz", step=100,
                                        unavailable_reason="Policy unavailable" if current is None else None))
    return settings


def nvidia_output(arguments: list[str]) -> str:
    """Run a read-only NVIDIA query; no shell or setting-changing option is used."""
    return run_command(
        ["nvidia-smi", *arguments], access="read",
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    ).stdout


def run_command(
    arguments: list[str], *, access: str, input_text: str | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Log and run one external command without invoking a shell."""
    record_command(arguments, access, input_text)
    return subprocess.run(arguments, input=input_text, **kwargs)


def probe_documented_values(settings: list[TuningValue]) -> list[TuningValue]:
    """Replace documented examples with current firmware and driver values."""
    by_key = {setting.key: setting for setting in settings}
    for key, attribute in LENOVO_ATTRIBUTES.items():
        setting = by_key.get(key)
        if setting is None:
            continue
        try:
            value = int((LENOVO_ATTRIBUTE_ROOT / attribute / "current_value").read_text().strip())
            if not setting.minimum <= value <= setting.maximum:
                raise ValueError("value outside documented range")
            setting.value = value
        except (OSError, ValueError):
            setting.value = None
            setting.unavailable_reason = "Live Lenovo value unavailable"

    power = by_key.get("nvidia-power-ceiling")
    if power is not None:
        try:
            report = nvidia_output(
                [
                    "-i",
                    "0",
                    "--query-gpu=enforced.power.limit,power.min_limit,power.max_limit",
                    "--format=csv,noheader,nounits",
                ]
            ).splitlines()[0]
            current, minimum, maximum = (round(float(value.strip())) for value in report.split(","))
            if minimum < 0 or maximum < minimum or not minimum <= current <= maximum:
                raise ValueError("invalid NVIDIA power limits")
            power.value = current
            power.minimum = minimum
            power.maximum = maximum
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            power.value = None
            power.unavailable_reason = "Live NVIDIA power limit unavailable"
    return settings


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

    def clock(
        name: str,
        current_value: int,
        supported: tuple[int, ...],
        choices: tuple[int, ...] | None = None,
    ) -> TuningValue:
        return TuningValue(
            name,
            current_value,
            supported[0],
            supported[-1],
            "MHz",
            choices=supported if choices is None else choices,
        )

    return [
        clock("NVIDIA core clock minimum", graphics_current, graphics),
        clock("NVIDIA core clock maximum", graphics_current, graphics),
        clock("NVIDIA memory clock minimum", memory_current, memory, ()),
        clock("NVIDIA memory clock maximum", memory_current, memory, ()),
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


def read_telemetry() -> Telemetry:
    """Read CPU and GPU telemetry without changing any hardware setting."""
    telemetry = Telemetry()
    try:
        report = json.loads(
            run_command(
                ["sensors", "-j"], access="read",
                check=True, capture_output=True, text=True, timeout=3
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
                    telemetry.cpu_temperature = float(temperature)
                    raise StopIteration
    except StopIteration:
        pass
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    try:
        output = nvidia_output(
            [
                "--query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.current.graphics,clocks.current.memory,enforced.power.limit,clocks.max.graphics,clocks.max.memory,temperature.gpu.tlimit,clocks_event_reasons.active",
                "--format=csv,noheader,nounits",
            ]
        ).splitlines()[0]
        parts = [part.strip() for part in output.split(",")]
        fields = ("gpu_temperature", "gpu_power", "gpu_utilization", "gpu_clock",
                  "gpu_memory_clock", "gpu_power_limit", "gpu_max_clock",
                  "gpu_max_memory", "gpu_headroom")
        for field, raw in zip(fields, parts):
            try:
                setattr(telemetry, field, float(raw))
            except ValueError:
                pass  # An unsupported field must not erase other readings.
        try:
            mask = int(parts[9], 16)
            reasons = ((1, "Idle"), (2, "Application clocks"), (4, "Power cap"),
                       (8, "Hardware slowdown"), (16, "Sync boost"),
                       (32, "Thermal slowdown"), (64, "HW thermal slowdown"),
                       (128, "Power brake"), (256, "Display clocks"))
            active = [label for bit, label in reasons if mask & bit]
            if mask & ~511:
                active.append("Other driver limit")
            telemetry.gpu_reasons = ", ".join(active) or "None active"
        except (ValueError, IndexError):
            pass
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass

    try:
        clocks = policy_values("scaling_cur_freq")
        if clocks:
            telemetry.cpu_clock = round(sum(clocks) / len(clocks))
        minimums = policy_values("scaling_min_freq")
        maximums = policy_values("scaling_max_freq")
        if minimums and maximums:
            telemetry.cpu_policy_minimum = max(minimums)
            telemetry.cpu_policy_maximum = min(maximums)
    except (OSError, ValueError):
        pass
    telemetry.turbo_enabled = turbo_state()
    groups: dict[tuple[int, int, int], list[int]] = {}
    for policy in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
        try:
            base, low, high, current = (
                int((policy / name).read_text().strip()) // 1000
                for name in ("base_frequency", "scaling_min_freq", "scaling_max_freq", "scaling_cur_freq")
            )
            groups.setdefault((base, low, high), []).append(current)
        except (OSError, ValueError):
            continue
    try:
        known_cpu = "i9-13900HX" in Path("/proc/cpuinfo").read_text()
    except OSError:
        known_cpu = False
    if groups:
        lines = []
        for (base, low, high), clocks in sorted(groups.items(), reverse=True):
            label = {2200: "P-cores", 1600: "E-cores"}.get(base, f"Base {base}") if known_cpu else f"Base {base}"
            lines.append(f"{label}: {low}–{high} MHz\n  Now {sum(clocks) / len(clocks):.0f} MHz avg")
        telemetry.cpu_policies = "\n".join(lines)
    if known_cpu:
        telemetry.cpu_boost = "Rated boost (not a live limit)\nP: up to 5400 · E: up to 3900 MHz"
    return telemetry


def load_last_values() -> dict[str, int]:
    """Load only integer values from a previously saved preview state."""
    try:
        payload = json.loads(LAST_VALUES.read_text(encoding="utf-8"))
        values = payload.get("values", {})
        return {key: value for key, value in values.items() if type(value) is int}
    except (OSError, ValueError, AttributeError):
        return {}


def write_last_values(settings: list[TuningValue], applied: bool = False, options=None) -> None:
    """Persist preview values; this never applies a hardware setting."""
    payload: dict[str, object] = {
        "options": options or {},
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
        if self.setting.key.startswith("nvidia-memory-clock"):
            yield Input(
                "" if self.setting.value is None else str(self.setting.value),
                id="value",
                classes="value",
                disabled=self.setting.value is None and self.setting.unavailable_reason is not None,
                restrict=r"\d*",
                validators=Number(self.setting.minimum, self.setting.maximum),
                validate_on=["changed"],
                compact=True,
            )
        else:
            yield Static("", id="value", classes="value")
        yield Button("+", id="up", classes="step")

    def on_mount(self) -> None:
        self.refresh_value()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.setting.change(-1 if event.button.id == "down" else 1)
        self.refresh_value()
        self.post_message(self.Changed(self))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.validation_result is None or not event.validation_result.is_valid:
            if self.setting.value is not None:
                self.setting.value = None
                self.refresh_buttons()
                self.post_message(self.Changed(self))
            return
        self.setting.value = int(event.value)
        self.refresh_buttons()
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
        value = self.query_one("#value")
        if isinstance(value, Input):
            expected = "" if self.setting.value is None else str(self.setting.value)
            if value.value != expected:
                value.value = expected
        else:
            value.update(value_text)
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        self.query_one("#down", Button).disabled = not self.setting.can_change(-1)
        self.query_one("#up", Button).disabled = not self.setting.can_change(1)


class StatusRail(VerticalScroll):
    """Always-visible live context for decisions made in the tuning list."""

    def compose(self) -> ComposeResult:
        yield Static("LIVE STATUS", classes="panel-title")
        yield Static("Refreshing every 2 seconds", classes="panel-subtitle")
        yield Static("CPU CLOCKS · LIVE POLICY", classes="section-title")
        yield Static("", id="clock-readout", classes="readout")
        yield Static("INTEL PACKAGE · LIVE", classes="section-title")
        yield Static("", id="intel-readout", classes="readout")
        yield Static("", id="cpu-thermal", classes="metric")
        yield Static("", id="gpu-thermal", classes="metric")
        yield Static("", id="gpu-power", classes="metric")
        yield Static("GPU CLOCKS · LIVE", classes="section-title")
        yield Static("", id="gpu-readout", classes="readout")
        yield Static("PLANNED LIMITS", classes="section-title")
        yield Static("", id="limit-readout", classes="readout")
        yield Static("Preview changes are saved locally. Apply sends the plan to hardware.", classes="panel-note")

    def refresh_status(self, settings: list[TuningValue], telemetry: Telemetry) -> None:
        self.query_one('#intel-readout', Static).update(intel_controls.live_limits())
        values = {setting.key: setting.value for setting in settings}

        def value(key: str):
            result = values.get(key)
            return "—" if result is None else result

        cpu_temp = telemetry.cpu_temperature
        gpu_temp = telemetry.gpu_temperature
        intel_offset = values.get('intel-thermal-offset')
        intel_target = f"{100 + intel_offset}°C (offset {intel_offset})" if type(intel_offset) is int else "unverified"
        self.query_one("#cpu-thermal", Static).update(
            f"CPU THERMAL  {cpu_temp:.0f}°C\nIntel thermal target (preview): {intel_target}"
            if cpu_temp is not None
            else "CPU THERMAL\nNo sensor reading"
        )
        headroom = "Unavailable" if telemetry.gpu_headroom is None else f"{telemetry.gpu_headroom:+.0f}°C"
        self.query_one("#gpu-thermal", Static).update(
            f"GPU THERMAL  {gpu_temp:.0f}°C\nDriver thermal margin: {headroom}\n{telemetry.gpu_reasons}"
            if gpu_temp is not None
            else "GPU THERMAL\nNo sensor reading"
        )
        gpu_power_limit = telemetry.gpu_power_limit
        self.query_one("#gpu-power", Static).update(
            f"GPU POWER {telemetry.gpu_power:.0f} / {gpu_power_limit:.0f} W live\n"
            f"{Telemetry.bar(telemetry.gpu_power, gpu_power_limit)}\n"
            f"Utilization: {telemetry.gpu_utilization if telemetry.gpu_utilization is not None else '—'}%"
            if telemetry.gpu_power is not None and gpu_power_limit is not None
            else "GPU POWER\nNo live power reading"
        )
        gpu_clock = "—" if telemetry.gpu_clock is None else f"{telemetry.gpu_clock:.0f} MHz"
        gpu_memory = "—" if telemetry.gpu_memory_clock is None else f"{telemetry.gpu_memory_clock:.0f} MHz"
        turbo = (
            "Allowed" if telemetry.turbo_enabled is True else
            "Disabled" if telemetry.turbo_enabled is False else
            "unavailable"
        )
        self.query_one("#clock-readout", Static).update(
            f"Turbo: {turbo}\n{telemetry.cpu_policies}\n{telemetry.cpu_boost}"
        )
        def mhz(number):
            return "—" if number is None else f"{number:.0f}"
        self.query_one("#gpu-readout", Static).update(
            f"Core now: {gpu_clock}\nVRAM now: {gpu_memory}\n"
            f"Driver max core: {mhz(telemetry.gpu_max_clock)} MHz\n"
            f"Driver max VRAM: {mhz(telemetry.gpu_max_memory)} MHz\n"
            "Active clock locks: unverified"
        )
        self.query_one("#limit-readout", Static).update(
            f"P: {value('cpu-p-core-minimum-frequency')}–{value('cpu-p-core-maximum-frequency')} MHz\n"
            f"E: {value('cpu-e-core-minimum-frequency')}–{value('cpu-e-core-maximum-frequency')} MHz\n"
            f"Core lock  {value('nvidia-core-clock-minimum')}–{value('nvidia-core-clock-maximum')} MHz\n"
            f"VRAM lock  {value('nvidia-memory-clock-minimum')}–{value('nvidia-memory-clock-maximum')} MHz\n"
            f"GPU  {value('nvidia-power-ceiling')} W ceiling\n"
            f"Lenovo PL1/PL2: {value('lenovo-cpu-sustained-limit')}/{value('lenovo-cpu-burst-limit')} W\n"
            f"Intel PL1/PL2: {value('intel-pl1-sustained-power')}/{value('intel-pl2-burst-power')} W\n"
            f"Intel thermal target: {intel_target} (preview, python undervolt --temp)\n"
            f"Lenovo CPU/GPU targets: {value('lenovo-cpu-temperature-target')}/{value('lenovo-gpu-temperature-target')}°C\n"
            "Firmware targets require Custom"
        )


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
    #workspace { width: 96%; max-width: 138; height: 1fr; margin: 1 2; }
    #tuning-plan { width: 1fr; padding-right: 2; }
    #plan-title { color: #fbfbfc; text-style: bold; margin-bottom: 1; }
    .group-title { color: #86b7ff; text-style: bold; margin: 1 0 0 0; }
    #notice { color: #8e949f; margin-bottom: 1; }
    #activity { color: #6ee7a8; margin-bottom: 1; }
    .value-row { height: 3; align: center middle; border-bottom: solid #292c32; }
    .setting-name { width: 1fr; }
    .range { width: 20; color: #8e949f; text-align: right; }
    .value { width: 13; text-align: center; color: #fbfbfc; }
    .step { min-width: 5; width: 5; background: #24272d; border: none; }
    .step:focus { background: #3d4655; }
    #actions { height: 3; margin-top: 1; }
    #actions Button { margin-right: 1; }
    #apply-log-title { color: #86b7ff; text-style: bold; margin-top: 1; }
    #apply-log { height: 10; border: solid #303641; padding: 0 1; background: #181b20; }
    #status-rail { width: 38; min-width: 34; height: 1fr; padding: 1 2; background: #181b20; border: solid #303641; }
    Screen.narrow #status-rail { display: none; }
    Screen.narrow #tuning-plan { padding-right: 0; }
    Screen.compact .range { display: none; }
    Screen.compact .value-row { height: 4; }
    .panel-title { color: #fbfbfc; text-style: bold; }
    .panel-subtitle { color: #8e949f; margin-bottom: 1; }
    .metric { color: #d9e1ea; padding: 0; margin-top: 1; border-bottom: solid #303641; }
    .section-title { color: #86b7ff; text-style: bold; margin-top: 1; }
    .readout { color: #c0c7d1; margin-top: 1; }
    .panel-note { color: #8e949f; margin-top: 1; }
    ApplyConfirmation { align: center middle; background: #00000099; }
    #confirm-dialog { width: 52; height: auto; padding: 1 2; background: #1b1e24; border: solid #4b5563; }
    #confirm-text { margin-bottom: 1; }
    .confirm-actions { height: 3; }
    .confirm-actions Button { margin-right: 1; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_values() + cpu_clock_values() + nvidia_clock_values() + intel_values()
        self.nvidia_power_limit_available = nvidia_power_limit_available()
        self.stress_processes: dict[str, subprocess.Popen[str]] = {}
        self.last_change: datetime | None = None
        self.last_apply: datetime | None = None
        try:
            self.profiles = Path('/sys/firmware/acpi/platform_profile_choices').read_text().split()
            profile = Path('/sys/firmware/acpi/platform_profile').read_text().strip()
        except OSError:
            self.profiles, profile = [], "unavailable"
        self.options = {"profile": profile, "turbo": "on" if turbo_state() else "off",
                        "core-mode": "keep", "memory-mode": "keep", "intel-mode": "keep"}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="workspace"):
            with VerticalScroll(id="tuning-plan"):
                yield Static("TUNING PLAN", id="plan-title")
                yield Static(
                    "Adjust the intended limits below. Live status appears alongside on wider terminals.",
                    id="notice",
                )
                yield Static("", id="activity")
                yield Label("Intel package limits · persistent configuration")
                yield Select([('Keep current Intel settings', 'keep'), ('Apply persistently via intel-undervolt', 'apply'), ('Apply live via Python undervolt', 'undervolt')], value='keep', allow_blank=False, id='intel-mode')
                yield Static('Intel windows use milliseconds (1000 ms = 1 s), not exact boost timers. Thermal offset is relative to 100°C on this CPU. Persistent apply updates /etc/intel-undervolt.conf and reapplies its existing voltage offsets. Python undervolt writes only these live limits through MSRs and does not persist them. UI bounds are app limits, not hardware guarantees.')
                yield Label("Lenovo profile · firmware sliders apply only in Custom")
                yield Select([(p, p) for p in self.profiles] or [("Unavailable", "unavailable")], value=self.options['profile'], allow_blank=False, id="profile", disabled=not self.profiles)
                yield Label("CPU Turbo · ceilings above base require Turbo on")
                yield Select([("On", "on"), ("Off", "off")], value=self.options['turbo'], allow_blank=False, id="turbo", disabled=turbo_state() is None)
                for domain in ("core", "memory"):
                    yield Label(f"GPU {domain} clocks")
                    yield Select([("Keep current mode", "keep"), ("Automatic / reset locks", "auto"), ("Locked to preview range", "locked")], value="keep", allow_blank=False, id=f"{domain}-mode")
                    yield Button(f"Reset {domain} clocks on Apply", id=f"reset-{domain}")
                previous_group = ""
                for setting in self.settings:
                    group = (
                        "Intel package limits" if setting.key.startswith("intel-") else
                        "CPU policy" if setting.key.startswith("cpu-") else
                        "Lenovo Custom Mode" if setting.key.startswith("lenovo-") else
                        "NVIDIA GPU"
                    )
                    if group != previous_group:
                        yield Static(group.upper(), classes="group-title")
                        previous_group = group
                    yield ValueRow(setting)
                with Horizontal(id="actions"):
                    yield Button("Stress CPU", id="stress-cpu", variant="warning")
                    yield Button("Stress GPU", id="stress-gpu", variant="warning")
                    yield Button("Restore saved", id="restore")
                    yield Button("Apply", id="apply", variant="error")
                yield Static("APPLY COMMAND LOG", id="apply-log-title")
                yield RichLog(id="apply-log", wrap=True, markup=False, max_lines=200)
            yield StatusRail(id="status-rail")
        yield Footer()

    def on_mount(self) -> None:
        self.update_layout(self.size.width)
        self.update_row_disabled_state()
        self.refresh_telemetry()
        self.refresh_activity()
        self.set_interval(2, self.refresh_telemetry)
        self.set_interval(1, self.refresh_activity)

    def on_resize(self, event: Resize) -> None:
        self.update_layout(event.size.width)

    def update_layout(self, width: int) -> None:
        # Reserve room for setting names as well as the 43-column controls.
        # The tuning screen stays at the bottom when confirmation is open.
        tuning_screen = self.screen_stack[0]
        tuning_screen.set_class(width < 120, "narrow")
        tuning_screen.set_class(width < 80, "compact")

    def on_value_row_changed(self, event: ValueRow.Changed) -> None:
        self.last_change = datetime.now()
        write_last_values(self.settings, options=self.options)
        self.refresh_activity()
        self.refresh_telemetry()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("stress-cpu", "stress-gpu"):
            self.toggle_stress(event.button.id.removeprefix("stress-"))
            return
        if event.button.id in ("reset-core", "reset-memory"):
            domain = event.button.id.removeprefix("reset-")
            self.query_one(f"#{domain}-mode", Select).value = "auto"
        if event.button.id == "restore":
            self.restore_saved()
        elif event.button.id == "apply":
            self.push_screen(ApplyConfirmation(), self.apply_confirmed)

    def toggle_stress(self, target: str) -> None:
        """Launch or stop an isolated opt-in workload without blocking the TUI."""
        self.refresh_stress_processes()
        button = self.query_one(f"#stress-{target}", Button)
        process = self.stress_processes.get(target)
        if process is not None and process.poll() is None:
            self.stop_stress(process)
            self.stress_processes.pop(target)
            button.label = f"Stress {target.upper()}"
            self.query_one("#activity", Static).update(f"● {target.upper()} stress stopped")
            return

        arguments = [sys.executable, str(Path(__file__).with_name("stress.py")), target]
        record_command(arguments, "exec")
        self.stress_processes[target] = subprocess.Popen(
            arguments,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        button.label = f"Stop {target.upper()} stress"
        self.query_one("#activity", Static).update(f"● {target.upper()} stress started; click again to stop")

    def refresh_stress_processes(self) -> bool:
        """Reap finished workloads and report failures instead of leaving stale UI."""
        failure_reported = False
        for target, process in list(self.stress_processes.items()):
            returncode = process.poll()
            if returncode is None:
                continue

            self.stress_processes.pop(target)
            self.query_one(f"#stress-{target}", Button).label = f"Stress {target.upper()}"
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            if returncode != 0:
                detail = stderr.splitlines()[-1] if stderr else f"exit status {returncode}"
                message = f"{target.upper()} stress failed: {detail}"
                activity = self.query_one("#activity", Static)
                activity.styles.color = "#ff6b6b"
                activity.update(f"● {message}")
                self.notify(message, severity="error", timeout=10)
                failure_reported = True
        return failure_reported

    def on_unmount(self) -> None:
        """Never leave stress processes running after the tuner exits."""
        for process in self.stress_processes.values():
            if process.poll() is None:
                self.stop_stress(process)

    @staticmethod
    def stop_stress(process: subprocess.Popen[str]) -> None:
        """Stop a launcher and every worker it created in its own process group."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def update_row_disabled_state(self) -> None:
        for row in self.query(ValueRow):
            if row.setting.key.startswith('intel-'):
                row.disabled = self.options['intel-mode'] == 'keep'
            elif row.setting.key.startswith("lenovo-"):
                row.disabled = self.options['profile'] != 'custom'
            elif row.setting.key.startswith("nvidia-core-clock"):
                row.disabled = self.options['core-mode'] != 'locked'
            elif row.setting.key.startswith("nvidia-memory-clock"):
                row.disabled = (
                    self.options['memory-mode'] != 'locked'
                    or (row.setting.value is None and row.setting.unavailable_reason is not None)
                )

    def on_select_changed(self, event: Select.Changed) -> None:
        key = event.select.id
        if key in self.options and isinstance(event.value, str) and self.options[key] != event.value:
            self.options[key] = event.value
            if key == 'intel-mode' and event.value == 'undervolt':
                live_values = intel_controls.live_control_values()
                for row in self.query(ValueRow):
                    if row.setting.key in live_values and row.setting.value is None:
                        row.setting.value = live_values[row.setting.key]
                        row.setting.unavailable_reason = None
                        row.refresh_value()
            write_last_values(self.settings, options=self.options)
            self.last_change = datetime.now()
        self.update_row_disabled_state()

    def refresh_telemetry(self) -> None:
        self.query_one(StatusRail).refresh_status(self.settings, read_telemetry())

    def refresh_activity(self) -> None:
        if self.refresh_stress_processes():
            return
        activity = self.query_one("#activity", Static)
        if self.last_apply is not None and (
            self.last_change is None or self.last_apply >= self.last_change
        ):
            age = datetime.now() - self.last_apply
            activity.styles.color = "#6ee7a8"
            if age < timedelta(seconds=5):
                activity.update("● Applied successfully just now — saved to last-values.json")
            else:
                activity.update(
                    f"● Applied successfully {int(age.total_seconds())}s ago"
                )
            return
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
        try:
            options = json.loads(LAST_VALUES.read_text(encoding="utf-8")).get('options', {})
            for key, allowed in {'intel-mode': ['keep', 'apply', 'undervolt'], 'profile': self.profiles, 'turbo': ['on', 'off'], 'core-mode': ['keep', 'auto', 'locked'], 'memory-mode': ['keep', 'auto', 'locked']}.items():
                if options.get(key) in allowed:
                    self.query_one(f'#{key}', Select).value = options[key]
                    self.options[key] = options[key]
        except (OSError, ValueError, AttributeError):
            pass
        restored = 0
        for setting in self.settings:
            value = saved.get(setting.key)
            if value is None:
                continue
            if setting.value is None:
                if setting.key.startswith('intel-'):
                    continue
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
        self.update_row_disabled_state()
        self.last_change = datetime.now()
        activity = self.query_one("#activity", Static)
        activity.styles.color = "#6ee7a8"
        activity.update(
            f"● Restored {restored} saved preview value(s); press Apply to set hardware"
        )
        self.refresh_telemetry()

    def apply_confirmed(self, approved: bool | None) -> None:
        if not approved:
            return
        apply_log = self.query_one("#apply-log", RichLog)
        apply_log.clear()
        command_failure_logged = False
        try:
            commands = self.apply_commands()
            for arguments, input_text in commands:
                rendered = render_command(arguments, input_text)
                try:
                    run_command(
                        arguments,
                        access="write",
                        input_text=input_text,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    apply_log.write(f"FAILED   {rendered}: {error}")
                    command_failure_logged = True
                    raise
                apply_log.write(f"APPLIED  {rendered}")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            if not command_failure_logged:
                apply_log.write(f"FAILED   Apply could not start: {error}")
            self.query_one("#activity", Static).update(
                f"● Apply failed: {error}. Run 'sudo -v' in the terminal, then retry."
            )
            return
        write_last_values(self.settings, applied=True, options=self.options)
        self.last_change = self.last_apply = datetime.now()
        activity = self.query_one("#activity", Static)
        activity.styles.color = "#6ee7a8"
        activity.update("● Applied successfully just now — saved to last-values.json")
        self.refresh_telemetry()

    def apply_commands(self) -> list[tuple[list[str], str | None]]:
        values = {s.key: s.value for s in self.settings}
        commands = []
        if self.options['intel-mode'] == 'apply':
            selected = {key: values[key] for key in intel_controls.SPECS}
            intel_controls.updated_config(intel_controls.CONFIG.read_text(), selected)
            commands.append((['sudo', '-n', sys.executable, str(Path(__file__).with_name('intel_controls.py'))], json.dumps(selected)))
        elif self.options['intel-mode'] == 'undervolt':
            selected = {key: values[key] for key in intel_controls.SPECS}
            commands.append((intel_controls.python_undervolt_command(selected), None))
        def write(path, value):
            commands.append((["sudo", "-n", "tee", str(path)], f"{value}\n"))
        profile = self.options['profile']
        if profile not in self.profiles:
            raise ValueError('Lenovo profile unavailable')
        write('/sys/firmware/acpi/platform_profile', profile)
        if profile == 'custom':
            for key, attribute in [('lenovo-cpu-cross-load-limit', 'ppt_cpu_cl'), ('lenovo-cpu-sustained-limit', 'ppt_pl1_spl'), ('lenovo-cpu-burst-limit', 'ppt_pl2_sppt'), ('lenovo-cpu-temperature-target', 'cpu_temp'), ('lenovo-gpu-temperature-target', 'gpu_temp')]:
                value = values[key]
                if value is None:
                    raise ValueError(f'{key} unavailable')
                write(f'/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes/{attribute}/current_value', value)
        if turbo_state() is None:
            raise ValueError('Turbo state unavailable')
        turbo = self.options['turbo'] == 'on'
        write('/sys/devices/system/cpu/intel_pstate/no_turbo', 0 if turbo else 1)
        for group, policies in cpu_groups().items():
            low = values[f'cpu-{group}-core-minimum-frequency']
            high = values[f'cpu-{group}-core-maximum-frequency']
            if not policies or low is None or high is None or low > high:
                raise ValueError(f'{group.upper()}-core frequency range unavailable or invalid')
            maximum = (5400 if group == 'p' else 3900) if turbo else (2200 if group == 'p' else 1600)
            if low < 800 or high > maximum:
                raise ValueError(f'{group.upper()}-core ceiling must be ≤ {maximum} MHz with Turbo {self.options["turbo"]}')
            for policy in policies:
                # Lower the floor first so lowering a ceiling never crosses it.
                write(policy / 'scaling_min_freq', 800000)
                write(policy / 'scaling_max_freq', high * 1000)
                write(policy / 'scaling_min_freq', low * 1000)
        if self.nvidia_power_limit_available:
            power_limit = values['nvidia-power-ceiling']
            if power_limit is None:
                raise ValueError('NVIDIA power limit unavailable')
            commands.append((["sudo", "-n", "nvidia-smi", "-i", "0", "-pl", str(power_limit)], None))
        for domain, lock, reset in [('core', '-lgc', '-rgc'), ('memory', '-lmc', '-rmc')]:
            mode = self.options[f'{domain}-mode']
            if mode == 'keep':
                continue
            arguments = ["sudo", "-n", "nvidia-smi", "-i", "0"]
            if mode == 'auto':
                arguments.append(reset)
            elif mode == 'locked':
                low, high = (values[f'nvidia-{domain}-clock-{bound}'] for bound in ('minimum', 'maximum'))
                if low is None or high is None or low > high:
                    raise ValueError(f'GPU {domain} clock range invalid or unavailable')
                arguments.extend([lock, f'{low},{high}'])
            else:
                raise ValueError('Unknown GPU clock mode')
            commands.append((arguments, None))
        return commands



if __name__ == "__main__":
    TunnerApp().run()
