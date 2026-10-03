#!/usr/bin/env bash
# Start as the normal desktop user so the browser uses their own session.
set -euo pipefail
exec /usr/bin/python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/customer_setup.py" "$@"
