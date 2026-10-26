# Dashboard

A small web UI for redeploying services on the machines of a Proxmox server
with one click. A service is usually a git clone on its host; a redeploy:

1. `git fetch` in the service's directory,
2. checks out the remote's default branch, or a branch, tag or commit hash
   you type in,
3. runs the service's steps — by default `docker compose pull` and
   `docker compose up -d --build`, or your own commands/scripts.

A service can also be just a list of commands, with no git involved, and it
can run on the dashboard's own machine instead of over SSH.

The output streams live in the browser and every run is kept in the history.

The dashboard reaches hosts over SSH with its own key. There is no login:
put it behind something that authenticates (e.g. a Cloudflare Tunnel with
Cloudflare Access).

## Install (in an Alpine LXC)

As root:

```sh
wget -qO- https://raw.githubusercontent.com/emdioh/things/dashboard/scripts/install.sh | sh
```

[`scripts/install.sh`](scripts/install.sh) installs the packages and uv,
clones this branch to `/opt/dashboard`, creates `/etc/dashboard/config.yaml`
from the example if it doesn't exist, and starts the `dashboard` OpenRC
service on port 8080 (logs: `/var/log/dashboard.log`). Edit the config, then
`rc-service dashboard restart`. Re-run the same command to update.

### Managing the services

```sh
/opt/dashboard/scripts/start.sh            # or stop.sh, restart.sh, status.sh
/opt/dashboard/scripts/restart.sh all      # dashboard and cloudflared
/opt/dashboard/scripts/status.sh cloudflared
```

These wrap `rc-service`. After a start or restart they wait until the
dashboard answers and, if it doesn't, print the end of its log. Restart
after editing `/etc/dashboard/config.yaml`.

### Cloudflare Tunnel

Create a tunnel in the Cloudflare dashboard (Zero Trust > Networks > Tunnels),
copy its token, then on the same LXC:

```sh
sh /opt/dashboard/scripts/install-cloudflared.sh            # prompts for the token
```

[`scripts/install-cloudflared.sh`](scripts/install-cloudflared.sh) installs
`cloudflared` as an OpenRC service with the token stored in
`/etc/cloudflared/token`, and switches the dashboard to listen on
`127.0.0.1` only, so the tunnel is the only way in. You can also pass the
token as an argument, or paste the whole `cloudflared service install …`
command Cloudflare shows. Re-run it to change the token.

In the tunnel's settings, add a public hostname pointing to
`http://127.0.0.1:8080` (not `localhost`, which can resolve to IPv6 `::1`), and protect that hostname with a Cloudflare Access
application: the dashboard has no login of its own.

The hosts the dashboard deploys to need `bash`,
`git`, Docker and an SSH server (on Alpine hosts: `apk add bash git openssh`,
then `rc-update add sshd && rc-service sshd start`).

## Configuration

See [`config.example.yaml`](config.example.yaml). In short, `hosts` are the
machines to SSH into and `services` are what you deploy or run:

| Service field | Meaning |
| --- | --- |
| `host` | name of a host in `hosts`; omit to run on the dashboard's own machine (no SSH) |
| `repo_dir` | path of a git clone: the deploy checks out a ref there, then runs the steps |
| `dir` | for services without `repo_dir`: where the steps run (default: the user's home) |
| `default_ref` | value used when the ref box is empty: a branch/tag/hash for git services (omit for the remote's default branch), an argument for the others |
| `steps` | shell commands, run in order; the first failure stops the deploy. Defaults to the docker compose steps for git services, required for the others |
| `description` | shown under the name |

There are four kinds of service, from `host` and `repo_dir`:

| | `repo_dir` set (git) | no `repo_dir` (commands) |
| --- | --- | --- |
| `host` set | checkout + steps over SSH | steps over SSH |
| no `host` | checkout + steps on the dashboard machine | steps on the dashboard machine |

**Git services:** steps can use `$DEPLOY_REF` and `$DEPLOY_SHA`. The
checkout is a detached HEAD at the chosen commit, so steps shouldn't run
`git checkout` or `git pull` (`git pull` fails on a detached HEAD). Local
edits to tracked files make the checkout fail rather than being thrown away.

**Command services:** the button reads *Run* and the box takes an optional
argument (same characters as a git ref: letters, digits, `._/+@-`), passed
to the steps as `$DEPLOY_REF`. To run a script, make it a step:
`./backup.sh "$DEPLOY_REF"`.

**Local services** run as the dashboard's user (root under the OpenRC
service) and need no SSH setup.

In every kind, steps run in one bash function with `set -euo pipefail`, so
variables carry over between steps; use `cmd || true` for a step that may
fail harmlessly.

Restart the service after editing the config.

## Apps

Small web utilities can be added under `apps` in the config (`title`, `url`
and an optional `description`). They are listed on the **Apps** page, which
is the dashboard's home page (without apps, the home page is **Deploy
Services**). Selecting one shows its page inside the dashboard; requests go through the dashboard to the app at
`/proxy/<name>/…`, so the app only needs to be reachable from the dashboard
LXC and is covered by the same Cloudflare Access login.

For an app to work behind that prefix:

- Use **relative URLs** for its own pages, assets and API calls
  (`static/app.js`, `api/delays`, `fetch("api/delays")`), not ones starting
  with `/`. Requests to `/…` would go to the dashboard instead of the app.
- Redirects to `/…` and to the app's own URL are rewritten automatically.
- Each request carries `X-Forwarded-Prefix: /proxy/<name>`, plus
  `X-Forwarded-Host` and `X-Forwarded-Proto` with the browser-facing host
  and scheme (e.g. `dash.example.com`, `https`). The `Host` header is the
  app's own, so an app that compares the browser's `Origin` with the host
  (a common CSRF check) must use `X-Forwarded-Host`.
- Frame-blocking headers (`X-Frame-Options`, CSP `frame-ancestors`) are
  removed so the page can be shown inside the dashboard.
- To match the dashboard's look (including dark mode), link
  `/static/dashboard.css` and use its classes (`card`, `row`, `muted`, …).

HTTP (including long polling and server-sent events) and WebSockets are
proxied; WebSocket subprotocols are passed through, so apps can
authenticate with them.

## Setting up SSH access

The dashboard creates its key pair (`data_dir/id_ed25519`) on first start, or
when you run `uv run dashboard pubkey`. To install it on the hosts:

- **Web:** the *SSH setup* page shows the public key and, per host, a
  command to paste. For hosts with a `ctid` it's a `pct exec` one-liner for
  the **Proxmox host shell**, plus a block that does every container at once.
  Hosts without a `ctid` (VMs, other machines) get a command to run inside
  that machine. Then press *Test connection*.
- **CLI** (inside the dashboard LXC, `DASHBOARD_CONFIG` set or config at the
  default path):

  ```sh
  uv run dashboard authorize        # print the commands for every host
  uv run dashboard check            # test SSH to every host
  ```

The commands are idempotent, so re-running them is harmless. Each target
needs an SSH server, `bash`, `git` and Docker (see the install notes above).
The first connection pins the host key in `data_dir/known_hosts`;
if you rebuild a container, delete its line there.

Fetching from private git remotes is the target host's business: give that
clone a deploy key or credentials so a plain `git fetch` works there.

## Development

```sh
uv sync
uv run pytest
DASHBOARD_CONFIG=./config.yaml uv run dashboard serve --port 8080
```
