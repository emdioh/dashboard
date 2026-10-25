#!/bin/sh
# Start, stop, restart or show the status of the dashboard's OpenRC services.
#
#   service.sh start|stop|restart|status [dashboard|cloudflared|all]
#
# The target defaults to the dashboard. start.sh, stop.sh, restart.sh and
# status.sh are shortcuts for the matching action.
set -eu

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() { die "usage: $(basename "$0") start|stop|restart|status [dashboard|cloudflared|all]"; }

action=${1:-}
target=${2:-dashboard}
case $action in start | stop | restart | status) ;; *) usage ;; esac
case $target in
    dashboard | cloudflared) services=$target ;;
    all) services="dashboard cloudflared" ;;
    *) usage ;;
esac
[ "$(id -u)" = 0 ] || die "run as root"

for s in $services; do
    if [ ! -x "/etc/init.d/$s" ]; then
        [ "$target" = all ] && continue
        die "$s is not installed"
    fi
    rc-service "$s" "$action" || [ "$action" = status ]
done

# After (re)starting the dashboard, wait until it answers HTTP.
case "$action $services" in
    "start "*dashboard* | "restart "*dashboard*)
        port=$( [ -f /etc/conf.d/dashboard ] && . /etc/conf.d/dashboard; echo "${DASHBOARD_PORT:-8080}")
        i=0
        until wget -qO /dev/null "http://127.0.0.1:$port/" 2>/dev/null; do
            i=$((i + 1))
            if [ "$i" -ge 20 ]; then
                echo "dashboard is not answering on port $port; last log lines:" >&2
                tail -n 15 /var/log/dashboard.log >&2 || true
                exit 1
            fi
            sleep 0.5
        done
        echo "dashboard is answering on port $port"
        ;;
esac
