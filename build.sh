#!/usr/bin/env bash
# Empaqueta lagdoc en un único ejecutable (dist/lagdoc) para copiarlo a otra instalación.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
stage=dist/.build
rm -rf "$stage" && mkdir -p "$stage"
cp -r lagdoc "$stage/"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
printf 'import sys\nfrom lagdoc.__main__ import main\nsys.exit(main())\n' > "$stage/__main__.py"
python3 -m zipapp "$stage" -p "/usr/bin/env python3" -o dist/lagdoc
rm -rf "$stage"
echo "Listo: dist/lagdoc ($(du -h dist/lagdoc | cut -f1)). Cópialo a ~/.local/bin/ en la otra instalación."
