"""Colores ANSI y pequeños elementos gráficos para la terminal."""

import os
import sys

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(code):
    if not COLOR:
        return lambda s: str(s)
    return lambda s: f"\x1b[{code}m{s}\x1b[0m"


bold = _style("1")
dim = _style("2")
red = _style("31")
green = _style("32")
yellow = _style("33")
blue = _style("34")
magenta = _style("35")
cyan = _style("36")

ICON = {"ok": green("✔"), "info": cyan("ℹ"), "warn": yellow("⚠"), "bad": red("✖")}
SEV_COLOR = {"ok": green, "info": cyan, "warn": yellow, "bad": red}
SEV_RANK = {"ok": 0, "info": 1, "warn": 2, "bad": 3}

SPARK = "▁▂▃▄▅▆▇█"


def level(value, warn, bad):
    """Función de color según el valor supere los umbrales de aviso o problema."""
    if value is None:
        return dim
    if value >= bad:
        return red
    if value >= warn:
        return yellow
    return green


def bar(pct, width=20, warn=70, bad=90):
    pct = max(0.0, min(100.0, pct or 0.0))
    n = round(pct / 100 * width)
    return level(pct, warn, bad)("█" * n) + dim("·" * (width - n))


def spark(values, warn=70, bad=90):
    return "".join(level(v, warn, bad)(SPARK[min(7, int(v / 100 * 8))]) for v in values)


def temp(value, limit):
    """Temperatura coloreada respecto a su límite (°C)."""
    if value is None:
        return dim("—")
    return level(value, limit - 12, limit - 5)(f"{value:.0f}°")
