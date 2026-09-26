import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_runtime.errors import PipelineInputError  # noqa: E402
from pipeline_runtime.verification_detect import _PY, detect_verification_plan  # noqa: E402


def test_returns_none_for_empty_workspace(tmp_path):
    assert detect_verification_plan(tmp_path) is None


def test_explicit_verify_json_takes_precedence(tmp_path):
    (tmp_path / "tests").mkdir()
    imece_dir = tmp_path / ".imece"
    imece_dir.mkdir()
    (imece_dir / "verify.json").write_text(json.dumps([
        {"id": "c1", "title": "Custom check", "argv": ["true"], "timeout_ms": 1000},
    ]), encoding="utf-8")

    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert [c.check_id for c in plan.checks] == ["c1"]
    assert plan.checks[0].request.argv == ("true",)
    assert plan.checks[0].request.timeout_ms == 1000


def test_explicit_verify_json_rejects_shell_string_argv(tmp_path):
    imece_dir = tmp_path / ".imece"
    imece_dir.mkdir()
    (imece_dir / "verify.json").write_text(json.dumps([
        {"id": "c1", "title": "Bad", "argv": "true && false"},
    ]), encoding="utf-8")

    with pytest.raises(PipelineInputError):
        detect_verification_plan(tmp_path)


def test_explicit_verify_json_rejects_non_list(tmp_path):
    imece_dir = tmp_path / ".imece"
    imece_dir.mkdir()
    (imece_dir / "verify.json").write_text(json.dumps({"id": "c1"}), encoding="utf-8")

    with pytest.raises(PipelineInputError):
        detect_verification_plan(tmp_path)


def test_python_tests_directory_detected(tmp_path):
    (tmp_path / "tests").mkdir()
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert [c.check_id for c in plan.checks] == ["python_pytest"]
    assert plan.checks[0].request.argv == (_PY, "-m", "pytest", "-q")


def test_python_test_files_at_root_detected(tmp_path):
    (tmp_path / "test_foo.py").write_text("def test_x(): pass\n", encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert plan.checks[0].check_id == "python_pytest"


def test_pytest_ini_detected_without_tests_dir(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert plan.checks[0].check_id == "python_pytest"


def test_pyproject_pytest_ini_options_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert plan.checks[0].check_id == "python_pytest"


def test_npm_test_detected_with_real_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert [c.check_id for c in plan.checks] == ["npm_test"]
    assert plan.checks[0].request.argv == ("npm", "test")


def test_npm_default_placeholder_script_not_detected(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
    }), encoding="utf-8")
    assert detect_verification_plan(tmp_path) is None


def test_npm_missing_test_script_not_detected(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8")
    assert detect_verification_plan(tmp_path) is None


def test_cargo_toml_detected(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert [c.check_id for c in plan.checks] == ["cargo_test"]
    assert plan.checks[0].request.argv == ("cargo", "test")


def test_go_mod_detected(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    assert [c.check_id for c in plan.checks] == ["go_test"]
    assert plan.checks[0].request.argv == ("go", "test", "./...")


def test_monorepo_detects_multiple_checks(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")

    plan = detect_verification_plan(tmp_path)
    assert plan is not None
    ids = {c.check_id for c in plan.checks}
    assert ids == {"python_pytest", "npm_test", "cargo_test", "go_test"}
