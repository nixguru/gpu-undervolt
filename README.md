# GPU Undervolt (Linux, NVIDIA) - USE AT YOUR OWN RISK  
Linux GPU Undervolt Tool for Modern NVIDIA GPUs  
> **One-shot or daemonized undervolting & efficiency tool** for NVIDIA GPUs on Linux.  
> Verified on Linux Mint 22.2 with NVIDIA 580.xx and an **ASUS ROG Strix RTX 3090**.

## Read This First!  
By using this tool, you are responsible for any and all consequences, intended or unintended. The author of this tool **BEARS NO RESPONSIBILITY** for your use or misuse of this tool. Please read this document in full before using.  

## Why this exists
Undervolting lets you hit the **same performance** at **lower voltage**, cutting **heat**, **noise**, and **power draw**. On Windows, you may use tools like **Afterburner**.  On Linux, NVIDIA does not expose a direct “set voltage” control, but we can **mimic undervolting** by:
1) **Shifting the voltage/frequency (V/F) curve up** with a **core clock offset**, then  
2) **Locking a target maximum graphics clock** (so that clock is reached at a lower voltage than stock).

This repo packages that into a single tool that can run either as a **one-shot** (apply once, persist for the session) or as a **daemon** (auto‑enable under load, auto‑revert at idle, with hysteresis and optional ramping). It also supports **VRAM (memory) overclock** and an optional **power limit** cap.


## What it does (in plain English)
- **Core undervolt**: Applies a **core offset** (raises allowed MHz at each voltage point) and **locks a max clock**; the GPU then sustains that clock at a **lower voltage** ⇒ better efficiency.
- **Memory OC (optional)**: Adds a VRAM offset for more bandwidth (watch junction temps on GDDR6X).
- **Power limit (optional)**: Set a watt cap; many users keep PL at stock or slightly higher for transient headroom.
- **One-shot mode**: Apply once; stays until reboot or manual revert.
- **Daemon mode**: Only enable the undervolt while under real load; automatically revert at idle. Includes hysteresis timers and optional ramp to the target clock.


## Quick start (RTX 3090 Strix baseline)
> **These are examples. TUNE FOR YOUR GPU AND WORKLOAD!**
- **Target clock**: `1800–1980 MHz`
- **Core offset**: `+120–150 MHz`
- **Memory offset**: `+500 MHz` (then try +750/+1000 if temps are good)
- **Power limit**: stock (or +5–10% for surge headroom); no temp cap changes

**One-shot (stock PL):**
```bash
# From your Xorg desktop terminal, allow root to talk to your X session once:
# this may not be necessary, you can test without
xhost +si:localuser:root

sudo gpu_undervolt.py --mode oneshot --index 0 \
  --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 --verify
```

**Daemon (auto-toggle, stock PL):**
```bash
sudo gpu_undervolt.py --mode daemon --index 0 \
  --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 \
  --transition-clock 1200 \
  --on-hold 2 --off-hold 8
```

Prefer more surge headroom? Add e.g. `--power-limit 380`.  
Prefer instant apply (no step ramp)? Omit `--ramp`.


---

## Table of contents
- [Requirements](#requirements)
- [Install](#install)
- [Enable Coolbits (for offsets)](#enable-coolbits-for-offsets)
- [Usage](#usage)
  - [One-shot mode](#one-shot-mode)
  - [Daemon mode](#daemon-mode)
  - [Verifying and monitoring](#verifying-and-monitoring)
  - [Reverting](#reverting)
- [Systemd services (optional)](#systemd-services-optional)
- [Tuning guide](#tuning-guide)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Security notes](#security-notes)
- [Command reference](#command-reference)
- [Uninstall](#uninstall)
- [Roadmap](#roadmap)
- [License](#license)


## Requirements
- Linux (Xorg for offsets; Wayland users can switch to Xorg at login screen).
- NVIDIA driver **555+** (580.xx tested).
- Tools:
  - `nvidia-smi` (always required).
  - `nvidia-settings` (**required if you want core/memory offsets**; needs Xorg + Coolbits).
- Root privileges (`sudo`) to set clocks/PL.
- For multi-GPU rigs: pick the right `--index` (see `nvidia-smi -L`).


## Install
```bash
# Put the script somewhere convenient, then:
chmod +x gpu_undervolt.py
sudo install -m 0755 gpu_undervolt.py /usr/local/bin/gpu_undervolt.py
```


## Enable Coolbits (for offsets)
Core and memory offsets on GeForce require Xorg with **Coolbits** enabled.

Create `/etc/X11/xorg.conf.d/20-nvidia-coolbits.conf`:
```bash
sudo mkdir -p /etc/X11/xorg.conf.d
sudo tee /etc/X11/xorg.conf.d/20-nvidia-coolbits.conf >/dev/null <<'EOF'
Section "Device"
    Identifier  "NvidiaGPU"
    Driver      "nvidia"
    Option      "Coolbits" "28"  # 24=OC+Fan, +4=PowerMizer; 28 is common
EndSection
EOF
```

Reboot (or restart display manager), then confirm:
```bash
nvidia-settings                 # you should see OC sliders
DISPLAY=:0 nvidia-settings -q [gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels \
                             -q [gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels
```

If you will run the tool via `sudo`, allow root to access your X session:
```bash
# run this from your logged-in Xorg desktop terminal
# this may not be necessary, you can test without
xhost +si:localuser:root
```


## Usage

### One-shot mode
Applies your settings once and **stays active** until reboot or manual revert. GPU still idles to `--min-clock` (e.g., 210 MHz).

```bash
sudo gpu_undervolt.py --mode oneshot --index 0 \
  --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 --verify
```

Optional: raise PL a bit for transient headroom (example 380 W):
```bash
sudo gpu_undervolt.py --mode oneshot --index 0 \
  --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 \
  --power-limit 380 --verify
```


### Daemon mode
Auto‑enables undervolt + clock lock when **under load** and reverts at **idle**. Avoids high offsets being active when you’re just browsing.

```bash
sudo gpu_undervolt.py --mode daemon --index 0 \
  --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 \
  --transition-clock 1200 \
  --on-hold 2 --off-hold 8
```

- `--transition-clock`: clock threshold that counts as “load.”  
- `--on-hold` / `--off-hold`: hysteresis timers to prevent flapping.  
- Omit `--ramp` to apply instantly; add `--ramp --ramp-step 15 --ramp-sleep 0.2` to step up smoothly.


### Verifying and monitoring
```bash
# live view (every 0.5s):
watch -n0.5 'nvidia-smi -i 0 --query-gpu=clocks.gr,power.draw,temperature.gpu,pstate --format=csv'

# detailed clocks:
nvidia-smi -i 0 -q -d CLOCK

# voltage (if your driver/GPU exposes it):
nvidia-smi -i 0 -q -d VOLTAGE

# offsets (Xorg + Coolbits):
DISPLAY=:0 nvidia-settings -q [gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels \
                           -q [gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels
```


### Reverting
```bash
sudo nvidia-smi -i 0 -rgc
DISPLAY=:0 nvidia-settings -a [gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels=0 \
                            -a [gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels=0
```


## Systemd services (optional)

Create `/etc/gpu-undervolt.conf` (edit for your system):
```bash
sudo tee /etc/gpu-undervolt.conf >/dev/null <<'EOF'
# Adjust these for your environment
DISPLAY=:0
XAUTHORITY=/home/YOUR_USER/.Xauthority
EXTRA_ARGS="--index 0 --use-offsets --display :0 \
  --core-offset 150 --memory-offset 500 \
  --min-clock 210 --target-clock 1860 \
  --transition-clock 1200 --on-hold 2 --off-hold 8"
EOF
```

`/etc/systemd/system/gpu-undervolt.service` (daemon):
```ini
[Unit]
Description=GPU Undervolt Daemon
After=graphical.target display-manager.service
Wants=display-manager.service

[Service]
Type=simple
EnvironmentFile=-/etc/gpu-undervolt.conf
Environment=DISPLAY=%E{DISPLAY}
Environment=XAUTHORITY=%E{XAUTHORITY}
ExecStart=/usr/local/bin/gpu_undervolt.py --mode daemon $EXTRA_ARGS
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/gpu-undervolt-oneshot.service`:
```ini
[Unit]
Description=GPU Undervolt One-Shot
After=graphical.target display-manager.service
Wants=display-manager.service

[Service]
Type=oneshot
EnvironmentFile=-/etc/gpu-undervolt.conf
Environment=DISPLAY=%E{DISPLAY}
Environment=XAUTHORITY=%E{XAUTHORITY}
ExecStart=/usr/local/bin/gpu_undervolt.py --mode oneshot $EXTRA_ARGS
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-undervolt.service
# or:
# sudo systemctl enable --now gpu-undervolt-oneshot.service
```

> If `nvidia-settings` errors in a service, you likely need correct `DISPLAY` and `XAUTHORITY`. You may also need to run `xhost +si:localuser:root` from the desktop session.


## Tuning guide
- Start conservative: `--target-clock 1800–1860`, `--core-offset 120–150`, `--memory-offset 500`.
- If rock solid and cool, try `--target-clock 1830–1890` or `--memory-offset 750/+1000`.
- If instability appears, first **reduce core offset** (15 MHz steps), then lower the target clock.
- For AI training (long VRAM stress), monitor **Memory Junction temp**; back down VRAM OC if it runs hot.
- For surge stability (e.g., RT spikes, heavy AI bursts), keep PL **stock or slightly higher**; you can tighten later if you prove it’s unnecessary.


## Troubleshooting
- **Offsets do nothing / can’t set**  
  You’re on Wayland or Coolbits isn’t enabled. Switch to **Xorg**, add the Coolbits file, reboot. Make sure `--display :0` and that root can access your X session (`xhost +si:localuser:root`).

- **`-lgc` (lock graphics clocks) fails**  
  Enable persistence: `sudo nvidia-smi -pm 1`. Some laptop/hybrid designs restrict `-lgc`.

- **Daemon keeps toggling (flapping)**  
  Lower `--transition-clock` (e.g., 1200), increase `--off-hold` (e.g., 8–10), remove `--ramp`. Menus/frame limiters can cause clock dips; hysteresis fixes that.

- **Voltage doesn’t show / is N/A**  
  Some driver/GPU combos don’t expose voltage in `nvidia-smi`. That’s normal; you can still infer efficiency from power draw and sustained clocks/temps.

- **I’m headless / no Xorg**  
  You can still use locked clocks + PL without offsets (less effective). An NVML-only backend for offsets is planned when broadly available.

- **My settings don’t persist after reboot**  
  One-shot is per-boot by design. Use the systemd service to apply automatically after login/display comes up.

- **Clocks/offsets apply but game crashes**  
  Back down **core offset** first (e.g., from +150 → +135), then lower target clock a notch. Each GPU is silicon-lottery unique.


## FAQ
**Is this compatible with my Linux distribution?**  
This should work with most modern Linux distributions that meet the requirements above. There are atomic (immutable) distributions like Bazzite, popular with gamers moving from Windows, but I have never tested these before. YMMV.  

**Does undervolting require the daemon?**  
No. One-shot is sufficient if you want a fixed target all the time. The **daemon** just enables it **only under load** and reverts at idle.  You may test using **sudo** in a shell first.

**Why do I need Xorg + Coolbits for offsets?**  
On GeForce, the reliable userland path for core/mem offsets is `nvidia-settings` with Coolbits. (NVML per-P-state offset APIs exist, but aren’t consistently exposed across Python bindings yet).

**Can I keep PL (Power Limit) stock (or raise it) while undervolting?**  
Yes. Undervolting and PL are independent. Many users keep PL at **stock or slightly higher** to prevent transient power-limit throttling/crashes during spikes. This seems counter-intuitive to keep this the same or even raise it, but it does not mean your GPU will draw more power, only that it **can** if it needs to. Undervolting will lower power draw at the same clock, but sometimes very heavy workloads can cause stability issues when PL is lowered.  

**If I lower the core offset, what happens to voltage at the same clock?**  
Lower offset ⇒ the V/F curve is **lower** ⇒ the driver needs **more voltage** for the same locked clock (higher watts/heat).

**What’s a good `--transition-clock`?**  
Pick a value that represents “real work” for your GPU—e.g., **1200–1500 MHz** for a 3090. Too high will flap around menu screens; add hysteresis with `--off-hold` to avoid chatter.

**Can I use this on laptops or Optimus systems?**  
Sometimes `-lgc` or offsets are blocked or behave differently. Desktop dGPUs are the primary target.

**Will undervolting reduce lifespan?**  
Generally, running **cooler** and **lower voltage** is gentler on silicon. As always, you’re applying tweaks at your own risk—test stability thoroughly.

**What inspired this tool?**  
This tool was inspired by another (https://github.com/jacklul/nvml-scripts), but the approach is different.  
Read more about this approach to undervolting here: https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/236

**What GPUs will this work on?**  
It works on a 3xxx-series (Ampere), and is presumed to work on anything newer. Please provide feedback.  

**How do I more easily test undervolting and memory overclocking options?**  
Run the script from the command-line in **daemon** mode **first**. You simply press **CTRL-C** and the script will exit and return to your default state. If your system crashes, you can simply reboot and try again.

## Security notes
- `xhost +si:localuser:root` authorizes **only the local root** to your X session. Do **not** use `xhost +` (which opens to all). Revoke with `xhost -si:localuser:root` if you like.
- Running as root is necessary to set clocks/PL. Limit access to the machine accordingly.


## Command reference
```
--help                        show this help message and exit
--version                     show program's version number and exit
--index N                     GPU index (default: 0)
--display :0                  X display to talk to nvidia-settings (required for offsets)
--mode {oneshot,daemon}       run once (persistent for session) or continuous toggler
--min-clock MHz               locked min graphics clock (default: 210)
--target-clock MHz            locked max graphics clock (required)
--transition-clock MHz        daemon: enable undervolt when clk >= this (default: target-300)
--use-offsets                 enable core/memory offsets via nvidia-settings (Coolbits)
--core-offset MHz             core clock offset (e.g., 150)
--memory-offset MHz           memory transfer rate offset (e.g., 500)
--power-limit POWER_LIMIT     Optional power limit in W (nvidia-smi -pl)
--temp-limit TEMP_LIMIT       daemon: thermal guard max °C (will reduce max clock if exceeded)
--poll POLL                   daemon: poll interval seconds (default: 0.5)
--on-hold seconds             daemon: time above threshold before enabling (default: 1.0)
--off-hold seconds            daemon: time below threshold before disabling (default: 2.0)
--ramp                        daemon: ramp max clock in steps
--ramp-step MHz               daemon: step size for ramp (default: 15)
--ramp-sleep seconds          daemon: sleep per ramp step (default: 0.2)
--power-limit W               optional power cap via nvidia-smi -pl (stock or slight + is fine)
--verify                      print status after applying (oneshot)
--dry-run                     print commands only; do not apply changes
--quiet                       less verbose
```


## Uninstall
```bash
sudo systemctl disable --now gpu-undervolt.service gpu-undervolt-oneshot.service
sudo rm -f /etc/systemd/system/gpu-undervolt*.service
sudo rm -f /etc/gpu-undervolt.conf
sudo rm -f /usr/local/bin/gpu_undervolt.py
sudo systemctl daemon-reload
```


## Roadmap
- TBD NVML-backend for offsets (no Xorg dependency) when broadly available via `pynvml`.
- TBD power/utilization-based trigger (instead of clock-based).
- TBD fan-curve integration via `nvidia-settings`.


## License
MIT
