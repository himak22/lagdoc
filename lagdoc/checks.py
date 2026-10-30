"""`lagdoc check`: revisión de la configuración en busca de causas típicas de lag."""

import glob
import json
import os
import re
import resource
import shutil
import textwrap

from . import kernlog
from .probe import Cpu, Gpu, meminfo, read, read_int, run
from .term import ICON, SEV_RANK, bold, cyan, dim, red, yellow

OK, INFO, WARN, BAD = "ok", "info", "warn", "bad"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq"


def os_name():
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', read("/etc/os-release") or "", re.M)
    return m.group(1) if m else "Linux"


def is_cachyos():
    return "cachyos" in os_name().lower()


def perf_launcher():
    """Cómo poner la CPU en modo rendimiento al lanzar un juego en esta distro."""
    return "game-performance" if shutil.which("game-performance") else "gamemode"


def bracketed(path):
    """Valor activo de ficheros tipo 'always [madvise] never'."""
    m = re.search(r"\[(\S+)\]", read(path) or "")
    return m.group(1) if m else None


def device_of(path):
    """Dispositivo de bloque (sda2, nvme0n1p2...) donde está una ruta."""
    src = run("findmnt", "-no", "SOURCE", "-T", path).strip()
    return os.path.basename(src.split("[")[0]) if src.startswith("/dev/") else None


def rotational(dev):
    """True si el disco es mecánico (HDD), None si no se sabe."""
    real = os.path.realpath(f"/sys/class/block/{dev}")
    for cand in (real, os.path.dirname(real)):  # una partición hereda el valor del disco
        v = read(f"{cand}/queue/rotational")
        if v is not None:
            return v == "1"
    return None


# ------------------------------------------------------------------ secciones


def check_system():
    out = []
    kernel = os.uname().release
    if "cachyos" in kernel:
        out.append((OK, f"{os_name()}, kernel {kernel}", None))
    elif is_cachyos():
        out.append((WARN, f"{os_name()} con un kernel que no es de CachyOS ({kernel})",
                    "Arrancaste un kernel genérico: pierdes BORE y los ajustes de latencia de linux-cachyos. "
                    "Elige linux-cachyos en el menú de arranque."))
    else:
        out.append((INFO, f"{os_name()}, kernel {kernel}",
                    "No es CachyOS. Si juegas en otra instalación, ejecuta lagdoc allí: la configuración que se "
                    "revisa aquí (energía, memoria, pantalla, kernel) es la de este sistema."))

    state = read("/sys/kernel/sched_ext/state")
    scx = state == "enabled"
    if scx:
        detail = run("scxctl", "get").strip() or None  # p. ej. "running LAVD in Gaming mode"
        out.append((OK, f"Planificador sched-ext activo: {read('/sys/kernel/sched_ext/root/ops') or '?'}", detail))
    elif state is not None:
        where = "CachyOS Kernel Manager (sched-ext)" if is_cachyos() else "el paquete scx-scheds"
        out.append((INFO, "Sin planificador sched-ext",
                    "Si notas tirones cuando hay algo en segundo plano (compilar, emitir, descargar), un planificador "
                    f"como scx_lavd o scx_bpfland suele mantener los FPS más estables. Se activa desde {where}."))
    if run("systemctl", "is-active", "ananicy-cpp").strip() == "active":
        if scx:
            out.append((INFO, "ananicy-cpp activo junto a un planificador sched-ext",
                        "Los dos tocan las prioridades de los procesos. Si notas tirones, prueba a pararlo un rato: "
                        "sudo systemctl stop ananicy-cpp"))
        else:
            out.append((OK, "ananicy-cpp activo (ajusta prioridades de procesos)", None))
    for ps in glob.glob("/sys/class/power_supply/*"):
        if read(f"{ps}/type") == "Mains" and read(f"{ps}/online") == "0":
            out.append((WARN, "Portátil funcionando con batería",
                        "Con batería la CPU y la GPU limitan su potencia. Conecta el cargador para jugar."))
    return out


def check_cpu():
    out = []
    cpu = Cpu()
    out.append((OK, f"{cpu.model} · {cpu.count} hilos · hasta {cpu.max_mhz} MHz", None))
    driver = read(f"{CPUFREQ}/scaling_driver")
    if driver is None:
        out.append((WARN, "No hay control de frecuencia de CPU (cpufreq)",
                    "La CPU podría quedarse a una frecuencia fija. Revisa en la BIOS que Cool'n'Quiet/SpeedStep "
                    "y los estados C estén activados."))
        return out
    govs = {read(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")} - {None}
    epp = read(f"{CPUFREQ}/energy_performance_preference")
    if "powersave" in govs and not epp:
        out.append((BAD, f"Governor powersave con {driver}",
                    "Con este driver 'powersave' deja la CPU a la frecuencia mínima. "
                    "Usa schedutil o performance: sudo cpupower frequency-set -g schedutil"))
    else:
        out.append((OK, f"Driver {driver}, governor {', '.join(sorted(govs))}", None))
    if epp == "performance":
        out.append((OK, "EPP = performance", None))
    elif epp == "balance_performance":
        out.append((INFO, "EPP = balance_performance",
                    f"Correcto para el día a día. Para jugar, {perf_launcher()} (o el perfil 'performance') lo cambia "
                    "a 'performance' y la CPU sube de frecuencia antes."))
    elif epp:
        out.append((WARN, f"EPP = {epp}: la CPU prioriza ahorrar energía",
                    "Los núcleos tardan en subir de frecuencia y eso se nota en bajadas de FPS. "
                    f"Cambia el perfil de energía a rendimiento o usa {perf_launcher()}."))
    boost = read(f"{CPUFREQ}/boost") or read("/sys/devices/system/cpu/cpufreq/boost")
    if boost == "0":
        out.append((BAD, "Turbo (boost) desactivado",
                    "La CPU no pasa de su frecuencia base: echo 1 | sudo tee /sys/devices/system/cpu/cpufreq/boost"))
    smax, cmax = read_int(f"{CPUFREQ}/scaling_max_freq"), read_int(f"{CPUFREQ}/cpuinfo_max_freq")
    if smax and cmax and smax < cmax * 0.95:
        out.append((BAD, f"Frecuencia máxima limitada a {smax // 1000} MHz (la CPU llega a {cmax // 1000})",
                    "Algo (TLP, auto-cpufreq, un perfil de energía) está capando la CPU."))
    if "X3D" in cpu.model:
        out.append((INFO, f"CPU X3D: baja frecuencia a partir de ~{cpu.tmax} °C",
                    "Es normal ver 75-85 °C jugando; lagdoc usa ese límite para avisar de temperatura."))
        modes = glob.glob("/sys/bus/platform/drivers/amd_x3d_vcache/*/amd_x3d_mode")
        if modes and (mode := read(modes[0])) != "cache":
            out.append((WARN, f"Modo X3D = {mode}",
                        f"En CPUs con dos CCD, el modo 'cache' manda los juegos al CCD con V-Cache: "
                        f"echo cache | sudo tee {modes[0]}"))
    profile = run("powerprofilesctl", "get").strip()
    if profile == "power-saver":
        out.append((WARN, "Perfil de energía: power-saver",
                    "Limita CPU y GPU. Cámbialo: powerprofilesctl set performance (o balanced)"))
    elif profile:
        out.append((OK, f"Perfil de energía: {profile}", None))
    for svc in ("tlp", "auto-cpufreq"):
        if run("systemctl", "is-active", svc).strip() == "active":
            out.append((INFO, f"{svc} está activo",
                        "Está pensado para portátiles y puede limitar la CPU. En un PC de sobremesa no hace falta."))
    return out


def check_gpu():
    out = []
    gpu = Gpu.find()
    if not gpu:
        vendors = {read(f"{d}/vendor") for d in glob.glob("/sys/class/drm/card*/device")}
        if "0x10de" in vendors:
            out.append((INFO, "GPU NVIDIA detectada",
                        "De momento lagdoc solo lee GPUs AMD; CPU, RAM, disco y log del kernel sí funcionan."))
        else:
            out.append((INFO, "No se encontró una GPU AMD", None))
        return out
    d = gpu.dev
    out.append((OK, f"{gpu.name} · {gpu.vram_total / 1024:.0f} GB VRAM · driver amdgpu", None))

    level = read(f"{d}/power_dpm_force_performance_level")
    if level == "low":
        out.append((BAD, "GPU forzada a bajo consumo (performance_level = low)",
                    f"echo auto | sudo tee {d}/power_dpm_force_performance_level"))
    elif level == "manual":
        out.append((INFO, "GPU en control manual (performance_level = manual)",
                    "Lo habrá puesto LACT, CoreCtrl u otra herramienta de overclock. Si hay cuelgues o tirones, "
                    "prueba con los valores de fábrica."))
    elif level:
        out.append((OK, f"Gestión de energía de la GPU: {level}", None))

    m = re.search(r"^\s*\d+\s+(\S+?)\s*\*", read(f"{d}/pp_power_profile_mode") or "", re.M)
    if m and m.group(1) == "POWER_SAVING":
        out.append((WARN, "Perfil de potencia de la GPU: POWER_SAVING",
                    "Prioriza consumo frente a rendimiento. Vuelve a BOOTUP_DEFAULT o 3D_FULL_SCREEN (LACT/CoreCtrl)."))
    elif m:
        out.append((OK, f"Perfil de potencia de la GPU: {m.group(1)}", None))

    cap, cap_def = read_int(f"{gpu.hw}/power1_cap"), read_int(f"{gpu.hw}/power1_cap_default")
    if cap and cap_def and cap < cap_def * 0.95:
        out.append((WARN, f"Límite de potencia reducido: {cap / 1e6:.0f} W (de fábrica {cap_def / 1e6:.0f} W)",
                    "Consume menos, pero también da menos FPS. Si no lo bajaste a propósito, revisa LACT/CoreCtrl."))
    elif cap:
        out.append((OK, f"Límite de potencia: {cap / 1e6:.0f} W", None))

    width, max_width = read_int(f"{d}/current_link_width"), read_int(f"{d}/max_link_width")
    speed, max_speed = read(f"{d}/current_link_speed"), read(f"{d}/max_link_speed")
    if width and max_width and width < max_width:
        out.append((WARN, f"PCIe x{width} (la tarjeta admite x{max_width})",
                    "Comprueba que esté en la ranura principal y bien asentada; un riser o una ranura x4 la limitan."))
    elif speed and max_speed and speed != max_speed:
        out.append((INFO, f"PCIe a {speed} (máx {max_speed})",
                    "En reposo es normal que baje. Si jugando sigue bajo, revisa la BIOS y la ranura."))
    elif speed:
        out.append((OK, f"PCIe {speed.removesuffix(' PCIe')} x{width}", None))

    icds = [os.path.basename(p) for p in glob.glob("/usr/share/vulkan/icd.d/*.json") + glob.glob("/etc/vulkan/icd.d/*.json")]
    if not any(n.startswith("radeon_icd") for n in icds):
        out.append((BAD, "Falta el driver Vulkan RADV",
                    "Sin él, Proton/DXVK no funcionan bien: sudo pacman -S vulkan-radeon lib32-vulkan-radeon"))
    elif any(n.startswith("amd_icd") for n in icds):
        out.append((WARN, "AMDVLK instalado junto a RADV",
                    "Algunos juegos pueden elegir AMDVLK, que suele ir peor con Proton. Desinstálalo "
                    "(amdvlk, lib32-amdvlk) o exporta AMD_VULKAN_ICD=RADV."))
    else:
        mesa = re.search(r"^mesa (?:\d+:)?(\S+)", run("pacman", "-Q", "mesa"), re.M)
        out.append((OK, "Vulkan: RADV" + (f" (mesa {mesa.group(1)})" if mesa else ""), None))
    if shutil.which("pacman") and "lib32-vulkan-radeon" not in run("pacman", "-Q", "lib32-vulkan-radeon"):
        out.append((WARN, "Falta lib32-vulkan-radeon",
                    "Steam y los juegos de 32 bits lo necesitan: sudo pacman -S lib32-vulkan-radeon"))

    prefixes = ("RADV_", "AMD_VULKAN", "MESA_", "DXVK_", "VKD3D_", "PROTON_", "WINE", "__GL_", "MANGOHUD", "ENABLE_")
    env = sorted(k for k in os.environ if k.startswith(prefixes))
    if env:
        out.append((INFO, "Variables de entorno globales que afectan a los juegos: " + ", ".join(env),
                    "Si las pusiste para probar algo y ya no las necesitas, quítalas."))
    return out


def check_display():
    out = []
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    session = os.environ.get("XDG_SESSION_TYPE", "?")
    if session == "x11":
        out.append((INFO, f"Escritorio {desk} en X11",
                    "Con varios monitores de distinta frecuencia, X11 puede limitar el juego al más lento o dar tirones. "
                    "Wayland lo gestiona mejor."))
    else:
        out.append((OK, f"Escritorio {desk} ({session})", None))
    if "Hyprland" in desk and shutil.which("hyprctl"):
        try:
            monitors = json.loads(run("hyprctl", "-j", "monitors") or "[]")
            vrr = json.loads(run("hyprctl", "-j", "getoption", "misc:vrr") or "{}").get("int")
        except json.JSONDecodeError:
            monitors, vrr = [], None
        for mon in monitors:
            res = f"{mon['width']}x{mon['height']}"
            rates = [float(r.split("@")[1].rstrip("Hz")) for r in mon.get("availableModes", []) if r.startswith(res + "@")]
            best = max(rates, default=mon["refreshRate"])
            if mon["refreshRate"] < best - 1:
                out.append((WARN, f"{mon['name']}: {res} a {mon['refreshRate']:.0f} Hz, pero admite {best:.0f} Hz",
                            f"En hyprland.conf: monitor = {mon['name']}, {res}@{best:.2f}, auto, 1"))
            else:
                out.append((OK, f"{mon['name']}: {res} a {mon['refreshRate']:.0f} Hz", None))
        if vrr == 0:
            out.append((INFO, "VRR/FreeSync desactivado (misc:vrr = 0)",
                        "Si tus monitores tienen FreeSync, misc:vrr = 2 (solo en pantalla completa) hace que las "
                        "bajadas de FPS se noten mucho menos."))
        elif vrr:
            out.append((OK, f"VRR activado (misc:vrr = {vrr})", None))
    elif "KDE" in desk:
        out.append((INFO, "Revisa en Ajustes → Pantalla la frecuencia de cada monitor y la 'Sincronización adaptativa'", None))
    elif "GNOME" in desk:
        out.append((INFO, "Revisa en Ajustes → Pantallas la frecuencia de cada monitor y la VRR", None))
    return out


def check_memory():
    out = []
    mem = meminfo()
    gb = mem["total"] / 1024
    if gb < 15:
        out.append((WARN, f"RAM: {gb:.0f} GB", "Muchos juegos actuales piden 16 GB; con menos habrá tirones por swap."))
    else:
        out.append((OK, f"RAM: {gb:.0f} GB", None))

    swaps = [line.split() for line in (read("/proc/swaps") or "").splitlines()[1:]]
    zram = [s for s in swaps if "zram" in s[0]]
    disk = [s for s in swaps if "zram" not in s[0]]
    if not swaps:
        out.append((WARN if gb < 32 else INFO, "Sin swap",
                    "Si la RAM se llena, el kernel cerrará el juego. Un zram (paquete zram-generator) es barato y lo evita."))
    if zram:
        out.append((OK, f"zram activo ({', '.join(os.path.basename(s[0]) for s in zram)})", None))
    for s in disk:
        dev = os.path.basename(s[0]) if s[1] == "partition" else device_of(s[0])
        if dev and rotational(dev):
            out.append((WARN, f"Swap en disco duro mecánico ({s[0]})",
                        "Si el sistema tira de swap, en un HDD los tirones son enormes. Usa zram o pon la swap en un SSD."))
        else:
            out.append((OK, f"Swap en {s[0]} ({int(s[2]) / 1024 / 1024:.0f} GB)", None))
    swappiness = read_int("/proc/sys/vm/swappiness")
    if disk and not zram and swappiness is not None and swappiness >= 60:
        out.append((INFO, f"vm.swappiness = {swappiness} con swap en disco",
                    "Cuando la RAM aprieta, el kernel manda a swap trozos del juego o del escritorio y al volver a usarlos "
                    "hay tirones. Prueba vm.swappiness = 10, o usa zram (lo que trae CachyOS por defecto)."))

    thp, defrag = bracketed("/sys/kernel/mm/transparent_hugepage/enabled"), bracketed("/sys/kernel/mm/transparent_hugepage/defrag")
    if defrag == "always":
        out.append((WARN, "THP defrag = always",
                    "El kernel puede pausar al juego para compactar memoria. "
                    "echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/defrag"))
    elif thp:
        out.append((OK, f"Transparent hugepages: {thp} (defrag {defrag})", None))

    mmc = read_int("/proc/sys/vm/max_map_count")
    if mmc is not None and mmc < 1048576:
        out.append((WARN, f"vm.max_map_count = {mmc}",
                    "Algunos juegos (CS2, Hogwarts Legacy, Star Citizen...) se cuelgan o dan tirones con un valor bajo: "
                    "echo 'vm.max_map_count = 1048576' | sudo tee /etc/sysctl.d/80-gamecompat.conf && sudo sysctl --system"))
    elif mmc:
        out.append((OK, f"vm.max_map_count = {mmc}", None))
    return out


def check_kernel():
    out = []
    cs = read("/sys/devices/system/clocksource/clocksource0/current_clocksource")
    if cs and cs != "tsc":
        out.append((BAD, f"Reloj del sistema: {cs} (debería ser tsc)",
                    "El kernel decidió que el TSC no es fiable y usa un reloj mucho más lento: stutter en todo. "
                    "Busca 'clocksource' en journalctl -k. Actualizar la BIOS suele arreglarlo; como último recurso, "
                    "el parámetro de kernel tsc=reliable."))
    elif cs:
        out.append((OK, "Reloj del sistema: tsc", None))

    flags = re.search(r"^flags\s*:(.*)$", read("/proc/cpuinfo") or "", re.M)
    flags = flags.group(1).split() if flags else []
    if "split_lock_detect" in flags or "bus_lock_detect" in flags:
        if read_int("/proc/sys/kernel/split_lock_mitigate") == 1:
            out.append((WARN, "kernel.split_lock_mitigate = 1",
                        "Si un juego provoca 'split locks', el kernel lo pausa a propósito y hay tirones fuertes "
                        "(CachyOS lo desactiva): echo 'kernel.split_lock_mitigate = 0' | sudo tee /etc/sysctl.d/99-splitlock.conf"))
        else:
            out.append((OK, "split_lock_mitigate desactivado", None))

    if os.path.exists("/dev/ntsync"):
        out.append((OK, "ntsync disponible", None))
    elif run("modinfo", "-n", "ntsync").strip():
        out.append((INFO, "El módulo ntsync no está cargado",
                    "Wine 11 y los Proton recientes lo usan para sincronizar hilos con menos sobrecarga que esync/fsync. "
                    "sudo modprobe ntsync y, para que cargue siempre: echo ntsync | sudo tee /etc/modules-load.d/ntsync.conf"))

    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft < 524288:
        out.append((WARN, f"Límite de archivos abiertos bajo ({soft})",
                    "esync de Proton necesita al menos 524288: revisa DefaultLimitNOFILE en /etc/systemd/system.conf y user.conf"))
    else:
        out.append((OK, f"Límite de archivos abiertos: {soft}", None))
    return out


def steam_libraries():
    roots = ["~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
             "~/.var/app/com.valvesoftware.Steam/data/Steam"]
    libs = set()
    for root in map(os.path.expanduser, roots):
        vdf = read(f"{root}/steamapps/libraryfolders.vdf")
        if vdf:
            libs.add(os.path.realpath(root))
            libs.update(os.path.realpath(p) for p in re.findall(r'"path"\s+"([^"]+)"', vdf) if os.path.isdir(p))
    return sorted(libs)


def check_tools():
    out = []
    if shutil.which("game-performance"):
        # Script de cachyos-settings: lanza el juego con el perfil 'performance' de power-profiles-daemon
        if run("systemctl", "is-active", "power-profiles-daemon").strip() == "active":
            out.append((OK, "game-performance disponible (CachyOS)",
                        "Úsalo en Steam con: game-performance %command%"))
        else:
            out.append((WARN, "game-performance no funcionará: power-profiles-daemon no está activo",
                        "sudo systemctl enable --now power-profiles-daemon"))
    elif shutil.which("gamemoderun"):
        out.append((OK, "gamemode instalado", "Úsalo en Steam con: gamemoderun %command%"))
    else:
        out.append((INFO, "gamemode no instalado",
                    "Pone CPU y GPU en modo rendimiento mientras juegas: sudo pacman -S gamemode lib32-gamemode "
                    "y en las opciones de lanzamiento de Steam: gamemoderun %command%"))
    if shutil.which("mangohud"):
        out.append((OK, "MangoHud instalado", "lagdoc report --mangohud LOG.csv cruza su log con tu sesión."))
    else:
        out.append((INFO, "MangoHud no instalado",
                    "Muestra FPS y frametimes; lagdoc puede cruzar su log con la sesión para explicar cada tirón: "
                    "sudo pacman -S mangohud lib32-mangohud"))
    if is_cachyos():
        if "proton-cachyos" in run("pacman", "-Q", "proton-cachyos"):
            out.append((OK, "proton-cachyos instalado",
                        "Elígelo por juego en Steam → Propiedades → Compatibilidad si el Proton oficial da problemas."))
        else:
            out.append((INFO, "proton-cachyos no instalado",
                        "Es el Proton compilado y parcheado por CachyOS; a veces va mejor que el oficial: "
                        "sudo pacman -S proton-cachyos"))
    libs = steam_libraries()
    if not libs:
        out.append((INFO, "No encontré Steam", None))
    for lib in libs:
        dev = device_of(lib)
        if dev and rotational(dev):
            out.append((WARN, f"Biblioteca de Steam en disco mecánico: {lib}",
                        "Los juegos actuales cargan datos mientras juegas; en un HDD eso da tirones. Muévela a un SSD."))
        else:
            out.append((OK, f"Biblioteca de Steam: {lib}" + (f" ({dev})" if dev else ""), None))
    return out


def check_kernel_log(boot):
    which = "este arranque" if boot == 0 else f"el arranque {boot}"
    events, err = kernlog.scan_boot(boot)
    if err:
        return [(INFO, "No pude leer el log del kernel", err)]
    if not events:
        return [(OK, f"Sin errores relevantes del kernel en {which}", None)]
    groups = {}
    for _, key, sev, msg in events:
        g = groups.setdefault(key, [sev, 0, msg])
        g[1] += 1
        g[2] = msg
    out = []
    for key, (sev, count, last) in sorted(groups.items(), key=lambda kv: -SEV_RANK[kv[1][0]]):
        out.append((sev, f"{kernlog.WHY[key]} ({count}× en {which})", f"Último: {last[:220]}"))
    return out


# ------------------------------------------------------------------- salida


def run_checks(args):
    sections = [
        ("Sistema", check_system()),
        ("CPU", check_cpu()),
        ("GPU", check_gpu()),
        ("Pantalla", check_display()),
        ("Memoria", check_memory()),
        ("Kernel", check_kernel()),
        ("Herramientas para jugar", check_tools()),
        ("Log del kernel", check_kernel_log(args.boot)),
    ]
    width = min(shutil.get_terminal_size((100, 30)).columns, 110)
    print(bold("lagdoc · revisión del sistema"))
    counts = dict.fromkeys(ICON, 0)
    for title, items in sections:
        print(f"\n{cyan('■')} {bold(title)}")
        for sev, msg, detail in items:
            counts[sev] += 1
            print(f"  {ICON[sev]} {msg}")
            if detail and (sev != OK or args.verbose):
                for line in textwrap.wrap(detail, width - 8):
                    print(f"      {dim(line)}")
    print()
    summary = [red(f"{counts['bad']} problemas"), yellow(f"{counts['warn']} avisos"), cyan(f"{counts['info']} sugerencias")]
    print(bold("Resumen: ") + " · ".join(summary))
    print(dim("Siguiente paso: abre el juego y ejecuta 'lagdoc monitor' (mejor en el otro monitor) mientras juegas."))
