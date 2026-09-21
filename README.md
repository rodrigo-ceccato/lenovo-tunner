# Tunner

Vibe-coded lenovo legion tunner

A minimal TUI for the adjustable ranges documented in
`lenovo-tuning.md`. At startup it reads the active Lenovo firmware values and
CPU frequency policies, and uses read-only NVIDIA queries to discover the
current GPU clocks, power limit, and driver-supported ranges. The probe runs in
the background behind a loading indicator, as does every later telemetry poll
and the Apply itself, so a slow driver never freezes the interface. It does
not execute, save, or generate hardware-tuning commands until you choose
**Apply** and confirm it.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo -v
.venv/bin/python app.py
```

Run the tests with `.venv/bin/python -m unittest`.

## The plan

The left pane groups the controls into CPU policy, Intel package limits,
Lenovo Custom Mode, and NVIDIA sections. Each section starts with the mode
selector that gates its rows (Turbo, the Intel apply mode, the Lenovo profile,
the GPU clock modes), and a row its selector does not apply shows why in place
of its range, such as *Needs Custom profile*. Every row is a typed field:
Tab to it and enter a number, or press `↑`/`↓` to step (`shift` steps ×10);
the `−`/`+` buttons and the mouse wheel over a focused row do the same. CPU
and GPU memory clocks use 100 MHz steps; a typed GPU core clock snaps to the
nearest driver-supported step when you press Enter or leave the field.

A row whose preview differs from its live reading is marked `●`, and the
**Planned limits** readout shows the reading it would replace. `ctrl+r` puts
one row back; **Revert to live** (`v`) puts every editable row back. Each
change is saved to `last-values.json` after a moment's pause; **Restore
saved** (`r`) loads it back into the interface, including the mode choices.

CPU policies are grouped by base frequency, so a hybrid CPU gets independent
P-core and E-core floors and ceilings and a uniform CPU gets one pair; the
bounds come from the driver's `cpuinfo_min_freq`/`cpuinfo_max_freq`. With
Turbo off the ceiling must stay at or below the base frequency, because the
kernel caps there anyway. For a 4 GHz P-core cap on an i9-13900HX, select
Turbo On and set the P-core maximum to 4000 MHz.

Keys: `a` Apply · `r` Restore saved · `v` Revert to live · `c`/`g` toggle CPU
or GPU stress · `t` show or hide the status pane · `l` jump to the Apply log ·
`q` quit. `ctrl+p` opens the command palette, which lists the same actions and
the theme switcher; the interface uses the terminal theme's colours, so light
themes work too.

## Live status

The right pane refreshes live temperatures, driver-reported GPU thermal
margin, enforced GPU power ceiling, utilization, clocks, and clock slowdown
reasons every two seconds, with two-minute sparklines for CPU and GPU
temperature, GPU power, and GPU core clock, so a stress run's trend is visible
at a glance. The thermal gauges scale to the lowest planned target that the
selected modes would write (Intel offset, Lenovo firmware target, or the
driver's slowdown point); with no target planned they scale to 100°C/90°C.
CPU policy ranges and average clocks are grouped per core type; rated boost
speeds are labeled separately from live limits. Intel thermal thresholds and
active GPU clock locks are marked unverified; preview targets are not used as
live thermal or power limits.

Below 120 columns the pane no longer fits: it is hidden and a one-line strip
under the header keeps the essentials (temperatures, GPU power, clocks,
profile, Turbo). `t` then swaps the whole window between the plan and the
pane. Below 80 columns the range hints are hidden too, to leave room for
setting names. Everything returns automatically when the terminal is widened.

## Modes

The Lenovo profile selector uses firmware-reported choices. Firmware sliders
apply only with Custom selected; stock profiles skip those writes. GPU core
and memory modes are independent: Keep current mode, Automatic (reset locks),
or Locked to the preview range. Reset buttons select Automatic for the next
confirmed Apply. Modes and profile choices are saved alongside preview values.
Older saved shared CPU limits are not mapped onto the per-core controls.

Intel package controls expose PL1 sustained watts and its time window, PL2
burst watts and its time window, and the thermal offset (−10°C corresponds
to a 90°C target on this CPU). Windows are entered in milliseconds; the CPU
rounds them to supported values. They are averaging windows, not exact timers.
The right pane reads back live RAPL power limits and windows.

Select **Apply persistently via intel-undervolt** to include these in the
confirmed Apply. This updates `/etc/intel-undervolt.conf`, saves a timestamped
backup beside it, and runs `intel-undervolt apply`, including the existing voltage
offsets. On failure the original configuration is restored, but hardware may
have been partially changed. Settings can be reapplied by your existing service
at boot. The app does not enable that service. UI ranges are application bounds,
not a guarantee that firmware accepts every value. Lenovo and Intel limits can
both constrain performance; an unsupported Lenovo time-window range is not editable.

The alternative **Apply live via Python undervolt** mode uses the `undervolt`
Python package to write PL1, PL2, their windows, and the thermal target directly
through Intel MSRs. It does not change voltage offsets or configuration files,
and its limits are not persistent across reboot. This mode requires the `msr`
kernel module and the project dependency installed from `requirements.txt`.
Selecting it fills any Intel row that has no configuration value with the live
RAPL limit; the thermal offset has no unprivileged readback, so it comes from
the last saved preview or the −10 default. A live reading the hardware stores
as "unlimited" is clamped to the 4095 W bound and the raw reading is shown in
place of the range.

## Apply

**Apply** first builds the plan; a plan the app rejects (a PL1 above PL2, a
ceiling above the Turbo cap, an unavailable value) is reported in the log and
never reaches a dialog. Otherwise a confirmation dialog lists every command
exactly as it will run, warns if `sudo` has no cached credentials, and
requires **Apply now** (`Escape` cancels). Authenticate first in the terminal
with `sudo -v`; the app intentionally uses non-interactive `sudo` and does not
capture passwords, and the notice under the title says so when the check
fails at startup. The confirmed commands run in the background with the Apply,
Restore, and Revert buttons disabled until they finish. A successful Apply
makes the written previews the new live readings.

The **Apply command log** shows the commands attempted by the latest Apply and
whether each one succeeded, with `APPLIED` in green and `FAILED` in red. A
failed command's error output (for example `sudo: a password is required`) is
shown under it, and what a command reported on success, such as the confirmed
NVIDIA power limit or the Intel helper's backup path, is listed beneath its
line. The log scrolls into view when an Apply ends; `l` jumps to it at any
time. Every command that changes hardware or launches a workload is also
appended to `log.txt` with a timestamp and a `WRITE` or `EXEC` label. Read-only
telemetry and probes are logged only when `TUNNER_LOG_READS=1` is set, since
they would otherwise add tens of thousands of lines a day.

If NVIDIA clock telemetry cannot be read, its controls remain visible but
disabled; this does not imply that the GPU itself is absent.

## Stress workloads

The **Stress CPU** and **Stress GPU** buttons (`c`, `g`) start separate
workloads only when pressed; pressing again stops the workload, and closing
Tunner stops both. CPU stress uses Python's `multiprocessing` module to load
every logical CPU. GPU stress uses the optional [CuPy](https://cupy.dev/)
Python library to run CUDA matrix multiplications. Install the CuPy wheel
matching your CUDA runtime before using GPU stress. CUDA 12 users can run
`pip install -r requirements-gpu-cuda12.txt`; for other CUDA versions, install
the corresponding CuPy package instead. A startup failure is shown in the app
and resets the stress button.
