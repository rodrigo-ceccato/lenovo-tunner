"""Validated Intel package configuration; privileged entry point used by Apply."""
import sys

if __name__ == '__main__':
    # This file runs under sudo. Never leave a root-owned bytecode cache
    # behind in the project directory.
    sys.dont_write_bytecode = True

import json
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime

CONFIG = Path('/etc/intel-undervolt.conf')
RAPL_ROOT = Path('/sys/class/powercap/intel-rapl:0')
# The MSR power-limit field is 15 bits of 1/8 W, so 4095 W is the largest
# whole-watt value the hardware can store; firmware uses it as "unlimited".
SPECS = {
    'intel-pl1-sustained-power': (1, 4095, 'W', 1),
    'intel-pl1-time-window': (1, 128000, 'ms', 250),
    'intel-pl2-burst-power': (1, 4095, 'W', 1),
    'intel-pl2-time-window': (1, 128000, 'ms', 250),
    'intel-thermal-offset': (-30, 0, '°C', 1),
}
LIVE_DEFAULTS = {
    'intel-pl1-sustained-power': 45,
    'intel-pl1-time-window': 28000,
    'intel-pl2-burst-power': 55,
    'intel-pl2-time-window': 2000,
    'intel-thermal-offset': -10,
}
RAPL_SOURCES = {
    'intel-pl1-sustained-power': ('constraint_0_power_limit_uw', 1_000_000),
    'intel-pl1-time-window': ('constraint_0_time_window_us', 1_000),
    'intel-pl2-burst-power': ('constraint_1_power_limit_uw', 1_000_000),
    'intel-pl2-time-window': ('constraint_1_time_window_us', 1_000),
}
# Only leading spaces and tabs: `\s` would also swallow the blank lines that
# precede a directive under re.M and delete them on every rewrite.
POWER_LINE = re.compile(r'^[ \t]*power[ \t]+package[ \t]+([^\s#]+)[ \t]+([^\s#]+)[ \t]*(?:#.*)?$', re.M)
THERMAL_LINE = re.compile(r'^[ \t]*tjoffset[ \t]+(-?\d+)[ \t]*(?:#.*)?$', re.M)


def in_spec(key, value):
    low, high, _, _ = SPECS[key]
    return type(value) is int and low <= value <= high


def validate_values(values):
    for key, (low, high, _, _) in SPECS.items():
        value = values.get(key)
        if not in_spec(key, value):
            raise ValueError(f'{key} must be an integer between {low} and {high}, got {value!r}')
    if values['intel-pl1-sustained-power'] > values['intel-pl2-burst-power']:
        raise ValueError('Intel PL1 must not exceed PL2')


# The config's units and how many of the app's units each one is: it keeps
# time windows in seconds, the app in milliseconds.
CONFIG_UNIT_SCALE = {'W': 1, 's': 1000, '°C': 1}


def config_readings(text):
    """Each control's token exactly as the config writes it, with the config's unit."""
    power = POWER_LINE.findall(text)
    thermal = THERMAL_LINE.findall(text)
    if len(power) != 1 or len(thermal) != 1:
        raise ValueError('Expected one package power line and one thermal offset')
    readings = {'intel-thermal-offset': (thermal[0], '°C')}
    for term, token in zip(('pl2-burst', 'pl1-sustained'), power[0]):
        if ':' in token:
            raise ValueError('Explicit enable/disable flags require manual configuration')
        watts, seconds = token.split('/')
        readings[f'intel-{term}-power'] = (watts, 'W')
        readings[f'intel-{term.split("-")[0]}-time-window'] = (seconds, 's')
    return readings


def parse_config(text):
    """Each control as a whole number in the app's units."""
    return {
        key: round(float(token) * CONFIG_UNIT_SCALE[unit])
        for key, (token, unit) in config_readings(text).items()
    }


def updated_config(text, values):
    parse_config(text)
    validate_values(values)
    line = f"power package {values['intel-pl2-burst-power']}/{values['intel-pl2-time-window']/1000:g} {values['intel-pl1-sustained-power']}/{values['intel-pl1-time-window']/1000:g}"
    text = POWER_LINE.sub(line, text)
    return THERMAL_LINE.sub(f"tjoffset {values['intel-thermal-offset']}", text)


def python_undervolt_command(values, executable=sys.executable):
    """Build a live-only MSR command for the Python undervolt package."""
    validate_values(values)
    return [
        'sudo', '-n', executable, '-m', 'undervolt',
        '-p1', str(values['intel-pl1-sustained-power']),
        f"{values['intel-pl1-time-window'] / 1000:g}",
        '-p2', str(values['intel-pl2-burst-power']),
        f"{values['intel-pl2-time-window'] / 1000:g}",
        '--temp', str(100 + values['intel-thermal-offset']),
    ]


def read_rapl_limits():
    """Return each RAPL-backed control as an int, or None when unreadable."""
    readings = {}
    for key, (name, divisor) in RAPL_SOURCES.items():
        try:
            readings[key] = round(int((RAPL_ROOT / name).read_text()) / divisor)
        except (OSError, ValueError):
            readings[key] = None
    return readings


def live_control_state(saved=None):
    """Return (values, notes) for live-mode editing without a persistent config.

    Each RAPL-backed key is the live reading when it is readable and within
    spec. An unreadable one falls back to the caller's saved preview, then to
    LIVE_DEFAULTS. A reading outside the spec (firmware stores 4095.875 W as
    "unlimited", for instance) is clamped to the nearest bound and explained
    in `notes`, so the row stays editable while showing the raw reading
    instead of silently replacing it with a default. A PL1 above PL2 is kept
    as read; validate_values rejects it at Apply with a clear message, and
    the buttons let the user fix either side. The thermal offset has no
    unprivileged readback, so it is the saved preview or the default.
    """
    saved = saved or {}
    values = dict(LIVE_DEFAULTS)
    notes = {}
    for key, reading in read_rapl_limits().items():
        if reading is None:
            if in_spec(key, saved.get(key)):
                values[key] = saved[key]
        elif in_spec(key, reading):
            values[key] = reading
        else:
            low, high, unit, _ = SPECS[key]
            values[key] = max(low, min(high, reading))
            notes[key] = f'RAPL reads {reading} {unit}; shown clamped to {values[key]}'
    if in_spec('intel-thermal-offset', saved.get('intel-thermal-offset')):
        values['intel-thermal-offset'] = saved['intel-thermal-offset']
    return values, notes


def live_control_values(saved=None):
    """Editable live-mode values, always within spec."""
    return live_control_state(saved)[0]


def live_limits():
    lines = []
    for index, label in ((0, 'PL1'), (1, 'PL2')):
        try:
            watts = int((RAPL_ROOT / f'constraint_{index}_power_limit_uw').read_text()) / 1e6
            seconds = int((RAPL_ROOT / f'constraint_{index}_time_window_us').read_text()) / 1e6
            lines.append(f'{label}: {watts:g} W / {seconds:.3f} s')
        except (OSError, ValueError):
            lines.append(f'{label}: unavailable')
    return '\n'.join(lines)


def main():
    """Rewrite the persistent config and reapply it. Runs as root via sudo.

    Progress goes to stdout and the failure reason to stderr so the calling
    app can show both; nothing here writes into the project directory.
    """
    try:
        values = json.loads(sys.stdin.read())
        original = CONFIG.read_text()
        replacement = updated_config(original, values)
        backup = CONFIG.with_name(CONFIG.name + '.tunner-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
        shutil.copy2(CONFIG, backup)
    except (OSError, ValueError) as error:
        raise SystemExit(f'Intel apply not started: {error}')
    command = ['intel-undervolt', 'apply']
    print(f'Backup: {backup}')
    print(f'Running: {" ".join(command)}')
    try:
        CONFIG.write_text(replacement)
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    except Exception as error:
        CONFIG.write_text(original)
        detail = str(error)
        stderr = getattr(error, 'stderr', None)
        if stderr and stderr.strip():
            detail = f'{detail}: {stderr.strip()}'
        raise SystemExit(
            f'Intel apply failed ({detail}); config restored from {backup}. '
            'Hardware may be partially changed.'
        )
    print(f'Intel settings applied; backup: {backup}')


if __name__ == '__main__':
    main()
