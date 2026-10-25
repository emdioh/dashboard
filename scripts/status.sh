#!/bin/sh
# Show the status of the dashboard (or: status.sh cloudflared | all). See service.sh.
exec "$(dirname "$0")/service.sh" status "$@"
