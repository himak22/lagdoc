"""`lagdoc monitor`: panel en vivo mientras juegas; graba la sesión para el informe."""

import collections
import contextlib
import json
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty

from . import rules
from .kernlog import KernelFollower
from .probe import Sampler
from .term import ICON, bar, bold, cyan, dim, level, spark, temp

SESSIONS = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "lagdoc")
COOLDOWN = 20.0  # segundos antes de repetir el mismo evento en pantalla


def session_path():
    os.makedirs(SESSIONS, exist_ok=True)
    return os.path.join(SESSIONS, time.strftime("sesion-%Y%m%d-%H%M%S.jsonl"))


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def render(s, info, live, recent, status):
    size = shutil.get_terminal_size((100, 30))
    lines = [f"{bold('lagdoc')} {dim('· monitor ' + clock(s['t']))}   {status}"]

    game = s.get("game")
    if game:
        thread = f"hilo top {cyan(game.get('thread') or '?')} {game.get('thread_cpu', 0):.0f}%"
        gpu = f"GPU {game['gpu']:.0f}%" if game.get("gpu") is not None else ""
        vram = f"VRAM {game['vram'] / 1024:.1f} GB" if game.get("vram") else ""
        lines.append(
            f"{bold('JUEGO')}  {cyan(game['name'])} {dim('pid ' + str(game['pid']))}  CPU {game['cpu']:.0f}%  {thread}  "
            f"{gpu}  {vram}  RAM {game['rss'] / 1024:.1f} GB  disco {game.get('read', 0):.1f} MB/s"
        )
    else:
        lines.append(f"{bold('JUEGO')}  {dim('no detectado: abre el juego o usa --game NOMBRE')}")

    if recent:
        counts = collections.Counter(recent)
        main, n = counts.most_common(1)[0]
        share = " · ".join(f"{rules.BOTTLENECK_SHORT[k]} {v * 100 // len(recent)}%" for k, v in counts.most_common(3))
        lines.append(f"{bold('LÍMITE')} {rules.BOTTLENECK[main]}  {dim('(últimos ' + str(len(recent)) + ' s: ' + share + ')')}")
    else:
        lines.append(f"{bold('LÍMITE')} {dim('—')}")
    lines.append("")

    cpu = s["cpu"]
    lines.append(
        f"CPU   {bar(cpu['use'])} {cpu['use']:3.0f}%  núcleo máx {cpu['max']:3.0f}%  "
        f"{cpu['mhz'] or '?'} MHz  {temp(cpu['temp'], info['cpu_tmax'])}"
    )
    lines.append(f"      {spark(cpu['cores'])}  {dim('uso por núcleo')}")

    gpu = s.get("gpu")
    if gpu:
        temps = "  ".join(
            f"{'hotspot' if k == 'junction' else k} {temp(v, info['gpu_crit'].get(k) or 100)}" for k, v in gpu["temp"].items()
        )
        power = f"{gpu['power']:.0f}/{gpu['cap']} W" if gpu.get("power") is not None else ""
        lines.append(f"GPU   {bar(gpu['busy'], warn=101, bad=101)} {gpu['busy']:3d}%  {gpu['sclk'] or '?'} MHz  {temps}  {power}")
        vram_pct = 100 * gpu["vram"] / info["vram_total"] if info["vram_total"] else 0
        lines.append(
            f"VRAM  {bar(vram_pct, warn=90, bad=95)} {gpu['vram'] / 1024:4.1f}/{info['vram_total'] / 1024:.0f} GB  "
            f"{dim('GTT ' + format(gpu['gtt'] / 1024, '.1f') + ' GB')}"
        )
    else:
        lines.append(f"GPU   {dim('sin datos (solo GPUs AMD)')}")

    mem, rate = s["mem"], s["rate"]
    used = 100 * (1 - mem["avail"] / mem["total"])
    swap = level(rate["swapin"], 0.1, 0.5)(f"swap in {rate['swapin']:.1f} MB/s")
    lines.append(
        f"RAM   {bar(used, warn=90, bad=95)} {(mem['total'] - mem['avail']) / 1024:4.1f}/{mem['total'] / 1024:.0f} GB  "
        f"swap usada {mem['swap'] / 1024:.1f} GB  {swap}"
    )
    psi = s["psi"]
    lines.append(
        "ESPERA "
        + "  ".join(level(psi.get(k, 0), w, b)(f"{name} {psi.get(k, 0):.0f}%") for name, k, w, b in
                    (("cpu", "cpu", 20, 40), ("memoria", "mem", 1, 5), ("disco", "io", 10, 20)))
        + dim("   (% del tiempo que algo esperaba por ese recurso)")
    )
    top = "  ".join(f"{n}{dim('×' + str(k)) if k > 1 else ''} {c:.0f}%" for n, c, k in s["top"][:5])
    lines.append(f"FONDO  {top or dim('—')}")
    if s.get("gtop"):
        lines.append(f"       GPU: " + "  ".join(f"{n} {g:.0f}%" for n, g in s["gtop"]))

    lines.append("")
    lines.append(bold("EVENTOS") + dim("  (más reciente arriba)"))
    room = max(3, size.lines - len(lines) - 1)
    if live:
        for t, sev, msg in list(live)[:room]:
            lines.append(f" {dim(clock(t))} {ICON[sev]} {msg[: size.columns - 14]}")
    else:
        lines.append(dim(" Nada raro por ahora."))
    return lines


class Screen:
    """Pantalla alternativa + teclado sin eco; se restaura siempre al salir."""

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.saved = None

    def __enter__(self):
        if self.fd is not None:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self.saved:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def draw(self, lines):
        sys.stdout.write("\x1b[H" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J")
        sys.stdout.flush()

    def wait(self, seconds):
        """Espera hasta `seconds`; devuelve True si se pulsó q."""
        if seconds <= 0:
            return False
        if self.fd is None:
            time.sleep(seconds)
            return False
        ready, _, _ = select.select([self.fd], [], [], seconds)
        return bool(ready) and os.read(self.fd, 32).lower().startswith(b"q")


def _stop(*_):
    raise KeyboardInterrupt


def run_monitor(args):
    try:
        os.nice(10)  # que lagdoc nunca compita con el juego
    except OSError:
        pass
    signal.signal(signal.SIGTERM, _stop)

    sampler = Sampler(game=args.game, pid=args.pid)
    info = sampler.info()
    info["interval"] = args.interval
    klog = KernelFollower()
    path = None if args.no_record else (args.output or session_path())
    rec = open(path, "w") if path else None
    if rec:
        rec.write(json.dumps(info) + "\n")

    interactive = sys.stdout.isatty() and not args.plain
    live = collections.deque(maxlen=50)
    recent = collections.deque(maxlen=30)
    last_shown = {}
    started = time.monotonic()
    count = 0
    status = (f"● grabando {path}" if path else "sin grabar") + dim("   q: salir")
    if not interactive:
        print(f"lagdoc monitor · {'grabando ' + path if path else 'sin grabar'} · Ctrl+C para salir", flush=True)

    screen = Screen() if interactive else None
    try:
        with screen or contextlib.nullcontext():
            deadline = time.monotonic()
            while True:
                deadline = max(deadline + args.interval, time.monotonic())  # no recuperar muestras perdidas a ráfagas
                if screen:
                    if screen.wait(deadline - time.monotonic()):
                        break
                else:
                    time.sleep(max(0.0, deadline - time.monotonic()))
                s = sampler.sample()
                s["kernel"] = klog.poll()
                count += 1
                if rec:
                    rec.write(json.dumps(s, separators=(",", ":")) + "\n")
                    if count % 5 == 0:
                        rec.flush()

                if (bn := rules.bottleneck(s)) is not None:
                    recent.append(bn)
                for key, sev, msg in rules.events(s, info):
                    if s["t"] - last_shown.get(key, 0) >= COOLDOWN:
                        last_shown[key] = s["t"]
                        live.appendleft((s["t"], sev, msg))
                        if not interactive:
                            print(f"{clock(s['t'])} {ICON[sev]} {msg}", flush=True)
                if screen:
                    screen.draw(render(s, info, live, recent, status))
                if args.duration and time.monotonic() - started >= args.duration:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        klog.close()
        if rec:
            rec.close()

    mins, secs = divmod(int(time.monotonic() - started), 60)
    print(f"Sesión de {mins} min {secs} s · {count} muestras · {len(live)} eventos distintos mostrados.")
    if path:
        print(f"Guardada en {path}")
        print(f"Analízala con: {bold('lagdoc report')}" + dim("  (añade --mangohud LOG.csv si grabaste con MangoHud)"))
