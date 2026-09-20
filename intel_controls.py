"""Validated Intel package configuration; privileged entry point used by Apply."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime

CONFIG = Path('/etc/intel-undervolt.conf')
SPECS = {
    'intel-pl1-sustained-power': (1, 157, 'W', 1),
    'intel-pl1-time-window': (1, 128000, 'ms', 250),
    'intel-pl2-burst-power': (1, 157, 'W', 1),
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

def validate_values(values):
    for key, (low, high, _, _) in SPECS.items():
        value = values.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
    if values['intel-pl1-sustained-power'] > values['intel-pl2-burst-power']:
        raise ValueError('Intel PL1 must not exceed PL2')

def parse_config(text):
    power = re.findall(r'^\s*power\s+package\s+([^\s#]+)\s+([^\s#]+)\s*(?:#.*)?$', text, re.M)
    thermal = re.findall(r'^\s*tjoffset\s+(-?\d+)\s*(?:#.*)?$', text, re.M)
    if len(power) != 1 or len(thermal) != 1:
        raise ValueError('Expected one package power line and one thermal offset')
    values = {'intel-thermal-offset': int(thermal[0])}
    for term, token in zip(('pl2-burst', 'pl1-sustained'), power[0]):
        if ':' in token:
            raise ValueError('Explicit enable/disable flags require manual configuration')
        watts, seconds = token.split('/')
        values[f'intel-{term}-power'] = round(float(watts))
        values[f'intel-{term.split("-")[0]}-time-window'] = round(float(seconds) * 1000)
    return values

def updated_config(text, values):
    parse_config(text)
    validate_values(values)
    line = f"power package {values['intel-pl2-burst-power']}/{values['intel-pl2-time-window']/1000:g} {values['intel-pl1-sustained-power']}/{values['intel-pl1-time-window']/1000:g}"
    text = re.sub(r'^\s*power\s+package[^\n]*', line, text, flags=re.M)
    return re.sub(r'^\s*tjoffset[^\n]*', f"tjoffset {values['intel-thermal-offset']}", text, flags=re.M)

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

def live_control_values():
    """Return editable live-mode values without requiring a persistent config."""
    values = LIVE_DEFAULTS.copy()
    root = Path('/sys/class/powercap/intel-rapl:0')
    paths = {
        'intel-pl1-sustained-power': (root / 'constraint_0_power_limit_uw', 1_000_000),
        'intel-pl1-time-window': (root / 'constraint_0_time_window_us', 1_000),
        'intel-pl2-burst-power': (root / 'constraint_1_power_limit_uw', 1_000_000),
        'intel-pl2-time-window': (root / 'constraint_1_time_window_us', 1_000),
    }
    for key, (path, divisor) in paths.items():
        try:
            value = round(int(path.read_text()) / divisor)
        except (OSError, ValueError):
            continue
        low, high, _, _ = SPECS[key]
        if low <= value <= high:
            values[key] = value
    if values['intel-pl1-sustained-power'] > values['intel-pl2-burst-power']:
        values.update({key: LIVE_DEFAULTS[key] for key in (
            'intel-pl1-sustained-power', 'intel-pl2-burst-power'
        )})
    return values

def live_limits():
    lines = []
    root = Path('/sys/class/powercap/intel-rapl:0')
    for index, label in ((0, 'PL1'), (1, 'PL2')):
        try:
            watts = int((root / f'constraint_{index}_power_limit_uw').read_text()) / 1e6
            seconds = int((root / f'constraint_{index}_time_window_us').read_text()) / 1e6
            lines.append(f'{label}: {watts:g} W / {seconds:.3f} s')
        except (OSError, ValueError):
            lines.append(f'{label}: unavailable')
    return '\n'.join(lines)

def main():
    values = json.loads(sys.stdin.read())
    original = CONFIG.read_text()
    replacement = updated_config(original, values)
    backup = CONFIG.with_name(CONFIG.name + '.tunner-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
    shutil.copy2(CONFIG, backup)
    try:
        CONFIG.write_text(replacement)
        subprocess.run(['intel-undervolt', 'apply'], check=True, capture_output=True, text=True, timeout=10)
    except Exception:
        CONFIG.write_text(original)
        raise RuntimeError(f'Intel apply failed; config restored from {backup}. Hardware may be partially changed.')
    print(f'Intel settings applied; backup: {backup}')

if __name__ == '__main__':
    main()
