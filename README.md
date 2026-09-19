# Tunner

A minimal TUI for the adjustable ranges documented in
`lenovo-tuning.md`. At startup it also reads the CPU frequency policies and
uses read-only NVIDIA queries to discover the current GPU clocks and
driver-supported clock steps. It does not execute, save, or generate
hardware-tuning commands until you choose **Apply** and confirm it.

```sh
sudo -v
.venv/bin/python app.py
```

Use the `−` and `+` buttons to preview values within their documented or
driver-reported ranges. CPU clocks use 100 MHz steps; GPU clocks use only
driver-reported supported steps. Each preview change is saved in
`last-values.json`; **Restore saved** loads it back into the interface.

**Apply** opens a confirmation dialog, then runs the documented CPU, Lenovo,
and NVIDIA commands. Authenticate first in the terminal with `sudo -v`; the
app intentionally uses non-interactive `sudo` and does not capture passwords.
The top of the screen refreshes CPU and GPU sensor telemetry every two seconds,
and the activity light reports recent changes and applies. If NVIDIA clock
telemetry cannot be read, its controls remain visible but disabled; this does
not imply that the GPU itself is absent.
