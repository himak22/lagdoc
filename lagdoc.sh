#!/usr/bin/env bash
# Lanzador: funciona también enlazado desde ~/.local/bin
DIR="$(dirname "$(readlink -f "$0")")"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m lagdoc "$@"
