# The agents' protocol

Status: **approved** (2026-11-13). Every turn of an agent is a JSON document in
and a JSON document out, both validated by a schema
([`src/beck/agents/schemas/`](../src/beck/agents/schemas/)): an input that
fails is beck's bug and never leaves; an output that fails is the agent's
and becomes `SignalRejected`, with the errors, and the turn is retried
under the repetition rule. One example per command, valid against the
schemas, is in [`src/beck/agents/examples.yaml`](../src/beck/agents/examples.yaml).

## The envelope

```json
{"protocol": 1, "agent": "unclebob", "command": "WriteRedTest",
 "cause": {"event_id": "…", "correlation_id": "…"},
 "context": { … the command's own … },
 "retry": {"attempt": 2, "reason": "the test passes already"}}
```

```json
{"protocol": 1, "command": "WriteRedTest", "outcome": "done",
 "result": { … the command's own … }}
```

* **JSON only, both ways**, also when the user is involved: beck wraps the
  user's answer into the next input of the session that asked (`answer`,
  with the question; the context is not sent again). A translation layer
  between the user and the agents (deterministic, with a small model as
  fallback) may decorate it later.
* `cause` is the event the turn answers. beck puts its id in the agent's
  environment too (`BECK_CAUSE`), so every `beck test` and `beck check` the
  agent runs joins the same chain.
* `outcome` is one of `done`, `question` (with `question`: only kent asks
  the user; beck relays the question and the answer) and `change_requested`
  (with `change`: an interface or architecture change the agent cannot
  make). Each command allows only some of them.
* `retry` is present when the previous attempt was rejected, with the
  verifier's reason (`RedRejected`, `GreenRejected`, a schema error).
* **What is not in the output**: anything beck can find out itself. An
  agent never reports the files it changed (git knows), whether the tests
  passed (the `TestsRun` knows), or a narrative of its work. The output
  carries only the decisions beck cannot derive: which test proves a
  clause, which findings a refactor addresses, what to ask the user.
* `run` and `check` in a context are the exact commands to run the tests
  and the checks of the step; the agent uses them as they are.

## Sessions

| Agent | Session | First turn | Later turns |
|---|---|---|---|
| kent | one per state of the model (fresh context each) and per proposal | the state's context | the answers to its questions |
| unclebob | one per group | the group's context (`beck index context`) | only the clause of the next cycle |
| martin | one per unit, from `ReviewUnit` to the landing | the unit | the next command |
| reader | one per summary | the log | none |
| argyris | its own, interactive, with the user (`beck audit --wake`) | the outbox's pending records | none: the user talks to it; the answer goes to `beck audit --settle` |

## The commands, and the policy that sends each

| Agent | Command | Sent by (event storming) | Outcomes |
|---|---|---|---|
| kent | `AskFollowUps` | `ProjectClassified` (phase 1) | done |
| kent | `ProposeBranchName` | `InterviewCompleted` (1) | done, question |
| kent | `BringEnvironmentUp` | the deterministic start failed (2) | done, question |
| kent | `DelimitCodeArea` | brownfield (2) | done, question |
| kent | `WriteShoppingList` | (2) | done, question |
| kent | `ProposeDslSync` | `DslDriftDetected` (2) | done, question |
| kent | `WriteDomain` | `ShoppingListApproved`, `DomainInvalid`, `DomainRevised` (3) | done, question |
| kent | `WriteArchitecture` | `DomainApproved`, `ArchitectureInvalid`, `ArchitectureRevised` (3) | done, question |
| kent | `WriteInterfaces` | `ArchitectureValidated`, `InterfacesBroken`, `FitnessFunctionsFailed` (3) | done, question |
| kent | `WriteFitnessFunctions` | untranslated rules (3) | done |
| kent | `AnchorIds` | unanchored operations (3) | done |
| kent | `VerifyIndex` | `IndexMarkedDirty` (cross-cutting) | done |
| kent | `ProposeResolution` (makes the changes in the feature branch's tree, uncommitted) | `DependentsParked`, `ResolutionRejected` (4) | done, question |
| kent | `PlanCorrections` | `CorrectionsRequested` (6) | done, question |
| kent | `WireCoverage` (finds or adds the coverage tool, wires the toolchain) | `CoverageUnwired`, once (2) | done, question |
| kent | `RouteDecision` (the user's words to one action of beck's menu) | `QuestionAnswered` of beck's own question (2, 6, any failing turn) | done, question |
| unclebob | `WriteRedTest` | `GroupOpened`, `CycleClosed`, `RedRejected` (4) | done, change_requested |
| unclebob | `WriteGreenCode` | `RedCommitted`, `FailureSummarised` (4) | done, change_requested |
| unclebob | `CoverLines` (tests that run the lines a clause names, passing at once) | `CoverageGapsFound`, once per unit (5) | done, change_requested |
| martin | `ReviewUnit` | `UnitMeasured` (5) | done, change_requested |
| martin | `RefactorUnit` (findings, or weak tests to strengthen) | `UnitReviewed`, `RefactorCommitted`, `RefactorRejected` (5) | done, change_requested |
| martin | `ResolveConflict` | `LandingConflicted` (5) | done, change_requested |
| martin | `FixStaticFindings` | `StaticChecksFailed` (6), and a unit's `ChecksRun` (5) | done, change_requested |
| reader | `SummariseOutput` | a `TestsRun` or `ChecksRun` beck could not read | done |
| argyris | `ReviewViolations` (each with the files of the rule it breaks) | the outbox's reader (`beck audit --wake`), not a policy: argyris needs the user | done |

## Decided in review (2026-11-13)

* **beck runs a step's tests itself when a policy asks for them** (the
  unit's tests, the full suite): beck is deterministic and does nothing of
  its own accord, so a turn of martin only to run a command is waste.
  unclebob still runs its cycle's test, through `beck test`.
* **A weak test is martin's to fix** (`RefactorUnit` with `weak_tests`,
  phase `strengthen`), under the three laws of TDD: the test first; if it
  turns red, then the code. No state machine for it; smart_audit checks the
  order from the tool calls.
* **The user's answer** reaches kent wrapped by beck, as above.
