import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ansible/roles/control_tests/files/control_tests.py"
)


@pytest.fixture
def control_tests(monkeypatch):
    spec = importlib.util.spec_from_file_location("control_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module,
        "THREAT_IDS",
        {"T90": ("both", "both test"), "T91": ("lima", "lima-only test")},
    )
    return module


def records(capsys):
    return {r["id"]: r for r in map(json.loads, capsys.readouterr().out.splitlines())}


def test_passes_when_every_mandatory_test_passes(control_tests, monkeypatch, capsys):
    monkeypatch.setattr(control_tests, "TESTS", {"T90": lambda: (True, "ok")})
    assert control_tests.main(["--env", "ci"]) == 0
    results = {id_: r["result"] for id_, r in records(capsys).items()}
    assert results == {"T90": "pass", "T91": "skip"}


@pytest.mark.parametrize("env", ["ci", "lima"])
def test_a_deleted_test_fails_the_run(control_tests, monkeypatch, capsys, env):
    monkeypatch.setattr(control_tests, "TESTS", {"T91": lambda: (True, "ok")})
    assert control_tests.main(["--env", env]) == 1
    assert records(capsys)["T90"]["result"] == "fail"


def test_only_runs_the_named_ids(control_tests, monkeypatch, capsys):
    monkeypatch.setattr(
        control_tests,
        "TESTS",
        {"T90": lambda: (True, "ok"), "T91": lambda: (False, "broken")},
    )
    assert control_tests.main(["--env", "lima", "--only", "T90"]) == 0
    results = {id_: r["result"] for id_, r in records(capsys).items()}
    assert results == {"T90": "pass", "T91": "skip"}
