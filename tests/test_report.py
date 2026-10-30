"""Pruebas del análisis con una sesión y un log de MangoHud sintéticos.

Ejecutar desde la raíz del proyecto: python3 -m unittest discover tests
"""

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from lagdoc import report, rules

HEADER = {
    "type": "header", "version": 1, "start": 0, "host": "test", "kernel": "test", "interval": 1.0,
    "cpu_model": "AMD Ryzen 7 5700X3D", "cpu_max_mhz": 4150, "cpu_tmax": 89, "gpu_name": "RX 7800 XT",
    "gpu_crit": {"edge": 100.0, "junction": 110.0, "mem": 108.0}, "vram_total": 16368, "sclk_max": 2254,
}


def sample(t, **over):
    s = {
        "t": t,
        "cpu": {"use": 30.0, "max": 70.0, "cores": [30] * 16, "mhz": 4000, "mhz_busy": 4100, "temp": 70.0},
        "gpu": {"busy": 98, "sclk": 2400, "mclk": 1218, "temp": {"edge": 65, "junction": 80, "mem": 70},
                "power": 230.0, "cap": 236, "vram": 9000, "gtt": 300, "fan": 1500},
        "mem": {"total": 32000, "avail": 18000, "swap": 0},
        "rate": {"swapin": 0.0, "swapout": 0.0, "majflt": 0},
        "psi": {"cpu": 1.0, "mem": 0.0, "mem_full": 0.0, "io": 0.0, "io_full": 0.0},
        "game": {"pid": 1234, "name": "Juego.exe", "cpu": 250.0, "gpu": 90.0, "vram": 8000, "rss": 9000,
                 "threads": 80, "thread": "MainThread", "thread_cpu": 70.0, "read": 0.0, "majflt": 0},
        "top": [["Hyprland", 10.0, 1]],
        "gtop": [],
        "kernel": [],
    }
    for path, value in over.items():
        node = s
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return s


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t0 = float(int(time.time()) - 600)
        samples = [sample(self.t0 + i) for i in range(1, 121)]
        samples[29] = sample(self.t0 + 30, psi__io=60.0, game__read=200.0)                            # carga de disco
        samples[59] = sample(self.t0 + 60, rate__swapin=5.0, psi__mem=12.0)                           # swap
        samples[89] = sample(self.t0 + 90, top=[["fossilize_repla", 300.0, 4], ["Hyprland", 10.0, 1]])  # shaders de Steam
        for i in range(99, 110):                                                                      # VRAM llena
            samples[i] = sample(self.t0 + i + 1, gpu__vram=16000, gpu__busy=60)
        samples[114] = sample(self.t0 + 115, kernel=[["gpu_hang", "bad", "amdgpu: ring gfx_0.0.0 timeout"]])
        self.session = os.path.join(self.tmp.name, "sesion.jsonl")
        with open(self.session, "w") as f:
            f.write(json.dumps(HEADER) + "\n")
            for s in samples:
                f.write(json.dumps(s) + "\n")
        self.samples = samples

        # MangoHud: 100 FPS estables con tirones en t=30, 60, 90 y 115, y FPS bajos entre t=100 y t=110
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(self.t0))
        self.mango = os.path.join(self.tmp.name, f"Juego_{stamp}.csv")
        rows, elapsed = [], 0.0
        while elapsed < 119:
            ft = 10.0
            if any(abs(elapsed - x) < 0.005 for x in (29.5, 59.5, 89.5, 114.5)):
                ft = 80.0
            elif 100 <= elapsed < 110:
                ft = 20.0
            elapsed += ft / 1000
            rows.append(f"{1000 / ft:.1f},{ft:.3f},30,0,98,70,65,2400,1218,9000,230,9000,0,9000,{int(elapsed * 1e9)}")
        with open(self.mango, "w") as f:
            f.write("os,cpu,gpu,ram,kernel,driver,cpuscheduler\nArch,5700X3D,7800XT,32,7.2,mesa,\n")
            f.write("--------------------FRAME METRICS--------------------\n")
            f.write("fps,frametime,cpu_load,cpu_power,gpu_load,cpu_temp,gpu_temp,gpu_core_clock,gpu_mem_clock,"
                    "gpu_vram_used,gpu_power,ram_used,swap_used,process_rss,elapsed\n")
            f.write("\n".join(rows) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_detected(self):
        keys = {k for s in self.samples for k, _, _ in rules.events(s, HEADER)}
        for expected in ("psi_io", "swap", "psi_mem", "bg:fossilize_repla", "vram", "k_gpu_hang"):
            self.assertIn(expected, keys)

    def test_bottleneck(self):
        self.assertEqual(rules.bottleneck(self.samples[0]), "gpu")
        self.assertEqual(rules.bottleneck(sample(1, gpu__busy=60, game__thread_cpu=99.0)), "cpu_hilo")
        self.assertIsNone(rules.bottleneck(sample(1, game__thread=None)))

    def test_mangohud_stutters_attributed(self):
        frames, from_name = report.load_mangohud(self.mango)
        self.assertTrue(from_name)
        stutters = report.find_stutters(frames)
        self.assertEqual(len(stutters), 4)
        times = [s["t"] for s in self.samples]
        base = {"bg": {"fossilize_repla": 2.5}, "game_cpu": 250.0}
        found = []
        for t, _, _ in stutters:
            i = report.bisect.bisect_left(times, t)
            found.append(report.causes_at(self.samples[i], HEADER, base))
        self.assertIn("esperas de disco / carga de datos", found[0])
        self.assertIn("lectura de swap", found[1])
        self.assertIn("pico de 'fossilize_repla' en segundo plano", found[2])
        self.assertTrue(any(c.startswith("kernel:") for c in found[3]))

    def test_full_report_runs(self):
        args = SimpleNamespace(list=False, session=self.session, mangohud=self.mango, offset=0.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(report.run_report(args), 0)
        text = out.getvalue()
        self.assertIn("Bajadas de FPS", text)
        self.assertIn("4 tirones", text)
        self.assertIn("VRAM llena", text)


if __name__ == "__main__":
    unittest.main()
