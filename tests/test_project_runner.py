"""project_runner plan ayrıştırma sözleşmesi."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_runner  # noqa: E402
from adapters import LLMResponse  # noqa: E402
from project_runner import _parse_requested_files, _plan_summary, run_project_task  # noqa: E402


def test_plan_summary_strips_files_protocol_block():
    text = "1. Dosyayı incele\n2. Dönüşümü yap\n\nFILES:\n- src/a.py\n- src/b.py"
    assert _plan_summary(text) == "1. Dosyayı incele\n2. Dönüşümü yap"


def test_requested_files_only_allows_project_files_and_caps_at_eight():
    valid = {f"src/{n}.py" for n in range(10)}
    text = "FILES:\n" + "\n".join(f"- src/{n}.py" for n in range(10)) + "\n- dışarı.py"
    assert _parse_requested_files(text, valid) == [f"src/{n}.py" for n in range(8)]


class _FakeAgent:
    """Records every user_prompt it was called with (fake project_runner.Agent)."""

    def __init__(self, role: str, provider: str = "fake"):
        self.role = role
        self.provider = provider
        self.calls: list[str] = []

    def run(self, user_prompt: str) -> LLMResponse:
        self.calls.append(user_prompt)
        if self.role == "reviewer":
            text = "VERDICT: APPROVED"
        elif self.role == "planner":
            text = "1. Do the thing\n\nFILES:\n"
        else:
            text = "no file blocks"
        return LLMResponse(text=text, provider=self.provider, model="fake-model")


def test_project_rules_are_prepended_to_every_legacy_role_prompt(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(
        "LEGACY-RULE-MARKER: never touch secrets.py.", encoding="utf-8",
    )
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    fake_agents = {
        "planner": _FakeAgent("planner"),
        "coder": _FakeAgent("coder"),
        "reviewer": _FakeAgent("reviewer"),
    }
    monkeypatch.setattr(project_runner, "build_agents", lambda routing=None: fake_agents)

    list(run_project_task(str(tmp_path), "Do something"))

    marker = "LEGACY-RULE-MARKER: never touch secrets.py."
    assert fake_agents["planner"].calls and marker in fake_agents["planner"].calls[0]
    assert fake_agents["coder"].calls and marker in fake_agents["coder"].calls[0]
    # Reviewer only runs when there are proposals; the fake coder produces
    # none, so the reviewer stage is not reached here -- covered by the
    # rendering-level assertion below instead.


def test_project_rules_reach_the_reviewer_too(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(
        "LEGACY-RULE-MARKER: never touch secrets.py.", encoding="utf-8",
    )
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    class _CoderAgent(_FakeAgent):
        def run(self, user_prompt: str) -> LLMResponse:
            self.calls.append(user_prompt)
            return LLMResponse(
                text="### FILE: a.txt\n```\nhello changed\n```\n", provider=self.provider, model="fake-model",
            )

    fake_agents = {
        "planner": _FakeAgent("planner"),
        "coder": _CoderAgent("coder"),
        "reviewer": _FakeAgent("reviewer"),
    }
    monkeypatch.setattr(project_runner, "build_agents", lambda routing=None: fake_agents)

    list(run_project_task(str(tmp_path), "Do something"))

    marker = "LEGACY-RULE-MARKER: never touch secrets.py."
    assert fake_agents["reviewer"].calls and marker in fake_agents["reviewer"].calls[0]


def test_project_rules_absent_means_no_rules_section(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    fake_agents = {
        "planner": _FakeAgent("planner"),
        "coder": _FakeAgent("coder"),
        "reviewer": _FakeAgent("reviewer"),
    }
    monkeypatch.setattr(project_runner, "build_agents", lambda routing=None: fake_agents)

    list(run_project_task(str(tmp_path), "Do something"))

    assert fake_agents["planner"].calls[0].startswith("Görev:")
    assert "untrusted" not in fake_agents["planner"].calls[0].lower()
