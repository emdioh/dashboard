#!/bin/sh
# Stop the dashboard (or: stop.sh cloudflared | all). See service.sh.
exec "$(dirname "$0")/service.sh" stop "$@"
