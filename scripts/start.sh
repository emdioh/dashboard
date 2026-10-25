#!/bin/sh
# Start the dashboard (or: start.sh cloudflared | all). See service.sh.
exec "$(dirname "$0")/service.sh" start "$@"
