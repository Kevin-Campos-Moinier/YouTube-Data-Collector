#!/bin/sh
# Starts the smdc dashboard, installing it on first use (Linux; macOS: dashboard.command).
cd "$(dirname "$0")" || exit 1
for py in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" launch.py "$@"
  fi
done
echo "Python 3.11 or newer is required: https://www.python.org/downloads/"
exit 1
