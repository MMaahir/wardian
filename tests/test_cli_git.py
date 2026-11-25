"""wardian push and fetch against a local repo standing in for the sandbox.

A fake ssh runs the remote command locally, so these tests run git's real
transport without a VM. Each control has a test that the bad thing doesn't
happen, and a mutation test that removes the control and shows the bad thing
does happen, so the first test can tell.
"""

import http.server
import shlex
import subprocess
import threading

import pytest

from wardian import cli

NAME = "toy"
FAKE_SSH = """#!/bin/sh
# Run the last argument, the remote command, as a local shell command.
for last; do :; done
exec /bin/sh -c "$last"
"""


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A Mac repo with one commit, and an empty guest repo reached by fake ssh."""
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "gitconfig").write_text("")

    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)

    guest = tmp_path / "guest"
    # A prefix rewrite of .../guest to .../redirected-guest lands here.
    elsewhere = tmp_path / "redirected-guest"
    for repo in (guest, elsewhere):
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        git(repo, "config", "receive.denyCurrentBranch", "updateInstead")

    mac = tmp_path / "mac"
    subprocess.run(["git", "init", "-q", "-b", "main", str(mac)], check=True)
    (mac / "words.txt").write_text("base\n")
    git(mac, "add", "words.txt")
    git(mac, "commit", "-q", "-m", "base")

    instance = {
        "status": "Running",
        "sshAddress": "127.0.0.1",
        "sshLocalPort": 60022,
        "config": {"user": {"name": "lima"}},
        "IdentityFile": "/dev/null",
    }
    monkeypatch.setattr(cli, "find_instance", lambda name: instance)
    monkeypatch.setattr(cli, "GUEST_REPO", str(guest))
    monkeypatch.setattr(cli, "AS_AGENT", "exec")
    monkeypatch.setattr(cli, "admin_ssh", lambda name, inst: [str(ssh), "host"])
    real_env = cli.git_env
    monkeypatch.setattr(
        cli,
        "git_env",
        lambda name, inst: {**real_env(name, inst), "GIT_SSH_COMMAND": str(ssh)},
    )

    class World:
        pass

    w = World()
    w.mac, w.guest, w.elsewhere, w.tmp = mac, guest, elsewhere, tmp_path
    w.url = f"ssh://lima@127.0.0.1:60022{guest}"
    w.elsewhere_url = f"ssh://lima@127.0.0.1:60022{elsewhere}"
    return w


def wardian(command, world):
    return cli.main([command, NAME, "--repo", str(world.mac)])


def agent_commits(world, files, branch="agent/fix"):
    """Commit files on an agent branch in the guest, as the agent would."""
    git(world.guest, "switch", "-q", "-c", branch)
    for path, text in files.items():
        target = world.guest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(world.guest, "add", path)
    git(world.guest, "commit", "-q", "-m", "agent work")
    git(world.guest, "switch", "-q", "main")
    return git(world.guest, "rev-parse", branch).stdout.strip()


def test_push_updates_main_and_records_the_base(world, capsys):
    head = git(world.mac, "rev-parse", "HEAD").stdout.strip()

    assert wardian("push", world) == 0

    assert (world.guest / "words.txt").read_text() == "base\n"
    assert git(world.guest, "rev-parse", "main").stdout.strip() == head
    assert (
        git(world.mac, "rev-parse", f"refs/wardian/{NAME}/base").stdout.strip() == head
    )
    assert "only the main branch moved" not in capsys.readouterr().out


def test_push_says_when_main_isnt_checked_out(world, capsys):
    assert wardian("push", world) == 0
    git(world.guest, "switch", "-q", "-c", "agent/x")
    (world.mac / "words.txt").write_text("new\n")
    git(world.mac, "commit", "-q", "-am", "new")

    assert wardian("push", world) == 0

    assert "has refs/heads/agent/x checked out" in capsys.readouterr().out
    assert (world.guest / "words.txt").read_text() == "base\n"


def test_push_is_refused_over_uncommitted_work(world, capsys):
    assert wardian("push", world) == 0
    (world.guest / "words.txt").write_text("agent's\n")
    (world.mac / "words.txt").write_text("new\n")
    git(world.mac, "commit", "-q", "-am", "new")

    assert wardian("push", world) == 1

    assert (world.guest / "words.txt").read_text() == "agent's\n"


def test_fetch_lists_tips_and_auto_run_files(world, capsys):
    assert wardian("push", world) == 0
    tip = agent_commits(
        world,
        {
            ".envrc": "export X=1\n",
            "src/app.py": "print(1)\n",
            "web/package.json": "{}\n",
            ".vscode/tasks.json": "{}\n",
            "words.txt": "fixed\n",
        },
    )
    git(world.guest, "tag", "t1")
    git(world.guest, "branch", "other")
    capsys.readouterr()

    assert wardian("fetch", world) == 0

    out = capsys.readouterr().out
    assert f"agent/fix {tip}\n" in out
    flagged = [line.strip() for line in out.splitlines() if line.startswith("    ")]
    assert flagged == [".envrc", ".vscode/tasks.json", "web/package.json"]
    refs = git(world.mac, "for-each-ref", "--format=%(refname)").stdout.split()
    assert f"refs/wardian/{NAME}/agent/fix" in refs
    assert "refs/tags/t1" not in refs
    assert not any("other" in ref for ref in refs)
    assert (world.mac / "words.txt").read_text() == "base\n"
    assert git(world.mac, "status", "--porcelain").stdout == ""


def test_fetch_strips_control_characters_from_paths(world, capsys):
    assert wardian("push", world) == 0
    agent_commits(world, {".evil\x1b[31m": "x\n"})
    capsys.readouterr()

    assert wardian("fetch", world) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "    .evil[31m\n" in out


# Each rewrites the sandbox URL to the elsewhere repo, for push or fetch.
REWRITES = {
    "insteadOf": lambda w: ("url." + w.elsewhere_url + ".insteadOf", w.url),
    "pushInsteadOf": lambda w: ("url." + w.elsewhere_url + ".pushInsteadOf", w.url),
    "prefix insteadOf": lambda w: (
        f"url.ssh://lima@127.0.0.1:60022{w.tmp}/redirected-.insteadOf",
        f"ssh://lima@127.0.0.1:60022{w.tmp}/",
    ),
    "remote named after the URL": lambda w: (
        f"remote.{w.url}.pushurl",
        w.elsewhere_url,
    ),
}


@pytest.mark.parametrize("scope", ["--local", "--global"])
@pytest.mark.parametrize("rewrite", sorted(REWRITES))
@pytest.mark.parametrize("command", ["push", "fetch"])
def test_refuses_config_that_redirects_the_sandbox(
    world, capsys, command, rewrite, scope
):
    key, value = REWRITES[rewrite](world)
    git(world.mac, "config", scope, key, value)

    assert wardian(command, world) == 1

    assert "refusing: git config" in capsys.readouterr().err
    assert (
        git(world.elsewhere, "rev-parse", "--verify", "-q", "main", check=False).stdout
        == ""
    )


@pytest.mark.parametrize("rewrite", sorted(REWRITES))
def test_mutation_without_the_check_push_goes_elsewhere(world, monkeypatch, rewrite):
    key, value = REWRITES[rewrite](world)
    git(world.mac, "config", key, value)
    monkeypatch.setattr(cli, "check_destination", lambda repo, url: None)

    wardian("push", world)

    assert git(
        world.elsewhere, "rev-parse", "--verify", "-q", "main", check=False
    ).stdout


@pytest.mark.parametrize("command", ["push", "fetch"])
def test_an_unrelated_rewrite_doesnt_block(world, command):
    git(
        world.mac,
        "config",
        "--global",
        "url.git@github.com:.insteadOf",
        "https://github.com/",
    )
    assert wardian("push", world) == 0
    assert wardian(command, world) == 0


@pytest.fixture
def bundle_listener():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/bundle", requests
    server.shutdown()


def test_fetch_ignores_a_configured_bundle_uri(world, bundle_listener):
    uri, requests = bundle_listener
    assert wardian("push", world) == 0
    agent_commits(world, {"words.txt": "fixed\n"})
    git(world.mac, "config", "fetch.bundleURI", uri)

    assert wardian("fetch", world) == 0

    assert requests == []


def test_mutation_without_the_override_fetch_asks_the_bundle_uri(
    world, bundle_listener, monkeypatch
):
    uri, requests = bundle_listener
    assert wardian("push", world) == 0
    agent_commits(world, {"words.txt": "fixed\n"})
    git(world.mac, "config", "fetch.bundleURI", uri)
    monkeypatch.setattr(cli, "FETCH_CHECKS", ("-c", "fetch.fsckObjects=true"))

    wardian("fetch", world)

    assert requests


HOOKS = ("pre-push", "reference-transaction", "post-merge", "fsmonitor-watchman")


def install_hooks(world):
    marker = world.tmp / "hook-ran"
    hooks = world.mac / ".git" / "hooks"
    for hook in HOOKS:
        (hooks / hook).write_text(
            f"#!/bin/sh\necho {hook} >> {shlex.quote(str(marker))}\n"
        )
        (hooks / hook).chmod(0o755)
    return marker


def test_no_hook_runs(world):
    marker = install_hooks(world)

    assert wardian("push", world) == 0
    agent_commits(world, {"words.txt": "fixed\n"})
    assert wardian("fetch", world) == 0

    assert not marker.exists()


def test_mutation_without_hooks_path_hooks_run(world, monkeypatch):
    marker = install_hooks(world)
    monkeypatch.setattr(cli, "NO_HOOKS", ())

    assert wardian("push", world) == 0

    assert marker.exists()


@pytest.mark.parametrize("command", ["push", "fetch"])
def test_refuses_an_old_git(world, monkeypatch, capsys, command):
    real = cli.capture

    def old_git(cmd, stdin_data=None):
        if cmd == ["git", "version"]:
            return "git version 2.41.0\n"
        return real(cmd, stdin_data)

    monkeypatch.setattr(cli, "capture", old_git)

    assert wardian(command, world) == 1
    assert "needs git 2.42 or later" in capsys.readouterr().err


def test_git_ssh_command_ignores_the_users_ssh_config(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    instance = {
        "sshAddress": "127.0.0.1",
        "sshLocalPort": 60022,
        "config": {"user": {"name": "lima"}},
        "IdentityFile": "/lima/_config/user",
    }

    ssh = shlex.split(cli.git_env(NAME, instance)["GIT_SSH_COMMAND"])

    assert ssh[:3] == ["ssh", "-F", "/dev/null"]
    assert "IdentityFile=/lima/_config/user" in ssh
    assert "StrictHostKeyChecking=yes" in ssh
    assert f"HostKeyAlias={NAME}" in ssh


@pytest.mark.parametrize(
    ("path", "flagged"),
    [
        (".envrc", True),
        ("a/.git-hooks/pre-commit", True),
        ("Makefile", True),
        ("sub/Cargo.toml", True),
        ("src/main.py", False),
        ("docs/makefile.md", False),
    ],
)
def test_auto_run_rule(path, flagged):
    assert cli.auto_run(path) is flagged
