"""`lagdoc report`: analiza una sesión grabada y, si se le da, un log de MangoHud."""

import bisect
import collections
import glob
import json
import os
import re
import statistics
import textwrap
import time

from . import kernlog, rules
from .monitor import SESSIONS, clock
from .term import ICON, SEV_RANK, bar, bold, cyan, dim, yellow


def load(path):
    hdr, samples = {}, []
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # p. ej. la última línea si el monitor se cortó de golpe
            if rec.get("type") == "header":
                hdr = rec
            else:
                samples.append(rec)
    return hdr, samples


def sessions(folder=SESSIONS):
    return sorted(glob.glob(os.path.join(folder, "sesion-*.jsonl")))


def duration(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} h {m} min"
    return f"{m} min {s} s" if m else f"{s} s"


def spans(times, gap):
    """Agrupa instantes en tramos continuos: [[inicio, fin]]."""
    out = []
    for t in times:
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def section(title):
    print(f"\n{cyan('■')} {bold(title)}")


def wrapped(text, indent=6):
    for line in textwrap.wrap(text, 100 - indent):
        print(" " * indent + dim(line))


# ----------------------------------------------------------------- MangoHud


def load_mangohud(path, offset=0.0):
    """[(instante unix, frametime ms)] y si la hora de inicio salió del nombre del archivo."""
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    head = next((i for i, line in enumerate(lines) if line.lower().startswith("fps,")), None)
    if head is None:
        raise ValueError("no parece un log CSV de MangoHud (falta la cabecera fps,frametime,...)")
    cols = [c.strip().lower() for c in lines[head].split(",")]
    if "frametime" not in cols:
        raise ValueError("el log de MangoHud no tiene columna frametime")
    i_ft = cols.index("frametime")
    i_el = cols.index("elapsed") if "elapsed" in cols else None
    rows, acc = [], 0.0
    for line in lines[head + 1 :]:
        parts = line.split(",")
        try:
            ft = float(parts[i_ft])
            if i_el is not None:
                elapsed = float(parts[i_el]) / 1e9  # MangoHud guarda nanosegundos
            else:
                acc += ft / 1000
                elapsed = acc
        except (ValueError, IndexError):
            continue
        if ft > 0:
            rows.append((elapsed, ft))
    if not rows:
        raise ValueError("el log de MangoHud no tiene fotogramas")
    m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", os.path.basename(path))
    if m:
        start = time.mktime(time.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S"))
    else:
        start = os.path.getmtime(path) - rows[-1][0]  # MangoHud escribe el archivo al terminar
    return [(start + el + offset, ft) for el, ft in rows], bool(m)


def find_stutters(frames, half=30):
    """Fotogramas que tardan >2× la mediana de su entorno y al menos 8 ms más: [(t, ms, ms habituales)].

    La ventana está centrada (fotogramas antes y después), así una bajada sostenida
    de FPS no cuenta como tirón; de esas se encarga el apartado de bajadas.
    """
    fts = [ft for _, ft in frames]
    ordered, raw = sorted(fts[:half]), []
    for i, (t, ft) in enumerate(frames):
        if i + half < len(fts):
            bisect.insort(ordered, fts[i + half])
        if i - half - 1 >= 0:
            ordered.pop(bisect.bisect_left(ordered, fts[i - half - 1]))
        med = ordered[len(ordered) // 2]
        if len(ordered) >= 20 and ft >= 2 * med and ft - med >= 8:
            raw.append((t, ft, med))
    merged, last = [], None  # tirones seguidos (a menos de 0,25 s) cuentan como uno
    for st in raw:
        if merged and st[0] - last <= 0.25:
            if st[1] > merged[-1][1]:
                merged[-1] = st
        else:
            merged.append(st)
        last = st[0]
    return merged


def causes_at(s, hdr, base):
    """Qué pasaba en el sistema durante la muestra `s` que pueda explicar un tirón o bajada."""
    out = []
    rate, psi, gpu, game = s["rate"], s["psi"], s.get("gpu"), s.get("game")
    for key, _, _ in s.get("kernel", []):
        out.append(f"kernel: {kernlog.WHY.get(key, key)}")
    if rate["swapin"] >= 0.2:
        out.append("lectura de swap")
    if psi.get("mem", 0) >= 1:
        out.append("presión de memoria")
    if psi.get("io", 0) >= 5 or (game and game.get("read", 0) >= 30):
        out.append("esperas de disco / carga de datos")
    if gpu and hdr.get("vram_total") and gpu["vram"] >= 0.95 * hdr["vram_total"]:
        out.append("VRAM llena")
    for name, pct, _ in s.get("top", []):
        if pct >= 30 and pct >= 3 * base["bg"].get(name, 0) + 10:
            out.append(f"pico de '{name}' en segundo plano")
    if psi.get("cpu", 0) >= 20:
        out.append("contención de CPU")
    if game and base["game_cpu"] and game["cpu"] >= 1.4 * base["game_cpu"] + 40:
        out.append("pico de CPU del juego (¿compilando shaders o cargando?)")
    for key, sev, _ in rules.events(s, hdr):
        if sev == "bad" and key in ("cpu_temp", "gpu_temp", "gpu_clock"):
            out.append("temperatura al límite")
            break
    return out


def report_frames(path, offset, hdr, samples):
    try:
        frames, from_name = load_mangohud(path, offset)
    except (OSError, ValueError) as e:
        print(f"\n{ICON['warn']} No pude leer el log de MangoHud: {e}")
        return
    section(f"Fotogramas (MangoHud: {os.path.basename(path)})")
    fts = sorted(ft for _, ft in frames)
    avg = 1000 / statistics.fmean(fts)
    low1 = 1000 / fts[min(len(fts) - 1, int(len(fts) * 0.99))]
    low01 = 1000 / fts[min(len(fts) - 1, int(len(fts) * 0.999))]
    print(f"  FPS medios {bold(f'{avg:.0f}')} · 1% low {bold(f'{low1:.0f}')} · 0.1% low {bold(f'{low01:.0f}')}")
    if len(frames) > 2 and (frames[-1][0] - frames[0][0]) / len(frames) > 0.09:
        wrapped("El log parece muestreado (log_interval > 0) y se pierden tirones entre muestras. "
                "Pon log_interval=0 en ~/.config/MangoHud/MangoHud.conf para registrar todos los fotogramas.", 2)

    interval = hdr.get("interval", 1.0)
    times = [s["t"] for s in samples]
    if frames[-1][0] < times[0] or frames[0][0] > times[-1]:
        print(f"  {ICON['warn']} El log ({clock(frames[0][0])}–{clock(frames[-1][0])}) no coincide con la sesión "
              f"({clock(times[0])}–{clock(times[-1])}).")
        if not from_name:
            wrapped("No encontré la hora de inicio en el nombre del archivo; usa --offset SEGUNDOS para alinearlo.", 2)
        return

    def sample_at(t):
        i = bisect.bisect_left(times, t)  # la muestra i cubre el intervalo (t[i-1], t[i]]
        return samples[i] if i < len(times) and times[i] - t <= 2 * interval else None

    base = {
        "bg": collections.defaultdict(float),
        "game_cpu": statistics.median([s["game"]["cpu"] for s in samples if s.get("game")] or [0]),
    }
    for s in samples:
        for name, pct, _ in s.get("top", []):
            base["bg"][name] += pct / len(samples)

    # Bajadas de FPS: intervalos de muestreo con FPS por debajo del 75 % de la mediana
    per_sample = collections.defaultdict(list)
    for t, ft in frames:
        i = bisect.bisect_left(times, t)
        if i < len(times) and times[i] - t <= 2 * interval:
            per_sample[i].append(ft)
    fps = {i: 1000 / statistics.fmean(v) for i, v in per_sample.items() if len(v) >= 3}
    if fps:
        med = statistics.median(fps.values())
        drops = [i for i, f in fps.items() if f < 0.75 * med]
        if drops:
            print(f"\n  {bold('Bajadas de FPS')} (por debajo de {0.75 * med:.0f}, el 75 % de la mediana de {med:.0f}): "
                  f"{duration(len(drops) * interval)} en total")
            bns = collections.Counter(rules.bottleneck(samples[i]) for i in drops)
            causes = collections.Counter(c for i in drops for c in causes_at(samples[i], hdr, base))
            for key, n in bns.most_common():
                if key:
                    print(f"    {bar(100 * n / len(drops), 15, 101, 101)} {100 * n / len(drops):3.0f}%  {rules.BOTTLENECK[key]}")
            for cause, n in causes.most_common(5):
                print(f"    {bar(100 * n / len(drops), 15, 101, 101)} {100 * n / len(drops):3.0f}%  {cause}")
        else:
            print(f"\n  {ICON['ok']} Sin bajadas de FPS sostenidas (mediana {med:.0f} FPS).")

    stutters = find_stutters(frames)
    if not stutters:
        print(f"\n  {ICON['ok']} Sin tirones: ningún fotograma tardó más del doble de lo habitual.")
        return
    print(f"\n  {bold(str(len(stutters)) + ' tirones')} (fotogramas >2× lo habitual y +8 ms)")
    found = []
    for t, ft, med in stutters:
        s = sample_at(t)
        found.append((t, ft, med, causes_at(s, hdr, base) if s else None))
    counts = collections.Counter()
    for *_, causes in found:
        if causes is None:
            causes = ["sin datos de lagdoc en ese momento"]
        for c in causes or ["sin causa visible en el sistema"]:
            counts[c] += 1
    print("  Coincidieron con:")
    for cause, n in counts.most_common(8):
        print(f"    {bar(100 * n / len(found), 15, 101, 101)} {n:4d}  {cause}")
    if counts.get("sin causa visible en el sistema", 0) >= len(found) / 2:
        wrapped("Si la mayoría no tiene causa visible, vienen del propio juego: compilación de shaders (DXVK/VKD3D), "
                "carga de zonas o la sincronización del motor. Deja que Steam procese los shaders, prueba otra versión "
                "de Proton y fija un límite de FPS algo por debajo de tu media (MangoHud fps_limit) para suavizarlos.", 4)
    print("  Peores tirones:")
    for t, ft, med, causes in sorted(found, key=lambda x: -x[1])[:8]:
        why = ", ".join(causes) if causes else ("sin causa visible" if causes is not None else "sin datos")
        print(f"    {clock(t)}  {yellow(f'{ft:5.0f} ms')} {dim(f'(normal {med:.0f} ms)')}  {why}")


# ------------------------------------------------------------------- informe


def stat_rows(samples, game_samples, hdr):
    gpu_labels = sorted({k for s in samples if s.get("gpu") for k in s["gpu"]["temp"]})
    rows = [
        ("CPU uso %", [s["cpu"]["use"] for s in samples]),
        ("CPU núcleo más cargado %", [s["cpu"]["max"] for s in samples]),
        ("CPU temperatura °C", [s["cpu"]["temp"] for s in samples]),
        ("GPU uso %", [s["gpu"]["busy"] for s in samples if s.get("gpu")]),
        ("GPU reloj MHz", [s["gpu"]["sclk"] for s in samples if s.get("gpu")]),
        *((f"GPU {'hotspot' if k == 'junction' else k} °C", [s["gpu"]["temp"].get(k) for s in samples if s.get("gpu")])
          for k in gpu_labels),
        ("GPU potencia W", [s["gpu"]["power"] for s in samples if s.get("gpu")]),
        ("VRAM GB", [s["gpu"]["vram"] / 1024 for s in samples if s.get("gpu")]),
        ("RAM libre GB", [s["mem"]["avail"] / 1024 for s in samples]),
        ("Lectura de swap MB/s", [s["rate"]["swapin"] for s in samples]),
        ("Espera por CPU %", [s["psi"].get("cpu") for s in samples]),
        ("Espera por memoria %", [s["psi"].get("mem") for s in samples]),
        ("Espera por disco %", [s["psi"].get("io") for s in samples]),
    ]
    if game_samples:
        rows += [
            ("Juego: CPU % (100 = 1 núcleo)", [s["game"]["cpu"] for s in game_samples]),
            ("Juego: hilo más cargado %", [s["game"].get("thread_cpu") for s in game_samples]),
            ("Juego: lectura de disco MB/s", [s["game"].get("read") for s in game_samples]),
        ]
    return rows


def conclusions(bns, groups, interval, has_game):
    out = []
    total = sum(bns.values())
    share = {k: v / total for k, v in bns.items()} if total else {}
    if share.get("gpu", 0) >= 0.5:
        out.append(f"La GPU limita el {share['gpu']:.0%} del tiempo. Es lo esperado en juegos exigentes: para más FPS baja "
                   "resolución o calidad, o activa FSR. Si los FPS son bajos para tu tarjeta, mira las incidencias de "
                   "temperatura y potencia.")
    if share.get("cpu_hilo", 0) >= 0.25:
        out.append(f"El hilo principal del juego es el límite el {share['cpu_hilo']:.0%} del tiempo (típico en estrategia, "
                   "simuladores, MMOs o con FPS muy altos). Subir la calidad gráfica no te quitará FPS; bajar opciones de "
                   "CPU (distancia de visión, multitudes, físicas) sí ayuda. Usa game-performance (CachyOS) o gamemode "
                   "y prueba el planificador scx_lavd.")
    if share.get("cpu", 0) >= 0.25:
        out.append(f"La CPU entera está saturada el {share['cpu']:.0%} del tiempo: cierra lo que tengas en segundo plano.")
    if share.get("espera", 0) >= 0.4:
        out.append(f"El {share['espera']:.0%} del tiempo ni la CPU ni la GPU van al máximo. Si tienes límite de FPS o vsync "
                   "es normal; si no, el juego está esperando a algo (disco, shaders, sincronización): revisa las incidencias.")
    worst = sorted(groups.items(), key=lambda kv: (-SEV_RANK[kv[1]["sev"]], -len(kv[1]["times"])))
    for key, g in worst[:3]:
        if SEV_RANK[g["sev"]] >= SEV_RANK["warn"]:
            out.append(f"{g['msg']} — {duration(len(g['times']) * interval)} en total. {rules.advice(key)}")
    if not out:
        if has_game:
            out.append("No se vio ningún problema del sistema. Si notaste tirones, probablemente son del propio juego "
                       "(shaders, carga de zonas): graba con MangoHud y pásalo con --mangohud para confirmarlo.")
        else:
            out.append("No se detectó el juego. Repite con lagdoc monitor --game NOMBRE para que el análisis sea completo.")
    return out


def run_report(args):
    folder = args.session if args.session and os.path.isdir(args.session) else SESSIONS
    if args.list:
        found = sessions(folder)
        if not found:
            print("No hay sesiones grabadas todavía.")
        for p in found:
            print(f"{p}  {dim(f'{os.path.getsize(p) / 1024:.0f} KB')}")
        return 0
    path = args.session if args.session and not os.path.isdir(args.session) else (sessions(folder) or [None])[-1]
    if not path:
        print(f"No hay sesiones grabadas en {folder}. Ejecuta 'lagdoc monitor' mientras juegas.")
        return 1
    hdr, samples = load(path)
    if len(samples) < 2:
        print(f"La sesión {path} no tiene suficientes muestras.")
        return 1
    interval = hdr.get("interval", 1.0)
    game_samples = [s for s in samples if s.get("game")]

    print(bold("lagdoc · informe de sesión"))
    t0, t1 = samples[0]["t"], samples[-1]["t"]
    print(f"  {dim('Archivo')}  {path}")
    print(f"  {dim('Inicio')}   {time.strftime('%d/%m/%Y %H:%M:%S', time.localtime(t0))} · {duration(t1 - t0 + interval)} · {len(samples)} muestras")
    print(f"  {dim('Equipo')}   {hdr.get('cpu_model', '?')} · {hdr.get('gpu_name') or 'GPU sin datos'} · kernel {hdr.get('kernel', '?')}")
    games = collections.Counter(s["game"]["name"] for s in game_samples)
    if games:
        print(f"  {dim('Juego')}    " + ", ".join(f"{n} ({duration(c * interval)})" for n, c in games.most_common(3)))
    else:
        print(f"  {ICON['warn']} No se detectó ningún juego; usa lagdoc monitor --game NOMBRE la próxima vez.")

    bns = collections.Counter(b for s in samples if (b := rules.bottleneck(s)))
    if bns:
        section("¿Qué limita los FPS?")
        total = sum(bns.values())
        for key, n in bns.most_common():
            print(f"  {bar(100 * n / total, 20, 101, 101)} {100 * n / total:3.0f}%  {rules.BOTTLENECK[key]}")

    section("Recursos")
    print(dim(f"  {'':32} {'mín':>8} {'media':>8} {'máx':>8}"))
    for name, values in stat_rows(samples, game_samples, hdr):
        vals = [v for v in values if v is not None]
        if vals:
            print(f"  {name:32} {min(vals):8.1f} {statistics.fmean(vals):8.1f} {max(vals):8.1f}")

    groups = {}
    for s in samples:
        for key, sev, msg in rules.events(s, hdr):
            g = groups.setdefault(key, {"sev": sev, "msg": msg, "times": []})
            g["times"].append(s["t"])
            if SEV_RANK[sev] > SEV_RANK[g["sev"]]:
                g["sev"], g["msg"] = sev, msg
    section("Incidencias")
    if not groups:
        print(f"  {ICON['ok']} Nada raro en temperaturas, memoria, disco, procesos de fondo ni log del kernel.")
    for key, g in sorted(groups.items(), key=lambda kv: (-SEV_RANK[kv[1]["sev"]], -len(kv[1]["times"]))):
        n = len(spans(g["times"], 3 * interval))
        print(f"  {ICON[g['sev']]} {g['msg'][:160]}")
        print(f"      {dim(f'{n} vez' if n == 1 else f'{n} veces')} · {duration(len(g['times']) * interval)} en total · "
              f"{clock(g['times'][0])}" + (f"–{clock(g['times'][-1])}" if len(g["times"]) > 1 else ""))
        if adv := rules.advice(key):
            wrapped("→ " + adv)

    if args.mangohud:
        report_frames(args.mangohud, args.offset, hdr, samples)

    section("Conclusión")
    for i, text in enumerate(conclusions(bns, groups, interval, bool(game_samples)), 1):
        lines = textwrap.wrap(text, 96)
        print(f"  {bold(str(i) + '.')} {lines[0]}")
        for line in lines[1:]:
            print(f"     {line}")
    return 0
