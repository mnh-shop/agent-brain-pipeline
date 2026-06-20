#!/bin/sh
set -eu

HERMES_HOME=${HERMES_HOME:-/opt/data}

mkdir -p "$HERMES_HOME"

exec "$@"
