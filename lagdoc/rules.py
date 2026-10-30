"""Reglas compartidas por el monitor en vivo y el informe.

Convierten una muestra en un "cuello de botella" y en una lista de eventos
(clave, severidad, mensaje). El informe vuelve a aplicarlas sobre la sesión
grabada, así que cambiar un umbral aquí afecta a ambos.
"""

from . import kernlog

BOTTLENECK = {
    "gpu": "GPU al límite",
    "cpu_hilo": "CPU: hilo principal del juego al límite",
    "cpu": "CPU entera al límite",
    "espera": "Ni CPU ni GPU: límite de FPS/vsync, o el juego espera (disco, shaders)",
    "mixto": "Equilibrado (ninguno al máximo)",
}
BOTTLENECK_SHORT = {"gpu": "GPU", "cpu_hilo": "hilo CPU", "cpu": "CPU", "espera": "espera", "mixto": "equilibrado"}

# Procesos en segundo plano conocidos: (explicación, % CPU a partir del cual avisar)
BACKGROUND = {
    "fossilize_repla": ("Steam está precompilando shaders en segundo plano", 20),
    "kswapd0": ("el kernel está liberando RAM: memoria al límite", 10),
    "kcompactd0": ("el kernel está compactando memoria (puede pausar al juego)", 10),
    "baloo_file": ("indexador de archivos de KDE", 30),
    "baloo_file_extr": ("indexador de archivos de KDE", 30),
    "tracker-miner-f": ("indexador de archivos de GNOME", 30),
    "packagekitd": ("actualizaciones en segundo plano", 30),
    "pacman": ("pacman instalando o actualizando paquetes", 30),
    "cc1plus": ("hay una compilación en marcha", 30),
    "cc1": ("hay una compilación en marcha", 30),
    "rustc": ("hay una compilación en marcha", 30),
    "updatedb": ("indexado de locate", 30),
    "btrfs-transacti": ("btrfs escribiendo transacciones", 20),
    "steamwebhelper": ("la interfaz web de Steam (tienda, overlay, descargas)", 40),
    "obs": ("OBS grabando o emitiendo", 40),
    "wineserver": ("Wine/Proton sincronizando hilos; si va alto, falta ntsync o fsync", 25),
}

ADVICE = {
    "cpu_temp": "Revisa la refrigeración de la CPU (pasta térmica, curva de ventiladores, flujo de aire). "
                "En X3D, un undervolt con Curve Optimizer en la BIOS baja bastante la temperatura.",
    "gpu_temp": "Limpia el polvo de la GPU, mejora el flujo de aire de la caja o sube la curva de ventiladores "
                "(LACT o CoreCtrl). Si el hotspot está >25 °C por encima del borde, la pasta térmica puede estar seca.",
    "gpu_clock": "La GPU trabaja pero no sube de reloj: suele ser temperatura o límite de potencia. "
                 "Revisa temperaturas y que no hayas bajado el power limit en LACT/CoreCtrl.",
    "vram": "Baja la calidad de texturas o la resolución: con la VRAM llena el juego tira de la RAM del sistema por PCIe "
            "y aparecen tirones.",
    "ram": "Cierra programas (navegador, Discord...) o amplía la RAM. Un zram evita que el sistema se quede sin memoria.",
    "swap": "El sistema está leyendo de swap: cierra programas, añade zram o baja vm.swappiness (p. ej. 10).",
    "psi_mem": "Hay procesos esperando memoria: cierra programas o añade zram.",
    "psi_io": "El juego espera al disco (carga de datos o compilación de shaders). Ponlo en un SSD/NVMe, activa "
              "'Shader Pre-Caching' en Steam y deja que termine de procesar shaders antes de jugar.",
    "psi_cpu": "Hay más trabajo que núcleos libres: cierra programas en segundo plano o prueba un planificador "
               "sched-ext como scx_lavd.",
    "cpu_freq": "La CPU no sube de frecuencia con carga: lanza el juego con game-performance (CachyOS) o gamemode, "
                "y revisa la temperatura.",
    "bg": "Cierra o pausa ese programa mientras juegas.",
    "gpu_bg": "Otro programa usa la GPU (vídeo en el navegador, fondo animado, OBS...). Ciérralo o desactiva su "
              "aceleración por hardware.",
    "k_gpu_hang": "Si tienes overclock/undervolt en la GPU, quítalo. Actualiza mesa y linux-firmware; si solo pasa "
                  "en un juego, prueba otra versión de Proton.",
    "k_gpu_fault": "Suele ser un bug del driver o del juego: actualiza mesa y prueba otra versión de Proton.",
    "k_nvidia_xid": "Actualiza el driver de NVIDIA y quita overclocks.",
    "k_oom": "Te quedaste sin RAM y el kernel cerró un proceso. Cierra programas o añade swap/zram.",
    "k_split_lock": "Desactiva la penalización: echo 'kernel.split_lock_mitigate = 0' | sudo tee /etc/sysctl.d/99-splitlock.conf",
    "k_clocksource": "Actualiza la BIOS. Como último recurso, añade tsc=reliable a los parámetros del kernel.",
    "k_clock_wd": "Normalmente inofensivo. Solo es grave si después aparece 'unstable' y el reloj cambia a hpet.",
    "k_mce": "Desactiva EXPO/XMP, overclocks o undervolts (PBO/Curve Optimizer) y prueba la RAM con memtest86+.",
    "k_pcie_aer": "Comprueba que la GPU/NVMe estén bien asentados; con riser, fija PCIe a Gen4 en la BIOS.",
    "k_thermal": "La CPU se calentó hasta bajar frecuencia: revisa la refrigeración.",
    "k_disk": "Revisa la salud del disco (sudo smartctl -a /dev/...) y el cable/puerto SATA.",
    "k_flip_timeout": "El driver de pantalla se atascó presentando un frame: actualiza mesa/kernel; si usas varios "
                      "monitores, prueba con uno solo.",
    "k_usb": "Si coincide con tirones o pérdida de control, revisa el cable o el hub del mando/ratón.",
}


def advice(key):
    return ADVICE.get(key) or ADVICE.get(key.split(":")[0], "")


def bottleneck(s):
    game, gpu = s.get("game"), s.get("gpu")
    if not game or not gpu or game.get("thread") is None:  # sin datos de hilos todavía, o juego en pausa
        return None
    busy = gpu["busy"]
    if busy >= 95:
        return "gpu"
    if busy < 85:
        if (game.get("thread_cpu") or 0) >= 85:
            return "cpu_hilo"
        if s["cpu"]["use"] >= 90:
            return "cpu"
    if busy < 70:
        return "espera"
    return "mixto"


def events(s, info):
    """Eventos de una muestra: [(clave, severidad, mensaje)]."""
    out = []
    cpu, gpu, mem, rate, psi, game = s["cpu"], s.get("gpu"), s["mem"], s["rate"], s["psi"], s.get("game")

    tmax, t = info.get("cpu_tmax", 95), cpu.get("temp")
    if t is not None and t >= tmax - 2:
        out.append(("cpu_temp", "bad", f"CPU a {t:.0f} °C: en su límite ({tmax} °C), está bajando frecuencia"))
    elif t is not None and t >= tmax - 8:
        out.append(("cpu_temp", "warn", f"CPU caliente: {t:.0f} °C (límite {tmax} °C)"))

    max_mhz = info.get("cpu_max_mhz") or 0
    if cpu["max"] >= 90 and cpu.get("mhz_busy") and max_mhz and cpu["mhz_busy"] < 0.7 * max_mhz:
        out.append(("cpu_freq", "warn", f"CPU con carga pero a {cpu['mhz_busy']} MHz (máx {max_mhz}): ahorro de energía o throttling"))

    if gpu:
        for label, v in gpu["temp"].items():
            crit = info.get("gpu_crit", {}).get(label)
            name = "hotspot" if label == "junction" else label
            if crit and v >= crit - 5:
                out.append(("gpu_temp", "bad", f"GPU {name} a {v} °C: en su límite ({crit:.0f} °C), está bajando reloj"))
            elif crit and v >= crit - 12:
                out.append(("gpu_temp", "warn", f"GPU {name} caliente: {v} °C (límite {crit:.0f} °C)"))
        sclk_max = info.get("sclk_max") or 0
        if gpu["busy"] >= 90 and gpu.get("sclk") and sclk_max and gpu["sclk"] < 0.6 * sclk_max:
            out.append(("gpu_clock", "warn", f"GPU al {gpu['busy']} % pero a {gpu['sclk']} MHz (máx {sclk_max}): temperatura o límite de potencia"))
        total = info.get("vram_total") or 0
        if total and gpu["vram"] >= 0.95 * total:
            out.append(("vram", "bad", f"VRAM llena: {gpu['vram'] / 1024:.1f} de {total / 1024:.1f} GB"))
        elif total and gpu["vram"] >= 0.90 * total:
            out.append(("vram", "warn", f"VRAM casi llena: {gpu['vram'] / 1024:.1f} de {total / 1024:.1f} GB"))

    if mem["avail"] < max(1024, 0.05 * mem["total"]):
        out.append(("ram", "bad", f"RAM casi agotada: quedan {mem['avail'] / 1024:.1f} GB"))
    elif mem["avail"] < 0.10 * mem["total"]:
        out.append(("ram", "warn", f"Queda poca RAM: {mem['avail'] / 1024:.1f} GB libres"))
    if rate["swapin"] >= 0.5:
        out.append(("swap", "bad", f"Leyendo de swap a {rate['swapin']:.1f} MB/s: provoca tirones"))
    elif rate["swapout"] >= 2:
        out.append(("swap", "warn", f"Enviando memoria a swap ({rate['swapout']:.1f} MB/s)"))
    if psi.get("mem", 0) >= 5:
        out.append(("psi_mem", "bad" if psi["mem"] >= 20 else "warn", f"Presión de memoria: {psi['mem']:.0f} % del tiempo esperando RAM"))
    if psi.get("io", 0) >= 20:
        who = f" (el juego lee {game['read']:.0f} MB/s)" if game and game.get("read", 0) >= 1 else ""
        out.append(("psi_io", "bad" if psi["io"] >= 50 else "warn", f"Esperas de disco: {psi['io']:.0f} % del tiempo{who}"))
    if psi.get("cpu", 0) >= 40:
        out.append(("psi_cpu", "warn", f"Contención de CPU: {psi['cpu']:.0f} % del tiempo hay hilos esperando núcleo"))

    if game:
        for name, pct, _ in s.get("top", []):
            why, limit = BACKGROUND.get(name, (None, 40))
            if pct >= limit:
                extra = f": {why}" if why else ""
                out.append((f"bg:{name}", "warn", f"'{name}' usa {pct:.0f} % de CPU en segundo plano{extra}"))
        for name, pct in s.get("gtop", []):
            if pct >= 10:
                out.append((f"gpu_bg:{name}", "warn", f"'{name}' usa {pct:.0f} % de la GPU mientras juegas"))

    for key, sev, msg in s.get("kernel", []):
        out.append((f"k_{key}", sev, f"kernel: {kernlog.WHY.get(key, key)} — {msg}"))
    return out
