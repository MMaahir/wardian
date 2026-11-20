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

    assert cli.main([command]) == 1
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

    assert cli.main(["create"]) == 1
    assert "security add-generic-password" in capsys.readouterr().err


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


def test_refuses_an_object_name_that_isnt_hex(monkeypatch):
    fake_guest(monkeypatch, CLEAN_STATUS, MAIN_REF + "--all refs/heads/agent/x\n")
    with pytest.raises(cli.WardianError, match="unexpected object name"):
        cli.unfetched_work("wardian", {}, cli.Path("/mac"))
