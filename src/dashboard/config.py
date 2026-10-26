"""Load the YAML config describing hosts and the services deployed on them."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/etc/dashboard/config.yaml"
DEFAULT_DATA_DIR = "/var/lib/dashboard"

# Used when a service does not list its own steps. `pull` refreshes any
# registry images the compose file references; `up --build` rebuilds the
# images that come from the checked-out code and recreates changed containers.
DEFAULT_STEPS = [
    "docker compose pull --ignore-buildable",
    "docker compose up -d --build --remove-orphans",
]


@dataclass
class Host:
    name: str
    address: str
    user: str = "root"
    port: int = 22
    ctid: int | None = None  # Proxmox container id, only used for setup hints


@dataclass
class Service:
    """Something the dashboard can redeploy.

    With `repo_dir` it is a git clone: the deploy checks out a ref, then runs
    the steps there. Without it the steps are plain commands, run in `dir`
    (or the user's home), and the ref box becomes an optional argument.
    With no `host` the steps run on the dashboard's own machine.
    """

    name: str
    host: str = ""  # empty means the dashboard's machine, without SSH
    repo_dir: str = ""
    dir: str = ""
    default_ref: str = ""  # git: empty means the remote's default branch (origin/HEAD)
    steps: list[str] = field(default_factory=lambda: list(DEFAULT_STEPS))
    description: str = ""

    @property
    def is_git(self) -> bool:
        return bool(self.repo_dir)

    @property
    def is_local(self) -> bool:
        return not self.host

    @property
    def workdir(self) -> str:
        return self.repo_dir or self.dir


@dataclass
class App:
    """A web utility shown in the dashboard's menu and proxied under /proxy/<name>/."""

    name: str
    url: str
    title: str = ""

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        self.title = self.title or self.name


@dataclass
class Config:
    data_dir: Path
    hosts: dict[str, Host]
    services: dict[str, Service]
    apps: dict[str, App] = field(default_factory=dict)

    @property
    def key_path(self) -> Path:
        return self.data_dir / "id_ed25519"

    @property
    def known_hosts_path(self) -> Path:
        return self.data_dir / "known_hosts"


def config_path() -> Path:
    return Path(os.environ.get("DASHBOARD_CONFIG", DEFAULT_CONFIG_PATH))


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    raw = yaml.safe_load(path.read_text()) or {}

    hosts = {}
    for name, h in (raw.get("hosts") or {}).items():
        hosts[name] = Host(name=name, **h)

    services = {}
    for name, s in (raw.get("services") or {}).items():
        service = Service(name=name, **s)
        if service.host and service.host not in hosts:
            raise ValueError(f"service {name!r} uses unknown host {service.host!r}")
        if service.repo_dir and service.dir:
            raise ValueError(f"service {name!r} has both repo_dir and dir: use one")
        if not service.repo_dir and "steps" not in s:
            raise ValueError(f"service {name!r} has no repo_dir, so it needs steps")
        if not service.steps:
            raise ValueError(f"service {name!r} has an empty steps list")
        services[name] = service

    apps = {}
    for name, a in (raw.get("apps") or {}).items():
        app = App(name=name, **a)
        if not app.url.startswith(("http://", "https://")):
            raise ValueError(f"app {name!r} needs an http:// or https:// url")
        apps[name] = app

    data_dir = Path(raw.get("data_dir") or DEFAULT_DATA_DIR)
    return Config(data_dir=data_dir, hosts=hosts, services=services, apps=apps)
