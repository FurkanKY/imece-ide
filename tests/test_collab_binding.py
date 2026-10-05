"""Canonical rebinding of a worker request from its detached render recipe.

`collab_runtime.binding.bind_worker_input` re-renders the EXACT request that
production already built, from the bounded recipe carried on
`InitialWorkerRequest.render_context` / `FixWorkerRequest.render_context`, plus
the safe point's authoritative snapshot. These tests pin the three properties
that make that safe:

- the recipe is a detached, bounded *display* copy (verification preview /
  pinned paths / attempt budget / classification) -- never the live plan,
  never a ProcessRequest's env or cwd, never a caller's mutable list;
- binding preserves the request's own identity fields verbatim and only ever
  swaps the rendered bytes for the canonical snapshot block;
- when the recipe is absent, of the wrong type, or has been mutated behind the
  frozen dataclass, the default binder fails closed at the safe point instead
  of inventing a prompt.

No model, no git, no network: every case runs on the in-memory consumer and
workspace stand-ins from tests/test_collab_safe_point.py.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collab_runtime.context import ARTIFACT_RELPATH, render_snapshot_block  # noqa: E402
from collab_runtime.coordinator import Snapshot  # noqa: E402
from collab_runtime.models import build_context, canonical_json_bytes  # noqa: E402
from collab_runtime.safe_point import NativeWorkerSafePoint, SafePointError  # noqa: E402
from context_runtime import load_project_rules  # noqa: E402
from fix_runtime import prompt as fix_prompt  # noqa: E402
from fix_runtime.errors import FixLoopInputError  # noqa: E402
from fix_runtime.models import (  # noqa: E402
    FixTrigger, FixTriggerKind, FixWorkerRenderContext, FixWorkerRequest,
    InitialWorkerRenderContext, InitialWorkerRequest,
)
from fix_runtime.prompt import (  # noqa: E402
    MAX_FIX_INPUT_CHARS, render_fix_worker_input, render_initial_worker_input,
)
from process_runtime.models import ProcessRequest  # noqa: E402
from verification_runtime import VerificationCheck, VerificationPlan  # noqa: E402
from test_collab_safe_point import (  # noqa: E402
    _MemoryConsumer, _shared, _snapshots, _unit_workspace,
)

_TASK = "implement the approved task"
_PLAN = "advisory plan"
_PINS = ("src/b.py", "src/a.py")  # deliberately unsorted: input order is the contract
_SECRETS = ("s3cr3t-env-value", "IMECE_TOKEN", "secret-dir")


def _detached_preview(plan) -> str:
    """Exercise the production preview capture seam directly."""
    return fix_prompt._capture_verification_preview(plan)


def _verification_plan(name: str = "Approved check", check_id: str = "approved-check", argv=("pytest", "-q")):
    return VerificationPlan("plan-vp", (
        VerificationCheck(check_id, name, ProcessRequest(argv)),
    ))


def _secret_bearing_plan() -> VerificationPlan:
    """A plan whose check carries secrets the prompt must NEVER display."""
    return VerificationPlan("plan-vp", (
        VerificationCheck(
            "approved-check", "Approved check",
            ProcessRequest(("pytest", "-q"), cwd="secrets/secret-dir", env={"IMECE_TOKEN": "s3cr3t-env-value"}),
        ),
    ))


def _initial_request(root, *, preview, pins, task=_TASK, plan=_PLAN, shared=None) -> InitialWorkerRequest:
    """Build an initial request exactly the way production builds it."""
    return InitialWorkerRequest(
        task, render_initial_worker_input(
            task=task, plan=plan, verification_preview=preview,
            rules=load_project_rules(root, shared_snapshot=shared), pinned_paths=pins,
        ),
        plan, InitialWorkerRenderContext(preview, pins),
    )


def _followup_trigger(feedback: str = "also handle negative numbers") -> FixTrigger:
    return FixTrigger(FixTriggerKind.USER_FEEDBACK, None, feedback=feedback, diff_sha256="a" * 64)


def _fix_request(root, *, trigger, pins=(), classification=None, max_fix_attempts=2,
                 task=_TASK, plan=_PLAN, shared=None) -> FixWorkerRequest:
    return FixWorkerRequest(
        task, trigger, 1, render_fix_worker_input(
            task=task, plan=plan, trigger=trigger, attempt_index=1,
            max_fix_attempts=max_fix_attempts,
            rules=load_project_rules(root, shared_snapshot=shared),
            pinned_paths=pins, classification=classification,
        ),
        plan=plan, render_context=FixWorkerRenderContext(max_fix_attempts, pins, classification),
    )


def _safe_point(snapshot, task, *, authoritative=None, consumer=None):
    """A NativeWorkerSafePoint with the DEFAULT binder (no explicit callback)."""
    consumer = consumer if consumer is not None else _MemoryConsumer(snapshot)
    return consumer, NativeWorkerSafePoint(
        consumer, lambda: authoritative or snapshot, initial_snapshot=snapshot,
        member_id="alice", approved_task=task,
    )


# ---------------- detached, bounded recipes ----------------


def test_initial_recipe_detaches_bounded_display_and_copies_the_pin_list(tmp_path):
    preview = _detached_preview(_secret_bearing_plan())

    # Bounded display facts only -- never the whole ProcessRequest.
    assert type(preview) is str
    assert "Approved check" in preview
    assert "approved-check" in preview
    assert "pytest -q" in preview
    for secret in _SECRETS:
        assert secret not in preview

    pins = ["src/b.py", "src/a.py"]
    recipe = InitialWorkerRenderContext(preview, pins)
    assert recipe.verification_preview == preview
    assert recipe.pinned_paths == _PINS
    assert isinstance(recipe.pinned_paths, tuple)
    for secret in _SECRETS:
        assert secret not in recipe.verification_preview

    # The caller's mutable list is copied: later mutation/reordering cannot
    # rewrite a frozen recipe an already-queued request depends on.
    pins.append("src/c.py")
    pins.reverse()
    assert recipe.pinned_paths == _PINS

    rendered = render_initial_worker_input(
        task=_TASK, plan=_PLAN, verification_preview=recipe.verification_preview,
        pinned_paths=recipe.pinned_paths,
    )
    assert recipe.verification_preview in rendered
    assert rendered.index("- src/b.py") < rendered.index("- src/a.py")


def test_bound_initial_prompt_follows_the_recipe_not_the_live_plan(tmp_path):
    live_plan = _secret_bearing_plan()
    recipe = InitialWorkerRenderContext(_detached_preview(live_plan), _PINS)
    # Even same-process mutation of the original plan cannot alter saved facts.
    object.__setattr__(live_plan, "checks", _verification_plan(
        "Other check", "other-check", ("ruff", "check"),
    ).checks)
    initial, task = _snapshots()
    _, helper = _safe_point(initial, task)

    prepared = helper.prepare(
        _initial_request(tmp_path, preview=recipe.verification_preview, pins=recipe.pinned_paths),
        _unit_workspace(initial.state.base_commit, tmp_path),
    )

    # Contrast: the live-plan rendering path DOES follow the plan object ...
    from_plan = render_initial_worker_input(
        task=_TASK, plan=_PLAN, pinned_paths=_PINS,
        verification_plan=live_plan,
    )
    assert "ruff check" in from_plan
    # ... while the bound request is re-rendered from the detached recipe.
    text = prepared.request.rendered_input
    assert "pytest -q" in text
    assert "ruff check" not in text
    assert prepared.request.render_context == recipe


# ---------------- canonical rebinding ----------------


def test_initial_rebind_replaces_a_stale_disk_snapshot_and_keeps_file_rules(tmp_path):
    root = tmp_path / "rules"
    root.mkdir()
    (root / "AGENTS.md").write_text("Keep the public API stable.\n", encoding="utf-8")
    initial, task = _snapshots()
    stale_state = replace(
        initial.state,
        context=build_context(goal="stale on-disk context", decisions=[], interfaces={}),
    )
    (root / ARTIFACT_RELPATH).parent.mkdir()
    (root / ARTIFACT_RELPATH).write_bytes(canonical_json_bytes(
        _shared(Snapshot(initial.revision, stale_state), task.id).to_dict(),
    ))
    event = SimpleNamespace(revision="c" * 40)
    fresh_state = replace(
        initial.state,
        context=build_context(goal="authoritative context", decisions=[], interfaces={}),
    )
    authoritative = Snapshot(event.revision, fresh_state)

    preview = _detached_preview(_secret_bearing_plan())
    request = _initial_request(root, preview=preview, pins=_PINS)
    assert "stale on-disk context" in request.rendered_input  # the pre-bind bytes were disk-bound

    consumer = _MemoryConsumer(initial, pending=(event,))
    _, helper = _safe_point(initial, task, authoritative=authoritative, consumer=consumer)
    prepared = helper.prepare(request, _unit_workspace(initial.state.base_commit, root))

    text = prepared.request.rendered_input
    assert render_snapshot_block(_shared(authoritative, task.id)) in text
    assert "authoritative context" in text
    assert "stale on-disk context" not in text
    assert "Keep the public API stable." in text          # file rules survive the swap
    assert preview in text
    assert "- src/b.py" in text and "- src/a.py" in text
    # Metadata preservation: binding swaps rendered bytes and nothing else.
    bound = prepared.request
    assert (bound.task, bound.plan) == (request.task, request.plan)
    assert bound.render_context == request.render_context
    assert bound.render_context.verification_preview == preview
    assert bound.render_context.pinned_paths == _PINS
    prepared.acknowledge()
    assert consumer.acks == [event.revision]


def test_fix_rebind_preserves_followup_trigger_task_and_recipe_verbatim(tmp_path):
    initial, task = _snapshots()
    trigger = _followup_trigger()
    request = _fix_request(
        tmp_path, trigger=trigger, pins=_PINS, classification="code_bug", max_fix_attempts=1,
    )
    _, helper = _safe_point(initial, task)

    bound = helper.prepare(request, _unit_workspace(initial.state.base_commit, tmp_path)).request

    assert bound.trigger == request.trigger
    assert bound.trigger.kind is FixTriggerKind.USER_FEEDBACK
    assert bound.trigger.feedback == "also handle negative numbers"
    assert (bound.task, bound.plan, bound.attempt_index) == (request.task, request.plan, 1)
    assert bound.render_context == request.render_context
    assert bound.render_context.max_fix_attempts == 1
    assert bound.render_context.classification == "code_bug"
    assert bound.render_context.pinned_paths == _PINS
    # Binding never rewrites the recipe -- not the bound copy, not the original.
    assert request.render_context.max_fix_attempts == 1
    assert request.render_context.pinned_paths == _PINS
    text = bound.rendered_input
    assert "FOLLOW-UP INSTRUCTION FROM THE USER" in text
    assert "also handle negative numbers" in text
    assert "decision_classification: code_bug" in text
    assert "max_fix_attempts: 1" in text
    assert text.count(render_snapshot_block(_shared(initial, task.id))) == 1


# ---------------- the default binder fails closed ----------------


@pytest.mark.parametrize("kind", ["initial", "fix"])
def test_default_binding_same_snapshot_preserves_entire_canonical_input(tmp_path, kind):
    initial, task = _snapshots()
    shared = _shared(initial, task.id)
    if kind == "initial":
        request = _initial_request(
            tmp_path, preview=_detached_preview(_secret_bearing_plan()), pins=_PINS, shared=shared,
        )
    else:
        request = _fix_request(
            tmp_path, trigger=_followup_trigger(), pins=_PINS, classification="code_bug", shared=shared,
        )
    _, helper = _safe_point(initial, task)
    bound = helper.prepare(request, _unit_workspace(initial.state.base_commit, tmp_path)).request
    assert bound == request
    assert bound.rendered_input == request.rendered_input


@pytest.mark.parametrize("construct", [
    lambda: InitialWorkerRenderContext(123),
    lambda: InitialWorkerRenderContext("p" * (MAX_FIX_INPUT_CHARS + 1)),
    lambda: InitialWorkerRenderContext("preview", (False,)),
    lambda: InitialWorkerRenderContext("preview", ("",)),
    lambda: FixWorkerRenderContext(True),
    lambda: FixWorkerRenderContext(0),
    lambda: FixWorkerRenderContext(6),
    lambda: FixWorkerRenderContext(2, ("a\x00b",)),
    lambda: FixWorkerRenderContext(2, classification=123),
    lambda: FixWorkerRenderContext(2, classification="x" * 129),
])
def test_render_recipe_direct_constructors_fail_closed_on_invalid_values(construct):
    with pytest.raises(FixLoopInputError):
        construct()


@pytest.mark.parametrize("kind", ["missing", "wrong_type", "mutated_recipe"])
def test_default_binder_fails_closed_on_unusable_metadata(tmp_path, kind):
    initial, task = _snapshots()
    event = SimpleNamespace(revision="c" * 40)
    authoritative = Snapshot(event.revision, initial.state)
    consumer = _MemoryConsumer(initial, pending=(event,))
    _, helper = _safe_point(initial, task, authoritative=authoritative, consumer=consumer)
    request = _fix_request(
        tmp_path, trigger=_followup_trigger(), pins=_PINS, classification="code_bug",
    )
    if kind == "missing":
        request = replace(request, render_context=None)
    elif kind == "wrong_type":
        # Frozen dataclasses can be bypassed; the binder must not trust that.
        object.__setattr__(request, "render_context", "FixWorkerRenderContext(2, (), 'code_bug')")
    else:
        # bool is not int: strict metadata bounds must be re-checked at bind time.
        object.__setattr__(request.render_context, "max_fix_attempts", True)

    with pytest.raises(SafePointError, match="worker attempt was not started"):
        helper.prepare(request, _unit_workspace(initial.state.base_commit, tmp_path))

    assert consumer.acks == []
    assert consumer.consumed == initial.revision
    assert consumer.pending == (event,)


def test_out_of_bounds_recipe_inputs_opt_out_without_touching_prompt_bytes(tmp_path):
    pins = [f"src/p{i:03d}.py" for i in range(300)]  # > the 256-pin metadata bound
    long_path = "src/" + "d" * 1_100 + ".py"         # > the 1_024-char path bound
    with pytest.raises(FixLoopInputError):
        InitialWorkerRenderContext("- pytest -q", tuple(pins))
    with pytest.raises(FixLoopInputError):
        FixWorkerRenderContext(2, (long_path,), None)

    initial_raw = render_initial_worker_input(
        task=_TASK, plan=_PLAN, verification_preview="- pytest -q", pinned_paths=(*pins, long_path),
    )
    assert initial_raw.count("\n- src/p") == 300
    assert long_path in initial_raw
    initial_request = InitialWorkerRequest(_TASK, initial_raw, _PLAN, None)
    assert initial_request.rendered_input == initial_raw  # opt-out bytes unchanged

    # An empty classification is representable, so it is preserved, not dropped.
    empty_classification = FixWorkerRenderContext(2, ("src/a.py",), "")
    assert empty_classification.classification == ""
    fix_raw = render_fix_worker_input(
        task=_TASK, plan=_PLAN, trigger=_followup_trigger(), attempt_index=1, max_fix_attempts=2,
        pinned_paths=tuple(pins), classification="",
    )
    assert fix_raw.count("\n- src/p") == 300
    assert "decision_classification: \n" in fix_raw
    fix_request = FixWorkerRequest(_TASK, _followup_trigger(), 1, fix_raw, plan=_PLAN, render_context=None)
    assert fix_request.rendered_input == fix_raw

    # Both prompts are valid; only the absent recipe stops a canonical bind.
    initial, task = _snapshots()
    _, helper = _safe_point(initial, task)
    workspace = _unit_workspace(initial.state.base_commit, tmp_path)
    for request in (initial_request, fix_request):
        with pytest.raises(SafePointError, match="worker attempt was not started"):
            helper.prepare(request, workspace)


def test_canonical_snapshot_survives_a_fully_saturated_prompt_budget(tmp_path):
    """Maximum legal pressure: 32k task + 8k follow-up feedback + an oversized
    rule file, so the rules block is clipped to the last remaining budget."""
    (tmp_path / "AGENTS.md").write_text("R" * 20_000, encoding="utf-8")
    initial, task = _snapshots()
    authoritative = Snapshot(initial.revision, replace(
        initial.state,
        context=build_context(
            goal="g" * 4_000,
            decisions=[f"d{n} " + "d" * 1_900 for n in range(64)],
            interfaces={},
        ),
    ))
    request = _fix_request(
        tmp_path, trigger=_followup_trigger("F" * 8_000), pins=_PINS, classification="code_bug",
        max_fix_attempts=2, task="T" * 32_000,
    )
    _, helper = _safe_point(initial, task, authoritative=authoritative)

    bound = helper.prepare(
        request, _unit_workspace(initial.state.base_commit, tmp_path),
    ).request

    canonical = render_snapshot_block(_shared(authoritative, task.id))
    assert len(bound.rendered_input) == MAX_FIX_INPUT_CHARS
    assert bound.rendered_input.count(canonical) == 1  # never clipped away silently
    assert bound.render_context == request.render_context
