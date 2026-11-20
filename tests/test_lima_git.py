"""Git in and out of a real Lima sandbox, and destroy's guard.

These tests need a running sandbox made by `wardian create`, named in
WARDIAN_LIMA_SANDBOX. They push into its repo, and the last test destroys the
sandbox. Run them in file order:

    WARDIAN_LIMA_SANDBOX=wardian uv run pytest tests/test_lima_git.py
"""

import os
import shlex
import subprocess

import pytest

from wardian import cli

NAME = os.environ.get("WARDIAN_LIMA_SANDBOX", "")
pytestmark = pytest.mark.skipif(
    not NAME, reason="set WARDIAN_LIMA_SANDBOX to a sandbox made by wardian create"
)

# The fetch from docs/cli.md, without the output filter.
FETCH = [
    "git", "-c", "fetch.fsckObjects=true", "fetch", "--no-tags",
    "--no-recurse-submodules", "sandbox",
    "+refs/heads/agent/*:refs/remotes/sandbox/agent/*",
]  # fmt: skip


@pytest.fixture(scope="module")
def instance():
    found = cli.find_instance(NAME)
    assert found is not None and found["status"] == "Running"
    return found


def guest(instance, script: str) -> str:
    """Run script as agent in the sandbox's repo and return its output."""
    command = f"cd {cli.GUEST_REPO} && {script}"
    return cli.capture(
        [*cli.admin_ssh(NAME, instance), f"{cli.AS_AGENT} sh -c {shlex.quote(command)}"]
    )


@pytest.fixture(scope="module")
def mac(instance, tmp_path_factory):
    """A Mac repo with the sandbox remote set up as docs/cli.md describes."""
    repo = tmp_path_factory.mktemp("mac")
    address, port, user, identity = cli.connection(instance)
    ssh = ["ssh", *cli.ssh_options(NAME), "-o", f"IdentityFile={identity}"]
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_SSH_COMMAND": shlex.join(ssh),
        "GIT_AUTHOR_NAME": "Mac",
        "GIT_AUTHOR_EMAIL": "mac@example.invalid",
        "GIT_COMMITTER_NAME": "Mac",
        "GIT_COMMITTER_EMAIL": "mac@example.invalid",
    }

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "--quiet", "--initial-branch=main")
    for args in (
        ["remote", "add", "--no-tags", "sandbox",
         f"ssh://{user}@{address}:{port}{cli.GUEST_REPO}"],
        ["config", "remote.sandbox.fetch",
         "+refs/heads/agent/*:refs/remotes/sandbox/agent/*"],
        ["config", "remote.sandbox.receivepack", f"{cli.AS_AGENT} git-receive-pack"],
        ["config", "remote.sandbox.uploadpack", f"{cli.AS_AGENT} git-upload-pack"],
    ):  # fmt: skip
        git(*args)
    git.repo = repo
    return git


def commit(mac, text: str) -> None:
    (mac.repo / "words.txt").write_text(text)
    mac("add", "words.txt")
    mac("commit", "--quiet", "--message", text)


def test_push_updates_the_worktree(instance, mac):
    commit(mac, "base\n")
    mac("push", "sandbox", "main")
    assert guest(instance, "git status --porcelain && cat words.txt") == "base\n"


def test_push_is_refused_over_uncommitted_changes(instance, mac):
    guest(instance, "echo agent >> words.txt")
    commit(mac, "new base\n")

    push = mac("push", "sandbox", "main", check=False)

    assert push.returncode != 0
    assert "Working directory has unstaged changes" in push.stderr
    assert guest(instance, "cat words.txt") == "base\nagent\n"

    guest(instance, "git checkout -- words.txt")
    mac("push", "sandbox", "main")
    assert guest(instance, "cat words.txt") == "new base\n"


@pytest.mark.parametrize(
    ("make", "undo", "reason"),
    [
        ("echo x >> words.txt", "git checkout -- words.txt",
         "uncommitted changes in 1 files"),
        ("touch new.txt", "rm new.txt", "1 untracked files"),
        ("echo x >> words.txt && git stash --quiet", "git stash drop --quiet",
         "a stash"),
        ("git tag v1", "git tag -d v1", "tag v1"),
        ("git switch --quiet -c agent/work && git commit --quiet --allow-empty -m w && git switch --quiet main",
         "git branch --quiet -D agent/work", "branch agent/work at"),
        ("git switch --quiet --detach && git commit --quiet --allow-empty -m loose",
         "git switch --quiet main", "detached HEAD at"),
    ],
    ids=["dirty", "untracked", "stash", "tag", "branch", "detached"],
)  # fmt: skip
def test_destroy_refuses_unfetched_work(instance, mac, capsys, make, undo, reason):
    guest(instance, make)
    try:
        assert cli.main(["destroy", NAME, "--repo", str(mac.repo)]) == 1
    finally:
        guest(instance, undo)
    err = capsys.readouterr().err
    assert f"has work that isn't fetched: {reason}" in err
    assert cli.find_instance(NAME) is not None


def test_fetch_takes_agent_branches_and_no_tags(instance, mac):
    guest(
        instance,
        "git switch --quiet -c agent/fix && echo fixed > words.txt"
        " && git commit --quiet -am fix && git tag t1"
        " && git branch other && git switch --quiet main",
    )
    tip = guest(instance, "git rev-parse agent/fix").strip()

    mac(*FETCH[1:])

    refs = mac("for-each-ref", "--format=%(refname) %(objectname)").stdout
    assert f"refs/remotes/sandbox/agent/fix {tip}" in refs
    assert "refs/tags/t1" not in refs
    assert "other" not in refs

    guest(instance, "git tag -d t1 && git branch -D other")


def test_destroy_succeeds_once_everything_is_fetched(instance, mac):
    assert cli.main(["destroy", NAME, "--repo", str(mac.repo)]) == 0
    assert cli.find_instance(NAME) is None
    assert not cli.known_hosts_file(NAME).exists()
