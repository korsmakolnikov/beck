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
    "AgentTurnEnded": _object({"agent": _STRING, "model": _STRING, "tokens": _TOKENS},
                              {"session": _STRING}),
    "AgentTurnFailed": _object({"agent": _STRING, "command": _STRING, "error": _STRING, "signature": _STRING}),
    "ViolationLogged": _object({"kind": _STRING, "agent": _STRING, "outbox": _STRING}),
    "ViolationSettled": _object({"outbox": _STRING, "how": _STRING, "reason": _STRING},
                                {"files": {"type": "array", "items": _STRING}, "pr": _STRING}),
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
        "coverage": _STRING,                     # the coverage report, when measured
        "coverage_format": _STRING,              # how to read it (beck.measures.coverage)
        "not_passed": {"type": "array", "items": _STRING},   # the report's tests that failed or erred
        "passed": {"type": "array", "items": _STRING},       # and those that passed: the flakiness detector's input
        "counts": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        "report": _STRING,                       # the report's path; absent when the run wrote none
        "report_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},   # the working tree after the run
        "unit": _STRING,
        "clause": _STRING,
    }),
    "ChecksRun": _object({
        "paths": {"type": "array", "items": _STRING},
        "whole_project": {"type": "boolean"},
        "checks": {"type": "array", "items": _object({
            "name": _STRING, "exit_code": {"type": "integer"}, "log": _STRING,
            "findings": {"type": ["integer", "null"], "minimum": 0},   # null: the output could not be parsed
        })},
    }, {"tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"}, "unit": _STRING}),
    # the machines' commands: every policy's decision, the anchor of idempotence
    "CommandIssued": _object({
        "command": {"type": "string", "pattern": "^[A-Z][A-Za-z]+$"},
        "target": {"enum": ["beck", "kent", "unclebob", "martin", "reader", "user"]},
        "args": {"type": "object"},
    }),
    "CommandDone": _object({"command": {"type": "string", "pattern": "^[A-Z][A-Za-z]+$"}}),
    # the cycle (phase 4)
    "CycleOpened": _object({"unit": _STRING, "clause": _STRING, "group": _STRING, "first": {"type": "boolean"}}),
    "RedDeclared": _object({"unit": _STRING, "clause": _STRING, "test": _STRING}),
    "RedVerified": _object({"unit": _STRING, "clause": _STRING, "test": _STRING, "run": _STRING}),
    "RedRejected": _object({"unit": _STRING, "clause": _STRING, "reason": _STRING, "signature": _STRING}),
    "RedCommitted": _object({"unit": _STRING, "clause": _STRING, "sha": _STRING}),
    "GreenDeclared": _object({"unit": _STRING, "clause": _STRING, "test": _STRING}),
    "GreenVerified": _object({"unit": _STRING, "clause": _STRING, "test": _STRING, "run": _STRING}),
    "GreenRejected": _object({"unit": _STRING, "clause": _STRING, "reason": _STRING, "signature": _STRING},
                             {"failure": _STRING}),
    "GreenCommitted": _object({"unit": _STRING, "clause": _STRING, "sha": _STRING}),
    "CycleClosed": _object({"unit": _STRING, "clause": _STRING}),
    "CycleBlocked": _object({"unit": _STRING, "clause": _STRING, "reason": _STRING}),
    "CycleReopened": _object({"unit": _STRING, "clause": _STRING, "group": _STRING,
                              "phase": {"enum": ["red", "green"]}, "reason": _STRING}),
    # the unit (phases 4 and 5) and the scheduler
    "UnitsPlanned": _object({"units": {"type": "array", "items": _object({
        "id": _STRING,
        "groups": {"type": "array", "items": _object({"id": _STRING, "clauses": {"type": "array", "items": _STRING}})},
        "depends_on": {"type": "array", "items": _STRING},
        "realizes": {"type": "array", "items": _STRING},
        "exclusive_with": {"type": "array", "items": _STRING},
    }, {"integration": {"type": "boolean"}})}}),
    "UnitStarted": _object({"unit": _STRING, "worktree": _STRING, "branch": _STRING, "base": _STRING}),
    "GroupOpened": _object({"unit": _STRING, "group": _STRING}),
    "GroupClosed": _object({"unit": _STRING, "group": _STRING}),
    "UnitImplemented": _object({"unit": _STRING}),
    "UnitClosed": _object({"unit": _STRING}),
    "UnitParked": _object({"unit": _STRING, "reason": _STRING, "because_of": _STRING}),
    "UnitResumed": _object({"unit": _STRING, "base": _STRING}),
    "ResolutionProposed": _object({"summary": _STRING, "changes": _STRING,
                                   "units": {"type": "array", "items": _STRING}}),
    "ResolutionApproved": _object({}),
    "ResolutionRejected": _object({"instructions": _STRING}),
    "ResolutionApplied": _object({"base": _STRING, "units": {"type": "array", "items": _STRING}, "summary": _STRING}),
    # the unit's refactor and landing (phase 5)
    "UnitVerified": _object({"unit": _STRING}),
    "RegressionFound": _object({"unit": _STRING, "tests": {"type": "array", "items": _STRING}, "failure": _STRING}),
    # the flakiness detector (beck.machine.flaky)
    "FlakyTestFound": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING}, {"unit": _STRING}),
    "FlakinessUnsure": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING}, {"unit": _STRING}),
    "FlakinessCleared": _object({"test": _STRING, "stage": _STRING, "evidence": _STRING}, {"unit": _STRING}),
    "FlakinessUnresolved": _object({"test": _STRING, "evidence": _STRING}),
    "SuiteRunsApproved": _object({}),
    "SuiteRunsRefused": _object({}, {"instructions": _STRING}),
    # the coverage (phase 5): measured, the gaps tried once, never a stop
    "CoverageMeasured": _object({"files": {"type": "object"}, "gaps": {"type": "object"}},
                                {"unit": _STRING, "before": {"type": ["number", "null"]},
                                 "after": {"type": ["number", "null"]}}),
    "CoverageDecreased": _object({"unit": _STRING, "before": {"type": "number"}, "after": {"type": "number"}},
                                 {"clauses": {"type": "object"}}),
    "CoverageUnwired": _object({"language": _STRING}),
    "CoverageWired": _object({"tool": _STRING, "summary": _STRING}),
    "DependenciesInstalled": _object({}),
    "CoverageGapsFound": _object({"unit": _STRING, "clauses": {"type": "object"}}),
    "CoverageTestsDeclared": _object({"unit": _STRING, "tests": {"type": "array"}}),
    "CoverageTestsVerified": _object({"unit": _STRING, "tests": {"type": "array"}}),
    "CoverageRejected": _object({"unit": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "CoverageTestsCommitted": _object({"unit": _STRING, "sha": _STRING}),
    "CoverageGivenUp": _object({"unit": _STRING, "reason": _STRING}),
    "UnitSquashed": _object({"unit": _STRING, "sha": _STRING, "subject": _STRING}),
    "UnitMeasured": _object({"unit": _STRING, "measures": {"type": "object"}}),
    "UnitReviewed": _object({"unit": _STRING, "findings": {"type": "array"}, "weak_tests": {"type": "array"}}),
    "RefactorDeclared": _object({"unit": _STRING, "phase": {"enum": ["tests", "code", "strengthen"]},
                                 "addressed": {"type": "array", "items": _STRING}}),
    "RefactorVerified": _object({"unit": _STRING, "addressed": {"type": "array", "items": _STRING},
                                 "measures": {"type": "object"}}),
    "RefactorRejected": _object({"unit": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "RefactorBlocked": _object({"unit": _STRING, "reason": _STRING}),
    "MeasureWorsened": _object({"unit": _STRING, "worsened": {"type": "array", "items": _STRING},
                                "addressed": {"type": "array", "items": _STRING}, "measures": {"type": "object"}}),
    "TradeoffAccepted": _object({}),
    "TradeoffRejected": _object({}, {"instructions": _STRING}),
    "RefactorCommitted": _object({"unit": _STRING, "sha": _STRING, "addressed": {"type": "array", "items": _STRING}}),
    "ChecksPassed": _object({"unit": _STRING}),
    "ChecksFailed": _object({"unit": _STRING, "findings": {"type": "array", "items": _STRING},
                             "signature": _STRING}),
    "UnitReady": _object({"unit": _STRING, "sha": _STRING}),
    "LandingDeferred": _object({"unit": _STRING, "reason": _STRING}),
    "LandingConflicted": _object({"unit": _STRING, "files": {"type": "array", "items": _STRING},
                                  "signature": _STRING}),
    "ConflictResolved": _object({"unit": _STRING}),
    "ConflictRejected": _object({"unit": _STRING, "reason": _STRING, "signature": _STRING}, {"failure": _STRING}),
    "UnitLanded": _object({"unit": _STRING, "sha": _STRING}),
    # integration and closing (phase 6)
    "UnitReopened": _object({"unit": _STRING, "worktree": _STRING, "branch": _STRING, "base": _STRING,
                             "reason": _STRING}),
    "SuiteGreen": _object({}),
    "SuiteFailed": _object({"tests": {"type": "array", "items": _STRING}, "failure": _STRING}),
    "StaticChecksPassed": _object({}),
    "StaticChecksFailed": _object({"findings": {"type": "array", "items": _STRING}, "signature": _STRING}),
    "BranchFixesDeclared": _object({}),
    "BranchFixesCommitted": _object({"fixups": {"type": "array", "items": _STRING}}),
    "BranchSquashed": _object({"sha": _STRING}),
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
    "ChangeRequested": _object({"agent": _STRING, "unit": _STRING, "kind": {"enum": ["interface", "architecture"]},
                                "target": _STRING, "problem": _STRING, "proposal": _STRING},
                               {"clause": _STRING}),
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
