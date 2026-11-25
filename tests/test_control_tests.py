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


@pytest.mark.parametrize("text", ["", "both\n", "Claude\n"])
def test_an_unknown_agent_mode_is_an_error(control_tests, tmp_path, monkeypatch, text):
    mode_file = tmp_path / "agent-mode"
    mode_file.write_text(text)
    monkeypatch.setattr(control_tests, "MODE_FILE", str(mode_file))
    with pytest.raises(ValueError, match="not claude or codex"):
        control_tests.agent_mode()


def test_t10_fails_when_a_codex_sandbox_has_a_token(
    control_tests, tmp_path, monkeypatch
):
    (tmp_path / "agent-mode").write_text("codex\n")
    monkeypatch.setattr(control_tests, "MODE_FILE", str(tmp_path / "agent-mode"))
    monkeypatch.setattr(control_tests, "TOKEN", str(tmp_path / "claude-token"))
    assert control_tests.others_cannot_read_token()[0] is True

    (tmp_path / "claude-token").write_text("sk-ant-oat01-x")
    assert control_tests.others_cannot_read_token() == (
        False,
        f"Codex mode, but {tmp_path / 'claude-token'} exists",
    )
