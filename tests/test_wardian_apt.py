import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ansible/roles/wardian_apt/files/wardian_apt.py"
)


@pytest.fixture
def wardian_apt():
    spec = importlib.util.spec_from_file_location("wardian_apt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_new_packages_change_nothing_installed(wardian_apt):
    assert wardian_apt.changes_to_installed(SIMULATION) == []


@pytest.mark.parametrize(
    "line",
    [
        "Inst libc6 [2.42-1] (2.42-2 Ubuntu:26.04/resolute-updates [arm64])",
        "Inst jq [1.8.1-1] (1.8.1-1 Ubuntu:26.04/resolute [arm64])",
        "Remv libfoo1 [1.0-1]",
        "Purg libfoo1 [1.0-1]",
    ],
)
def test_upgrades_reinstalls_and_removals_are_changes(wardian_apt, line):
    assert wardian_apt.changes_to_installed(SIMULATION + line + "\n") == [line]


class BrokenStderr:
    def write(self, text):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


@pytest.mark.parametrize("unchanged", [lambda: False, lambda: 1 / 0])
def test_powers_off_even_when_stderr_is_broken(wardian_apt, monkeypatch, unchanged):
    calls = []
    monkeypatch.setattr(wardian_apt, "firewall_unchanged", unchanged)
    monkeypatch.setattr(wardian_apt.sys, "stderr", BrokenStderr())
    monkeypatch.setattr(
        wardian_apt.subprocess, "run", lambda argv, **kwargs: calls.append(argv)
    )
    assert wardian_apt.check_firewall() is False
    assert calls == [["/usr/bin/systemctl", "poweroff"]]
