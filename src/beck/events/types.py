"""The registry of event types: every event's payload is checked against its schema.

An event of an unknown type, or with a payload that does not match, never
enters the store. Types are added here as the state machines need them.
"""

from __future__ import annotations

from typing import Any

import jsonschema

_STRING = {"type": "string", "minLength": 1}
_TOKENS = {
    "type": "object",
    "required": ["input", "output"],
    "additionalProperties": False,
    "properties": {k: {"type": "integer", "minimum": 0}
                   for k in ("input", "output", "cache_read", "cache_creation")},
}


def _object(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "object", "required": sorted(required), "additionalProperties": False,
            "properties": {**required, **(optional or {})}}


SCHEMAS: dict[str, dict[str, Any]] = {
    # session lifecycle (phase 1, cross-cutting)
    "SessionStarted": _object({"project": _STRING}),
    "SessionAttached": _object({"from_seq": {"type": "integer", "minimum": 0}}),
    "SessionInterrupted": _object({}, {"reason": _STRING}),
    "BranchCreated": _object({"branch": _STRING}),
    "IterationClosed": _object({}),
    # agents (cross-cutting)
    # the turn as it happened, for a replay from the events alone: the agent's JSON as it answered (before the
    # protocol checks it), the hash of the definition it ran with (prompt, tools, examples), how long it took
    "AgentTurnEnded": _object({"agent": _STRING, "model": _STRING, "tokens": _TOKENS},
                              {"session": _STRING, "output": {}, "definition": _STRING,
                               "duration_ms": {"type": "integer", "minimum": 0}}),
    # what the turn changed in its folder, as a binary git patch (files added, changed, removed)
    "TurnChanged": _object({"agent": _STRING, "patch": _STRING}),
    "AgentTurnFailed": _object({"agent": _STRING, "command": _STRING, "error": _STRING, "signature": _STRING}),
    "BeckCrashed": _object({"where": {"enum": ["policy", "handler", "translator", "turn", "apply", "guard"]},
                            "name": _STRING,
                            "error": _STRING, "traceback": _STRING, "signature": _STRING},
                           {"input": {"enum": ["turn", "answer", "command", "start", "input"]}}),
    "ToolCalled": _object({"agent": _STRING, "tool": _STRING, "input": {"type": "object"}}),
    "SignalRejected": _object({"agent": _STRING, "errors": {"type": "array", "items": _STRING}},
                              {"command": _STRING, "signature": _STRING}),
    # the report as evidence (phases 4 and 5)
    "TestsRun": _object({
        "command": _STRING,
        "tests": {"type": "array", "items": _STRING},
        "exit_code": {"type": "integer"},
    }, {
        "log": _STRING,                          # the runner's console output
        "timed_out": {"type": "boolean"},        # stopped at the toolchain's timeout
        "coverage": _STRING,                     # the coverage report, when measured
        "coverage_format": _STRING,              # how to read it (beck.measures.coverage)
        "not_passed": {"type": "array", "items": _STRING},   # the report's tests that failed or erred
        "passed": {"type": "array", "items": _STRING},       # and those that passed: the flakiness detector's input
        "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        "report": _STRING,                       # the report's path; absent when the run wrote none
        "report_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},   # the working tree after the run
        "hunt": {"type": "boolean"},             # a run of the flakiness hunt: never itself evidence of flakiness
        "component": _STRING,
        "requirement": _STRING,
    }),
    "ChecksRun": _object({
        "paths": {"type": "array", "items": _STRING},
        "whole_project": {"type": "boolean"},
        "checks": {"type": "array", "items": _object({
            "name": _STRING, "exit_code": {"type": "integer"}, "log": _STRING,
            "findings": {"type": ["integer", "null"], "minimum": 0},   # null: the output could not be parsed
        })},
    }, {"tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"}, "component": _STRING}),
    # beck asked an agent for a turn, or the user for a decision: the facts the commands for the outside leave
    # (a command for beck is never stored); the request is the cause of what answers it
    "AgentTurnRequested": _object({
        "command": {"type": "string", "pattern": "^[A-Z][A-Za-z]+$"},
        "target": {"enum": ["kent", "unclebob", "martin", "reader"]},
        "args": {"type": "object"},
    }),
    "DecisionRequested": _object({
        "command": {"type": "string", "pattern": "^[A-Z][A-Za-z]+$"},
        "target": {"const": "user"},
        "args": {"type": "object"},
    }),
    # a requested turn started: the input the agent got, its folder and the folder's snapshot (a turn started
    # again after a crash first gets its folder back to the snapshot)
    "AgentTurnStarted": _object({"agent": _STRING, "command": _STRING, "envelope": {"type": "object"},
                                 "folder": _STRING},
                                {"snapshot": _STRING, "again": {"type": "boolean"}}),
    # the cycle (phase 4)
    "CycleOpened": _object({"component": _STRING, "requirement": _STRING, "unit": _STRING, "first": {"type": "boolean"}}),
    "RedDeclared": _object({"component": _STRING, "requirement": _STRING, "test": _STRING}),
    "RedVerified": _object({"component": _STRING, "requirement": _STRING, "test": _STRING, "run": _STRING},
                           {"already_green": {"type": "boolean"}}),
    "RedRejected": _object({"component": _STRING, "requirement": _STRING, "reason": _STRING, "signature": _STRING}),
    "RedCommitted": _object({"component": _STRING, "requirement": _STRING, "sha": _STRING}),
    "GreenDeclared": _object({"component": _STRING, "requirement": _STRING, "test": _STRING}),
    "GreenVerified": _object({"component": _STRING, "requirement": _STRING, "test": _STRING, "run": _STRING}),
    "GreenRejected": _object({"component": _STRING, "requirement": _STRING, "reason": _STRING, "signature": _STRING},
                             {"failure": _STRING}),
    "GreenCommitted": _object({"component": _STRING, "requirement": _STRING, "sha": _STRING}),
    "CycleClosed": _object({"component": _STRING, "requirement": _STRING}),
    "RegressionUnassigned": _object({"component": _STRING, "failure": _STRING}),
    "RedRewound": _object({"component": _STRING, "requirement": _STRING, "unit": _STRING, "reason": _STRING}),
    "CycleDropped": _object({"component": _STRING, "requirement": _STRING, "reason": _STRING}),
    "CycleBlocked": _object({"component": _STRING, "requirement": _STRING, "reason": _STRING}),
    "CycleReopened": _object({"component": _STRING, "requirement": _STRING, "unit": _STRING,
                              "phase": {"enum": ["red", "green"]}, "reason": _STRING}),
    # the component (phases 4 and 5) and the scheduler
    "ExecutionDerived": _object({"components": {"type": "array", "items": _object({
        "id": _STRING,
        "units": {"type": "array", "items": _object({"id": _STRING, "requirements": {"type": "array", "items": _STRING}})},
        "depends_on": {"type": "array", "items": _STRING},
        "implements": {"type": "array", "items": _STRING},
        "exclusive_with": {"type": "array", "items": _STRING},
    }, {"integration": {"type": "boolean"}, "waits_for": {"type": "array", "items": _STRING}})}}),
    "ComponentStarted": _object({"component": _STRING, "worktree": _STRING, "branch": _STRING, "base": _STRING}),
    "UnitOpened": _object({"component": _STRING, "unit": _STRING}),
    "UnitClosed": _object({"component": _STRING, "unit": _STRING}),
    "ComponentImplemented": _object({"component": _STRING}),
    "ComponentClosed": _object({"component": _STRING}),
    "ComponentParked": _object({"component": _STRING, "reason": _STRING, "because_of": _STRING}),
    "ComponentResumed": _object({"component": _STRING, "base": _STRING}),
    "RestartProposed": _object({"summary": _STRING, "changes": _STRING,
                                   "components": {"type": "array", "items": _STRING}}, {"subject": _STRING}),
    "RestartApproved": _object({}),
    "RestartRejected": _object({"instructions": _STRING}),
    "RestartApplied": _object({"base": _STRING, "components": {"type": "array", "items": _STRING}, "summary": _STRING}),
    # the component's refactor and landing (phase 5)
    "ComponentVerified": _object({"component": _STRING}),
    "RegressionFound": _object({"component": _STRING, "tests": {"type": "array", "items": _STRING}, "failure": _STRING}),
    # the flakiness detector (beck.machine.flaky)
    "FlakyTestFound": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING, "tree": _STRING}, {"component": _STRING}),
    "FlakinessUnsure": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING, "tree": _STRING}, {"component": _STRING}),
    "FlakinessCleared": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING, "tree": _STRING},
                                {"component": _STRING}),
    "FlakinessUnresolved": _object({"test": _STRING, "evidence": _STRING}),
    "SuiteRunsApproved": _object({}),
    "SuiteRunsRefused": _object({}, {"instructions": _STRING}),
    # the coverage (phase 5): measured, the gaps tried once, never a stop
    "CoverageMeasured": _object({"files": {"type": "object"}, "gaps": {"type": "object"}},
                                {"component": _STRING, "before": {"type": ["number", "null"]},
                                 "after": {"type": ["number", "null"]}}),
    "CoverageDecreased": _object({"component": _STRING, "before": {"type": "number"}, "after": {"type": "number"}},
                                 {"requirements": {"type": "object"}}),
    "CoverageUnwired": _object({"language": _STRING}),
    "CoverageWired": _object({"tool": _STRING, "summary": _STRING}),
    "DependenciesInstalled": _object({}),
    "CoverageGapsFound": _object({"component": _STRING, "requirements": {"type": "object"}}),
    "CoverageTestsDeclared": _object({"component": _STRING, "tests": {"type": "array"}}),
    "CoverageTestsVerified": _object({"component": _STRING, "tests": {"type": "array"}}),
    "CoverageRejected": _object({"component": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "CoverageTestsCommitted": _object({"component": _STRING, "sha": _STRING}),
    "CoverageGivenUp": _object({"component": _STRING, "reason": _STRING}),
    "ComponentSquashed": _object({"component": _STRING, "sha": _STRING, "subject": _STRING}),
    "ComponentMeasured": _object({"component": _STRING, "measures": {"type": "object"}}),
    "ComponentReviewed": _object({"component": _STRING, "findings": {"type": "array"}, "weak_tests": {"type": "array"}}),
    "RefactorDeclared": _object({"component": _STRING, "phase": {"enum": ["tests", "code", "strengthen", "none"]},
                                 "addressed": {"type": "array", "items": _STRING}}, {"why": _STRING}),
    "RefactorVerified": _object({"component": _STRING, "addressed": {"type": "array", "items": _STRING},
                                 "measures": {"type": "object"}}),
    "RefactorRejected": _object({"component": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "RefactorBlocked": _object({"component": _STRING, "reason": _STRING}),
    "MeasureWorsened": _object({"component": _STRING, "worsened": {"type": "array", "items": _STRING},
                                "addressed": {"type": "array", "items": _STRING}, "measures": {"type": "object"}}),
    "TradeoffAccepted": _object({}),
    "TradeoffRejected": _object({}, {"instructions": _STRING}),
    "RefactorCommitted": _object({"component": _STRING, "sha": _STRING, "addressed": {"type": "array", "items": _STRING}}),
    "ChecksPassed": _object({"component": _STRING}),
    "ChecksFailed": _object({"component": _STRING, "findings": {"type": "array", "items": _STRING},
                             "signature": _STRING}),
    "ComponentReady": _object({"component": _STRING, "sha": _STRING}),
    "LandingDeferred": _object({"component": _STRING, "reason": _STRING}),
    "LandingConflicted": _object({"component": _STRING, "files": {"type": "array", "items": _STRING},
                                  "signature": _STRING}),
    "ConflictResolved": _object({"component": _STRING}),
    "ConflictRejected": _object({"component": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "ComponentLanded": _object({"component": _STRING, "sha": _STRING}),
    # integration and closing (phase 6)
    "ComponentReopened": _object({"component": _STRING, "worktree": _STRING, "branch": _STRING, "base": _STRING,
                             "reason": _STRING}),
    "SuiteGreen": _object({}),
    "SuiteFailed": _object({"tests": {"type": "array", "items": _STRING}, "failure": _STRING}),
    "StaticChecksPassed": _object({}),
    "StaticChecksFailed": _object({"findings": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "BranchFixesDeclared": _object({}),
    "BranchFixesCommitted": _object({"fixups": {"type": "array", "items": _STRING}}),
    "BranchSquashed": _object({"sha": _STRING, "then": {"enum": ["review", "close"]}}),
    "CorrectionsRequested": _object({"text": _STRING}),
    "ReviewAccepted": _object({}),
    "CorrectionsPlanned": _object({"plan": {"type": "array"}}),
    # session, survey and model (phases 1 to 3)
    "ConfigLoaded": _object({"config": {"type": "object"}}),
    "ConfigInvalid": _object({"errors": _STRING}),
    "ProjectClassified": _object({"kind": {"enum": ["greenfield", "brownfield", "undetermined"]}}),
    "InterviewAnswered": _object({"key": _STRING, "question": _STRING, "answer": _STRING}),
    "FollowUpsProposed": _object({"questions": {"type": "array"}}),
    "InterviewCompleted": _object({}),
    "ProjectScaffolded": _object({"language": _STRING}),
    "ScaffoldFailed": _object({"failure": _STRING}),
    "BranchNameProposed": _object({"name": _STRING}),
    "BranchNamed": _object({"name": _STRING}),
    "ToolchainLoaded": _object({"language": _STRING}),
    "ToolchainDetected": _object({"language": _STRING}),
    "ToolchainConfirmed": _object({"language": _STRING}),
    "EnvironmentUp": _object({}, {"steps": {"type": "array"}}),
    "EnvironmentDown": _object({"failure": _STRING}),
    "SuiteVerified": _object({"tests": {"type": "integer", "minimum": 0}}),
    "SuiteBroken": _object({"failure": _STRING}),
    "IndexReconciled": _object({}),
    "DslDriftDetected": _object({"drift": {"type": "array"}}),
    "DslSyncProposed": _object({"summary": _STRING}),
    "DslSyncApproved": _object({}),
    "DslSyncRejected": _object({"instructions": _STRING}),
    "IndexRebuilt": _object({}),
    "CodeAreaDelimited": _object({"paths": {"type": "array", "items": _STRING}}),
    "ShoppingListWritten": _object({"file": _STRING}),
    "ShoppingListApproved": _object({}),
    "ShoppingListRevised": _object({"instructions": _STRING}),
    "DomainWritten": _object({}),
    "DomainValidated": _object({}),
    "DomainInvalid": _object({"errors": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "DomainApproved": _object({}),
    "DomainRevised": _object({"instructions": _STRING}),
    "ArchitectureWritten": _object({}),
    "ArchitectureValidated": _object({}),
    "ArchitectureInvalid": _object({"errors": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "InterfacesWritten": _object({"files": {"type": "array", "items": _STRING}}),
    "InterfacesCompiled": _object({}),
    "InterfacesBroken": _object({"errors": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "FitnessFunctionsGenerated": _object({"untranslated": {"type": "array"}, "files": {"type": "array"}}),
    "FitnessFunctionsWritten": _object({}),
    "FitnessFunctionsPassed": _object({}),
    "FitnessFunctionsFailed": _object({"errors": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "ArchitectureApproved": _object({}),
    "ArchitectureRevised": _object({"instructions": _STRING}),
    "IndexBuilt": _object({"unanchored": {"type": "array", "items": _STRING},
                           "unknown": {"type": "array", "items": _STRING}}),
    "IdsAnchored": _object({}),
    "ModelCommitted": _object({"sha": _STRING}),
    "ModelBlocked": _object({"step": _STRING, "reason": _STRING}),
    # the user, through beck
    "QuestionAsked": _object({"agent": _STRING, "command": _STRING, "question": {"type": "object"}}),
    "QuestionAnswered": _object({"text": _STRING}),
    # beck's own question, routed by kent to an action of its menu
    "DecisionRouted": _object({"situation": _STRING, "action": _STRING, "args": {"type": "object"}, "why": _STRING,
                               "ask": _STRING}),
    "RouteRejected": _object({"reason": _STRING, "signature": _STRING}),
    "ChangeRequested": _object({"agent": _STRING, "component": _STRING, "kind": {"enum": ["interface", "architecture", "test"]},
                                "target": _STRING, "problem": _STRING, "proposal": _STRING},
                               {"requirement": _STRING}),
}

_VALIDATORS = {name: jsonschema.Draft202012Validator(schema) for name, schema in SCHEMAS.items()}


class InvalidEvent(ValueError):
    pass


def check(type_: str, payload: Any) -> None:
    validator = _VALIDATORS.get(type_)
    if validator is None:
        raise InvalidEvent(f"unknown event type {type_!r}")
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(map(str, e.path)))
    if errors:
        raise InvalidEvent(f"{type_}: " + "; ".join(
            f"{'.'.join(map(str, e.path)) or '<payload>'}: {e.message}" for e in errors))
