import importlib.util
import subprocess
from pathlib import Path

import pytest

FILES = Path(__file__).resolve().parents[1] / "ansible/roles/wardian_apt/files"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wardian_apt():
    return load("wardian_apt")


@pytest.fixture
def worker():
    return load("wardian_apt_worker")


@pytest.mark.parametrize(
    "name", ["jq", "libstdc++6", "libc6-dev", "python3.13", "r-base"]
)
def test_accepts_package_names(wardian_apt, name):
    assert wardian_apt.refused_names([name]) == []


@pytest.mark.parametrize(
    "name",
    [
        "-y",
        "--reinstall",
        "-oAPT::Get::Assume-Yes=1",
        "/tmp/jq.deb",  # noqa: S108 - an argument the wrapper must refuse
        "./jq.deb",
        "../jq",
        "jq+",
        "jq-",
        "g++",
        "jq=1.7",
        "jq/resolute",
        "JQ",
        "j",
        "",
        "jq\n",
    ],
)
def test_refuses_anything_else(wardian_apt, name):
    assert wardian_apt.refused_names(["jq", name]) == [name]


def test_refuses_before_running_anything(wardian_apt, monkeypatch, capsys):
    monkeypatch.setattr(wardian_apt.os, "geteuid", lambda: 0)

    def refuse(*args, **kwargs):
        raise AssertionError("ran a subprocess")

    monkeypatch.setattr(wardian_apt.subprocess, "run", refuse)
    assert wardian_apt.main(["--reinstall", "jq"]) == 2
    assert "'--reinstall'" in capsys.readouterr().err


SIMULATION = """\
NOTE: This is only a simulation!
Reading package lists...
The following NEW packages will be installed:
  jq libjq1 libonig5
Inst libonig5 (6.9.10-1 Ubuntu:26.04/resolute [arm64])
Inst libjq1 (1.8.1-1 Ubuntu:26.04/resolute [arm64])
Inst jq (1.8.1-1 Ubuntu:26.04/resolute [arm64])
Conf libonig5 (6.9.10-1 Ubuntu:26.04/resolute [arm64])
Conf libjq1 (1.8.1-1 Ubuntu:26.04/resolute [arm64])
Conf jq (1.8.1-1 Ubuntu:26.04/resolute [arm64])
"""


def test_new_packages_change_nothing_installed(worker):
    assert worker.changes_to_installed(SIMULATION) == []


@pytest.mark.parametrize(
    "line",
    [
        "Inst libc6 [2.42-1] (2.42-2 Ubuntu:26.04/resolute-updates [arm64])",
        "Inst jq [1.8.1-1] (1.8.1-1 Ubuntu:26.04/resolute [arm64])",
        "Remv libfoo1 [1.0-1]",
        "Purg libfoo1 [1.0-1]",
    ],
)
def test_upgrades_reinstalls_and_removals_are_changes(worker, line):
    assert worker.changes_to_installed(SIMULATION + line + "\n") == [line]


class BrokenStderr:
    def write(self, text):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


@pytest.mark.parametrize("unchanged", [lambda: False, lambda: 1 / 0])
def test_powers_off_even_when_stderr_is_broken(worker, monkeypatch, unchanged):
    calls = []
    monkeypatch.setattr(worker, "firewall_unchanged", unchanged)
    monkeypatch.setattr(worker.sys, "stderr", BrokenStderr())
    monkeypatch.setattr(
        worker.subprocess, "run", lambda argv, **kwargs: calls.append(argv)
    )
    assert worker.check_firewall() is False
    assert calls == [["/usr/bin/systemctl", "poweroff"]]


def test_waits_for_the_status_file(wardian_apt, tmp_path, monkeypatch):
    status = tmp_path / "status"
    polls = []

    def is_active(argv, **kwargs):
        polls.append(argv)
        if len(polls) == 2:
            status.write_text("3\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(wardian_apt.subprocess, "run", is_active)
    monkeypatch.setattr(wardian_apt.time, "sleep", lambda seconds: None)
    assert wardian_apt.wait_for("wardian-apt-x", str(status)) == 3


def test_a_worker_that_stops_without_a_status_fails(wardian_apt, tmp_path, monkeypatch):
    monkeypatch.setattr(
        wardian_apt.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3),
    )
    assert wardian_apt.wait_for("wardian-apt-x", str(tmp_path / "status")) == 1
