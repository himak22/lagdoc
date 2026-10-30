# lagdoc

Herramienta de terminal para averiguar **por qué** hay lag, tirones o bajadas de FPS al jugar en Linux
(CachyOS / Arch). Python 3 sin dependencias: lee directamente `/proc`, `/sys` y el log del kernel.

## Instalación

```sh
ln -s "$PWD/lagdoc.sh" ~/.local/bin/lagdoc   # o ejecútalo como ./lagdoc.sh
```

## Uso

```sh
lagdoc check      # 1. revisa la configuración (governor, GPU, swap, VRR, kernel...)
lagdoc monitor    # 2. déjalo abierto en el otro monitor mientras juegas (q para salir)
lagdoc report     # 3. analiza la última sesión grabada
```

- `monitor` detecta el juego solo (el proceso que más GPU usa). Si no lo encuentra: `--game nombre` o `--pid N`.
- Las sesiones se guardan en `~/.local/share/lagdoc/` (`lagdoc report --list`).
- `lagdoc check --boot -1` revisa el log del kernel del arranque anterior (útil si el PC se colgó jugando).

### Con MangoHud (recomendado para analizar FPS)

lagdoc no puede medir FPS por sí mismo; MangoHud sí. Si cruzas su log con la sesión, el informe
te dice qué estaba pasando en el sistema en **cada tirón** y en cada bajada de FPS.

`~/.config/MangoHud/MangoHud.conf`:

```ini
output_folder=/home/TU_USUARIO/mangologs
log_interval=0          # registra todos los fotogramas, no uno cada 100 ms
toggle_logging=Shift_L+F2
```

En Steam: `game-performance mangohud %command%` en CachyOS (`gamemoderun mangohud %command%` en otras). Arranca `lagdoc monitor`, juega, pulsa Shift+F2 para
empezar y terminar el log, y luego:

```sh
lagdoc report --mangohud ~/mangologs/Juego_2026-10-30_21-15-03.csv
```

La hora se saca del nombre del archivo (precisión de ±1 s). Si no cuadra, ajusta con `--offset SEG`.

## Si juegas en otra instalación (p. ej. Arch + CachyOS en el mismo PC)

`check` y `monitor` tienen que ejecutarse **en la instalación donde juegas**: la configuración y el log
del kernel son de cada sistema. `report` puede ejecutarse en cualquiera.

```sh
./build.sh                     # crea dist/lagdoc: un único ejecutable (zipapp de Python)
```

Copia `dist/lagdoc` a `~/.local/bin/` de la otra instalación (necesita `python`). Desde Arch, con
CachyOS en btrfs:

```sh
sudo mount -o subvolid=5 /dev/nvme0n1p2 /mnt      # raíz de btrfs: verás @, @home...
cp dist/lagdoc /mnt/@home/TU_USUARIO/.local/bin/  # con sudo + chown si el UID no coincide
sudo umount /mnt
```

Para analizar las sesiones de CachyOS desde Arch, móntalo en solo lectura y pasa la carpeta:

```sh
sudo mount -o ro,subvolid=5 /dev/nvme0n1p2 /mnt
./lagdoc.sh report /mnt/@home/TU_USUARIO/.local/share/lagdoc/   # usa la última sesión
```

En CachyOS, `check` revisa además el planificador sched-ext (`scxctl`), ananicy-cpp,
`game-performance` (necesita power-profiles-daemon) y proton-cachyos.

## Qué detecta

| Mientras juegas (`monitor` / `report`) | Configuración (`check`) |
|---|---|
| Cuello de botella: GPU, hilo principal de la CPU, CPU entera o "esperando" | Governor / EPP / boost / frecuencia capada |
| CPU o GPU al límite de temperatura, GPU que no sube de reloj | Modo de energía de la GPU, power limit, enlace PCIe |
| VRAM llena, RAM agotada, lectura de swap | RADV vs AMDVLK, lib32, variables de entorno |
| Presión (PSI) de CPU, memoria y disco | Frecuencia de los monitores y VRR (Hyprland) |
| Procesos de fondo: `fossilize_replay`, indexadores, `kswapd0`, `wineserver`... | Swap en HDD, swappiness, THP, `max_map_count` |
| Otros programas usando la GPU | Reloj TSC, split lock, ntsync, límite de archivos |
| Log del kernel: GPU colgada, OOM, MCE, PCIe, disco, reloj inestable | gamemode, MangoHud, biblioteca de Steam en HDD |

## Limitaciones

- Datos de GPU solo para AMD (`amdgpu`). En NVIDIA/Intel funciona todo lo demás.
- La detección automática del juego es heurística; con launchers raros usa `--game`.
- El monitor muestrea cada segundo: ve tendencias y causas, no fotogramas individuales (para eso, MangoHud).

## Pruebas

```sh
python3 -m unittest discover tests
```
