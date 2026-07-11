#!/usr/bin/env bash
# idc-migrate launcher. Prefers the system interpreter (deps already present
# on this box); falls back to a local venv if a required module is missing.
# Usage: ./run.sh ingest --source all | ./run.sh serve --port 8010 | ./run.sh doctor
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

# load .env if present (the python code also loads it, but export here too)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

missing=0
for mod in fastapi uvicorn httpx pydantic typer rich; do
  python3 -c "import $mod" 2>/dev/null || missing=1
done

if [ "$missing" -eq 0 ]; then
  exec python3 -m idc.cli.main "$@"
fi

echo "[run.sh] system python missing deps; creating .venv …" >&2
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip 2>/dev/null || true
pip install -q fastapi 'uvicorn[standard]' httpx pydantic typer rich
exec python -m idc.cli.main "$@"