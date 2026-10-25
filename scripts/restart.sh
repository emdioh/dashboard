#!/bin/sh
# Restart the dashboard (or: restart.sh cloudflared | all). See service.sh.
exec "$(dirname "$0")/service.sh" restart "$@"
