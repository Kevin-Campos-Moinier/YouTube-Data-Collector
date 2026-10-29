#!/bin/sh
# macOS: double-click in Finder to start the smdc dashboard (installs it on first use).
cd "$(dirname "$0")" && exec sh ./dashboard.sh "$@"
