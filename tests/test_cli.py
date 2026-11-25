import copy
import subprocess

import pytest

from wardian import cli

# Lima 2.3's merged config for lima/wardian.yaml, cut to the fields the CLI checks.
GOOD_CONFIG = {
    "ssh": {"localPort": 60022, "forwardAgent": False, "overVsock": False},
    "portForwards": [dict(cli.CATCH_ALL_IGNORE)],
    "networkOpts": {"hostLoopback": False},
    "propagateProxyEnv": False,
    "user": {"name": "lima", "uid": 1000},
}


@pytest.fixture
def no_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"ran a subprocess: {args[0]}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.mark.parametrize("command", ["create", "test"])
@pytest.mark.parametrize("filename", ["default.yaml", "override.yaml", "base.yaml"])
def test_refuses_lima_global_config(
    command, filename, tmp_path, monkeypatch, no_subprocess, capsys
):
    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / filename).write_text("mounts: [{location: '~'}]\n")
    monkeypatch.setenv("LIMA_HOME", str(tmp_path))

    args = ["create", "--agent", "codex"] if command == "create" else [command]
    assert cli.main(args) == 1
    assert filename in capsys.readouterr().err


def test_good_config_has_no_problems():
    assert cli.config_problems(GOOD_CONFIG) == []


def test_absent_lists_are_fine():
    assert cli.config_problems({**GOOD_CONFIG, "mounts": [], "provision": None}) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(mounts=[{"location": "~"}]),
        lambda c: c.update(networks=[{"lima": "shared"}]),
        lambda c: c.update(provision=[{"mode": "system", "script": "true"}]),
        lambda c: c["networkOpts"].update(hostLoopback=True),
        lambda c: c.pop("networkOpts"),
        lambda c: c.update(propagateProxyEnv=True),
        lambda c: c["ssh"].update(forwardAgent=True),
        lambda c: c["ssh"].update(overVsock=True),
        lambda c: c["ssh"].pop("overVsock"),
        lambda c: c.update(portForwards=[]),
        lambda c: c["portForwards"].append({"guestPort": 22, "hostPort": 2222}),
        lambda c: c["portForwards"][0].update(ignore=False),
        lambda c: c["user"].update(name="agent"),
    ],
)
def test_bad_config_is_refused(change):
    config = copy.deepcopy(GOOD_CONFIG)
    change(config)
    assert cli.config_problems(config)


@pytest.mark.parametrize("name", ["wardian", "a", "toy-2", "a" * 31])
def test_valid_names(name):
    assert cli.sandbox_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "Wardian", "2toy", "-toy", "toy_2", "a" * 32, "toy\n", "../x"]
)
def test_invalid_names(name):
    with pytest.raises(Exception, match="invalid sandbox name"):
        cli.sandbox_name(name)


def test_clean_strips_control_characters():
    assert cli.clean("a\x1b[31mb\x07c\rd\x9be\x7f\tf\n") == "a[31mbcde\tf\n"


def test_destroy_refuses_instance_wardian_did_not_create(
    tmp_path, monkeypatch, no_subprocess, capsys
):
    monkeypatch.setattr(cli, "find_instance", lambda name: {"dir": str(tmp_path)})

    assert cli.main(["destroy"]) == 1
    assert "wasn't made by wardian create" in capsys.readouterr().err


def test_reads_claude_token_from_keychain(monkeypatch):
    calls = []

    def fake_capture(cmd, stdin_data=None):
        calls.append(cmd)
        return "sk-ant-oat01-abc_DEF-123\n"

    monkeypatch.setattr(cli, "capture", fake_capture)

    assert cli.read_claude_token() == b"sk-ant-oat01-abc_DEF-123"
    assert calls == [
        ["security", "find-generic-password", "-s", "wardian-claude-token", "-w"]
    ]


@pytest.mark.parametrize(
    "item", ["", "\n", "hunter2\n", "sk-ant-api03-key\n", "sk-ant-oat01-a\nb\n"]
)
def test_refuses_keychain_item_that_is_not_a_claude_token(item, monkeypatch):
    monkeypatch.setattr(cli, "capture", lambda cmd, stdin_data=None: item)

    with pytest.raises(cli.WardianError, match="isn't a Claude token") as exc:
        cli.read_claude_token()
    if item.strip():
        assert item.strip() not in str(exc.value)


def test_create_stops_before_building_a_vm_without_a_token(
    tmp_path, monkeypatch, no_subprocess, capsys
):
    monkeypatch.setenv("LIMA_HOME", str(tmp_path))

    def missing(cmd, stdin_data=None):
        raise cli.WardianError("security find-generic-password exited 44")

    monkeypatch.setattr(cli, "capture", missing)

    assert cli.main(["create", "--agent", "claude"]) == 1
    assert "security add-generic-password" in capsys.readouterr().err


def test_create_requires_an_agent(capsys):
    with pytest.raises(SystemExit):
        cli.main(["create"])
    assert "--agent" in capsys.readouterr().err


def test_codex_mode_never_reads_the_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMA_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "read_claude_token", lambda: pytest.fail("read it"))

    def stop(cmd, env=None):
        raise cli.WardianError("stop at limactl create")

    monkeypatch.setattr(cli, "run", stop)

    assert cli.main(["create", "--agent", "codex"]) == 1


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    return tmp_path


def test_host_block_uses_agent_and_the_cli_ssh_options(home):
    path = cli.write_host_block("toy", "127.0.0.1", 60022)

    assert path == home / ".wardian" / "ssh_config"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == (
        "# BEGIN wardian-toy\n"
        "Host wardian-toy\n"
        "    HostName 127.0.0.1\n"
        "    Port 60022\n"
        "    User agent\n"
        f'    IdentityFile "{home}/.wardian/toy/agent_ed25519"\n'
        "    IdentitiesOnly yes\n"
        "    ForwardAgent no\n"
        "    ControlMaster no\n"
        "    ControlPath none\n"
        "    StrictHostKeyChecking yes\n"
        "    HostKeyAlias toy\n"
        f'    UserKnownHostsFile "{home}/.ssh/known_hosts.d/toy"\n'
        "    GlobalKnownHostsFile /dev/null\n"
        "# END wardian-toy\n"
    )


def test_host_blocks_for_other_sandboxes_are_kept(home):
    cli.write_host_block("one", "127.0.0.1", 60022)
    cli.write_host_block("two", "127.0.0.1", 60022)
    cli.write_host_block("one", "127.0.0.1", 60023)

    text = cli.host_config().read_text()
    assert text.count("Host wardian-one\n") == 1
    assert "Port 60023" in text
    assert "Host wardian-two\n" in text

    cli.remove_host_block("one")
    text = cli.host_config().read_text()
    assert "wardian-one" not in text
    assert text.startswith("# BEGIN wardian-two\n")


def test_agent_key_is_new_and_private(home):
    public = cli.make_agent_key("toy")
    key = cli.agent_key("toy")

    assert public.startswith("ssh-ed25519 ")
    assert public.endswith(" agent@toy")
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.parent.stat().st_mode & 0o777 == 0o700
    assert cli.make_agent_key("toy") != public


def test_destroy_refuses_a_sandbox_that_isnt_running(
    tmp_path, monkeypatch, no_subprocess, capsys
):
    (tmp_path / cli.OWNER_MARKER).touch()
    instance = {"dir": str(tmp_path), "status": "Stopped"}
    monkeypatch.setattr(cli, "find_instance", lambda name: instance)

    assert cli.main(["destroy"]) == 1
    err = capsys.readouterr().err
    assert "is Stopped" in err
    assert "Discarding a sandbox by hand" in err


OID_A = "a" * 40
OID_B = "b" * 40
CLEAN_STATUS = f"# branch.oid {OID_A}\n# branch.head main\n"
MAIN_REF = f"{OID_A} refs/heads/main\n"


def fake_guest(monkeypatch, status, refs, on_mac=(OID_A,)):
    """Make the sandbox's repo answer with status and refs."""

    def fake_capture(cmd, stdin_data=None):
        if cmd[0] == "git":  # the Mac repo check
            return ".git\n"
        subcommand = cmd[-1].split(f"git -C {cli.GUEST_REPO} ")[1].split()[0]
        return {"status": status, "for-each-ref": refs}[subcommand]

    monkeypatch.setattr(cli, "admin_ssh", lambda name, instance: ["ssh"])
    monkeypatch.setattr(cli, "capture", fake_capture)
    monkeypatch.setattr(cli, "on_mac", lambda repo, oid: oid in on_mac)


def test_fully_fetched_repo_has_no_unfetched_work(monkeypatch):
    fake_guest(monkeypatch, CLEAN_STATUS, MAIN_REF)
    assert cli.unfetched_work("wardian", {}, cli.Path("/mac")) == []


def test_empty_repo_has_no_unfetched_work(monkeypatch):
    status = "# branch.oid (initial)\n# branch.head main\n"
    fake_guest(monkeypatch, status, "", on_mac=())
    assert cli.unfetched_work("wardian", {}, cli.Path("/mac")) == []


@pytest.mark.parametrize(
    ("status", "refs", "problem"),
    [
        (CLEAN_STATUS + "1 .M N... 100644 100644 100644 x y a.py\n", MAIN_REF,
         "uncommitted changes in 1 files"),
        (CLEAN_STATUS + "? new.py\n? b.py\n", MAIN_REF, "2 untracked files"),
        (CLEAN_STATUS, MAIN_REF + f"{OID_A} refs/stash\n", "a stash"),
        (CLEAN_STATUS, MAIN_REF + f"{OID_A} refs/tags/v1\n", "tag v1"),
        (CLEAN_STATUS, MAIN_REF + f"{OID_B} refs/heads/agent/x\n",
         f"branch agent/x at {OID_B[:12]} isn't in /mac"),
        (f"# branch.oid {OID_B}\n# branch.head (detached)\n", MAIN_REF,
         f"detached HEAD at {OID_B[:12]} isn't in /mac"),
        (CLEAN_STATUS, MAIN_REF + f"{OID_A} refs/tags/\x1b[31mred\n", "tag [31mred"),
    ],
)  # fmt: skip
def test_unfetched_work_names_each_reason(monkeypatch, status, refs, problem):
    fake_guest(monkeypatch, status, refs)
    assert cli.unfetched_work("wardian", {}, cli.Path("/mac")) == [problem]


def test_a_mac_dir_that_isnt_a_repo_keeps_the_other_reasons(monkeypatch):
    fake_guest(monkeypatch, CLEAN_STATUS + "? new.py\n", MAIN_REF)
    guest_capture = cli.capture

    def fake_capture(cmd, stdin_data=None):
        if cmd[0] == "git":
            raise cli.WardianError("git -C exited 128: fatal: not a git repository")
        return guest_capture(cmd)

    monkeypatch.setattr(cli, "capture", fake_capture)
    assert cli.unfetched_work("wardian", {}, cli.Path("/mac")) == [
        "1 untracked files",
        "can't read the Mac repo: git -C exited 128: fatal: not a git repository",
    ]


def test_refuses_an_object_name_that_isnt_hex(monkeypatch):
    fake_guest(monkeypatch, CLEAN_STATUS, MAIN_REF + "--all refs/heads/agent/x\n")
    with pytest.raises(cli.WardianError, match="unexpected object name"):
        cli.unfetched_work("wardian", {}, cli.Path("/mac"))
