"""Canonical rendering of validated collaboration context into worker input."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from collab_runtime.context import SharedSnapshot
from context_runtime import load_project_rules
from fix_runtime.models import (
    FixWorkerRenderContext,
    FixWorkerRequest,
    InitialWorkerRenderContext,
    InitialWorkerRequest,
)
from fix_runtime.prompt import render_fix_worker_input, render_initial_worker_input


def bind_worker_input(snapshot: SharedSnapshot, request: Any, workspace: Any) -> Any:
    """Re-render a request from its saved recipe plus this authoritative snapshot.

    Legacy requests intentionally fail closed: absent render metadata means
    there is no trustworthy way to reconstruct the exact original prompt.
    """
    if not isinstance(snapshot, SharedSnapshot):
        raise ValueError("A validated SharedSnapshot is required.")
    if type(request) is InitialWorkerRequest:
        recipe = request.render_context
        if not isinstance(recipe, InitialWorkerRenderContext):
            raise ValueError("Initial worker render metadata is required.")
        rules = load_project_rules(workspace.root, shared_snapshot=snapshot)
        rendered = render_initial_worker_input(
            task=request.task,
            plan=request.plan,
            verification_preview=recipe.verification_preview,
            pinned_paths=recipe.pinned_paths,
            rules=rules,
        )
    elif type(request) is FixWorkerRequest:
        recipe = request.render_context
        if not isinstance(recipe, FixWorkerRenderContext):
            raise ValueError("Fix worker render metadata is required.")
        rules = load_project_rules(workspace.root, shared_snapshot=snapshot)
        rendered = render_fix_worker_input(
            task=request.task,
            plan=request.plan,
            trigger=request.trigger,
            attempt_index=request.attempt_index,
            max_fix_attempts=recipe.max_fix_attempts,
            pinned_paths=recipe.pinned_paths,
            classification=recipe.classification,
            rules=rules,
        )
    else:
        raise ValueError("Unsupported worker request type.")
    return replace(request, rendered_input=rendered)
