"""Lectura de /proc y /sys: CPU, GPU, memoria, presión del sistema y procesos.

Todo se lee directamente del kernel, sin dependencias externas.
"""

import glob
import os
import platform
import re
import subprocess
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
MB = 1024 * 1024


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def run(*cmd, timeout=3):
    """stdout de un comando, o "" si no existe o falla."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def hwmon_dir(*names):
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if read(f"{d}/name") in names:
            return d
    return None


def hwmon_temps(d):
    """{etiqueta: (ruta del sensor, temperatura crítica °C o None)}."""
    temps = {}
    for inp in sorted(glob.glob(f"{d}/temp*_input")):
        base = inp[: -len("_input")]
        label = read(f"{base}_label") or os.path.basename(base)
        crit = read_int(f"{base}_crit")
        temps[label] = (inp, crit / 1000 if crit else None)
    return temps


def parse_stat(text):
    """(comm, campos desde el estado en adelante) de /proc/<pid>/stat."""
    r = text.rfind(")")
    return text[text.find("(") + 1 : r], text[r + 2 :].split()


def cmdline(pid):
    raw = read(f"/proc/{pid}/cmdline") or ""
    return raw.replace("\0", " ")


# --------------------------------------------------------------------- CPU


class Cpu:
    def __init__(self):
        self.model = ""
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    self.model = line.split(":", 1)[1].strip()
                    break
        self.count = os.cpu_count() or 1
        maxes = [read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq")]
        self.max_mhz = max((m for m in maxes if m), default=0) // 1000
        # Temperatura a la que la CPU empieza a bajar frecuencia
        if "X3D" in self.model:
            self.tmax = 89
        elif "AMD" in self.model:
            self.tmax = 95
        else:
            self.tmax = 100
        self.temp_path = self._find_temp()
        self._freq_paths = [f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq" for i in range(self.count)]
        self._prev = self._read_stat()

    @staticmethod
    def _find_temp():
        d = hwmon_dir("k10temp", "zenpower")
        if d:
            temps = hwmon_temps(d)
            for label in ("Tctl", "Tdie"):
                if label in temps:
                    return temps[label][0]
        d = hwmon_dir("coretemp")
        if d:
            for label, (path, _) in hwmon_temps(d).items():
                if label.startswith("Package"):
                    return path
        return None

    @staticmethod
    def _read_stat():
        stat = {}
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                if parts[0] == "cpu":
                    continue
                v = [int(x) for x in parts[1:9]]
                stat[parts[0]] = (sum(v), v[3] + v[4])  # total, idle + iowait
        return stat

    def sample(self):
        cur = self._read_stat()
        cores = []
        for name, (total, idle) in cur.items():
            ptotal, pidle = self._prev.get(name, (total, idle))
            dt = total - ptotal
            cores.append(100 * (1 - (idle - pidle) / dt) if dt > 0 else 0.0)
        self._prev = cur
        freqs = [read_int(p) for p in self._freq_paths]
        valid = [f for f in freqs if f]
        busy = [f for f, u in zip(freqs, cores) if f and u >= 50]
        t = read_int(self.temp_path) if self.temp_path else None
        return {
            "use": round(sum(cores) / len(cores), 1) if cores else 0.0,
            "max": round(max(cores, default=0.0), 1),
            "cores": [round(c) for c in cores],
            "mhz": round(sum(valid) / len(valid) / 1000) if valid else None,
            "mhz_busy": round(sum(busy) / len(busy) / 1000) if busy else None,
            "temp": round(t / 1000, 1) if t is not None else None,
        }


# --------------------------------------------------------------------- GPU


class Gpu:
    """GPU AMD (driver amdgpu) leída desde sysfs."""

    def __init__(self, dev):
        self.dev = dev
        self.slot = os.path.basename(os.path.realpath(dev))
        hw = glob.glob(f"{dev}/hwmon/hwmon*")
        self.hw = hw[0] if hw else None
        self.temps = hwmon_temps(self.hw) if self.hw else {}
        self.vram_total = (read_int(f"{dev}/mem_info_vram_total") or 0) // MB
        self.sclk_max = max(self._dpm("pp_dpm_sclk")[1], default=0)
        self.name = self._name()

    @staticmethod
    def find():
        """La GPU AMD con más VRAM (la dedicada si también hay integrada)."""
        best = None
        for dev in glob.glob("/sys/class/drm/card*/device"):
            if not re.fullmatch(r"card\d+", dev.split("/")[-2]):
                continue
            if read(f"{dev}/vendor") != "0x1002" or not os.path.exists(f"{dev}/gpu_busy_percent"):
                continue
            vram = read_int(f"{dev}/mem_info_vram_total") or 0
            if best is None or vram > best[0]:
                best = (vram, dev)
        return Gpu(best[1]) if best else None

    def _name(self):
        names = re.findall(r"\[([^\]]+)\]", run("lspci", "-s", self.slot))
        if names:
            return names[-1]
        m = re.search(r"PCI_ID=(\S+)", read(f"{self.dev}/uevent") or "")
        return f"GPU AMD {m.group(1)}" if m else "GPU AMD"

    def _dpm(self, fname):
        """(MHz actual, [MHz de cada nivel]) de pp_dpm_sclk / pp_dpm_mclk."""
        cur, levels = None, []
        for line in (read(f"{self.dev}/{fname}") or "").splitlines():
            m = re.search(r"(\d+)\s*Mhz(\s*\*)?", line, re.I)
            if m:
                levels.append(int(m.group(1)))
                if m.group(2):
                    cur = int(m.group(1))
        return cur, levels

    def _hw_int(self, name):
        return read_int(f"{self.hw}/{name}") if self.hw else None

    def sample(self):
        sclk, mclk = self._hw_int("freq1_input"), self._hw_int("freq2_input")
        power = self._hw_int("power1_average") or self._hw_int("power1_input")
        cap = self._hw_int("power1_cap")
        return {
            "busy": read_int(f"{self.dev}/gpu_busy_percent", 0),
            "sclk": round(sclk / 1e6) if sclk else self._dpm("pp_dpm_sclk")[0],
            "mclk": round(mclk / 1e6) if mclk else self._dpm("pp_dpm_mclk")[0],
            "temp": {label: round(read_int(path, 0) / 1000) for label, (path, _) in self.temps.items()},
            "power": round(power / 1e6, 1) if power else None,
            "cap": round(cap / 1e6) if cap else None,
            "vram": (read_int(f"{self.dev}/mem_info_vram_used") or 0) // MB,
            "gtt": (read_int(f"{self.dev}/mem_info_gtt_used") or 0) // MB,
            "fan": self._hw_int("fan1_input"),
        }


class GpuClients:
    """Uso de GPU y VRAM por proceso, leído de /proc/<pid>/fdinfo (drm-engine-*)."""

    RESCAN = 5.0  # segundos entre búsquedas de procesos que usan la GPU

    def __init__(self, slot):
        self.slot = slot
        self.fds = {}
        self._last_scan = 0.0
        self._prev = {}

    def _scan(self):
        fds = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                entries = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue  # procesos de otros usuarios
            for fd in entries:
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                        fds.setdefault(int(pid), []).append(fd)
                except OSError:
                    pass
        self.fds = fds
        self._last_scan = time.monotonic()

    def sample(self, dt):
        """{pid: (% de GPU, MB de VRAM)}."""
        if not self.slot:
            return {}
        if time.monotonic() - self._last_scan > self.RESCAN:
            self._scan()
        out, cur = {}, {}
        for pid, fds in self.fds.items():
            clients = {}  # varios fd pueden ser el mismo cliente DRM
            for fd in fds:
                txt = read(f"/proc/{pid}/fdinfo/{fd}")
                if not txt or (f"drm-pdev:\t{self.slot}" not in txt and "drm-pdev" in txt):
                    continue
                cid = re.search(r"drm-client-id:\s*(\d+)", txt)
                busy = sum(int(x) for x in re.findall(r"drm-engine-(?:gfx|compute)\S*:\s*(\d+) ns", txt))
                vram = re.search(r"drm-(?:memory|resident)-vram:\s*(\d+) (KiB|MiB)", txt)
                vram_mb = (int(vram.group(1)) / (1024 if vram.group(2) == "KiB" else 1)) if vram else 0
                clients[cid.group(1) if cid else fd] = (busy, vram_mb)
            if not clients:
                continue
            busy = sum(c[0] for c in clients.values())
            cur[pid] = busy
            if pid in self._prev:
                pct = min(100.0, max(0.0, (busy - self._prev[pid]) / (dt * 1e9) * 100))
                out[pid] = (pct, sum(c[1] for c in clients.values()))
        self._prev = cur
        return out


# ------------------------------------------------------------ memoria y PSI


def meminfo():
    """RAM y swap en MB."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.split()[0]) // 1024
    return {
        "total": info["MemTotal"],
        "avail": info["MemAvailable"],
        "swap": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
    }


def vmstat():
    want = {"pswpin", "pswpout", "pgmajfault"}
    out = dict.fromkeys(want, 0)
    with open("/proc/vmstat") as f:
        for line in f:
            key, val = line.split()
            if key in want:
                out[key] = int(val)
    return out


def psi_totals():
    """Microsegundos acumulados en los que algo esperaba por CPU, memoria o disco."""
    out = {}
    for res, short in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
        for line in (read(f"/proc/pressure/{res}") or "").splitlines():
            kind, *fields = line.split()
            if res == "cpu" and kind == "full":
                continue  # sin significado a nivel de sistema
            out[short if kind == "some" else f"{short}_full"] = int(fields[-1].split("=")[1])
    return out


# ---------------------------------------------------------------- procesos


class Procs:
    """CPU (100 % = un núcleo) y RSS de cada proceso."""

    def __init__(self):
        self._prev = {}

    def sample(self, dt):
        """{pid: (comm, % CPU, RSS en MB)}."""
        cur, out = {}, {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat") as f:
                    comm, fields = parse_stat(f.read())
            except OSError:
                continue
            pid = int(name)
            ticks, start = int(fields[11]) + int(fields[12]), int(fields[19])
            cur[pid] = (ticks, start)
            prev = self._prev.get(pid)
            cpu = (ticks - prev[0]) / CLK_TCK / dt * 100 if prev and prev[1] == start else 0.0
            out[pid] = (comm, cpu, int(fields[21]) * PAGE // MB)
        self._prev = cur
        return out


class GameThreads:
    """Hilo más ocupado, lecturas de disco y fallos de página del juego."""

    def __init__(self):
        self.pid = None
        self._prev = {}
        self._io = None

    def sample(self, pid, dt):
        if pid != self.pid:
            self.pid, self._prev, self._io = pid, {}, None
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            return None
        cur, top_name, top_cpu = {}, None, 0.0
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/stat") as f:
                    comm, fields = parse_stat(f.read())
            except OSError:
                continue
            ticks = int(fields[11]) + int(fields[12])
            cur[tid] = ticks
            if tid in self._prev:
                cpu = (ticks - self._prev[tid]) / CLK_TCK / dt * 100
                if cpu > top_cpu:
                    top_name, top_cpu = comm, cpu
        self._prev = cur

        read_bytes = None
        m = re.search(r"read_bytes:\s*(\d+)", read(f"/proc/{pid}/io") or "")
        if m:
            read_bytes = int(m.group(1))
        stat = read(f"/proc/{pid}/stat")
        majflt = int(parse_stat(stat)[1][9]) if stat else None
        io = (read_bytes, majflt)
        prev_io, self._io = self._io, io
        rd = mf = 0.0
        if prev_io:
            if read_bytes is not None and prev_io[0] is not None:
                rd = (read_bytes - prev_io[0]) / MB / dt
            if majflt is not None and prev_io[1] is not None:
                mf = (majflt - prev_io[1]) / dt
        return {
            "threads": len(tids),
            "thread": top_name,
            "thread_cpu": round(top_cpu, 1),
            "read": round(rd, 2),
            "majflt": round(mf),
        }


# ----------------------------------------------------- detección del juego

NOT_GAMES = {
    "hyprland", "xwayland", "kwin_wayland", "kwin_x11", "gnome-shell", "sway", "mutter", "plasmashell",
    "xorg", "gamescope", "gamescope-wl", "steam", "steamwebhelper", "firefox", "firefox-bin", "librewolf",
    "zen", "zen-bin", "chrome", "chromium", "brave", "vivaldi-bin", "opera", "discord", "vesktop",
    "electron", "code", "obs", "mpv", "vlc", "waybar", "hyprpaper", "swww-daemon", "hyprlock", "kitty",
    "alacritty", "foot", "ghostty", "wezterm-gui", "konsole", "nvtop", "spotify", "telegram-desktop",
    "thunderbird", "dolphin", "nautilus", "xdg-desktop-por", "pipewire", "wireplumber", "lact", "corectrl",
    "wineserver", "services.exe", "winedevice.exe", "explorer.exe", "plugplay.exe", "svchost.exe",
    "rpcss.exe", "tabtip.exe", "conhost.exe", "start.exe", "steam.exe", "rundll32.exe", "winedbg",
    "steamservice.ex", "ealink.exe", "eadesktop.exe", "epicgameslaunc", "upc.exe", "battle.net.exe",
}


def is_candidate(comm):
    c = comm.lower()
    if c in NOT_GAMES:
        return False
    return not any(x in c for x in ("webhelper", "web content", "isolated web", "crashpad", "kworker", " process"))


class GameTracker:
    """Decide qué proceso es el juego. Se queda con él mientras siga vivo."""

    REEVALUATE = 10.0

    def __init__(self, hint=None, pid=None):
        self.hint = hint.lower() if hint else None
        self.fixed = pid
        self.pid = None
        self._last = 0.0

    def update(self, procs, gpu_use):
        if self.fixed:
            return self.fixed if self.fixed in procs else None
        now = time.monotonic()
        alive = self.pid in procs
        if alive and now - self._last < self.REEVALUATE:
            return self.pid
        self._last = now
        best = self._pick(procs, gpu_use)
        if not alive:
            self.pid = best
        elif best and best != self.pid:
            # Cambiar solo si el nuevo usa claramente más GPU (p. ej. launcher → juego)
            new, old = gpu_use.get(best, (0, 0))[0], gpu_use.get(self.pid, (0, 0))[0]
            if new > max(20, 2 * old):
                self.pid = best
        return self.pid

    def _pick(self, procs, gpu_use):
        own = os.getpid()
        if self.hint:
            matches = [
                pid for pid, p in procs.items()
                if pid != own and (self.hint in p[0].lower() or self.hint in cmdline(pid).lower())
            ]
            return max(matches, key=lambda pid: procs[pid][1], default=None)
        gpu = [(u[0], pid) for pid, u in gpu_use.items() if pid in procs and u[0] >= 2 and is_candidate(procs[pid][0])]
        if gpu:
            return max(gpu)[1]
        exe = [(p[1], pid) for pid, p in procs.items() if p[0].lower().endswith(".exe") and is_candidate(p[0]) and p[1] >= 5]
        return max(exe)[1] if exe else None


# ---------------------------------------------------------------- muestreo


class Sampler:
    def __init__(self, game=None, pid=None):
        self.cpu = Cpu()
        self.gpu = Gpu.find()
        self.procs = Procs()
        self.gpu_clients = GpuClients(self.gpu.slot if self.gpu else None)
        self.threads = GameThreads()
        self.tracker = GameTracker(game, pid)
        self.own = os.getpid()
        self._t = time.monotonic()
        self._vm = vmstat()
        self._psi = psi_totals()
        self.procs.sample(1.0)
        self.gpu_clients.sample(1.0)

    def info(self):
        """Datos fijos del equipo; van en la cabecera de la sesión."""
        gpu = self.gpu
        return {
            "type": "header",
            "version": 1,
            "start": time.time(),
            "host": platform.node(),
            "kernel": platform.release(),
            "cpu_model": self.cpu.model,
            "cpu_max_mhz": self.cpu.max_mhz,
            "cpu_tmax": self.cpu.tmax,
            "gpu_name": gpu.name if gpu else None,
            "gpu_crit": {label: crit for label, (_, crit) in gpu.temps.items()} if gpu else {},
            "vram_total": gpu.vram_total if gpu else 0,
            "sclk_max": gpu.sclk_max if gpu else 0,
        }

    def sample(self):
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now
        vm, psi = vmstat(), psi_totals()
        rate = {
            "swapin": round((vm["pswpin"] - self._vm["pswpin"]) * PAGE / MB / dt, 2),
            "swapout": round((vm["pswpout"] - self._vm["pswpout"]) * PAGE / MB / dt, 2),
            "majflt": round((vm["pgmajfault"] - self._vm["pgmajfault"]) / dt),
        }
        pressure = {k: round(min(100.0, (v - self._psi.get(k, v)) / (dt * 1e6) * 100), 1) for k, v in psi.items()}
        self._vm, self._psi = vm, psi

        procs = self.procs.sample(dt)
        gpu_use = self.gpu_clients.sample(dt)
        game_pid = self.tracker.update(procs, gpu_use)
        game = None
        if game_pid in procs:
            comm, cpu, rss = procs[game_pid]
            gpu_pct, vram = gpu_use.get(game_pid, (None, None))
            game = {
                "pid": game_pid,
                "name": comm,
                "cpu": round(cpu, 1),
                "gpu": round(gpu_pct, 1) if gpu_pct is not None else None,
                "vram": round(vram) if vram is not None else None,
                "rss": rss,
                **(self.threads.sample(game_pid, dt) or {}),
            }

        # Resto de procesos agrupados por nombre (firefox con 12 procesos cuenta como uno)
        groups = {}
        for pid, (comm, cpu, _) in procs.items():
            if pid in (game_pid, self.own):
                continue
            g = groups.setdefault(comm, [0.0, 0])
            g[0] += cpu
            g[1] += 1
        top = sorted(([n, round(c, 1), k] for n, (c, k) in groups.items() if c >= 1), key=lambda x: -x[1])[:6]
        gtop = sorted(
            ([procs[pid][0], round(u[0], 1)] for pid, u in gpu_use.items() if pid != game_pid and pid in procs and u[0] >= 1),
            key=lambda x: -x[1],
        )[:4]

        return {
            "t": round(time.time(), 2),
            "cpu": self.cpu.sample(),
            "gpu": self.gpu.sample() if self.gpu else None,
            "mem": meminfo(),
            "rate": rate,
            "psi": pressure,
            "game": game,
            "top": top,
            "gtop": gtop,
        }
