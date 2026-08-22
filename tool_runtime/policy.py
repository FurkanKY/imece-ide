"""Explicit action/resource permission policy evaluation."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Sequence

from tool_runtime.errors import ToolPolicyError
from tool_runtime.models import PermissionEffect, PermissionRequest


@dataclass(frozen=True, slots=True)
class PermissionRule:
    action_pattern: str
    resource_pattern: str
    effect: PermissionEffect

    def __post_init__(self) -> None:
        if not isinstance(self.action_pattern, str) or not self.action_pattern:
            raise ToolPolicyError("PermissionRule.action_pattern boş olmayan bir string olmalı.")
        if not isinstance(self.resource_pattern, str) or not self.resource_pattern:
            raise ToolPolicyError("PermissionRule.resource_pattern boş olmayan bir string olmalı.")
        if not isinstance(self.effect, PermissionEffect):
            raise ToolPolicyError("PermissionRule.effect PermissionEffect olmalı.")

    def matches(self, request: PermissionRequest) -> bool:
        return fnmatch.fnmatchcase(request.action, self.action_pattern) and fnmatch.fnmatchcase(
            request.resource, self.resource_pattern
        )


@dataclass(frozen=True, slots=True)
class PermissionEvaluation:
    request: PermissionRequest
    effect: PermissionEffect
    matched_rules: tuple[PermissionRule, ...]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PermissionEffect
    evaluations: tuple[PermissionEvaluation, ...]


class PolicyEvaluator:
    def __init__(
        self,
        rules: Sequence[PermissionRule] = (),
        *,
        default_effect: PermissionEffect = PermissionEffect.ASK,
    ) -> None:
        if not isinstance(default_effect, PermissionEffect):
            raise ToolPolicyError("PolicyEvaluator.default_effect PermissionEffect olmalı.")
        self._rules = tuple(rules)
        if any(not isinstance(rule, PermissionRule) for rule in self._rules):
            raise ToolPolicyError("PolicyEvaluator.rules yalnızca PermissionRule içerebilir.")
        self.default_effect = default_effect

    def evaluate_one(self, request: PermissionRequest) -> PermissionEvaluation:
        if not isinstance(request, PermissionRequest):
            raise ToolPolicyError("Policy yalnızca PermissionRequest değerlendirebilir.")
        matches = tuple(rule for rule in self._rules if rule.matches(request))
        effects = {rule.effect for rule in matches}
        if PermissionEffect.DENY in effects:
            effect = PermissionEffect.DENY
        elif PermissionEffect.ASK in effects:
            effect = PermissionEffect.ASK
        elif PermissionEffect.ALLOW in effects:
            effect = PermissionEffect.ALLOW
        else:
            effect = self.default_effect
        return PermissionEvaluation(request=request, effect=effect, matched_rules=matches)

    def evaluate(self, requests: Sequence[PermissionRequest]) -> PolicyDecision:
        requests = tuple(requests)
        if not requests:
            raise ToolPolicyError("En az bir PermissionRequest gerekli.")
        evaluations = tuple(self.evaluate_one(request) for request in requests)
        effects = {evaluation.effect for evaluation in evaluations}
        if PermissionEffect.DENY in effects:
            effect = PermissionEffect.DENY
        elif PermissionEffect.ASK in effects:
            effect = PermissionEffect.ASK
        else:
            effect = PermissionEffect.ALLOW
        return PolicyDecision(effect=effect, evaluations=evaluations)
