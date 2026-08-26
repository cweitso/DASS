#!/usr/bin/env bash
# Re-render every .puml in docs/diagrams to a PNG beside it.
#
# Uses the PlantUML container so nothing has to be installed on the host.
set -euo pipefail

DIAGRAMS="$(cd "$(dirname "${BASH_SOURCE[0]}")/diagrams" && pwd)"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker not found. Install it, or run PlantUML yourself:" >&2
  echo "       plantuml -tpng $DIAGRAMS/*.puml" >&2
  exit 1
}

docker run --rm -v "$DIAGRAMS:/data" plantuml/plantuml -tpng -o . /data
echo "Rendered:"
ls -1 "$DIAGRAMS"/*.png
