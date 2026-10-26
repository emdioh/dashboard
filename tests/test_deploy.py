import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from dashboard import ssh
from dashboard.app import create_app
from dashboard import config
from dashboard.config import Config, Host, Service
from dashboard.deploy import build_script, command_argv, validate_ref


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A clone of an origin with main (2 commits), a tag on the first, and a `next` branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "f").write_text("one")
    git(origin, "add", "f")
    git(origin, "commit", "-qm", "one")
    git(origin, "tag", "v1")
    (origin / "f").write_text("two")
    git(origin, "commit", "-qam", "two")
    git(origin, "checkout", "-qb", "next")
    (origin / "f").write_text("next")
    git(origin, "commit", "-qam", "next")
    git(origin, "checkout", "-q", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def run_script(service, ref, env=None):
    return subprocess.run(
        ["bash", "-s", "--", service.workdir, ref], env=env,
        input=build_script(service), capture_output=True, text=True,
    )


@pytest.mark.parametrize("ref, expected", [("", "two"), ("main", "two"), ("v1", "one"), ("next", "next")])
def test_checkout_refs(repo, ref, expected):
    origin, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])
    r = run_script(service, ref)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "out").read_text() == expected


def test_checkout_by_hash_and_new_commits(repo):
    origin, clone = repo
    first = git(origin, "rev-parse", "v1")
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])
    assert run_script(service, first[:8]).returncode == 0
    assert (clone / "out").read_text() == "one"

    (origin / "f").write_text("three")
    git(origin, "commit", "-qam", "three")
    assert run_script(service, "").returncode == 0
    assert (clone / "out").read_text() == "three"


def test_failing_step_stops_deploy(repo):
    _, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["false", "touch never"])
    r = run_script(service, "")
    assert r.returncode != 0
    assert not (clone / "never").exists()


def test_step_reading_stdin_does_not_eat_script(repo):
    _, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat > /dev/null", "touch after"])
    assert run_script(service, "").returncode == 0
    assert (clone / "after").exists()


def test_unknown_ref_fails(repo):
    _, clone = repo
    r = run_script(Service(name="s", host="h", repo_dir=str(clone)), "nope")
    assert r.returncode != 0


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "feature/x", "abc123f", " main "])
def test_valid_refs(ref):
    assert validate_ref(ref) == ref.strip()


@pytest.mark.parametrize("ref", ["-rf", "a b", "x;rm", "$(id)", "a..b", "`x`"])
def test_invalid_refs(ref):
    with pytest.raises(ValueError):
        validate_ref(ref)


def test_authorize_snippet_is_valid_shell():
    host = Host(name="h", address="x", ctid=101)
    cmd = ssh.authorize_command("ssh-ed25519 AAAA dashboard", host)
    assert cmd.startswith("pct exec 101 -- sh -c ")
    subprocess.run(["sh", "-n", "-c", cmd], check=True)


def test_web_deploy_end_to_end(repo, tmp_path, monkeypatch):
    _, clone = repo
    # Run the "remote" command locally instead of over SSH.
    monkeypatch.setattr(ssh, "ssh_argv", lambda cfg, host, remote: ["sh", "-c", remote])
    monkeypatch.setattr(ssh, "ensure_key", lambda cfg: False)
    cfg = Config(
        data_dir=tmp_path / "data",
        hosts={"h": Host(name="h", address="localhost")},
        services={"s": Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])},
    )
    with TestClient(create_app(cfg)) as client:
        assert "Redeploy" in client.get("/services").text
        assert client.get("/", follow_redirects=False).headers["location"] == "/services"  # no apps
        r = client.post("/services/s/deploy", data={"ref": "v1"})
        job_url = r.headers["HX-Redirect"]
        for _ in range(100):
            if "running" not in client.get(job_url + "/log").text:
                break
            time.sleep(0.05)
        page = client.get(job_url).text
        assert "==&gt; done" in page and "status ok" in page
        assert (clone / "out").read_text() == "one"
        assert "v1" in client.get("/history").text

        bad = client.post("/services/s/deploy", data={"ref": "$(id)"})
        assert "invalid git ref" in bad.text


def test_commands_run_in_dir_with_argument(tmp_path):
    service = Service(name="s", dir=str(tmp_path), steps=['echo "arg=$DEPLOY_REF" > out', "pwd >> out"])
    r = run_script(service, "v2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out").read_text() == f"arg=v2\n{tmp_path}\n"
    assert "git" not in r.stdout


def test_commands_default_to_home(tmp_path):
    service = Service(name="s", steps=["pwd > out"])
    r = run_script(service, "", env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "out").read_text().strip() == str(tmp_path)


def test_commands_failing_step_stops(tmp_path):
    service = Service(name="s", dir=str(tmp_path), steps=["false", "touch never"])
    assert run_script(service, "").returncode != 0
    assert not (tmp_path / "never").exists()


def test_command_argv(tmp_path):
    cfg = Config(data_dir=tmp_path, hosts={"h": Host(name="h", address="10.0.0.5")}, services={})
    local = Service(name="l", dir="/srv/x", steps=["true"])
    assert command_argv(cfg, local, "v1") == ["bash", "-s", "--", "/srv/x", "v1"]
    remote = Service(name="r", host="h", repo_dir="/opt/my app")
    argv = command_argv(cfg, remote, "")
    assert argv[0] == "ssh" and argv[-2] == "root@10.0.0.5"
    assert argv[-1] == "bash -s -- '/opt/my app' ''"


def load_yaml(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return config.load(path)


def test_config_service_kinds(tmp_path):
    cfg = load_yaml(tmp_path, """
hosts:
  h: {address: 10.0.0.5}
services:
  git_remote: {host: h, repo_dir: /opt/a}
  git_local: {repo_dir: /opt/b}
  cmd_remote: {host: h, steps: [apk upgrade]}
  cmd_local: {dir: /tmp, steps: [echo hi]}
""")
    s = cfg.services
    assert s["git_remote"].is_git and not s["git_remote"].is_local
    assert s["git_remote"].steps == config.DEFAULT_STEPS
    assert s["git_local"].is_git and s["git_local"].is_local
    assert not s["cmd_remote"].is_git and not s["cmd_remote"].is_local
    assert not s["cmd_local"].is_git and s["cmd_local"].is_local and s["cmd_local"].workdir == "/tmp"


@pytest.mark.parametrize("service, error", [
    ("{host: nope, repo_dir: /x}", "unknown host"),
    ("{dir: /x}", "needs steps"),
    ("{repo_dir: /x, dir: /y, steps: [true]}", "both repo_dir and dir"),
    ("{repo_dir: /x, steps: []}", "empty steps"),
])
def test_config_errors(tmp_path, service, error):
    with pytest.raises(ValueError, match=error):
        load_yaml(tmp_path, f"services:\n  s: {service}\n")


def test_web_local_commands_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh, "ensure_key", lambda cfg: False)
    work = tmp_path / "work"
    work.mkdir()
    cfg = Config(
        data_dir=tmp_path / "data",
        hosts={},
        services={"s": Service(name="s", dir=str(work), steps=['echo "$DEPLOY_REF" > out'])},
    )
    with TestClient(create_app(cfg)) as client:
        index = client.get("/services").text
        assert "this machine" in index and ">Run<" in index
        job_url = client.post("/services/s/deploy", data={"ref": "v3"}).headers["HX-Redirect"]
        for _ in range(100):
            if "running" not in client.get(job_url + "/log").text:
                break
            time.sleep(0.05)
        assert "status ok" in client.get(job_url).text
        assert (work / "out").read_text() == "v3\n"
