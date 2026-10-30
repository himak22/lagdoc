"""Mensajes del kernel que suelen explicar tirones, cuelgues o cierres de juegos."""

import os
import re
import subprocess

# (clave, severidad, expresión, explicación). Gana la primera que coincida.
PATTERNS = [
    ("gpu_hang", "bad", r"amdgpu.*(ring \S+ timeout|GPU reset|gpu reset|GPU recovery|hang)",
     "La GPU se colgó y el driver tuvo que reiniciarla"),
    ("gpu_fault", "warn", r"amdgpu.*(page fault|VM_L2_PROTECTION_FAULT|retry fault)",
     "Fallo de memoria en la GPU (bug del driver o del juego)"),
    ("nvidia_xid", "bad", r"NVRM: Xid", "Error de la GPU NVIDIA (Xid)"),
    ("oom", "bad", r"(Out of memory|oom-kill|oom_reaper|Killed process)",
     "El kernel cerró un proceso por quedarse sin RAM"),
    ("split_lock", "warn", r"split.lock", "El kernel frena al juego por 'split locks' (tirones fuertes)"),
    ("clocksource", "bad", r"clocksource.*(unstable|Switched to clocksource (hpet|acpi_pm))|tsc.*unstable",
     "El reloj TSC se marcó inestable: el sistema usa un reloj lento"),
    ("clock_wd", "info", r"clocksource: (Watchdog|timekeeping watchdog)", "Aviso del vigilante del reloj del sistema"),
    ("mce", "bad", r"(mce: \[Hardware Error\]|Machine check|Hardware Error)",
     "Error de hardware (MCE): CPU o RAM inestables"),
    ("pcie_aer", "warn", r"(AER: .*(Corrected|Uncorrected)|PCIe Bus Error)", "Errores en el bus PCIe"),
    ("thermal", "warn", r"(temperature above threshold|clock throttled|thermal.*critical)", "Evento térmico de la CPU"),
    ("disk", "warn", r"(nvme\S*: .*(timeout|reset|I/O error)|ata\d+.*(failed command|hard resetting|exception)|I/O error, dev)",
     "Errores o timeouts de disco"),
    ("flip_timeout", "warn", r"(flip_done timed out|commit wait timed out|\[drm\].*timed out)",
     "La pantalla tardó en mostrar un frame (driver de pantalla)"),
    ("usb", "info", r"usb \S+: USB disconnect", "Se desconectó un dispositivo USB"),
]
_COMPILED = [(key, sev, re.compile(rx, re.I), why) for key, sev, rx, why in PATTERNS]
WHY = {key: why for key, _, _, why in PATTERNS}


def classify(msg):
    for key, sev, rx, _ in _COMPILED:
        if rx.search(msg):
            return key, sev
    return None


def _parse(line):
    """'1730291234.123 host kernel: mensaje' → (timestamp, mensaje)."""
    head, sep, msg = line.partition(" kernel: ")
    if not sep:
        return None
    try:
        return float(head.split()[0]), msg.strip()
    except (ValueError, IndexError):
        return None


def scan_boot(boot=0):
    """Eventos relevantes de un arranque: ([(ts, clave, sev, mensaje)], error o None)."""
    try:
        res = subprocess.run(
            ["journalctl", "-k", "-b", str(boot), "-o", "short-unix", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"No se pudo ejecutar journalctl: {e}"
    if res.returncode != 0 and not res.stdout:
        return [], (res.stderr.strip() or "journalctl falló") + " (¿tu usuario está en el grupo wheel o systemd-journal?)"
    events = []
    for line in res.stdout.splitlines():
        parsed = _parse(line)
        if parsed and (hit := classify(parsed[1])):
            events.append((parsed[0], hit[0], hit[1], parsed[1]))
    return events, None


class KernelFollower:
    """Sigue journalctl -k en segundo plano sin bloquear el bucle del monitor."""

    def __init__(self):
        self.buf = b""
        try:
            self.proc = subprocess.Popen(
                ["journalctl", "-k", "-f", "-n", "0", "-o", "short-unix", "-q"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            os.set_blocking(self.proc.stdout.fileno(), False)
        except OSError:
            self.proc = None

    def poll(self):
        """Eventos nuevos desde la última llamada: [[clave, sev, mensaje]]."""
        if not self.proc:
            return []
        while True:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        out = []
        for raw in lines:
            parsed = _parse(raw.decode(errors="replace"))
            if parsed and (hit := classify(parsed[1])):
                out.append([hit[0], hit[1], parsed[1][:300]])
        return out

    def close(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
