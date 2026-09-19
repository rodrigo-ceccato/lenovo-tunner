# Farpoint Lenovo tuning reference

This reference covers the CPU, Lenovo firmware, and NVIDIA controls available
on Farpoint (Intel Core i9-13900HX and RTX 4090 Laptop GPU).

Run these commands as the desktop user. Commands using `sudo` change live
hardware settings. Most NVIDIA, CPU governor, and CPU frequency settings reset
at reboot. The `intel-undervolt` service reapplies `/etc/intel-undervolt.conf`
at boot and resume.

Define the Lenovo firmware attribute directory before using the Lenovo rows:

```sh
A=/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes
```

| Name | Description | Command |
|---|---|---|
| Read all active Intel limits | Shows voltage offsets, CPU package power windows, and thermal offset. | `sudo intel-undervolt read` |
| Enable CPU Turbo | Allows clocks above base frequency. Required before applying a 3 GHz CPU ceiling. Temporary until reboot. | `printf '%s\n' 0 \| sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo` |
| Disable CPU Turbo | Restores the current no-Turbo behavior: P-cores max at 2.2 GHz and E-cores max at 1.6 GHz. | `printf '%s\n' 1 \| sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo` |
| CPU maximum frequency | With Turbo enabled, caps all CPU policies at 3 GHz. Temporary until reboot. | `sudo cpupower frequency-set --max 3GHz` |
| CPU minimum frequency | Sets the lowest allowed CPU clock. Normally leave this at 800 MHz. | `sudo cpupower frequency-set --min 800MHz` |
| CPU governor: performance | Favors performance over efficiency. | `sudo cpupower frequency-set --governor performance` |
| CPU governor: powersave | Lets Intel P-state favor efficiency; Turbo can still work if enabled. | `sudo cpupower frequency-set --governor powersave` |
| Inspect CPU clock policy | Shows CPU frequency ceilings, governor, and boost state. | `cpupower frequency-info` |
| CPU short/sustained package watts | Persistent Intel package-power limit. In the config, the first pair is short watts/seconds and the second pair sustained watts/seconds. Current setting: `55/2 45/28`. | `sudoedit /etc/intel-undervolt.conf`, edit `power package 55/2 45/28`, then `sudo intel-undervolt apply` |
| CPU thermal throttle point | Persistent Intel thermal offset. `tjoffset -10` targets 90°C on a 100°C CPU; a more-negative value lowers the target. | `sudoedit /etc/intel-undervolt.conf`, edit `tjoffset -10`, then `sudo intel-undervolt apply` |
| Lenovo profile choices | Lists stock and Custom Mode profiles. | `cat /sys/firmware/acpi/platform_profile_choices` |
| Switch Lenovo profile | `custom` is required before changing Lenovo power attributes. Return to `performance` for the stock Lenovo profile. | `printf '%s\n' custom \| sudo tee /sys/firmware/acpi/platform_profile` or `printf '%s\n' performance \| sudo tee /sys/firmware/acpi/platform_profile` |
| Lenovo CPU cross-load limit | Custom Mode CPU limit; allowed range: 20–110 W. The separate Intel 45 W sustained package limit still applies if it is lower. | `printf '%s\n' 55 \| sudo tee "$A/ppt_cpu_cl/current_value"` |
| Lenovo CPU sustained limit | Custom Mode firmware PL1/SPL; allowed range: 30–140 W. | `printf '%s\n' 70 \| sudo tee "$A/ppt_pl1_spl/current_value"` |
| Lenovo CPU burst limit | Custom Mode firmware PL2/SPPT; allowed range: 55–190 W. | `printf '%s\n' 119 \| sudo tee "$A/ppt_pl2_sppt/current_value"` |
| Lenovo CPU temperature target | Custom Mode firmware CPU temperature target; allowed range: 85–100°C. | `printf '%s\n' 97 \| sudo tee "$A/cpu_temp/current_value"` |
| NVIDIA power ceiling | GPU power ceiling; allowed range: 5–175 W. Current ceiling: 175 W. | `sudo nvidia-smi -i 0 -pl 175` |
| NVIDIA core-clock range | Locks the GPU graphics core to a supported MHz range. Obtain valid clock pairs before setting one. | `sudo nvidia-smi -i 0 -lgc <minMHz>,<maxMHz>` |
| Reset NVIDIA core clocks | Removes a graphics-core clock lock. | `sudo nvidia-smi -i 0 -rgc` |
| NVIDIA memory-clock range | Locks memory clocks only; it does not limit GPU core clock. | `sudo nvidia-smi -i 0 -lmc <minMHz>,<maxMHz>` |
| Reset NVIDIA memory clocks | Removes a memory-clock lock. | `sudo nvidia-smi -i 0 -rmc` |
| Supported NVIDIA clocks | Lists supported GPU graphics/memory clock combinations. | `nvidia-smi -q -d SUPPORTED_CLOCKS` |
| Lenovo GPU temperature target | Custom Mode firmware GPU temperature target; allowed range: 75–87°C. The current maximum of 87°C is being reached under CS2 load. | `printf '%s\n' 87 \| sudo tee "$A/gpu_temp/current_value"` |
| GPU live telemetry | Distinguishes a thermal, power, or utilization limit while a game is active. | `watch -n 1 'nvidia-smi --query-gpu=power.draw,temperature.gpu,utilization.gpu,clocks.current.graphics,clocks.current.memory --format=csv,noheader'` |
| Whole-system temperatures and fans | Shows CPU package, GPU, and Legion fan readings. | `sensors` |

Do not change `gpu_nv_ac_offset`, `gpu_nv_ctgp`, or `gpu_nv_ppab`: the
firmware exposes no writable range for those vendor-specific attributes on
this machine.
