# Tunner

Vibe-coded lenovo legion tunner

A minimal TUI for the adjustable ranges documented in
`lenovo-tuning.md`. At startup it reads the active Lenovo firmware values and
CPU frequency policies, and uses read-only NVIDIA queries to discover the
current GPU clocks, power limit, and driver-supported ranges. It does not execute, save, or generate
hardware-tuning commands until you choose **Apply** and confirm it.

```sh
sudo -v
.venv/bin/python app.py
```

Use the `−` and `+` buttons to preview values within their documented or
driver-reported ranges. CPU and GPU memory clocks use 100 MHz steps; GPU core
clocks use driver-reported supported steps. The left pane groups the controls into CPU,
Lenovo Custom Mode, and NVIDIA sections. The persistent right pane refreshes
live temperatures, driver-reported GPU thermal margin, enforced GPU power
ceiling, utilization, clocks, and clock slowdown reasons. CPU policy ranges
and average clocks are grouped separately for P/E cores on the i9-13900HX;
rated boost speeds are labeled separately from live limits. On the i9-13900HX,
P/E-core floors and ceilings are independent (up to 5.4/3.9 GHz with Turbo on).
For a 4 GHz P-core cap, select Turbo On and set the P-core maximum to 4000 MHz.
Turbo Off requires ceilings at or below the base speeds (2.2/1.6 GHz).
Scroll the right pane to reach GPU details and planned limits on short terminals.
Below 120 columns, the status pane is hidden to keep tuning controls readable.
Below 80 columns, range hints are also hidden to leave room for setting names.
Both return automatically when the terminal is widened.
Intel thermal thresholds and active GPU clock locks are marked unverified;
preview targets are not used as live thermal or power limits. Each
preview change is saved in `last-values.json`; **Restore saved** loads it back
into the interface.

The Lenovo profile selector uses firmware-reported choices. Firmware sliders
apply only with Custom selected; stock profiles skip those writes. GPU core
and memory modes are independent: Keep current mode, Automatic (reset locks),
or Locked to the preview range. Reset buttons select Automatic for the next
confirmed Apply. Modes and profile choices are saved alongside preview values.
Older saved shared CPU limits are not mapped onto the new P/E controls.

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

**Apply** opens a confirmation dialog, then runs the documented CPU, Lenovo,
and NVIDIA commands. Authenticate first in the terminal with `sudo -v`; the
app intentionally uses non-interactive `sudo` and does not capture passwords.
The top of the screen refreshes CPU and GPU sensor telemetry every two seconds,
and the activity light reports recent changes and applies. If NVIDIA clock
telemetry cannot be read, its controls remain visible but disabled; this does
not imply that the GPU itself is absent.

The **Apply command log** shows the commands attempted by the latest Apply and
whether each one succeeded. A failed command's error output (for example
`sudo: a password is required`) is shown under it, and what a command reported
on success, such as the confirmed NVIDIA power limit or the Intel helper's
backup path, is listed beneath its line. Every external command Tunner
launches, including read-only telemetry and probes, is also appended to
`log.txt` with a timestamp and a `READ`, `WRITE`, or `EXEC` label.

The **Stress CPU** and **Stress GPU** buttons start separate workloads only when
clicked; clicking an active button stops its workload, and closing Tunner stops
both. CPU stress uses Python's `multiprocessing` module to load every logical
CPU. GPU stress uses the optional [CuPy](https://cupy.dev/) Python library to
run CUDA matrix multiplications. Install the CuPy wheel matching your CUDA
runtime before using GPU stress. CUDA 12 users can run
`pip install -r requirements-gpu-cuda12.txt`; for other CUDA versions, install
the corresponding CuPy package instead. A startup failure is shown in the app
and resets the stress button.
