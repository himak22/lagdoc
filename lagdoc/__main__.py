"""lagdoc: averigua por qué hay lag, tirones o bajadas de FPS al jugar en Linux."""

import argparse
import sys


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="lagdoc",
        description="Diagnóstico de lag, tirones y bajadas de FPS al jugar en Linux (CachyOS/Arch).",
        epilog="Flujo típico: lagdoc check → lagdoc monitor (mientras juegas) → lagdoc report",
    )
    sub = p.add_subparsers(dest="cmd", metavar="comando")

    c = sub.add_parser("check", help="revisa la configuración del sistema")
    c.add_argument("--boot", type=int, default=0, metavar="N",
                   help="arranque cuyo log del kernel revisar (0 = actual, -1 = el anterior)")
    c.add_argument("-v", "--verbose", action="store_true", help="muestra también los detalles de lo que está bien")

    m = sub.add_parser("monitor", help="panel en vivo mientras juegas; graba la sesión")
    m.add_argument("-i", "--interval", type=float, default=1.0, metavar="SEG", help="segundos entre muestras (1)")
    m.add_argument("-g", "--game", metavar="NOMBRE", help="parte del nombre del juego, si no lo detecta solo")
    m.add_argument("-p", "--pid", type=int, help="PID del juego")
    m.add_argument("-o", "--output", metavar="ARCHIVO", help="dónde guardar la sesión (por defecto ~/.local/share/lagdoc)")
    m.add_argument("--no-record", action="store_true", help="no grabar la sesión")
    m.add_argument("--plain", action="store_true", help="sin panel: imprime solo los eventos, línea a línea")
    m.add_argument("-d", "--duration", type=float, metavar="SEG", help="parar tras SEG segundos")

    r = sub.add_parser("report", help="analiza una sesión grabada (por defecto la última)")
    r.add_argument("session", nargs="?", help="archivo .jsonl de la sesión, o carpeta de sesiones (usa la última)")
    r.add_argument("-m", "--mangohud", metavar="CSV", help="log de MangoHud de la misma partida")
    r.add_argument("--offset", type=float, default=0.0, metavar="SEG",
                   help="segundos a sumar a las horas de MangoHud si no cuadran con la sesión")
    r.add_argument("-l", "--list", action="store_true", help="lista las sesiones grabadas")

    args = p.parse_args(argv)
    if args.cmd == "check":
        from .checks import run_checks
        return run_checks(args)
    if args.cmd == "monitor":
        if args.interval < 0.2:
            p.error("el intervalo mínimo es 0.2 s")
        from .monitor import run_monitor
        return run_monitor(args)
    if args.cmd == "report":
        from .report import run_report
        return run_report(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
