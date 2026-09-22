"""Preview hardware settings and apply them only after explicit confirmation."""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Container, Horizontal, VerticalScroll
from textual.events import Key, MouseScrollDown, MouseScrollUp, Resize
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.validation import Number
from textual.worker import Worker, get_current_worker
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Select,
    Sparkline,
    Static,
)
from textual.widgets.option_list import Option

import intel_controls
from command_log import record_command, render_command


DOCUMENT = Path(__file__).with_name("lenovo-tuning.md")
LAST_VALUES = Path(__file__).with_name("last-values.json")
# Named snapshots of the plan, saved and loaded from the Profiles dialogs.
PROFILES_FILE = Path(__file__).with_name("profiles.json")
PROFILE_NAME_LENGTH = 40
LENOVO_ATTRIBUTES = {
    "lenovo-cpu-cross-load-limit": "ppt_cpu_cl",
    "lenovo-cpu-sustained-limit": "ppt_pl1_spl",
    "lenovo-cpu-burst-limit": "ppt_pl2_sppt",
    "lenovo-cpu-temperature-target": "cpu_temp",
    "lenovo-gpu-temperature-target": "gpu_temp",
}
LENOVO_ATTRIBUTE_ROOT = Path("/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
CPUFREQ_ROOT = Path("/sys/devices/system/cpu/cpufreq")
NO_TURBO = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
NVIDIA_GPU_INDEX = "0"
# LenovoLegionLinux's command-line tool, resolved on PATH at probe time.
LEGION_CLI = "legion_cli"
# Its boolean features: each has -status, -enable and -disable subcommands.
# (name, legion_cli feature, what Enable does); hybrid mode alone waits for
# a reboot.
LEGION_FEATURES = (
    ("Fan unlock", "fan-unlock", "lifts the firmware fan-speed ceiling"),
    ("Maximum fan speed", "maximumfanspeed", "runs the fans at full speed"),
    ("Lock fan controller", "lockfancontroller", "holds the fans at their current speed"),
    ("Mini fan curve", "minifancurve", "lets the firmware idle the fans while cool"),
    ("Battery conservation", "batteryconservation", "holds the charge near 60 %"),
    ("Rapid charging", "rapid-charging", "charges faster and turns conservation off"),
    ("Always-on USB charging", "always-on-usb-charging", "powers USB ports while off"),
    ("Fn lock", "fnlock", "swaps F1–F12 with their Fn functions"),
    ("Touchpad", "touchpad", "keeps the touchpad enabled"),
    ("Hybrid mode", "hybrid-mode", "switches the GPU mode; takes effect after a reboot"),
)
# Features the firmware cannot hold on together: enabling one turns the
# other off. A plan asking for both, or enabling one while the other is on
# and left on Keep current, is rejected before the dialog.
LEGION_EXCLUSIVE = (("batteryconservation", "rapid-charging"),)
# Features whose write only takes effect after a reboot, so reading one
# back right after an Apply shows the old state; that is not a failed write.
LEGION_REBOOT_FEATURES = frozenset({"hybrid-mode"})
LEGION_CHOICES = [("Keep current", "keep"), ("Enable", "on"), ("Disable", "off")]
LEGION_HELP = (
    "legion_cli is LenovoLegionLinux's tool; Enable and Disable run its "
    "<feature>-enable or -disable subcommand as root on Apply, and Keep "
    "leaves the feature alone. Live states are read at startup and after "
    "an Apply, not polled.\n"
    + "\n".join(f"{name}: {description}." for name, _, description in LEGION_FEATURES)
)
RANGE_PATTERN = re.compile(
    r"allowed range:\s*(?P<minimum>\d+)\s*[–-]\s*(?P<maximum>\d+)\s*(?P<unit>[^. `|]+)",
    re.IGNORECASE,
)
# Policy groups are named by base frequency, highest first: P/E on a hybrid
# CPU, "all" on a uniform one. The short form goes into row names and keys.
CPU_GROUP_LABELS = {"p": "P-cores", "e": "E-cores", "all": "All cores"}
CPU_GROUP_SHORT = {"p": "P", "e": "E", "all": "all"}
NARROW_WIDTH = 120  # Below this the status pane no longer fits beside the plan.
COMPACT_WIDTH = 80  # Below this the range hints go too, to keep names readable.
HISTORY_LENGTH = 60  # Two minutes of 2-second samples per sparkline.
BAR_WIDTH = 24
SAVE_DELAY = 0.5  # Seconds of quiet before a preview change is written to disk.
INTEL_HELP = (
    "Intel windows use milliseconds (1000 ms = 1 s), not exact boost timers. "
    "Thermal offset is relative to 100°C on this CPU. Persistent apply updates "
    "/etc/intel-undervolt.conf and reapplies its existing voltage offsets. "
    "Python undervolt writes only these live limits through MSRs and does not "
    "persist them. UI bounds are app limits, not hardware guarantees."
)
TONES = ("ok", "error", "muted", "warning")

PlannedCommand = tuple[list[str], str | None]


@dataclass
class ApplyPlan:
    """What one Apply would run, and the previews those commands carry."""

    commands: list[PlannedCommand] = field(default_factory=list)
    # key -> value for every setting a command was built from, captured when
    # the plan was made. A successful Apply makes exactly these the live
    # readings: not a row the modes never wrote, and not an edit typed while
    # the commands were running.
    values: dict[str, int] = field(default_factory=dict)
    # Per command, the subset of `values` that command alone makes live, so a
    # plan that fails part-way promotes what did run. A bare plan built by a
    # test may leave this shorter than `commands`.
    carried: list[dict[str, int]] = field(default_factory=list)
    # key -> state for every legion_cli toggle a command was built from, in
    # command order. A finished Apply re-reads every state rather than
    # assuming the writes stuck, and this says what each read should show.
    toggles: dict[str, bool] = field(default_factory=dict)

    def written_toggles(self, completed: int) -> dict[str, bool]:
        """The toggle states the first `completed` commands asked for.

        The toggle commands are the plan's last ones, so a plan that failed
        part-way reached only the first few of them, or none.
        """
        reached = completed - (len(self.commands) - len(self.toggles))
        return dict(list(self.toggles.items())[:max(0, reached)])


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
    # Shown in place of the range when the value needs a caveat, such as a
    # live reading that had to be clamped into the spec.
    note: str | None = None
    # The value the row started from: the hardware reading at startup or the
    # last confirmed Apply. A preview that differs from it is marked edited.
    live: int | None = None

    def nearest_choice(self, value: int) -> int:
        return min(self.choices, key=lambda choice: abs(choice - value))

    def change(self, amount: int) -> None:
        """Move `amount` steps, or supported clocks, in either direction."""
        if self.value is None or amount == 0:
            return
        if self.choices:
            if self.value in self.choices:
                index = self.choices.index(self.value) + amount
            else:
                # A typed value between two supported clocks steps to its
                # neighbour on the requested side first, not past it.
                position = bisect_left(self.choices, self.value)
                index = position + amount - 1 if amount > 0 else position + amount
            self.value = self.choices[max(0, min(len(self.choices) - 1, index))]
            return
        self.value = max(self.minimum, min(self.maximum, self.value + amount * self.step))

    def can_change(self, amount: int) -> bool:
        if self.value is None:
            return False
        if self.choices:
            return self.value != (self.choices[-1] if amount > 0 else self.choices[0])
        return self.value != (self.maximum if amount > 0 else self.minimum)

    @property
    def modified(self) -> bool:
        return self.value != self.live

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


@dataclass
class LegionToggle:
    """One boolean LenovoLegionLinux feature that legion_cli can enable or disable."""

    name: str
    feature: str
    description: str
    # The state legion_cli reported at startup or after the last Apply.
    live: bool | None = None
    # Why the row cannot be written: legion_cli or the feature is missing.
    unavailable_reason: str | None = None
    # Why `live` is unknown although the feature exists. The row stays
    # editable: writes run as root, so a read denied to the user says
    # nothing about whether they would work.
    read_failure: str | None = None
    # The full output behind a short reason, shown on hover.
    detail: str | None = None

    @property
    def key(self) -> str:
        return legion_key(self.feature)

    @property
    def state_text(self) -> str:
        """What the row shows in place of a range: the state, or why there is none."""
        if self.unavailable_reason is not None:
            return self.unavailable_reason
        if self.live is None:
            return self.read_failure or "live: unknown"
        return f"live: {'on' if self.live else 'off'}"

    def modified(self, choice: str) -> bool:
        """Whether `choice` would write a state other than the live one."""
        target = legion_target(choice)
        return target is not None and target != self.live


def legion_key(feature: str) -> str:
    """The options key, and Select id, of one legion_cli feature."""
    return f"legion-{feature}"


def legion_target(choice: str) -> bool | None:
    """The state a Keep/Enable/Disable choice writes; None for Keep."""
    return {"on": True, "off": False}.get(choice)


def name_list(names: list[str]) -> str:
    """Names as prose: 'A', 'A and B', 'A, B and C'."""
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


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
    turbo_enabled: bool | None = None
    profile: str | None = None
    cpu_policies: str = "Policy readings unavailable"
    cpu_boost: str = "Rated boost unavailable"
    gpu_power_limit: float | None = None
    gpu_max_clock: float | None = None
    gpu_max_memory: float | None = None
    gpu_headroom: float | None = None
    gpu_reasons: str = "Unavailable"
    intel_limits: str = "PL1: unavailable\nPL2: unavailable"
    # group -> (floor, ceiling) MHz the driver allows now; Turbo moves the
    # ceiling, so the rows' bounds follow it rather than the startup probe.
    cpu_limits: dict[str, tuple[int, int]] = field(default_factory=dict)

    @staticmethod
    def bar(value: float | None, ceiling: float | None, width: int = BAR_WIDTH) -> str:
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


def policy_reading(policy: Path, attribute: str) -> int:
    """Read one cpufreq attribute of a policy, in MHz."""
    return int((policy / attribute).read_text(encoding="utf-8").strip()) // 1_000


def turbo_state() -> bool | None:
    """Return the live Intel P-state turbo state, if this CPU exposes it."""
    try:
        # Intel exposes 0 for turbo allowed and 1 for turbo disabled.
        return NO_TURBO.read_text(encoding="utf-8").strip() == "0"
    except OSError:
        return None


def live_profile() -> str | None:
    """Return the firmware's current platform profile, if it exposes one."""
    try:
        return PLATFORM_PROFILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def cpu_groups() -> dict[str, list[Path]]:
    """Group cpufreq policies by base frequency without assuming a CPU model.

    Two or more tiers are named "p", "e" (then "e2", ...) from the highest
    base down; a uniform CPU is a single "all" group. A driver that exposes
    no base_frequency gets one "all" group too: the rated maximum is not a
    core type (amd-pstate ranks preferred cores a step higher), so it must
    not split a homogeneous CPU into P- and E-cores.
    """
    policies = sorted(CPUFREQ_ROOT.glob("policy*"))
    if not policies:
        return {}
    tiers: dict[int, list[Path]] = {}
    for policy in policies:
        try:
            tiers.setdefault(policy_reading(policy, "base_frequency"), []).append(policy)
        except (OSError, ValueError):
            continue
    if len(tiers) <= 1:
        return {"all": policies}
    names = ["p", "e"] + [f"e{index}" for index in range(2, len(tiers))]
    return {name: tiers[base] for name, base in zip(names, sorted(tiers, reverse=True))}


def cpu_group_limits(policies: list[Path]) -> tuple[int, int | None, int]:
    """Return the driver's (floor, base, ceiling) in MHz for a policy group."""
    floor = min(policy_reading(policy, "cpuinfo_min_freq") for policy in policies)
    ceiling = max(policy_reading(policy, "cpuinfo_max_freq") for policy in policies)
    try:
        base = max(policy_reading(policy, "base_frequency") for policy in policies)
    except (OSError, ValueError):
        base = None
    return floor, base, ceiling


def cpu_driver_limits(groups: dict[str, list[Path]]) -> dict[str, tuple[int, int]]:
    """Each group's current (floor, ceiling) in MHz; unreadable groups are left out."""
    limits = {}
    for group, policies in groups.items():
        try:
            floor, _, ceiling = cpu_group_limits(policies)
        except (OSError, ValueError):
            continue
        if floor <= ceiling:
            limits[group] = (floor, ceiling)
    return limits


def cpu_group_name(group: str) -> str:
    return CPU_GROUP_SHORT.get(group, group.upper())


def intel_values() -> list[TuningValue]:
    try:
        readings = intel_controls.config_readings(intel_controls.CONFIG.read_text())
        values = intel_controls.parse_readings(readings)
    except (OSError, ValueError):
        values, readings = {}, {}
    settings = []
    for key, (low, high, unit, step) in intel_controls.SPECS.items():
        value, note = values.get(key), None
        if value is not None and not low <= value <= high:
            # A config holding firmware's "unlimited" (4095.875 W rounds past
            # the spec) stays editable, shown clamped like a live reading. The
            # note quotes the file's own text, in its own unit, so the number
            # can be found in the config.
            value = max(low, min(high, value))
            raw, raw_unit = readings[key]
            note = f'Config reads {raw} {raw_unit}; shown clamped to {value} {unit}'
        settings.append(TuningValue(
            key.replace('-', ' ').capitalize().replace('pl1', 'PL1').replace('pl2', 'PL2'),
            value, low, high, unit, step=step, note=note,
            unavailable_reason='Intel config unavailable or unsupported' if key not in values else None,
        ))
    return settings


def cpu_clock_values(groups: dict[str, list[Path]]) -> list[TuningValue]:
    """One floor and one ceiling row per policy group, bounded by the driver."""
    settings = []
    for group, policies in groups.items():
        try:
            floor, _, ceiling = cpu_group_limits(policies)
            # A fixed-frequency policy reports floor == ceiling; only an
            # inverted range means the driver's limits cannot be trusted.
            limits_known = floor <= ceiling
        except (OSError, ValueError):
            floor, ceiling, limits_known = 0, 0, False
        for bound in ("minimum", "maximum"):
            attribute = "scaling_min_freq" if bound == "minimum" else "scaling_max_freq"
            try:
                readings = [policy_reading(policy, attribute) for policy in policies]
                current = max(readings) if bound == "minimum" else min(readings)
            except (OSError, ValueError):
                current = None
            unavailable = None
            if not limits_known:
                current, unavailable = None, "Policy limits unavailable"
            elif current is None:
                unavailable = "Policy unavailable"
            else:
                current = max(floor, min(ceiling, current))
            settings.append(TuningValue(f"CPU {cpu_group_name(group)}-core {bound} frequency", current,
                                        floor, ceiling, "MHz", step=100, unavailable_reason=unavailable))
    return settings


def nvidia_output(arguments: list[str]) -> str:
    """Run a read-only NVIDIA query; no shell or setting-changing option is used."""
    return run_command(
        ["nvidia-smi", "-i", NVIDIA_GPU_INDEX, *arguments], access="read",
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


def describe_error(error: BaseException) -> str:
    """One line for an error; unexpected types are named so 'x' is not all we see."""
    if isinstance(error, (ValueError, OSError, subprocess.SubprocessError)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def failure_lines(error: BaseException) -> list[str]:
    """Describe a failed command: its exception, then whatever it wrote to stderr."""
    stderr = getattr(error, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    lines = [describe_error(error)]
    if isinstance(stderr, str):
        lines.extend(line.strip() for line in stderr.splitlines() if line.strip())
    return lines


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
        # The current clock is where the lock preview starts, but an idle GPU
        # sits below the lowest lockable step, so it starts at the nearest one.
        setting = TuningValue(
            name,
            max(supported[0], min(supported[-1], current_value)),
            supported[0],
            supported[-1],
            "MHz",
            choices=supported if choices is None else choices,
        )
        if setting.choices:
            setting.value = setting.nearest_choice(setting.value)
        return setting

    return [
        clock("NVIDIA core clock minimum", graphics_current, graphics),
        clock("NVIDIA core clock maximum", graphics_current, graphics),
        clock("NVIDIA memory clock minimum", memory_current, memory, ()),
        clock("NVIDIA memory clock maximum", memory_current, memory, ()),
    ]


def nvidia_power_limit_available(settings: list[TuningValue]) -> bool:
    """Whether the probed power row holds a live limit inside a sane range.

    probe_documented_values already queried the limits; a row it left
    unavailable had none, so no second nvidia-smi launch is needed.
    """
    power = next((setting for setting in settings if setting.key == "nvidia-power-ceiling"), None)
    return power is not None and power.value is not None and power.unavailable_reason is None


def read_legion_status(legion_cli: str, toggle: LegionToggle) -> None:
    """Fill a fresh `toggle` from `legion_cli <feature>-status`; this never writes.

    A feature legion_cli reports missing disables the row. Any other failed
    read only leaves the state unknown: the write runs as root, so a status
    the user may not read (or that did not decode) says nothing about it.
    """
    try:
        result = run_command(
            [legion_cli, f"{toggle.feature}-status"], access="read",
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        # A launch that fails, times out, or does not decode (a
        # UnicodeDecodeError is a ValueError) fails this one read rather
        # than the whole hardware probe; a programming error still surfaces.
        toggle.read_failure, toggle.detail = "Status read failed", describe_error(error)
        return
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    # hybrid-mode prints a reboot notice before its value, so read the last line.
    if result.returncode == 0 and lines and lines[-1] in ("True", "False"):
        toggle.live = lines[-1] == "True"
        return
    errors = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    toggle.detail = "\n".join(lines + errors) or f"exit status {result.returncode}"
    # legion_cli's own wording when the sysfs node is absent or the module is not loaded.
    if any("not available" in line for line in lines):
        toggle.unavailable_reason = "Feature unavailable"
    else:
        toggle.read_failure = "Status read failed"


def probe_legion_toggles() -> tuple[str | None, list[LegionToggle]]:
    """Resolve legion_cli and read every feature's state; this never writes."""
    legion_cli = shutil.which(LEGION_CLI)
    toggles = [LegionToggle(name, feature, description) for name, feature, description in LEGION_FEATURES]
    if legion_cli is None:
        for toggle in toggles:
            toggle.unavailable_reason = "legion_cli not installed"
        return None, toggles
    # Each status is a separate Python process; reading them side by side
    # keeps the startup probe short.
    with ThreadPoolExecutor(max_workers=max(1, len(toggles))) as pool:
        # Consumed so a programming error in the reader still surfaces.
        list(pool.map(lambda toggle: read_legion_status(legion_cli, toggle), toggles))
    return legion_cli, toggles


def sudo_ready() -> bool:
    """Return whether sudo has cached credentials, without ever prompting."""
    try:
        return run_command(
            ["sudo", "-n", "true"], access="exec", capture_output=True, text=True, timeout=5
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cpu_temperature_from_sensors(report: object) -> float | None:
    """Pick the package, Tctl, or CPU temperature out of `sensors -j` output.

    Only temp*_input readings qualify, so a fan labelled "CPU Fan" never
    passes its RPM off as a temperature, and the package sensor wins over a
    generic "cpu" label whatever order the chips are listed in.
    """
    if not isinstance(report, dict):
        return None
    terms = ("package", "tctl", "cpu")
    found: dict[str, float] = {}
    for chip in report.values():
        if not isinstance(chip, dict):
            continue
        for label, readings in chip.items():
            if not isinstance(readings, dict):
                continue
            term = next((term for term in terms if term in label.lower()), None)
            temperature = next(
                (
                    value for key, value in readings.items()
                    if key.startswith("temp") and key.endswith("_input")
                    and isinstance(value, (int, float))
                ),
                None,
            )
            if term is not None and temperature is not None:
                found.setdefault(term, float(temperature))
    return next((found[term] for term in terms if term in found), None)


def cpu_boost_text(groups: dict[str, list[Path]]) -> str:
    """The rated boost per group; fixed, so the probe reads it once."""
    boosts = []
    for group, policies in groups.items():
        try:
            rated = max(policy_reading(policy, "cpuinfo_max_freq") for policy in policies)
        except (OSError, ValueError):
            continue
        boosts.append(f"{cpu_group_name(group)}: up to {rated}")
    if not boosts:
        return "Rated boost unavailable"
    return "Rated boost (not a live limit)\n" + " · ".join(boosts) + " MHz"


def describe_cpu_policies(groups: dict[str, list[Path]]) -> tuple[str, int | None]:
    """Return (live policy ranges per group, average MHz)."""
    lines, currents = [], []
    for group, policies in groups.items():
        try:
            floors = [policy_reading(policy, "scaling_min_freq") for policy in policies]
            ceilings = [policy_reading(policy, "scaling_max_freq") for policy in policies]
            now = [policy_reading(policy, "scaling_cur_freq") for policy in policies]
        except (OSError, ValueError):
            continue
        currents += now
        label = CPU_GROUP_LABELS.get(group, group.upper())
        lines.append(
            f"{label}: {max(floors)}–{min(ceilings)} MHz\n  Now {sum(now) / len(now):.0f} MHz avg"
        )
    policies_text = "\n".join(lines) or "Policy readings unavailable"
    average = round(sum(currents) / len(currents)) if currents else None
    return policies_text, average


def read_telemetry(groups: dict[str, list[Path]]) -> Telemetry:
    """Read CPU and GPU telemetry without changing any hardware setting."""
    telemetry = Telemetry()
    try:
        report = json.loads(
            run_command(
                ["sensors", "-j"], access="read",
                check=True, capture_output=True, text=True, timeout=3
            ).stdout
        )
        telemetry.cpu_temperature = cpu_temperature_from_sensors(report)
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
        for name, raw in zip(fields, parts):
            try:
                number = float(raw)
            except ValueError:
                continue  # An unsupported field must not erase other readings.
            setattr(telemetry, name, round(number) if name == "gpu_utilization" else number)
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

    telemetry.turbo_enabled = turbo_state()
    telemetry.profile = live_profile()
    telemetry.cpu_policies, telemetry.cpu_clock = describe_cpu_policies(groups)
    telemetry.cpu_limits = cpu_driver_limits(groups)
    telemetry.intel_limits = intel_controls.live_limits()
    return telemetry


def saved_preview(payload: object) -> tuple[dict[str, int], dict[str, str]]:
    """The integer preview values and option names in one saved payload.

    Anything malformed reads as nothing saved rather than an error.
    """
    values = payload.get("values") if isinstance(payload, dict) else None
    options = payload.get("options") if isinstance(payload, dict) else None
    values = values if isinstance(values, dict) else {}
    options = options if isinstance(options, dict) else {}
    return (
        {key: value for key, value in values.items() if type(value) is int},
        {key: value for key, value in options.items() if isinstance(value, str)},
    )


def preview_payload(settings: list[TuningValue], options=None) -> dict[str, object]:
    """The previews and mode choices as saved to disk, stamped with the time."""
    return {
        "options": dict(options or {}),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "values": {setting.key: setting.value for setting in settings if setting.value is not None},
    }


def load_saved_state() -> tuple[dict[str, int], dict[str, str]]:
    """Load the integer preview values and option names saved previously."""
    try:
        return saved_preview(json.loads(LAST_VALUES.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError):
        return {}, {}


def load_profiles() -> dict[str, dict[str, object]]:
    """Every named profile in profiles.json; a missing or broken file has none."""
    try:
        payload = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {name: profile for name, profile in payload.items() if isinstance(profile, dict)}


def write_profiles(profiles: dict[str, dict[str, object]]) -> None:
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def save_profile(name: str, settings: list[TuningValue], options: dict[str, str]) -> None:
    """Store the plan under `name`, replacing a profile of that name; no hardware is touched."""
    profiles = load_profiles()
    profiles[name] = preview_payload(settings, options)
    write_profiles(profiles)


def delete_profile(name: str) -> None:
    profiles = load_profiles()
    if profiles.pop(name, None) is not None:
        write_profiles(profiles)


def write_last_values(settings: list[TuningValue], applied: bool = False, options=None) -> None:
    """Persist preview values; this never applies a hardware setting."""
    payload = preview_payload(settings, options)
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


@dataclass
class Probe:
    """Everything the interface needs from hardware before it can be drawn."""

    settings: list[TuningValue]
    cpu_groups: dict[str, list[Path]]
    cpu_boost: str
    nvidia_power_limit_available: bool
    profiles: list[str]
    profile: str
    turbo: bool | None
    # None when the check never ran, so no notice claims that sudo failed.
    sudo_ready: bool | None
    # The resolved legion_cli path, or None when it is not installed.
    legion_cli: str | None
    legion_toggles: list[LegionToggle]


def probe_system() -> Probe:
    """Run every startup read; this blocks, so the app calls it off the loop."""
    # The policy grouping cannot change while the app runs, so it is read
    # once here and handed to every later poll and plan.
    groups = cpu_groups()
    settings = load_values() + cpu_clock_values(groups) + nvidia_clock_values() + intel_values()
    for setting in settings:
        setting.live = setting.value
    profile = live_profile()
    try:
        profiles = PLATFORM_PROFILE.with_name("platform_profile_choices").read_text().split()
    except OSError:
        profiles = []
    if profile is None or not profiles:
        profiles, profile = [], "unavailable"
    legion_cli, legion_toggles = probe_legion_toggles()
    return Probe(
        settings, groups, cpu_boost_text(groups), nvidia_power_limit_available(settings),
        profiles, profile, turbo_state(), sudo_ready(), legion_cli, legion_toggles,
    )


def gate_hint(key: str, options: dict[str, str], nvidia_power_available: bool) -> str | None:
    """Why the selected modes would not write the setting `key`, or None.

    This is the one statement of which rows an Apply writes: the plan reads
    only ungated previews, the rows show the hint in place of their range,
    and the status pane counts only ungated targets, so none can disagree.
    """
    if key.startswith("intel-") and options["intel-mode"] == "keep":
        return "Needs an Intel mode"
    if key.startswith("lenovo-") and options["profile"] != "custom":
        return "Needs Custom profile"
    if key.startswith("nvidia-core-clock") and options["core-mode"] != "locked":
        return "Needs Locked mode"
    if key.startswith("nvidia-memory-clock") and options["memory-mode"] != "locked":
        return "Needs Locked mode"
    if key == "nvidia-power-ceiling" and not nvidia_power_available:
        return "NVIDIA power range unavailable"
    return None


class NumberInput(Input):
    """An Input that lets a few harmless shortcuts through while it has focus.

    `a` (Apply, which confirms first), `t` (status pane) and `l` (log) still
    reach the app's bindings from a value field. Every other letter is
    swallowed: a stray `q` must not quit, `r` restore, `c`/`g` launch a
    workload or `v` discard every edit while someone is typing a number.
    Only printable keys are claimed, as Input does, so Tab, Escape and the
    other navigation keys keep their screen bindings. The test suite checks
    that every passthrough key is still an app binding.
    """

    PASSTHROUGH_KEYS = frozenset("atl")

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return super().check_consume_key(key, character) and character not in self.PASSTHROUGH_KEYS

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # A one-line field has nothing to scroll; dropping the inherited
        # scroll bindings lets the row's ↑/↓ steps reach the Footer.
        if action in ("scroll_up", "scroll_down"):
            return False
        return super().check_action(action, parameters)

    async def _on_key(self, event) -> None:
        if event.is_printable and not self.check_consume_key(event.key, event.character):
            # Textual runs every _on_key in the class hierarchy, so stop
            # Input's here; the event still bubbles up to the bindings.
            event.prevent_default()


class ValueRow(Horizontal):
    """One editable setting: a typed field with step buttons and key bindings."""

    BINDINGS = [
        Binding("up", "step(1)", "Step", key_display="↑/↓"),
        Binding("down", "step(-1)", "Step down", show=False),
        Binding("shift+up", "step(10)", "×10 up", show=False),
        Binding("shift+down", "step(-10)", "×10 down", show=False),
        Binding("ctrl+r", "revert", "Revert"),
    ]

    class Changed(Message):
        def __init__(self, value_row: "ValueRow") -> None:
            self.value_row = value_row
            super().__init__()

    def __init__(self, setting: TuningValue) -> None:
        super().__init__(classes="value-row")
        self.setting = setting
        # Why the row is disabled by a mode selector, shown in the range slot.
        self.hint: str | None = None
        # Bounds follow the setting, which Restore may narrow after compose.
        self.range_validator = Number(setting.minimum, setting.maximum)

    def compose(self) -> ComposeResult:
        yield Label("", classes="setting-name")
        yield Static("", id="range", classes="range")
        down = Button("−", id="down", classes="step", compact=True)
        down.can_focus = False
        yield down
        yield NumberInput(
            "" if self.setting.value is None else str(self.setting.value),
            id="value",
            classes="value",
            disabled=self.input_disabled,
            restrict=r"-?\d*" if self.setting.minimum < 0 else r"\d*",
            validators=self.range_validator,
            validate_on=["changed"],
            compact=True,
        )
        up = Button("+", id="up", classes="step", compact=True)
        up.can_focus = False
        yield up

    @property
    def input_disabled(self) -> bool:
        return self.setting.value is None and self.setting.unavailable_reason is not None

    @property
    def expected_text(self) -> str:
        return "" if self.setting.value is None else str(self.setting.value)

    def on_mount(self) -> None:
        self.refresh_value()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_step(-1 if event.button.id == "down" else 1)

    def action_step(self, amount: int) -> None:
        if self.disabled or not self.setting.can_change(amount):
            return
        self.setting.change(amount)
        self.refresh_value()
        self.post_message(self.Changed(self))

    def revert(self) -> bool:
        """Put the preview back to its live reading; False when there is none.

        A mode gate disables the field, not the data: an edit made while the
        section was enabled is still an edit, so Revert discards it too.
        """
        if not self.setting.modified or self.setting.live is None:
            return False
        self.setting.value = self.setting.live
        self.refresh_value()
        return True

    def action_revert(self) -> None:
        if self.revert():
            self.post_message(self.Changed(self))

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.has_focus_within:
            event.stop()
            self.action_step(1)

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.has_focus_within:
            event.stop()
            self.action_step(-1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.value == self.expected_text:
            # Input posts Changed for its initial text at mount. Text that
            # already matches the setting is not an edit: it must not blank
            # a live reading outside the range, mark the row, or save.
            return
        if event.validation_result is None or not event.validation_result.is_valid:
            if self.setting.value is not None:
                self.setting.value = None
                self.refresh_decorations()
                self.post_message(self.Changed(self))
            return
        self.setting.value = int(event.value)
        self.refresh_decorations()
        self.post_message(self.Changed(self))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.commit_input()
        self.screen.focus_next()

    def on_input_blurred(self, event: Input.Blurred) -> None:
        self.commit_input()

    def commit_input(self) -> None:
        """Snap a typed value onto the supported steps, if the setting has them."""
        setting = self.setting
        if setting.choices and setting.value is not None and setting.value not in setting.choices:
            setting.value = setting.nearest_choice(setting.value)
            self.refresh_value()
            self.post_message(self.Changed(self))

    def refresh_value(self) -> None:
        value = self.query_one("#value", Input)
        self.range_validator.minimum = self.setting.minimum
        self.range_validator.maximum = self.setting.maximum
        value.disabled = self.input_disabled
        expected = self.expected_text
        if value.value != expected:
            # A programmatic sync must not be validated as user input:
            # that would re-check it against stale bounds and clear it.
            with value.prevent(Input.Changed):
                value.value = expected
        self.refresh_decorations()

    def refresh_bounds(self) -> None:
        """Follow new bounds without replacing text the user is still typing.

        A half-typed value is not a setting yet, so its field is left alone,
        but it is checked again: text the old bounds rejected may fit the new
        ones. Otherwise refresh_value only rewrites text that bounds clamped.
        """
        if self.setting.value is not None:
            self.refresh_value()
            return
        self.range_validator.minimum = self.setting.minimum
        self.range_validator.maximum = self.setting.maximum
        value = self.query_one("#value", Input)
        result = value.validate(value.value) if value.value else None
        if result is not None and result.is_valid:
            self.setting.value = int(value.value)
            self.post_message(self.Changed(self))
        self.refresh_decorations()

    def refresh_decorations(self) -> None:
        """Update the edited marker, the range or hint text, and the buttons."""
        modified = self.setting.modified
        name = self.query_one(".setting-name", Label)
        name.update(f"{'●' if modified else ' '} {self.setting.name}")
        name.set_class(modified, "modified")
        hinted = self.disabled and self.hint is not None
        range_text = self.hint if hinted else (
            self.setting.unavailable_reason or self.setting.note
            or f"{self.setting.minimum}–{self.setting.maximum} {self.setting.unit}"
        )
        range_widget = self.query_one("#range", Static)
        range_widget.update(range_text or "")
        range_widget.set_class(hinted, "hint")
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        self.query_one("#down", Button).disabled = not self.setting.can_change(-1)
        self.query_one("#up", Button).disabled = not self.setting.can_change(1)


class ToggleRow(Horizontal):
    """One legion_cli feature: its live state beside a Keep/Enable/Disable choice."""

    def __init__(self, toggle: LegionToggle, choice: str) -> None:
        super().__init__(classes="value-row toggle-row")
        self.toggle = toggle
        self.choice = choice

    def compose(self) -> ComposeResult:
        yield Label("", classes="setting-name")
        yield Static("", id="range", classes="range")
        yield Select(
            LEGION_CHOICES, value=self.choice, allow_blank=False,
            id=self.toggle.key, classes="choice", compact=True,
        )

    def on_mount(self) -> None:
        self.refresh_state()

    def refresh_state(self, choice: str | None = None) -> None:
        """Update the edited marker, the live state or reason, and the selector."""
        if choice is not None:
            self.choice = choice
        toggle = self.toggle
        modified = toggle.modified(self.choice)
        name = self.query_one(".setting-name", Label)
        name.update(f"{'●' if modified else ' '} {toggle.name}")
        name.set_class(modified, "modified")
        name.tooltip = f"Enable {toggle.description}"
        unavailable = toggle.unavailable_reason is not None
        state = self.query_one("#range", Static)
        state.update(toggle.state_text)
        # Only a row that cannot be written takes the disabled colour; a
        # state that could not be read is flagged, since the row stays live.
        state.set_class(unavailable, "hint")
        state.set_class(not unavailable and toggle.read_failure is not None, "unknown")
        state.tooltip = None if toggle.detail is None else Text(toggle.detail)
        self.query_one(Select).disabled = unavailable


class StatusRail(VerticalScroll):
    """Always-visible live context for decisions made in the tuning list."""

    def compose(self) -> ComposeResult:
        yield Static("LIVE STATUS", classes="panel-title")
        yield Static("Refreshing every 2 s · t hides", classes="panel-subtitle")
        yield Static("", id="cpu-thermal", classes="metric")
        yield Sparkline([], id="cpu-temperature-trend", classes="trend", summary_function=max)
        yield Static("", id="gpu-thermal", classes="metric")
        yield Sparkline([], id="gpu-temperature-trend", classes="trend", summary_function=max)
        yield Static("", id="gpu-power", classes="metric")
        yield Sparkline([], id="gpu-power-trend", classes="trend", summary_function=max)
        yield Static("CPU CLOCKS · LIVE POLICY", classes="section-title")
        yield Static("", id="clock-readout", classes="readout")
        yield Static("INTEL PACKAGE · LIVE", classes="section-title")
        yield Static("", id="intel-readout", classes="readout")
        yield Static("GPU CLOCKS · LIVE", classes="section-title")
        yield Static("", id="gpu-readout", classes="readout")
        yield Sparkline([], id="gpu-clock-trend", classes="trend", summary_function=max)
        yield Static("PLANNED LIMITS", classes="section-title")
        yield Static("", id="limit-readout", classes="readout")
        yield Static("● marks a preview that differs from its live reading. Apply sends the plan to hardware.", classes="panel-note")

    def refresh_status(
        self,
        settings: list[TuningValue],
        gate: Callable[[str], str | None],
        telemetry: Telemetry,
        history: dict[str, deque[float]],
        legion: str = "",
    ) -> None:
        by_key = {setting.key: setting for setting in settings}

        def preview(key: str) -> int | None:
            setting = by_key.get(key)
            return None if setting is None else setting.value

        def planned(key: str) -> int | None:
            """The preview, but only when the selected modes would write it."""
            return None if gate(key) is not None else preview(key)

        def shown(number: object, unit: str = "") -> str:
            return "—" if number is None else f"{number}{unit}"

        for name, trend in history.items():
            self.query_one(f"#{name.replace('_', '-')}-trend", Sparkline).data = list(trend)

        # A target is only a target when the mode that writes it is selected.
        cpu_targets = []
        intel_offset = planned("intel-thermal-offset")
        if type(intel_offset) is int:
            cpu_targets.append((100 + intel_offset, "Intel preview"))
        lenovo_cpu = planned("lenovo-cpu-temperature-target")
        if type(lenovo_cpu) is int:
            cpu_targets.append((lenovo_cpu, "Lenovo preview"))
        cpu_target = min(cpu_targets) if cpu_targets else None
        cpu_temp = telemetry.cpu_temperature
        self.query_one("#cpu-thermal", Static).update(
            f"CPU THERMAL  {cpu_temp:.0f}°C\n"
            f"{Telemetry.bar(cpu_temp, cpu_target[0] if cpu_target else 100)}\n"
            + (f"Target {cpu_target[0]}°C ({cpu_target[1]})" if cpu_target else "No target set · scale 100°C")
            if cpu_temp is not None
            else "CPU THERMAL\nNo sensor reading"
        )

        gpu_temp = telemetry.gpu_temperature
        gpu_targets = []
        if gpu_temp is not None and telemetry.gpu_headroom is not None:
            gpu_targets.append((round(gpu_temp + telemetry.gpu_headroom), "driver slowdown"))
        lenovo_gpu = planned("lenovo-gpu-temperature-target")
        if type(lenovo_gpu) is int:
            gpu_targets.append((lenovo_gpu, "Lenovo preview"))
        gpu_target = min(gpu_targets) if gpu_targets else None
        headroom = "—" if telemetry.gpu_headroom is None else f"{telemetry.gpu_headroom:+.0f}°C"
        self.query_one("#gpu-thermal", Static).update(
            f"GPU THERMAL  {gpu_temp:.0f}°C · margin {headroom}\n"
            f"{Telemetry.bar(gpu_temp, gpu_target[0] if gpu_target else 90)}\n"
            + (f"Target {gpu_target[0]}°C ({gpu_target[1]})" if gpu_target else "No target set · scale 90°C")
            + f"\n{telemetry.gpu_reasons}"
            if gpu_temp is not None
            else "GPU THERMAL\nNo sensor reading"
        )

        gpu_power_limit = telemetry.gpu_power_limit
        self.query_one("#gpu-power", Static).update(
            f"GPU POWER {telemetry.gpu_power:.0f} / {gpu_power_limit:.0f} W live\n"
            f"{Telemetry.bar(telemetry.gpu_power, gpu_power_limit)}\n"
            f"Utilization: {shown(telemetry.gpu_utilization)}%"
            if telemetry.gpu_power is not None and gpu_power_limit is not None
            else "GPU POWER\nNo live power reading"
        )

        turbo = (
            "Allowed" if telemetry.turbo_enabled is True else
            "Disabled" if telemetry.turbo_enabled is False else
            "unavailable"
        )
        self.query_one("#clock-readout", Static).update(
            f"Turbo: {turbo}\n{telemetry.cpu_policies}\n{telemetry.cpu_boost}"
        )
        self.query_one("#intel-readout", Static).update(telemetry.intel_limits)

        def mhz(number: float | None) -> str:
            return "—" if number is None else f"{number:.0f}"

        self.query_one("#gpu-readout", Static).update(
            f"Core now: {mhz(telemetry.gpu_clock)} MHz\nVRAM now: {mhz(telemetry.gpu_memory_clock)} MHz\n"
            f"Driver max core: {mhz(telemetry.gpu_max_clock)} MHz\n"
            f"Driver max VRAM: {mhz(telemetry.gpu_max_memory)} MHz\n"
            "Active clock locks: unverified"
        )

        def plan_lines(label: str, keys: tuple[str, ...], unit: str, separator: str) -> list[str]:
            """The planned value(s), and the reading they replace when edited."""
            lines = [f"{label}: {separator.join(shown(preview(key)) for key in keys)}{unit}"]
            if any(key in by_key and by_key[key].modified for key in keys):
                was = separator.join(
                    shown(by_key[key].live) if key in by_key else "—" for key in keys
                )
                lines.append(f"  was {was}{unit}")
            return lines

        limits: list[str] = []
        cpu_groups_present: list[str] = []
        for setting in settings:
            match = re.fullmatch(r"cpu-(.+)-core-(?:minimum|maximum)-frequency", setting.key)
            if match and match.group(1) not in cpu_groups_present:
                cpu_groups_present.append(match.group(1))
        for group in cpu_groups_present:
            limits += plan_lines(
                CPU_GROUP_LABELS.get(group, group.upper()),
                (f"cpu-{group}-core-minimum-frequency", f"cpu-{group}-core-maximum-frequency"),
                " MHz", "–",
            )
        limits += plan_lines("Core lock", ("nvidia-core-clock-minimum", "nvidia-core-clock-maximum"), " MHz", "–")
        limits += plan_lines("VRAM lock", ("nvidia-memory-clock-minimum", "nvidia-memory-clock-maximum"), " MHz", "–")
        limits += plan_lines("GPU ceiling", ("nvidia-power-ceiling",), " W", "")
        limits += plan_lines("Lenovo PL1/PL2", ("lenovo-cpu-sustained-limit", "lenovo-cpu-burst-limit"), " W", "/")
        limits += plan_lines("Intel PL1/PL2", ("intel-pl1-sustained-power", "intel-pl2-burst-power"), " W", "/")
        offset = preview("intel-thermal-offset")
        limits.append(
            f"Intel target: {100 + offset}°C (offset {offset})" if type(offset) is int
            else "Intel target: unverified"
        )
        limits += plan_lines("Lenovo CPU/GPU targets", ("lenovo-cpu-temperature-target", "lenovo-gpu-temperature-target"), "°C", "/")
        limits.append("Firmware targets require Custom")
        if legion:
            limits.append(legion)
        self.query_one("#limit-readout", Static).update("\n".join(limits))


class TuningPlan(VerticalScroll):
    """The editable plan; each mode selector sits above the rows it gates."""

    def __init__(
        self,
        settings: list[TuningValue],
        profiles: list[str],
        options: dict[str, str],
        legion_toggles: Iterable[LegionToggle] = (),
        *,
        turbo_available: bool,
    ) -> None:
        super().__init__(id="tuning-plan")
        self.settings = settings
        self.profiles = profiles
        self.options = options
        self.legion_toggles = list(legion_toggles)
        self.turbo_available = turbo_available

    def rows(self, prefix: str) -> list[ValueRow]:
        if prefix == "nvidia-":
            chosen = [s for s in self.settings if not s.key.startswith(("cpu-", "intel-", "lenovo-"))]
        else:
            chosen = [s for s in self.settings if s.key.startswith(prefix)]
        return [ValueRow(setting) for setting in chosen]

    def compose(self) -> ComposeResult:
        yield Static("TUNING PLAN", id="plan-title")
        yield Static("", id="notice")
        yield Static("", id="activity")

        yield Static("CPU POLICY", classes="group-title")
        yield Label("Turbo · ceilings above base frequency require Turbo on", classes="control-label")
        yield Select(
            [("On", "on"), ("Off", "off")], value=self.options["turbo"], allow_blank=False,
            id="turbo", disabled=not self.turbo_available, compact=True,
        )
        yield from self.rows("cpu-")

        yield Static("INTEL PACKAGE LIMITS", classes="group-title")
        yield Label("Apply mode · persistent config file or live MSR writes", classes="control-label")
        yield Select(
            [
                ("Keep current Intel settings", "keep"),
                ("Apply persistently via intel-undervolt", "apply"),
                ("Apply live via Python undervolt", "undervolt"),
            ],
            value=self.options["intel-mode"], allow_blank=False, id="intel-mode", compact=True,
        )
        with Collapsible(title="About Intel limits", collapsed=True):
            yield Static(INTEL_HELP)
        yield from self.rows("intel-")

        yield Static("LENOVO CUSTOM MODE", classes="group-title")
        yield Label("Profile · firmware sliders apply only in Custom", classes="control-label")
        yield Select(
            [(profile, profile) for profile in self.profiles] or [("Unavailable", "unavailable")],
            value=self.options["profile"], allow_blank=False, id="profile",
            disabled=not self.profiles, compact=True,
        )
        yield from self.rows("lenovo-")

        if self.legion_toggles:
            yield Static("LEGION FEATURES · legion_cli", classes="group-title")
            yield Label("Toggles · Enable/Disable run legion_cli on Apply; Keep leaves it alone", classes="control-label")
            with Collapsible(title="About Legion features", collapsed=True):
                yield Static(LEGION_HELP)
            for toggle in self.legion_toggles:
                yield ToggleRow(toggle, self.options.get(toggle.key, "keep"))

        yield Static("NVIDIA GPU", classes="group-title")
        for domain in ("core", "memory"):
            yield Label(f"{domain.capitalize()} clocks · Locked enables the range below", classes="control-label")
            with Horizontal(classes="mode-row"):
                yield Select(
                    [("Keep current mode", "keep"), ("Automatic / reset locks", "auto"), ("Locked to preview range", "locked")],
                    value=self.options[f"{domain}-mode"], allow_blank=False, id=f"{domain}-mode", compact=True,
                )
                yield Button(f"Reset {domain} on Apply", id=f"reset-{domain}", compact=True)
        yield from self.rows("nvidia-")

        with Horizontal(id="workloads"):
            yield Button("Stress CPU", id="stress-cpu")
            yield Button("Stress GPU", id="stress-gpu")
        with Horizontal(id="profile-actions"):
            yield Button("Save profile…", id="save-profile")
            yield Button("Load profile…", id="load-profile")
        with Horizontal(id="actions"):
            yield Button("Restore saved", id="restore")
            yield Button("Revert to live", id="revert")
            yield Button("Apply", id="apply", variant="warning")
        yield Static("APPLY COMMAND LOG", id="apply-log-title")
        yield RichLog(id="apply-log", wrap=True, markup=False, max_lines=200)


class ApplyConfirmation(ModalScreen[ApplyPlan | None]):
    """Show exactly what will run, and require a second action to run it."""

    # Keep navigation keys available to the command preview.  Every other
    # keyboard answer is deliberately a yes/no answer: Y applies, anything
    # else backs out without changing hardware.
    SCROLL_KEYS = frozenset({
        "up", "down", "left", "right", "pageup", "pagedown", "home", "end",
        "ctrl+home", "ctrl+end", "tab", "shift+tab",
    })

    def __init__(self, plan: ApplyPlan, sudo_ok: bool = True) -> None:
        super().__init__()
        self.plan = plan
        self.sudo_ok = sudo_ok

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static(
                f"Apply {len(self.plan.commands)} command(s) to live hardware?", id="confirm-text"
            )
            with Horizontal(id="confirm-prompt"):
                yield Static("Apply now? [Y/n]", id="confirm-question")
                yield Button("Apply now [Y]", id="confirm", variant="error")
                yield Button("Cancel", id="cancel")
            if not self.sudo_ok:
                yield Static(
                    "sudo is not authenticated: these commands will fail until you run "
                    "`sudo -v` in a terminal.",
                    id="confirm-warning",
                )
            with VerticalScroll(id="confirm-commands"):
                yield Static(
                    "\n".join(render_command(arguments, input_text) for arguments, input_text in self.plan.commands)
                    or "No commands planned.",
                    markup=False,
                )

    def on_mount(self) -> None:
        # Let ↑/↓, Page Up/Down, Home and End scroll the preview immediately.
        self.query_one("#confirm-commands", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(self.plan if event.button.id == "confirm" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        self.dismiss(self.plan)

    def on_key(self, event: Key) -> None:
        """Treat the prompt as Y/n without trapping command-preview scrolling."""
        if event.key in self.SCROLL_KEYS:
            return
        if event.key.lower() == "y":
            self.action_confirm()
        else:
            self.action_cancel()
        event.stop()


class SaveProfileDialog(ModalScreen[str | None]):
    """Ask for a name to store the current plan under."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, existing: Iterable[str]) -> None:
        super().__init__()
        self.existing = sorted(existing)

    def compose(self) -> ComposeResult:
        with Container(classes="profile-dialog"):
            yield Static("Save the plan as a profile", classes="dialog-title")
            yield Static(
                "Stores every preview value and mode choice; nothing is applied.",
                classes="dialog-note",
            )
            yield Input(
                placeholder="Profile name", id="profile-name", max_length=PROFILE_NAME_LENGTH,
            )
            yield Static(self.default_hint(), id="profile-hint", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def default_hint(self) -> str:
        if not self.existing:
            return "No profiles saved yet."
        return "Saved: " + ", ".join(self.existing)

    def on_mount(self) -> None:
        self.query_one("#profile-name", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        name = event.value.strip()
        hint = self.query_one("#profile-hint", Static)
        hint.remove_class("error")
        hint.update(f"Replaces the saved profile “{name}”." if name in self.existing else self.default_hint())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_cancel()

    def action_save(self) -> None:
        name = self.query_one("#profile-name", Input).value.strip()
        if not name:
            hint = self.query_one("#profile-hint", Static)
            hint.add_class("error")
            hint.update("Type a name first.")
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LoadProfileDialog(ModalScreen[str | None]):
    """Pick a saved profile to load into the plan, or delete one."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("delete", "delete", "Delete profile"),
        # The list has focus, so q would otherwise quit the app behind it.
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, profiles: dict[str, dict[str, object]]) -> None:
        super().__init__()
        self.profiles = profiles

    def compose(self) -> ComposeResult:
        with Container(classes="profile-dialog"):
            yield Static("Load a profile into the plan", classes="dialog-title")
            yield Static(
                "Loading only changes the previews; press Apply afterwards to set hardware.",
                classes="dialog-note",
            )
            yield OptionList(*self.options(), id="profile-list")
            yield Static("", id="profile-hint", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Load", id="load", variant="primary")
                yield Button("Delete", id="delete", variant="error")
                yield Button("Cancel", id="cancel")

    def options(self) -> list[Option]:
        options = []
        for name in sorted(self.profiles, key=str.casefold):
            saved_at = self.profiles[name].get("updated_at")
            stamp = saved_at.replace("T", " ")[:16] if isinstance(saved_at, str) else "unknown time"
            options.append(Option(Text(f"{name}  ·  saved {stamp}"), id=name))
        return options

    def on_mount(self) -> None:
        self.query_one("#profile-list", OptionList).focus()
        self.refresh_empty()

    def refresh_empty(self) -> None:
        empty = not self.profiles
        self.query_one("#load", Button).disabled = empty
        self.query_one("#delete", Button).disabled = empty
        if empty:
            self.query_one("#profile-hint", Static).update("No profiles saved yet; press s on the plan to save one.")

    def selected(self) -> str | None:
        profile_list = self.query_one("#profile-list", OptionList)
        if profile_list.highlighted is None:
            return None
        return profile_list.get_option_at_index(profile_list.highlighted).id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "load":
            name = self.selected()
            if name is not None:
                self.dismiss(name)
        elif event.button.id == "delete":
            self.action_delete()
        else:
            self.action_cancel()

    def action_delete(self) -> None:
        name = self.selected()
        if name is None:
            return
        hint = self.query_one("#profile-hint", Static)
        try:
            delete_profile(name)
        except OSError as error:
            hint.update(f"Could not delete “{name}”: {error}")
            return
        self.profiles.pop(name, None)
        profile_list = self.query_one("#profile-list", OptionList)
        index = profile_list.highlighted or 0
        profile_list.clear_options()
        profile_list.add_options(self.options())
        # clear_options drops the highlight, which would leave Enter, Load
        # and Delete doing nothing until an arrow key picked a row again.
        if profile_list.option_count:
            profile_list.highlighted = min(index, profile_list.option_count - 1)
        hint.update(f"Deleted “{name}”.")
        self.refresh_empty()

    def action_cancel(self) -> None:
        self.dismiss(None)


class TunnerCommands(Provider):
    """Expose the tuner's actions to the command palette (ctrl+p)."""

    COMMANDS = (
        ("Apply plan to hardware", "apply", "Review the planned commands, then confirm"),
        ("Restore saved preview", "restore", "Load last-values.json into the plan"),
        ("Revert to live values", "revert", "Discard every edit since startup or the last Apply"),
        ("Save plan as profile", "save_profile", "Store the previews and modes under a name"),
        ("Load saved profile", "load_profile", "Replace the plan with a named profile"),
        ("Toggle CPU stress", "stress('cpu')", "Start or stop the CPU workload"),
        ("Toggle GPU stress", "stress('gpu')", "Start or stop the CUDA workload"),
        ("Toggle live status pane", "toggle_rail", "Show or hide telemetry"),
        ("Jump to apply log", "show_log", "Scroll to the latest Apply outcome"),
    )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, action, help_text in self.COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), partial(self.app.run_action, action), help=help_text)

    async def discover(self) -> Hits:
        for name, action, help_text in self.COMMANDS:
            yield DiscoveryHit(name, partial(self.app.run_action, action), help=help_text)


class TunnerApp(App[None]):
    """Minimal tuner with explicit confirmation before any hardware write."""

    TITLE = "Tunner"
    CSS = """
    Screen { background: $background; }
    #status-strip { display: none; height: 1; padding: 0 2; color: $text-muted; background: $surface; }
    Screen.narrow #status-strip, Screen.rail-hidden #status-strip { display: block; }
    #loading { width: 1fr; height: 1fr; }
    #probe-failure { width: 1fr; padding: 1 2; color: $error; }
    #workspace { width: 96%; max-width: 138; height: 1fr; margin: 1 2; }
    #tuning-plan { width: 1fr; padding-right: 2; }
    #plan-title { text-style: bold; }
    #notice { color: $text-muted; margin-bottom: 1; }
    #notice.warning { color: $warning; }
    #activity { margin-bottom: 1; }
    #activity.ok { color: $success; }
    #activity.error { color: $error; }
    #activity.warning { color: $warning; }
    #activity.muted { color: $text-muted; }
    .group-title { color: $primary; text-style: bold; margin-top: 1; }
    .control-label { color: $text-muted; }
    .mode-row { height: auto; }
    .mode-row Select { width: 1fr; }
    .mode-row Button { margin-left: 1; }
    Collapsible { margin-bottom: 0; }
    .value-row { height: 1; }
    .value-row:focus-within { background: $boost; }
    .setting-name { width: 1fr; }
    .setting-name.modified { color: $warning; }
    .range { width: 20; color: $text-muted; text-align: right; }
    .range.hint { color: $text-disabled; }
    .range.unknown { color: $text-warning; }
    .value { width: 13; }
    .step { min-width: 5; width: 5; }
    .toggle-row .choice { width: 22; margin-left: 1; }
    #workloads, #profile-actions { height: auto; margin-top: 1; }
    #actions { dock: bottom; height: auto; margin-top: 1; padding: 1 0 0 0; background: $background; }
    #workloads Button, #profile-actions Button, #actions Button { margin-right: 1; }
    #apply-log-title { color: $primary; text-style: bold; margin-top: 1; }
    #apply-log { height: 10; border: solid $border-blurred; padding: 0 1; background: $surface; }
    #apply-log:focus { border: solid $border; }
    #status-rail { width: 38; min-width: 34; height: 1fr; padding: 1 2; background: $panel; border: solid $border-blurred; }
    Screen.narrow #status-rail, Screen.rail-hidden #status-rail { display: none; }
    Screen.narrow #tuning-plan, Screen.rail-hidden #tuning-plan { padding-right: 0; }
    Screen.rail-only #tuning-plan { display: none; }
    Screen.rail-only #status-rail { width: 1fr; }
    Screen.compact .range { display: none; }
    .panel-title { text-style: bold; }
    .panel-subtitle { color: $text-muted; margin-bottom: 1; }
    .metric { margin-top: 1; }
    .trend { height: 1; }
    .section-title { color: $primary; text-style: bold; margin-top: 1; }
    .readout { color: $foreground-muted; }
    .panel-note { color: $text-muted; margin-top: 1; }
    ApplyConfirmation { align: center middle; background: $background 60%; }
    #confirm-dialog { width: 90%; max-width: 100; height: 90%; padding: 1 2; background: $panel; border: solid $border; }
    #confirm-text { margin-bottom: 1; text-style: bold; }
    #confirm-prompt { height: auto; margin-bottom: 1; }
    #confirm-question { width: 1fr; text-style: bold; }
    #confirm-prompt Button { margin-left: 1; }
    #confirm-warning { color: $warning; margin-bottom: 1; }
    #confirm-commands { height: 1fr; border: solid $border-blurred; padding: 0 1; }
    SaveProfileDialog, LoadProfileDialog { align: center middle; background: $background 60%; }
    .profile-dialog { width: 90%; max-width: 72; height: auto; max-height: 90%; padding: 1 2; background: $panel; border: solid $border; }
    .dialog-title { text-style: bold; }
    .dialog-note { color: $text-muted; margin-bottom: 1; }
    #profile-list { height: auto; max-height: 14; }
    #profile-hint { color: $text-muted; margin-top: 1; }
    #profile-hint.error { color: $error; }
    .dialog-buttons { height: auto; margin-top: 1; }
    .dialog-buttons Button { margin-right: 1; }
    """
    BINDINGS = [
        Binding("a", "apply", "Apply"),
        Binding("r", "restore", "Restore"),
        Binding("v", "revert", "Revert all", show=False),
        Binding("s", "save_profile", "Save profile"),
        Binding("o", "load_profile", "Load profile"),
        Binding("c", "stress('cpu')", "Stress CPU", show=False),
        Binding("g", "stress('gpu')", "Stress GPU", show=False),
        Binding("t", "toggle_rail", "Status"),
        Binding("l", "show_log", "Log", show=False),
        Binding("q", "quit", "Quit"),
    ]
    COMMANDS = App.COMMANDS | {TunnerCommands}
    # Actions that act on the plan behind a profile dialog; while one is
    # open its keys belong to the dialog.
    PLAN_ACTIONS = frozenset({
        "apply", "restore", "revert", "save_profile", "load_profile", "stress", "toggle_rail", "show_log",
    })

    def __init__(self) -> None:
        super().__init__()
        self.settings: list[TuningValue] = []
        self.cpu_groups: dict[str, list[Path]] = {}
        self.cpu_boost = "Rated boost unavailable"
        self.nvidia_power_limit_available = False
        self.profiles: list[str] = []
        self.options = {"profile": "unavailable", "turbo": "off",
                        "core-mode": "keep", "memory-mode": "keep", "intel-mode": "keep",
                        **{legion_key(feature): "keep" for _, feature, _ in LEGION_FEATURES}}
        self.legion_cli: str | None = None
        self.legion_toggles: list[LegionToggle] = []
        self.plan: TuningPlan | None = None
        self.stress_processes: dict[str, subprocess.Popen[str]] = {}
        self.last_change: datetime | None = None
        self.last_apply: datetime | None = None
        # (when, text, tone) of the last one-off message; it stays on screen
        # until a newer preview change, apply, or message replaces it.
        self.activity_message: tuple[datetime, str, str] | None = None
        self.telemetry = Telemetry()
        self.history: dict[str, deque[float]] = {
            name: deque(maxlen=HISTORY_LENGTH)
            for name in ("cpu_temperature", "gpu_temperature", "gpu_power", "gpu_clock")
        }
        self.applying = False
        self.save_timer: Timer | None = None
        self.telemetry_worker: Worker[None] | None = None
        # Wide terminals: the pane can be hidden. Narrow ones: it can replace
        # the plan instead of disappearing.
        self.rail_visible = True
        self.narrow_rail = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status-strip")
        with Horizontal(id="workspace"):
            yield LoadingIndicator(id="loading")
            yield StatusRail(id="status-rail")
        yield Footer()

    def on_mount(self) -> None:
        self.update_layout(self.size.width)
        self.probe_hardware()

    @work(exclusive=True, group="probe", exit_on_error=False)
    async def probe_hardware(self) -> None:
        """Read hardware off the event loop, then build the plan from it."""
        failure = None
        try:
            probe = await asyncio.to_thread(probe_system)
        except Exception as error:
            # Nothing was read, sudo included, so the plan starts empty and
            # the notice must not claim that sudo failed.
            probe = Probe([], {}, "Rated boost unavailable", False, [], "unavailable", None, None, None, [])
            failure = describe_error(error)
        try:
            await self.finish_probe(probe)
        except Exception as error:
            # The worker swallows errors, so an endless spinner would be the
            # only symptom; put the reason where the plan should have been.
            failure = describe_error(error)
            await self.query("#loading").remove()
            await self.query_one("#workspace").mount(
                Static(Text(f"Could not build the plan: {failure}\nPress q to quit."), id="probe-failure"),
                before=self.query_one(StatusRail),
            )
        if failure is None:
            return
        if self.plan_ready:
            self.show_activity(f"● Hardware probe failed: {failure}", "error")
        self.notify(f"Hardware probe failed: {failure}", severity="error", timeout=10)

    async def finish_probe(self, probe: Probe) -> None:
        self.settings = probe.settings
        self.cpu_groups = probe.cpu_groups
        self.cpu_boost = probe.cpu_boost
        self.nvidia_power_limit_available = probe.nvidia_power_limit_available
        self.profiles = probe.profiles
        self.options["profile"] = probe.profile
        self.options["turbo"] = "on" if probe.turbo else "off"
        self.legion_cli = probe.legion_cli
        self.legion_toggles = probe.legion_toggles
        await self.query_one("#loading").remove()
        plan = TuningPlan(
            self.settings, self.profiles, self.options, self.legion_toggles,
            turbo_available=probe.turbo is not None,
        )
        await self.query_one("#workspace").mount(plan, before=self.query_one(StatusRail))
        self.plan = plan
        self.update_sudo_notice(probe.sudo_ready)
        self.populate_live_intel_values()
        self.update_row_disabled_state()
        self.refresh_telemetry()
        self.refresh_activity()
        self.refresh_status_rail()
        self.set_interval(2, self.refresh_telemetry)
        self.set_interval(1, self.refresh_activity)

    @property
    def plan_ready(self) -> bool:
        return self.plan is not None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.PLAN_ACTIONS and isinstance(self.screen, (SaveProfileDialog, LoadProfileDialog)):
            return False
        return super().check_action(action, parameters)

    def on_resize(self, event: Resize) -> None:
        self.update_layout(event.size.width)

    def update_layout(self, width: int) -> None:
        # Reserve room for setting names as well as the 43-column controls.
        # The tuning screen stays at the bottom when confirmation is open.
        screen = self.screen_stack[0]
        narrow = width < NARROW_WIDTH
        screen.set_class(narrow and not self.narrow_rail, "narrow")
        screen.set_class(narrow and self.narrow_rail, "rail-only")
        screen.set_class(not narrow and not self.rail_visible, "rail-hidden")
        screen.set_class(width < COMPACT_WIDTH, "compact")

    def action_toggle_rail(self) -> None:
        if self.size.width < NARROW_WIDTH:
            self.narrow_rail = not self.narrow_rail
        else:
            self.rail_visible = not self.rail_visible
        self.update_layout(self.size.width)

    def action_show_log(self) -> None:
        if not self.plan_ready:
            return
        if self.narrow_rail:
            self.narrow_rail = False
            self.update_layout(self.size.width)
        apply_log = self.query_one("#apply-log", RichLog)
        apply_log.scroll_visible()
        apply_log.focus()

    def update_sudo_notice(self, ready: bool | None) -> None:
        """Warn only when sudo was actually checked and has no cached credentials."""
        notice = self.query_one("#notice", Static)
        notice.set_class(ready is False, "warning")
        notice.update(
            "sudo is not authenticated: run `sudo -v` in a terminal before Apply, or every command will fail."
            if ready is False
            else "Tab to a value, type it or press ↑/↓ (shift for ×10); ctrl+p lists every command."
        )

    def on_value_row_changed(self, event: ValueRow.Changed) -> None:
        self.last_change = datetime.now()
        self.schedule_save()
        self.refresh_activity()
        self.refresh_status_rail()

    def schedule_save(self) -> None:
        """Coalesce a burst of edits into one write of last-values.json."""
        if self.save_timer is not None:
            self.save_timer.stop()
        self.save_timer = self.set_timer(SAVE_DELAY, self.flush_save)

    def flush_save(self) -> None:
        if self.save_timer is not None:
            self.save_timer.stop()
            self.save_timer = None
        self.save_last_values()

    def save_last_values(self, applied: bool = False) -> bool:
        """Write last-values.json; a failure is shown, since the hardware is unaffected."""
        try:
            write_last_values(self.settings, applied=applied, options=self.options)
        except OSError as error:
            if self.is_running:
                message = f"Could not save {LAST_VALUES.name}: {error}"
                self.show_activity(f"● {message}", "error")
                self.notify(message, severity="error", timeout=10)
            return False
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("stress-"):
            self.toggle_stress(button_id.removeprefix("stress-"))
        elif button_id.startswith("reset-"):
            self.query_one(f"#{button_id.removeprefix('reset-')}-mode", Select).value = "auto"
        elif button_id == "restore":
            self.action_restore()
        elif button_id == "revert":
            self.action_revert()
        elif button_id == "save-profile":
            self.action_save_profile()
        elif button_id == "load-profile":
            self.action_load_profile()
        elif button_id == "apply":
            self.action_apply()

    def action_stress(self, target: str) -> None:
        if self.plan_ready and target in ("cpu", "gpu"):
            self.toggle_stress(target)

    def toggle_stress(self, target: str) -> None:
        """Launch or stop an isolated opt-in workload without blocking the TUI."""
        self.refresh_stress_processes()
        button = self.query_one(f"#stress-{target}", Button)
        process = self.stress_processes.get(target)
        if process is not None and process.poll() is None:
            self.stop_stress(process)
            self.stress_processes.pop(target)
            button.label = f"Stress {target.upper()}"
            self.show_activity(f"● {target.upper()} stress stopped")
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
        self.show_activity(f"● {target.upper()} stress started; press again to stop")

    def refresh_stress_processes(self) -> bool:
        """Reap finished workloads and report failures instead of leaving stale UI."""
        failure_reported = False
        for target, process in list(self.stress_processes.items()):
            returncode = process.poll()
            if returncode is None:
                continue

            self.stress_processes.pop(target)
            self.query_one(f"#stress-{target}", Button).label = f"Stress {target.upper()}"
            stderr = self.drain_stderr(process)
            if returncode != 0:
                detail = stderr.splitlines()[-1] if stderr else f"exit status {returncode}"
                message = f"{target.upper()} stress failed: {detail}"
                self.show_activity(f"● {message}", "error")
                self.notify(message, severity="error", timeout=10)
                failure_reported = True
        return failure_reported

    def on_unmount(self) -> None:
        """Never leave stress processes running or an edit unsaved after exit."""
        for process in self.stress_processes.values():
            if process.poll() is None:
                self.stop_stress(process)
        if self.save_timer is not None:
            self.flush_save()

    @classmethod
    def stop_stress(cls, process: subprocess.Popen[str]) -> None:
        """Stop a launcher and every worker in its process group, then reap it.

        The caller has checked that the launcher is still alive, so its pid is
        still ours and safe to signal as a group id. SIGKILL is only a fallback
        for a launcher that ignores SIGTERM; the waits are short so the UI
        never stalls for long.
        """
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        cls.drain_stderr(process)

    @staticmethod
    def drain_stderr(process: subprocess.Popen[str]) -> str:
        """Read what a launcher wrote to stderr without blocking, then close the pipe.

        Workers inherit the pipe, so a blocking read could hang the UI if any
        of them outlived the launcher.
        """
        if process.stderr is None:
            return ""
        chunks: list[bytes] = []
        try:
            descriptor = process.stderr.fileno()
            os.set_blocking(descriptor, False)
            while True:
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            process.stderr.close()
        return b"".join(chunks).decode(errors="replace").strip()

    def gate(self, key: str) -> str | None:
        """Why the selected modes would not write `key`; None when they would."""
        return gate_hint(key, self.options, self.nvidia_power_limit_available)

    def update_row_disabled_state(self) -> None:
        """Disable rows their mode selector does not apply, and say why."""
        for row in self.query(ValueRow):
            key = row.setting.key
            row.hint = self.gate(key)
            row.disabled = row.hint is not None or (key.startswith("nvidia-memory-clock") and row.input_disabled)
            row.refresh_value()
        self.refresh_toggle_rows()

    def refresh_toggle_rows(self) -> None:
        for row in self.query(ToggleRow):
            row.refresh_state(self.options.get(row.toggle.key, "keep"))

    def on_select_changed(self, event: Select.Changed) -> None:
        # Every Select posts Changed for its initial value at mount, and
        # Restore sets the option before the message lands; neither is an
        # edit, and refreshing every row for each would be wasted work.
        key = event.select.id
        if key not in self.options or not isinstance(event.value, str) or self.options[key] == event.value:
            return
        self.options[key] = event.value
        if key == "intel-mode":
            self.populate_live_intel_values()
        self.last_change = datetime.now()
        self.schedule_save()
        self.refresh_activity()
        self.update_row_disabled_state()
        self.refresh_status_rail()

    def populate_live_intel_values(self) -> None:
        """Give Intel rows without a persistent config a live starting value.

        Only the Python-undervolt mode works without /etc/intel-undervolt.conf,
        so only that mode fills empty rows. Rows that already hold a value
        (from the config file or from Restore) are left alone. A reading that
        had to be clamped into the spec is shown with its raw value in place
        of the range.
        """
        if self.options["intel-mode"] != "undervolt":
            return
        values, notes = intel_controls.live_control_state(load_saved_state()[0])
        for row in self.query(ValueRow):
            key = row.setting.key
            if key not in values or row.setting.value is not None:
                continue
            row.setting.value = row.setting.live = values[key]
            row.setting.unavailable_reason = None
            row.setting.note = notes.get(key)
            row.refresh_value()

    def refresh_cpu_clock_ranges(self) -> None:
        """Re-read the CPU driver limits now, after a Turbo-state write."""
        self.update_cpu_clock_bounds(cpu_driver_limits(self.cpu_groups))

    def update_cpu_clock_bounds(
        self, limits: dict[str, tuple[int, int]], widen_only: bool = False,
    ) -> bool:
        """Give the CPU rows the driver's current bounds; True when any moved.

        intel_pstate lowers ``cpuinfo_max_freq`` to the base clock while
        Turbo is disabled, so the startup probe cannot know the boost range
        until Turbo is on. The driver may publish it a moment after the
        no_turbo write, an Apply that failed later on still turned Turbo on,
        and another tool can flip it too, so every telemetry poll brings the
        limits here, not only a successful Apply. Previews are kept, only
        clamped into the new range.

        A poll passes widen_only: Turbo turned off behind the app's back must
        not quietly clamp a preview, which would stay clamped once Turbo came
        back. Narrower driver limits are still enforced when Apply plans.
        """
        settings = {setting.key: setting for setting in self.settings}
        moved: set[str] = set()
        for group, (floor, ceiling) in limits.items():
            for bound in ("minimum", "maximum"):
                setting = settings.get(f"cpu-{group}-core-{bound}-frequency")
                # A row the probe could not read stays unavailable.
                if setting is None or setting.unavailable_reason is not None:
                    continue
                low, high = floor, ceiling
                if widen_only:
                    low, high = min(low, setting.minimum), max(high, setting.maximum)
                if (setting.minimum, setting.maximum) == (low, high):
                    continue
                setting.minimum, setting.maximum = low, high
                if setting.value is not None:
                    setting.value = max(low, min(high, setting.value))
                if setting.live is not None:
                    setting.live = max(low, min(high, setting.live))
                moved.add(setting.key)
        if not moved:
            return False
        self.cpu_boost = cpu_boost_text(self.cpu_groups)
        self.telemetry.cpu_boost = self.cpu_boost
        for row in self.query(ValueRow):
            if row.setting.key in moved:
                row.refresh_bounds()
        return True

    def show_activity(self, text: str, tone: str = "ok") -> None:
        """Show a one-off message and keep it until something newer happens."""
        self.activity_message = (datetime.now(), text, tone)
        self.set_activity(text, tone)

    def set_activity(self, text: str, tone: str) -> None:
        activity = self.query_one("#activity", Static)
        for name in TONES:
            activity.set_class(name == tone, name)
        # Command output ends up here; `[E]` in it is text, not a markup tag.
        activity.update(Text(text))

    def notify(self, message: str, **kwargs) -> None:  # type: ignore[override]
        # Toasts carry command output too; show it verbatim.
        kwargs.setdefault("markup", False)
        super().notify(message, **kwargs)

    def refresh_telemetry(self) -> None:
        """Start a poll unless the previous one is still running.

        A thread cannot be stopped, and cancelling a slow poll on every tick
        would mean no sample ever lands; skipping the tick instead just lowers
        the rate to whatever the driver manages.
        """
        if not self.is_running:
            return
        worker = self.telemetry_worker
        if worker is not None and worker.is_running:
            return
        self.telemetry_worker = self.poll_telemetry()

    @work(thread=True, group="telemetry", exit_on_error=False)
    def poll_telemetry(self) -> None:
        """Read sensors off the event loop so a slow driver never freezes input."""
        telemetry = read_telemetry(self.cpu_groups)
        try:
            self.call_from_thread(self.apply_telemetry, telemetry)
        except RuntimeError:
            pass  # The app stopped while this poll was in flight.

    def apply_telemetry(self, telemetry: Telemetry) -> None:
        if not self.is_running:
            return
        if self.plan_ready:
            self.update_cpu_clock_bounds(telemetry.cpu_limits, widen_only=True)
        telemetry.cpu_boost = self.cpu_boost
        self.telemetry = telemetry
        for name, trend in self.history.items():
            reading = getattr(telemetry, name)
            if reading is not None:
                trend.append(float(reading))
        self.refresh_status_rail()

    def refresh_status_rail(self) -> None:
        if not self.is_running:
            return  # Shutdown prunes the screen before it stops the timers.
        self.query_one(StatusRail).refresh_status(
            self.settings, self.gate, self.telemetry, self.history, self.legion_summary()
        )
        self.query_one("#status-strip", Static).update(self.status_strip_text())

    def planned_legion_toggles(self) -> list[tuple[LegionToggle, bool]]:
        """Every toggle the selected choices would write, with the state it gets.

        This is the one statement of which toggles an Apply runs: the plan
        builds its legion_cli commands from it and the status pane lists it.
        A row whose status could not be read is included, since the write
        runs as root; only a missing feature, or no legion_cli, keeps one out.
        """
        if self.legion_cli is None:
            return []
        return [
            (toggle, target) for toggle in self.legion_toggles
            if toggle.unavailable_reason is None
            and (target := legion_target(self.options.get(toggle.key, "keep"))) is not None
        ]

    def legion_summary(self) -> str:
        """The legion_cli writes the selected choices call for, for the status rail."""
        if not self.legion_toggles:
            return ""
        if self.legion_cli is None:
            return "Legion: legion_cli not installed"
        planned = [f"{toggle.name} {'on' if target else 'off'}" for toggle, target in self.planned_legion_toggles()]
        return "Legion: " + (", ".join(planned) if planned else "no toggles planned")

    def status_strip_text(self) -> str:
        """The live essentials for when the status pane is hidden: one line."""
        telemetry = self.telemetry
        parts = [
            "CPU —" if telemetry.cpu_temperature is None else f"CPU {telemetry.cpu_temperature:.0f}°C",
            "GPU —" if telemetry.gpu_temperature is None else f"GPU {telemetry.gpu_temperature:.0f}°C",
        ]
        if telemetry.gpu_power is not None and telemetry.gpu_power_limit is not None:
            parts.append(f"{telemetry.gpu_power:.0f}/{telemetry.gpu_power_limit:.0f} W")
        if telemetry.gpu_clock is not None:
            parts.append(f"GPU {telemetry.gpu_clock:.0f} MHz")
        if telemetry.cpu_clock is not None:
            parts.append(f"CPU avg {telemetry.cpu_clock} MHz")
        parts.append("profile " + (telemetry.profile or "?"))
        turbo = telemetry.turbo_enabled
        parts.append("turbo " + ("on" if turbo else "off" if turbo is False else "?"))
        return " · ".join(parts)

    def refresh_activity(self) -> None:
        """Render the newest of: a one-off message, the last apply, the last change.

        On an exact timestamp tie a message beats an apply, which beats a
        change, matching the order in which the code records them.
        """
        if not self.is_running or not self.plan_ready:
            return  # Shutdown prunes the screen before it stops this timer.
        self.refresh_stress_processes()
        events: list[tuple[datetime, int, str]] = []
        if self.activity_message is not None:
            events.append((self.activity_message[0], 2, "message"))
        if self.last_apply is not None:
            events.append((self.last_apply, 1, "apply"))
        if self.last_change is not None:
            events.append((self.last_change, 0, "change"))
        if not events:
            self.set_activity("○ No preview change in this session", "muted")
            return
        when, _, kind = max(events)
        age = datetime.now() - when
        recent = age < timedelta(seconds=5)
        if kind == "message":
            _, text, tone = self.activity_message
            self.set_activity(text, tone)
        elif kind == "apply":
            self.set_activity(
                "● Applied successfully just now — saved to last-values.json"
                if recent
                else f"● Applied successfully {int(age.total_seconds())}s ago",
                "ok",
            )
        elif recent:
            self.set_activity("● Changed just now — saved to last-values.json", "ok")
        else:
            self.set_activity(f"● Last preview change {int(age.total_seconds())}s ago", "muted")

    def action_restore(self) -> None:
        if self.plan_ready and not self.applying:
            self.restore_saved()

    def restore_saved(self) -> None:
        if self.save_timer is not None:
            # A save still pending holds the edits Restore is about to
            # replace; writing it first would make Restore load those.
            self.save_timer.stop()
            self.save_timer = None
        saved, options = load_saved_state()
        restored = self.load_preview(saved, options)
        self.last_change = datetime.now()
        self.show_activity(
            f"● Restored {restored} saved preview value(s); press Apply to set hardware"
        )
        self.refresh_status_rail()

    def load_preview(self, saved: dict[str, int], options: dict[str, str]) -> int:
        """Put saved previews and mode choices into the plan; returns the values loaded."""
        allowed = {
            "intel-mode": ["keep", "apply", "undervolt"],
            "profile": self.profiles,
            "turbo": ["on", "off"],
            "core-mode": ["keep", "auto", "locked"],
            "memory-mode": ["keep", "auto", "locked"],
        }
        for toggle in self.legion_toggles:
            allowed[toggle.key] = [choice for _, choice in LEGION_CHOICES]
        for key, choices in allowed.items():
            if options.get(key) in choices:
                self.query_one(f"#{key}", Select).value = options[key]
                self.options[key] = options[key]
        # Modes first, so live Intel values back a restored undervolt mode.
        self.populate_live_intel_values()
        restored = 0
        for setting in self.settings:
            value = saved.get(setting.key)
            if value is None:
                continue
            # A row whose typed text was rejected also has no value, but its
            # reading and bounds are known: clamp into them like any other.
            if setting.value is None and (
                setting.unavailable_reason is not None or setting.key.startswith("intel-")
            ):
                if setting.key.startswith("intel-"):
                    # The spec bounds are known even without a config file,
                    # so a saved value inside them is a valid preview.
                    if not intel_controls.in_spec(setting.key, value):
                        continue
                    setting.value = value
                    setting.unavailable_reason = None
                else:
                    # The stored value remains usable for an Apply operation even
                    # when live NVIDIA telemetry is temporarily inaccessible.
                    setting.value = value
                    setting.minimum = value
                    setting.maximum = value
                    setting.unavailable_reason = "saved value; live clock query failed"
            else:
                setting.value = max(setting.minimum, min(setting.maximum, value))
            restored += 1
        self.update_row_disabled_state()
        return restored

    def action_save_profile(self) -> None:
        if not self.plan_ready or isinstance(self.screen, ModalScreen):
            return
        for row in self.query(ValueRow):
            row.commit_input()
        self.push_screen(SaveProfileDialog(load_profiles()), self.save_profile)

    def save_profile(self, name: str | None) -> None:
        if name is None:
            return
        try:
            save_profile(name, self.settings, self.options)
        except OSError as error:
            message = f"Could not save profile “{name}”: {error}"
            self.show_activity(f"● {message}", "error")
            self.notify(message, severity="error", timeout=10)
            return
        self.show_activity(f"● Saved profile “{name}” to {PROFILES_FILE.name}")

    def action_load_profile(self) -> None:
        if not self.plan_ready or self.applying or isinstance(self.screen, ModalScreen):
            return
        self.push_screen(LoadProfileDialog(load_profiles()), self.load_profile)

    def load_profile(self, name: str | None) -> None:
        if name is None or self.applying:
            return
        profile = load_profiles().get(name)
        if profile is None:
            self.show_activity(f"● Profile “{name}” is no longer saved", "error")
            return
        if self.save_timer is not None:
            self.save_timer.stop()
            self.save_timer = None
        loaded = self.load_preview(*saved_preview(profile))
        # The loaded plan is the new preview, so last-values.json follows it.
        self.last_change = datetime.now()
        self.schedule_save()
        self.show_activity(
            f"● Loaded profile “{name}” ({loaded} value(s)); press Apply to set hardware"
        )
        self.refresh_status_rail()

    def action_revert(self) -> None:
        """Put every editable row back to the value it started from."""
        if not self.plan_ready or self.applying:
            return
        reverted = sum(row.revert() for row in self.query(ValueRow))
        if reverted:
            self.last_change = datetime.now()
            self.schedule_save()
        self.show_activity(f"● Reverted {reverted} value(s) to the live reading")
        self.refresh_status_rail()

    def set_apply_controls(self, enabled: bool) -> None:
        for selector in ("#apply", "#restore", "#revert", "#load-profile"):
            self.query_one(selector, Button).disabled = not enabled

    def action_apply(self) -> None:
        """Plan first, so a rejected plan is reported without a dialog."""
        if not self.plan_ready or self.applying or isinstance(self.screen, ApplyConfirmation):
            return
        for row in self.query(ValueRow):
            row.commit_input()
        apply_log = self.query_one("#apply-log", RichLog)
        try:
            plan = self.apply_commands()
        except Exception as error:
            # A programming error while planning lands in the log too.
            detail = describe_error(error)
            apply_log.clear()
            self.log_apply("FAILED", f"Apply could not start: {detail}")
            apply_log.scroll_visible()
            self.show_activity(f"● Apply failed: {detail}", "error")
            self.notify(f"Apply not started: {detail}", severity="error", timeout=10)
            return
        self.confirm_apply(plan)

    @work(exclusive=True, group="confirm", exit_on_error=False)
    async def confirm_apply(self, plan: ApplyPlan) -> None:
        """Check sudo off the loop, then show the plan for its second action."""
        ready = await asyncio.to_thread(sudo_ready)
        self.update_sudo_notice(ready)
        self.push_screen(ApplyConfirmation(plan, ready), self.apply_confirmed)

    def apply_confirmed(self, plan: ApplyPlan | None) -> None:
        if plan is None or self.applying:
            return
        # Flagged here, on the loop, so a second Apply cannot slip in before
        # the worker's first message lands.
        self.begin_apply()
        self.run_apply(plan)

    @work(thread=True, group="apply", exit_on_error=False)
    def run_apply(self, plan: ApplyPlan) -> None:
        """Run the confirmed plan off the event loop, reporting each command."""
        failure: str | None = None
        completed = 0
        try:
            for arguments, input_text in plan.commands:
                rendered = render_command(arguments, input_text)
                try:
                    result = run_command(
                        arguments,
                        access="write",
                        input_text=input_text,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    lines = failure_lines(error)
                    first, *rest = lines
                    self.call_from_thread(self.log_apply, "FAILED", f"{rendered}: {first}", rest)
                    failure = lines[-1]
                    break
                # Show what the command reported (nvidia-smi confirms the new
                # limit, the Intel helper names its backup), but not tee's
                # echo of the value it was given.
                echoed = (input_text or "").strip()
                details = [
                    line.strip() for line in (result.stdout or "").splitlines()
                    if line.strip() and line.strip() != echoed
                ]
                self.call_from_thread(self.log_apply, "APPLIED", rendered, details)
                completed += 1
        except Exception as error:
            # Anything unexpected must land in the log rather than tear down
            # the app mid-apply.
            failure = describe_error(error)
            self.call_from_thread(self.log_apply, "FAILED", f"Apply aborted: {failure}")
        self.call_from_thread(self.finish_apply, failure, plan, completed)

    def begin_apply(self) -> None:
        self.applying = True
        self.set_apply_controls(False)
        self.query_one("#apply-log", RichLog).clear()
        self.show_activity("● Applying…", "muted")

    def log_apply(self, kind: str, text: str, details: list[str] | tuple[str, ...] = ()) -> None:
        apply_log = self.query_one("#apply-log", RichLog)
        style = {"APPLIED": "bold green", "FAILED": "bold red"}.get(kind, "bold yellow")
        apply_log.write(Text.assemble((f"{kind:<8} ", style), text))
        for line in details:
            apply_log.write(f"         {line}")

    def finish_apply(self, failure: str | None, plan: ApplyPlan, completed: int = 0) -> None:
        self.applying = False
        self.set_apply_controls(True)
        self.query_one("#apply-log", RichLog).scroll_visible()
        if self.legion_cli is not None and completed:
            # Any command can move a toggle, not only a toggle write: a
            # profile switch can reset fan state, whatever ran before a
            # failure may have flipped one, and a write can change another
            # feature (rapid charging turns conservation off) or wait for a
            # reboot (hybrid mode). The states are read back, not assumed,
            # and checked against what the toggle writes that ran asked for.
            # When nothing ran, nothing changed, and ten reads are spared.
            self.refresh_legion_toggles(plan.written_toggles(completed))
        # The plan's own values become the readings: not a row's current
        # text, which may have changed while the commands ran, and not a row
        # the selected modes never wrote. After a failure only the commands
        # that ran changed hardware, so only what they carried is promoted.
        if failure is None:
            applied = plan.values
        else:
            applied = {}
            for carried in plan.carried[:completed]:
                applied.update(carried)
        for setting in self.settings:
            if setting.key in applied:
                setting.live = applied[setting.key]
        # Enabling Turbo makes intel_pstate publish the rated boost ceiling
        # only after its no_turbo write succeeds.  Re-read it now so the CPU
        # fields expand immediately instead of requiring an app restart.
        if failure is None and any(arguments[-1] == str(NO_TURBO) for arguments, _ in plan.commands):
            self.refresh_cpu_clock_ranges()
        # Only the marker and range text change here; the field itself is
        # left alone, since this lands whenever the commands happen to
        # finish and would otherwise wipe a value being typed.
        for row in self.query(ValueRow):
            row.refresh_decorations()
        if failure is not None:
            self.show_activity(
                f"● Apply failed: {failure}. Run 'sudo -v' in the terminal, then retry.", "error"
            )
            self.notify(f"Apply failed: {failure}", severity="error", timeout=10)
        else:
            self.last_change = self.last_apply = datetime.now()
            if self.save_last_values(applied=True):
                self.refresh_activity()
            self.notify("Applied to hardware", timeout=5)
        self.refresh_telemetry()
        self.refresh_status_rail()

    @work(thread=True, exclusive=True, group="legion", exit_on_error=False)
    def refresh_legion_toggles(self, expected: dict[str, bool] | None = None) -> None:
        """Re-read every legion_cli state off the loop.

        `expected` names the states the Apply just asked for, by toggle key,
        so a write the firmware did not keep is reported rather than left
        as a marker the user has to notice.
        """
        if not self.is_running:
            return
        worker = get_current_worker()
        legion_cli, toggles = probe_legion_toggles()
        if worker.is_cancelled:
            return
        try:
            self.call_from_thread(self.apply_legion_states, legion_cli, toggles, expected)
        except RuntimeError:
            pass  # The app stopped while this read was in flight.

    def apply_legion_states(
        self, legion_cli: str | None, toggles: list[LegionToggle], expected: dict[str, bool] | None = None
    ) -> None:
        if not self.is_running:
            return
        self.legion_cli = legion_cli
        fresh = {toggle.feature: toggle for toggle in toggles}
        missed: list[str] = []
        for toggle in self.legion_toggles:
            state = fresh.get(toggle.feature)
            if state is not None:
                toggle.live = state.live
                toggle.unavailable_reason = state.unavailable_reason
                toggle.read_failure = state.read_failure
                toggle.detail = state.detail
            asked = (expected or {}).get(toggle.key)
            # A state that could not be read says nothing about the write.
            if asked is None or toggle.live is None or toggle.live == asked:
                continue
            note = f"{toggle.name}: asked {'on' if asked else 'off'}, reads {'on' if toggle.live else 'off'}"
            if toggle.feature in LEGION_REBOOT_FEATURES:
                self.log_apply("READBACK", f"{note}; it takes effect after a reboot")
            else:
                self.log_apply("READBACK", f"{note}; the write did not take")
                missed.append(toggle.name)
        if missed:
            self.notify(f"{name_list(missed)} did not take; see the apply log", severity="warning", timeout=10)
        # Only the toggle rows: this lands seconds after an Apply, and a
        # value row's refresh would replace whatever is being typed into it.
        self.refresh_toggle_rows()
        self.refresh_status_rail()

    def apply_commands(self) -> ApplyPlan:
        """Build the commands the selected modes call for, from ungated previews."""
        plan = ApplyPlan()
        # A control can be missing (its documentation row failed to parse) as
        # well as unavailable or gated; .get keeps all three on the ValueError path.
        previews = {s.key: s.value for s in self.settings if self.gate(s.key) is None}
        settings_by_key = {setting.key: setting for setting in self.settings}

        def planned(key: str) -> int | None:
            """Read a preview into the plan, so Apply knows what it wrote."""
            value = previews.get(key)
            if value is not None:
                plan.values[key] = value
            return value

        def add(arguments: list[str], input_text: str | None = None, keys: tuple[str, ...] = ()) -> None:
            """Queue a command with the planned keys a successful run makes live."""
            plan.commands.append((arguments, input_text))
            plan.carried.append({key: plan.values[key] for key in keys})

        def write(path, value, keys: tuple[str, ...] = ()) -> None:
            add(["sudo", "-n", "tee", str(path)], f"{value}\n", keys)

        intel_mode = self.options['intel-mode']
        if intel_mode in ('apply', 'undervolt'):
            selected = {key: planned(key) for key in intel_controls.SPECS}
            intel_keys = tuple(key for key, value in selected.items() if value is not None)
            if intel_mode == 'apply':
                intel_controls.updated_config(intel_controls.CONFIG.read_text(), selected)
                add(['sudo', '-n', sys.executable, str(Path(__file__).with_name('intel_controls.py'))], json.dumps(selected), intel_keys)
            else:
                add(intel_controls.python_undervolt_command(selected), None, intel_keys)

        profile = self.options['profile']
        if profile not in self.profiles:
            raise ValueError('Lenovo profile unavailable')
        write(PLATFORM_PROFILE, profile)
        if profile == 'custom':
            for key, attribute in LENOVO_ATTRIBUTES.items():
                value = planned(key)
                if value is None:
                    raise ValueError(f'{key} unavailable')
                write(LENOVO_ATTRIBUTE_ROOT / attribute / 'current_value', value, (key,))
        # Without intel_pstate there is no Turbo switch to write and nothing
        # caps a ceiling at base, so the driver's rated maximum is the limit.
        current_turbo = turbo_state()
        turbo = None if current_turbo is None else self.options['turbo'] == 'on'
        # Do not touch CPU policy state as a side effect of applying an
        # unrelated setting.  A changed Turbo choice is still an explicit
        # request, and needs its sysfs write before any changed ranges.
        if turbo is not None and turbo != current_turbo:
            write(NO_TURBO, 0 if turbo else 1)
        for group, policies in self.cpu_groups.items():
            name = cpu_group_name(group)
            low_key = f'cpu-{group}-core-minimum-frequency'
            high_key = f'cpu-{group}-core-maximum-frequency'
            low_setting = settings_by_key.get(low_key)
            high_setting = settings_by_key.get(high_key)
            # CPU ranges have no separate Apply mode.  They are therefore
            # written only when their preview differs from the live reading;
            # this prevents a GPU-only Apply from needlessly resetting CPU
            # policies.  An unknown reading remains safe to write.
            if (
                low_setting is not None and high_setting is not None
                and low_setting.live is not None and high_setting.live is not None
                and not low_setting.modified and not high_setting.modified
            ):
                continue
            low = planned(low_key)
            high = planned(high_key)
            if not policies or low is None or high is None or low > high:
                raise ValueError(f'{name}-core frequency range unavailable or invalid')
            try:
                floor, base, ceiling = cpu_group_limits(policies)
            except (OSError, ValueError) as error:
                raise ValueError(f'{name}-core driver limits unavailable: {error}') from error
            # With Turbo off the kernel caps at base, so a higher ceiling is a lie.
            maximum = base if turbo is False and base is not None else ceiling
            if low < floor or high > maximum:
                raise ValueError(
                    f'{name}-core range must stay within {floor}–{maximum} MHz with Turbo {self.options["turbo"]}'
                )
            range_keys = (low_key, high_key)
            for index, policy in enumerate(policies):
                # Lower the floor first so lowering a ceiling never crosses it.
                write(policy / 'scaling_min_freq', floor * 1000)
                write(policy / 'scaling_max_freq', high * 1000)
                # The group's range is live once its last policy holds it.
                write(policy / 'scaling_min_freq', low * 1000, range_keys if index == len(policies) - 1 else ())
        if self.nvidia_power_limit_available:
            power_limit = planned('nvidia-power-ceiling')
            if power_limit is None:
                raise ValueError('NVIDIA power limit unavailable')
            add(["sudo", "-n", "nvidia-smi", "-i", NVIDIA_GPU_INDEX, "-pl", str(power_limit)], None, ('nvidia-power-ceiling',))
        for domain, lock, reset in [('core', '-lgc', '-rgc'), ('memory', '-lmc', '-rmc')]:
            mode = self.options[f'{domain}-mode']
            if mode == 'keep':
                continue
            arguments = ["sudo", "-n", "nvidia-smi", "-i", NVIDIA_GPU_INDEX]
            keys: tuple[str, ...] = ()
            if mode == 'auto':
                arguments.append(reset)
            elif mode == 'locked':
                keys = (f'nvidia-{domain}-clock-minimum', f'nvidia-{domain}-clock-maximum')
                low, high = (planned(key) for key in keys)
                if low is None or high is None or low > high:
                    raise ValueError(f'GPU {domain} clock range invalid or unavailable')
                arguments.extend([lock, f'{low},{high}'])
            else:
                raise ValueError('Unknown GPU clock mode')
            add(arguments, None, keys)
        # Last, once the profile is set: switching profiles can reset fan state.
        toggles = self.planned_legion_toggles()
        enabled = {toggle.feature: toggle.name for toggle, target in toggles if target}
        # A feature that is on and left on Keep current would be turned off
        # behind the user's back by enabling its partner; Keep promises to
        # leave it alone, so they are asked to choose Disable for it.
        kept_on = {
            toggle.feature: toggle.name for toggle in self.legion_toggles
            if toggle.live and legion_target(self.options.get(toggle.key, "keep")) is None
        }
        for group in LEGION_EXCLUSIVE:
            asked = [enabled[feature] for feature in group if feature in enabled]
            if len(asked) > 1:
                # The second write would silently undo the first.
                raise ValueError(f"Only one of {name_list(asked)} can be enabled")
            held = [kept_on[feature] for feature in group if feature in kept_on]
            if asked and held:
                raise ValueError(
                    f"Enabling {asked[0]} would turn {name_list(held)} off: "
                    f"set {name_list(held)} to Disable rather than Keep current"
                )
        for toggle, target in toggles:
            plan.toggles[toggle.key] = target
            subcommand = f"{toggle.feature}-{'enable' if target else 'disable'}"
            add(["sudo", "-n", self.legion_cli, subcommand])
        return plan


if __name__ == "__main__":
    TunnerApp().run()
