#!/usr/bin/env python3
"""
gpu_undervolt.py
Undervolt/efficiency tool for NVIDIA GPUs (Ampere+). Works as one-shot or daemon.

Features
- Lock graphics clocks (min,max) via nvidia-smi
- Apply core/memory offsets via nvidia-settings (Coolbits required)
- Optional power cap (nvidia-smi -pl)
- Daemon mode: auto-enable offsets + lock when under load, revert at idle
- Hysteresis (time-based), optional ramping steps to target clock
- Thermal guard: reduce locked max if temp > limit (daemon)
- Clean revert on exit
"""

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# ---------- Version ----------
VERSION = "1.0"

# ---------- Utilities ----------

def run(cmd, check=True, capture=False, env=None):
    """Run a shell command list safely."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if capture:
        res = subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        return res.stdout.strip()
    else:
        subprocess.run(cmd, check=check, env=env)


def which(exe):
    from shutil import which as _which
    return _which(exe)


def require_tool(name):
    if not which(name):
        sys.exit(f"ERROR: Required tool '{name}' not found in PATH.")


def require_root():
    if os.geteuid() != 0:
        sys.exit("ERROR: This script must be run as root (sudo).")


def log(msg, *, verbose=True):
    if verbose:
        print(msg, flush=True)


def now():
    return time.time()


# ---------- NVIDIA Controls ----------

@dataclass
class NvCtl:
    index: int
    display: str = None      # e.g., ":0"
    use_offsets: bool = False
    dry_run: bool = False
    verbose: bool = True

    def _nvidia_smi(self, args, capture=False):
        cmd = ["nvidia-smi", "-i", str(self.index)] + args
        if self.dry_run:
            log(f"[dry-run] {' '.join(shlex.quote(c) for c in cmd)}", verbose=self.verbose)
            return ""
        return run(cmd, capture=capture)

    def _nvidia_settings(self, attr, value):
        if not self.use_offsets:
            return
        if not which("nvidia-settings"):
            sys.exit("ERROR: --core-offset/--memory-offset requires 'nvidia-settings' and Coolbits.")
        if not self.display:
            sys.exit("ERROR: --display is required when using offsets via nvidia-settings (e.g., --display :0).")
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        cmd = ["nvidia-settings", "-a", f"[gpu:{self.index}]/{attr}={value}"]
        if self.dry_run:
            log(f"[dry-run] DISPLAY={env['DISPLAY']} {' '.join(shlex.quote(c) for c in cmd)}", verbose=self.verbose)
            return
        run(cmd, env=env)

    # ---- Queries ----

    def query_basic(self):
        """
        Return dict: clocks_gr (MHz), temp (C), power (W), util (%), pstate (e.g., P0)
        """
        fmt = "clocks.gr,temperature.gpu,power.draw,utilization.gpu,pstate"
        out = self._nvidia_smi(["--query-gpu=" + fmt, "--format=csv,noheader,nounits"], capture=True)
        if not out:
            return {}
        parts = [p.strip() for p in out.split(",")]
        return {
            "clocks_gr": int(float(parts[0])),
            "temp": int(float(parts[1])),
            "power": float(parts[2]),
            "util": int(float(parts[3])),
            "pstate": parts[4]
        }

    def query_supported_gc(self):
        """
        Return list of supported graphics clocks (MHz) if available.
        """
        out = self._nvidia_smi(["--query-supported-clocks=gr", "--format=csv,noheader,nounits"], capture=True)
        if not out:
            return []
        try:
            vals = [int(v.strip()) for v in out.splitlines()]
            vals.sort()
            return vals
        except Exception:
            return []

    # ---- Settings ----

    def enable_persistence(self):
        self._nvidia_smi(["-pm", "1"])

    def set_power_limit(self, watts):
        # Check constraints (optional)
        try:
            out = self._nvidia_smi(["--query-gpu=power.limit,power.min_limit,power.max_limit",
                                    "--format=csv,noheader,nounits"], capture=True)
            cur, mn, mx = [float(x.strip()) for x in out.split(",")]
            if not (mn <= watts <= mx):
                log(f"WARNING: Requested power limit {watts}W not in [{mn},{mx}]W range. Clamping.", verbose=self.verbose)
                watts = max(mn, min(mx, watts))
        except Exception:
            pass
        self._nvidia_smi(["-pl", str(int(watts))])

    def lock_graphics_clock(self, min_mhz, max_mhz):
        self._nvidia_smi(["-lgc", f"{int(min_mhz)},{int(max_mhz)}"])

    def unlock_graphics_clock(self):
        self._nvidia_smi(["-rgc"])

    def set_core_offset(self, offset_mhz):
        # Apply across all performance levels; daemon only enables during load.
        self._nvidia_settings("GPUGraphicsClockOffsetAllPerformanceLevels", str(int(offset_mhz)))

    def reset_core_offset(self):
        self._nvidia_settings("GPUGraphicsClockOffsetAllPerformanceLevels", "0")

    def set_mem_offset(self, offset_mhz):
        self._nvidia_settings("GPUMemoryTransferRateOffsetAllPerformanceLevels", str(int(offset_mhz)))

    def reset_mem_offset(self):
        self._nvidia_settings("GPUMemoryTransferRateOffsetAllPerformanceLevels", "0")


# ---------- Daemon Loop ----------

class UndervoltDaemon:
    def __init__(self, nv: NvCtl, target_clock, transition_clock,
                 min_clock=210, core_offset=0, mem_offset=0, power_limit=None,
                 temp_limit=None, poll=0.5, on_hold=1.0, off_hold=2.0,
                 ramp=False, ramp_step=15, ramp_sleep=0.2, verbose=True):
        self.nv = nv
        self.target_clock = int(target_clock)
        self.transition_clock = int(transition_clock)
        self.min_clock = int(min_clock)
        self.core_offset = int(core_offset)
        self.mem_offset = int(mem_offset)
        self.power_limit = int(power_limit) if power_limit else None
        self.temp_limit = int(temp_limit) if temp_limit else None
        self.poll = float(poll)
        self.on_hold = float(on_hold)
        self.off_hold = float(off_hold)
        self.ramp = bool(ramp)
        self.ramp_step = int(ramp_step)
        self.ramp_sleep = float(ramp_sleep)
        self.verbose = verbose

        self.active = False
        self.last_above_ts = 0.0
        self.last_below_ts = now()

        self._stop = False

    def _handle_sig(self, signum, frame):
        log(f"\n[daemon] Caught signal {signum}, reverting...", verbose=True)
        try:
            self.revert()
        finally:
            self._stop = True

    def apply_active(self):
        # Apply offsets (requires X/Coolbits) first, then lock clocks.
        if self.nv.use_offsets and self.core_offset:
            log(f"[daemon] Applying core offset: +{self.core_offset} MHz", verbose=self.verbose)
            self.nv.set_core_offset(self.core_offset)
        if self.nv.use_offsets and self.mem_offset:
            log(f"[daemon] Applying memory offset: +{self.mem_offset} MHz", verbose=self.verbose)
            self.nv.set_mem_offset(self.mem_offset)

        # Optional power cap (one-time)
        if self.power_limit:
            log(f"[daemon] Setting power limit: {self.power_limit} W", verbose=self.verbose)
            self.nv.set_power_limit(self.power_limit)

        # Lock graphics clock
        if self.ramp:
            # Ramp up max from transition to target in steps
            current = max(self.transition_clock, self.min_clock)
            while current < self.target_clock and not self._stop:
                step_to = min(current + self.ramp_step, self.target_clock)
                log(f"[daemon] Lock ramp -> {step_to} MHz", verbose=self.verbose)
                self.nv.lock_graphics_clock(self.min_clock, step_to)
                time.sleep(self.ramp_sleep)
                current = step_to
        log(f"[daemon] Locking clocks min={self.min_clock} max={self.target_clock} MHz", verbose=self.verbose)
        self.nv.lock_graphics_clock(self.min_clock, self.target_clock)

        self.active = True

    def revert(self):
        # Unlock and reset offsets
        try:
            self.nv.unlock_graphics_clock()
        except Exception as e:
            log(f"[daemon] unlock_graphics_clock: {e}", verbose=self.verbose)
        if self.nv.use_offsets:
            try:
                if self.core_offset:
                    self.nv.reset_core_offset()
                if self.mem_offset:
                    self.nv.reset_mem_offset()
            except Exception as e:
                log(f"[daemon] reset offsets: {e}", verbose=self.verbose)
        self.active = False

    def thermal_guard(self):
        if not self.temp_limit or not self.active:
            return
        info = self.nv.query_basic()
        if not info:
            return
        if info["temp"] > self.temp_limit:
            # Drop max by one step
            new_max = max(self.min_clock, self.target_clock - self.ramp_step)
            if new_max < self.target_clock:
                log(f"[daemon] Temp {info['temp']}°C > {self.temp_limit}°C, reducing max to {new_max} MHz", verbose=self.verbose)
                self.target_clock = new_max
                self.nv.lock_graphics_clock(self.min_clock, self.target_clock)

    def run(self):
        signal.signal(signal.SIGINT, self._handle_sig)
        signal.signal(signal.SIGTERM, self._handle_sig)

        log(f"[daemon] Starting (v{VERSION})...", verbose=self.verbose)
        # Enable persistence for reliability
        try:
            self.nv.enable_persistence()
        except Exception as e:
            log(f"[daemon] persistence warn: {e}", verbose=self.verbose)

        try:
            while not self._stop:
                info = self.nv.query_basic()
                if not info:
                    time.sleep(self.poll)
                    continue

                clk = info["clocks_gr"]

                # Hysteresis timers
                t = now()
                if clk >= self.transition_clock:
                    if self.last_above_ts == 0:
                        self.last_above_ts = t
                    # Enough sustained?
                    if (t - self.last_above_ts) >= self.on_hold and not self.active:
                        log(f"[daemon] Enabling undervolt (clk={clk} MHz)", verbose=self.verbose)
                        self.apply_active()
                    # Reset below timer
                    self.last_below_ts = t
                else:
                    if self.last_below_ts == 0:
                        self.last_below_ts = t
                    # Enough sustained below?
                    if (t - self.last_below_ts) >= self.off_hold and self.active:
                        log(f"[daemon] Disabling undervolt (clk={clk} MHz)", verbose=self.verbose)
                        self.revert()
                    # Reset above timer
                    self.last_above_ts = t

                # Thermal guard (only when active)
                self.thermal_guard()

                time.sleep(self.poll)
        finally:
            # Ensure clean revert on any exit
            self.revert()
            log("[daemon] Stopped.", verbose=True)


# ---------- One-shot ----------

def oneshot(nv: NvCtl, min_clock, target_clock, core_offset, mem_offset, power_limit, verify, verbose):
    log(f"[oneshot] Starting (v{VERSION})...", verbose=verbose)

    # Enable persistence
    try:
        nv.enable_persistence()
    except Exception as e:
        log(f"[oneshot] persistence warn: {e}", verbose=verbose)

    # Optional power cap first
    if power_limit:
        log(f"[oneshot] Setting power limit: {power_limit} W", verbose=verbose)
        nv.set_power_limit(power_limit)

    # Offsets (if requested)
    if nv.use_offsets and core_offset:
        log(f"[oneshot] Applying core offset: +{core_offset} MHz", verbose=verbose)
        nv.set_core_offset(core_offset)
    if nv.use_offsets and mem_offset:
        log(f"[oneshot] Applying memory offset: +{mem_offset} MHz", verbose=verbose)
        nv.set_mem_offset(mem_offset)

    # Lock clocks
    log(f"[oneshot] Locking clocks min={min_clock} max={target_clock} MHz", verbose=verbose)
    nv.lock_graphics_clock(min_clock, target_clock)

    if verify:
        time.sleep(0.5)
        info = nv.query_basic()
        log(f"[oneshot] Now: clk={info.get('clocks_gr','?')} MHz, temp={info.get('temp','?')}C, power={info.get('power','?')}W, pstate={info.get('pstate','?')}", verbose=verbose)

    # Note: In one-shot we do NOT revert on exit (it is set-and-forget).
    log("[oneshot] Done. Settings persist until reboot or manual revert.", verbose=verbose)


# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description="NVIDIA GPU undervolt / efficiency tool (one-shot or daemon).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--index", type=int, default=0, help="GPU index (default: 0)")
    parser.add_argument("--display", type=str, default=None, help="X DISPLAY for nvidia-settings (e.g., :0). Required if using offsets.")
    parser.add_argument("--mode", choices=["oneshot", "daemon"], default="daemon", help="Run as one-shot or daemon (default: daemon)")
    parser.add_argument("--min-clock", type=int, default=210, help="Locked min graphics clock MHz (default: 210)")
    parser.add_argument("--target-clock", type=int, required=True, help="Locked max graphics clock MHz when active")
    parser.add_argument("--transition-clock", type=int, help="Daemon: enable undervolt when clk >= this (default: target-300)")
    parser.add_argument("--core-offset", type=int, default=0, help="Core clock offset MHz (requires nvidia-settings + Coolbits)")
    parser.add_argument("--memory-offset", type=int, default=0, help="Memory transfer rate offset MHz (requires nvidia-settings + Coolbits)")
    parser.add_argument("--power-limit", type=int, help="Optional power limit in W (nvidia-smi -pl)")
    parser.add_argument("--temp-limit", type=int, help="Daemon: thermal guard max °C (will reduce max clock if exceeded)")
    parser.add_argument("--poll", type=float, default=0.5, help="Daemon: poll interval seconds (default: 0.5)")
    parser.add_argument("--on-hold", type=float, default=1.0, help="Daemon: seconds above threshold before enabling (default: 1.0)")
    parser.add_argument("--off-hold", type=float, default=2.0, help="Daemon: seconds below threshold before disabling (default: 2.0)")
    parser.add_argument("--ramp", action="store_true", help="Daemon: ramp max clock in steps instead of jumping")
    parser.add_argument("--ramp-step", type=int, default=15, help="Daemon: ramp step MHz (default: 15)")
    parser.add_argument("--ramp-sleep", type=float, default=0.2, help="Daemon: sleep between ramp steps seconds (default: 0.2)")
    parser.add_argument("--use-offsets", action="store_true", help="Enable using nvidia-settings for core/mem offsets")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only; do not apply changes")
    parser.add_argument("--verify", action="store_true", help="One-shot: print status after applying")
    parser.add_argument("--quiet", action="store_true", help="Less verbose output")
    args = parser.parse_args()

    require_root()
    require_tool("nvidia-smi")
    if args.use_offsets:
        require_tool("nvidia-settings")
        if not args.display:
            sys.exit("ERROR: --display is required with --use-offsets (e.g., --display :0)")

    verbose = not args.quiet
    nv = NvCtl(index=args.index, display=args.display, use_offsets=args.use_offsets,
               dry_run=args.dry_run, verbose=verbose)

    # Default transition threshold
    transition_clock = args.transition_clock if args.transition_clock is not None else max(args.target_clock - 300, args.min_clock)

    # Sanity: Check target in supported clocks (best-effort)
    try:
        sup = nv.query_supported_gc()
        if sup and args.target_clock not in sup:
            # Suggest nearest valid step (usually 15MHz grid)
            nearest = min(sup, key=lambda x: abs(x - args.target_clock))
            log(f"WARNING: {args.target_clock} MHz not in supported list; nearest is {nearest} MHz", verbose=verbose)
    except Exception:
        pass

    if args.mode == "oneshot":
        oneshot(nv, args.min_clock, args.target_clock, args.core_offset, args.memory_offset, args.power_limit, args.verify, verbose)
    else:
        daemon = UndervoltDaemon(
            nv=nv,
            target_clock=args.target_clock,
            transition_clock=transition_clock,
            min_clock=args.min_clock,
            core_offset=args.core_offset,
            mem_offset=args.memory_offset,
            power_limit=args.power_limit,
            temp_limit=args.temp_limit,
            poll=args.poll,
            on_hold=args.on_hold,
            off_hold=args.off_hold,
            ramp=args.ramp,
            ramp_step=args.ramp_step,
            ramp_sleep=args.ramp_sleep,
            verbose=verbose,
        )
        daemon.run()


if __name__ == "__main__":
    main()

