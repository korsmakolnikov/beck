# beck

[![CI](https://github.com/korsmakolnikov/beck/actions/workflows/ci.yml/badge.svg)](https://github.com/korsmakolnikov/beck/actions/workflows/ci.yml)

Status: **implemented, not yet tried on a real project**. Every phase runs
end to end in the tests, with scripted agents on real repositories and real
test runs; the Claude Code driver was tried on one real turn. These notes
record what was discussed and approved; the code is in `src/beck/`, the
tests in `tests/` (`make check`). beck replaces the workflow of
[minime](https://github.com/korsmakolnikov/minime) and depends on nothing
of it.

beck is an application that replaces the `kent` agent of minime and
redesigns its architecture-first workflow around it. The workflow as agents
costs too many tokens (in minime's measures: unclebob 70.8 %, joshua
10.7 %, kent 6.4 %), is slow, and needs the user at every component. Once beck
is stable, beck rewrites beck: a released version builds the next, as a
compiler bootstraps itself.

## Development

```
make deps        the system packages (Python 3.12+, uv, git, make; Elixir), and whether claude and gh are there
make sync        beck's environment (uv sync)
make install     the beck command on the PATH, editable (uv tool install --editable)
make check       ruff, mypy, pytest
make build       the wheel and the sdist
```

beck needs Claude Code (`claude`) to run its agents and the GitHub CLI
(`gh`) for argyris's pull requests.

## The goal

**A machine that programs by itself, with the human in the pipeline as its
controller.** The human becomes the software's architect: they define the
domain, the architecture, the guidelines and the method, and they approve
what beck cannot decide. beck is the engineer that implements: it writes
the code test-first through its agents, checks every step itself, corrects
itself, and improves its own agents through argyris. Every rule below is
weighed against this goal: a rule that stops the machine for a detail a
controller would not look at (one line no test runs) is a wrong rule; a
finding the controller should see goes in front of them at the review,
without stopping the work.

## Principles

* **Deterministic first, agent as fallback.** Every decision goes through a
  chain of responsibility: a deterministic handler answers when it can; when
  it cannot, the event passes to an agent (often a small one, on Haiku).
  beck is an agentic application made of several state machines, not a
  script, and not a single LLM orchestrator.
* **Structured input and output.** Every agent receives JSON and answers
  JSON, each with a schema, also when the user is in the loop (beck wraps
  the user's words; a translation layer may come later), so that beck can check as much as possible deterministically
  (a repeated failure pattern, a declared scope, a signal); an output that
  does not match its schema is rejected (`SignalRejected`).
* **The user's words become the flow's actions through kent.** When beck
  asks the user about a situation its rules cannot settle (a suite failing
  outside the components, checks that keep failing, an agent's turn that keeps
  failing, an environment that does not come up), the question carries a
  closed menu of the actions valid there; kent turns the user's answer into
  one of them (`RouteDecision`, checked against the menu), and the machine
  that asked acts on it.
* **Event storming before design.** The domain of the workflow is modelled
  from its events first; the actors and their boundaries come after, and the
  old agents' boundaries are not sacred.
* **Agents never commit.** beck commits, after checking each TDD phase
  deterministically.
* **Agents never search the code twice.** The index maps every DSL id to the
  code and the tests; beck hands each agent only the slice it needs.
* **The user is the architect at the start and the reviewer at the end**,
  not a reviewer of every component: the workflow can run overnight, and on
  holidays. What it cannot solve alone it parks, with a proposal, and goes on
  with the rest.
* **Event sourcing.** Everything that happens is an event; state, resume,
  policies, audits, metrics and replays all read the events.
* **KISS.** No layer, abstraction or option without a concrete need today.

## Configuration

* `~/.config/beck/config.yml`: global defaults. Anything tied to a work
  environment lives only here, never in this repository.
* `.beck/config.yml` in the project: extends and overrides the global one.
* minime's `~/.config/minime/*.toml` stays with minime's workflow; beck
  does not read it.
* Settings so far:
  * whether `.beck/` is versioned (replaces the old rule "versioned on
    Claude Code, never on Copilot", which no longer holds once Claude Code
    runs on company code);
  * the branch name format (when it contains a task code, the user gives it);
  * the model of each actor;
  * the token budget, per agent session and per iteration (infinite allowed:
    first we measure);
  * the limits of a cycle (see "Blocked cycles"): 3 repetitions of a
    pattern, 30 attempts;
  * the CLI driver (Claude Code, Copilot);
  * the components in parallel: 4 by default.

## Project layout: `.beck/`

Everything beck needs lives in `.beck/`; minime's `.sources/`,
`.documentation/`, `.adl/` and `.shopping_lists/` are ignored.

* `config.yml`
* shopping lists, one per branch
* the DSLs: `domains/<domain>.yaml` (a spec per domain) and `architecture.yaml`; no
  components map (see [`docs/dsl.md`](docs/dsl.md))
* `toolchain.yml`: how to run the tests (`src/beck/toolchain.py`)
* `reports/`: the JUnit reports of the test runs (never versioned)
* `index.db`: the DSL-to-code index (derived, never versioned)
* the event stores (never versioned)

When versioned, beck commits `.beck/` itself (`docs(workflow): …`), only
between components; no agent is needed for it.

## Actors (draft)

| Actor | What | Notes |
|---|---|---|
| beck | the state machines, the policies, the commits, the deterministic checks, the TUI | no LLM |
| kent (inside beck) | studies the existing code and delimits the area the iteration touches; brings the environment up from the project's docs (`AGENTS.md`, README, Makefile); writes the shopping list; is the DSL author: the domain, then the architecture and the interfaces (hughes and ford merged); anchors the spec ids to the code; keeps the DSLs in sync with the code; proposes the restart of a blocked cycle or of an interface change | name kept from Kent Beck; never runs together with the old `kent` agent; the domain and the architecture are two states of the machine, each with a fresh context and its own approval; in brownfield it reads the code, only the slice of the delimited area the index hands it |
| unclebob | TDD, one unit of spec ids per session, context cut by the index | N in parallel, one component each, in its own worktree; may request an interface change |
| martin | reviews the component, including whether each test verifies the requirement it references; refactors it on green tests and closes it on its own; strengthens the weak tests itself, under the three laws of TDD; resolves the conflicts when its component lands | runs in parallel with the unclebobs; no new tests (temporary benchmarks only); outside the component only code covered by tests; missing tests send the work back to unclebob; weak tests are flagged for argyris; may request an interface change, never makes one; the refactor (blue) phase may move back into unclebob once the graph has cut unclebob's consumption |
| smart_audit | (implemented, `src/beck/machine/audit.py`) the observer for argyris, deterministic: a projector triggered by given events reads the event store, looks for patterns of violations (`SignalRejected`, a forbidden tool, a violated scope, an interruption, tests run that are not the cycle's, how the parallelism stalls) and writes each to an outbox table | replaces joshua, whose name is dropped; a small agent comes back inside it as a fallback only when the projector's complexity explodes, or when a problem never reached argyris (the audit failed) |
| argyris | improves beck's agents: one of beck's own agents (`src/beck/agents/prompts/argyris.md`), not an installed file; the outbox's reader (`src/beck/argyris.py`, `beck audit --wake`) opens its session, in beck's source, with a structured message, a `ReviewViolations` of the pending records, each with the files of the rule it breaks; argyris settles them with the user and proposes each change as a **pull request to beck** (`beck contrib start`, `diff`, `submit`: a worktree per change, then commit, push and `gh pr create`); its JSON answer, through `beck audit --settle`, marks the records handled and records a `ViolationSettled` with the pull request, caused by the event each is about; runs replays and A/B tests of models; with smart_audit, tunes the parallelism | |
| test reader | on Haiku: summarises a failure for unclebob | only when the deterministic parser is not enough |
| environment checker | is the environment up? deterministic from the container status, Haiku as fallback on long logs | |

Removed: **hughes** and **ford** (merged into kent), **simon** (its output
was never used; may return on demand), **marvin** (`AGENTS.md` updates
proved harmful; later, `AGENTS.md` will import the DSLs), **joshua**
(replaced by smart_audit), **the old kent** (after fade-out).

## DSLs

The manifesto, the formats and the graph built from them are in
[`docs/dsl.md`](docs/dsl.md); the YAML schemas that validate them in
[`src/beck/dsl/schemas/`](src/beck/dsl/schemas/). The DSLs change freely until they fit beck.

* **ADL dropped**, replaced by a YAML architecture DSL in the style of the
  domain spec, keeping ADL's features (the kinds of `DEFINE`, `ASSERT`,
  `FOREACH`), with an id on every element and every assertion.
* **No components map**: which component implements each spec id is written
  in the architecture YAML (a design decision, so it lives in a versioned
  source); the index derives the rest down to the code.
* The domain and the architecture are two parallel branches of the model,
  each with its own approval; in practice they run in sequence (the
  architecture is derived from the domain, and the user, who reads and
  discusses both, is the bottleneck).
* **martin follows the architecture to the letter**: it never moves or
  splits a module, across components or inside one; a change of structure
  is an architecture change. How to automate these decisions comes later.
* The fitness functions are generated deterministically from the
  assertions; an agent writes only those the generator cannot translate.
* Future stages, not in the first iteration: Alloy and TLA+ where they make
  sense.

## Index (SQLite, embedded)

Implemented: `beck index build | plan | context` (`src/beck/index/`); the
fitness functions: `beck fitness generate` (`src/beck/fitness/`).

* Maps spec id → component → symbols (functions, modules) → tests.
  Symbols from tree-sitter (Elixir, Python, C grammars).
* Few heavy writes (when the DSLs are created), then small updates; mostly
  reads, graph walks with `WITH RECURSIVE`.
* The index is derived: beck rebuilds it deterministically, kent anchors the
  ids to the symbols where needed. The DSLs are design decisions: kent
  proposes their changes, the user approves them.
* Each file is tracked by content hash; a commit event marks the index of
  its files dirty, and a policy has kent verify them.
* **At the start of every iteration** the whole map is checked against the
  diff from the last indexed commit, including other people's commits (a new
  PR merged into the base). This covers most cases. When the diff changes
  the behaviour of code that implements spec ids, the DSLs have drifted: kent
  proposes how to bring them back in sync, the user approves, beck rebuilds
  the index.
* An agent that finds the code different from what the index says reports
  it, and the area is reindexed.

## Event store (SQLite, embedded)

Implemented: `src/beck/events/`, with `beck sessions`, `beck events` and
`beck trace`.

* **Only events in an event store.** What does not derive from the events
  lives in the project database `.beck/beck.db`: the catalog of the
  iterations and smart_audit's outbox (whether argyris handled a record is
  state of its own, and outlives its iteration). The index stays in
  `.beck/index.db`, derived from the DSLs and the code.
* **Correlation.** Every event has an id, the `causation_id` of the event
  that directly caused it and the `correlation_id` of the root of its chain.
  The policies' messages, the projections (with the last event they folded),
  the outbox's records and every line of `.beck/beck.log` carry the same
  ids: `beck trace <iteration> <event>` shows what caused an event and what
  it caused.
* **Every payload has a schema**; an unknown type or a malformed payload
  never enters the store, and the store refuses updates and deletes.
* **One event store per iteration.** It is created as soon as a session
  starts, with a random id; when the branch is created, the catalog records
  the iteration's name (the branch's name). Old iterations can be deleted.
  The catalog table holds: event store, session id, iteration name, start
  date, end date.
* `beck sessions` lists the event stores of the project (id, branch, start,
  end, last event); `beck attach <id>` resumes one, replaying its events up
  to the last. Ctrl-C is safe; `session-state` is no longer needed.
* Every event records the repository's commit sha and, for an agent turn,
  its tokens: a replay rebuilds the starting point.
* **Policies** react to events and send signals to the agents (a dirty
  index → kent verifies; a turn ended → the auditor).
* **Replays for improvements and A/B tests of models**: an event's input is
  run again N times, and the outcomes are judged by the deterministic checks.
  The verdict is statistical (errors, complexity, tokens, compared across the
  runs). The replays also tune what can only be tuned on real data, such as
  the normalisation of the failure signatures.
* **Replay of an iteration** (implemented, `src/beck/replay.py`, `beck
  replay <iteration> [--agents live|recorded|mixed] [--model AGENT=MODEL]`):
  a new iteration in an empty folder, from the recorded start and
  configuration (`ConfigLoaded`), each decision of the user with the
  recorded answer. `live` (the default) has the agents implement the
  branch again, with today's prompts and beck and the models given: the
  benchmark of a model, a prompt or a version. `recorded` answers each turn
  from the events alone (`AgentTurnEnded.output`, the `TurnChanged` patch,
  the `beck` commands of `ToolCalled` run again) and runs beck's own steps
  with the current code: the integration test of beck on real data. It
  stops at a decision it lacks (`beck answer`, then `--resume <replay>`)
  and compares the two runs: events, and turns, tokens and time per agent.
* **The events are the single source of truth of a turn**: its JSON as the
  agent answered (`AgentTurnEnded.output`), the hash of the definition it
  ran with (`definition`: prompt, tools, examples), its duration, and its
  changes as a git patch (`TurnChanged`). Iterations recorded before
  2026-11-16 lack them: their replay reads the Claude Code transcripts.
* **The consumption is a projection** (implemented, `src/beck/usage.py`):
  every `AgentTurnEnded`, `AgentTurnFailed` and `SignalRejected` becomes a
  row of a ledger in `beck.db` (`usage_event`: the event's id, seq, command,
  component, agent, model, tokens, time), and in the same transaction adds to the
  totals per iteration, agent, model and command (`usage`) and per time
  window (`usage_window`, `usage.window_minutes`, 10 by default). Every
  number is a sum of ledger rows, and every row names its event: `beck usage
  <iteration> --events` lists them, `beck trace` follows one. The session
  projects them after each commit and signals the dashboards attached to its
  socket (`beck dashboard --attach`, run in the session's folder), which read
  them again and follow the log; the dashboard projects nothing. Nothing
  there is a fact: `beck usage --rebuild` projects them again.
* The flow log stays, only as a human-readable log.

## Parallelism and commits

* **As parallel as possible.** Every component (a component, the integration
  flow) gets its own git worktree, branched from the feature branch once the
  interfaces are committed. N unclebobs work on different components at once, and
  martin refactors the finished components beside them.
* An unclebob does not wait for the components its component depends on through
  an interface (`boundary: interface`): it trusts the declared interfaces,
  which is what they are for. A `concrete` dependency is tested with the
  real module, so the component waits for it to land (`waits_for` in
  `ExecutionDerived`); its worktree then branches from a feature branch that
  holds it. The integration component at the end exists to find where the
  components diverge from the interfaces.
* **A requirement already met.** A red test that passes on its first run proves
  its requirement when the code exists: the integration component's, or a requirement the
  index, built on the component's worktree, anchors to code in the component's scope
  that is no longer an unimplemented (an earlier green did more than its requirement).
  beck commits the test and an empty green, to keep the TDD order of the
  commits (`already_green`).
* beck commits every TDD phase in the component's worktree as soon as it has
  verified it: the new test fails (red), then passes with no test changed
  (green). The code is never left uncommitted.
* **The history a reviewer sees is always autosquashed.** The agents can
  make 90 commits for one component; that many commits throw away the value of
  git and force the reviewer to rebuild the context from the pieces. So:
  * once martin has verified the component's tests, beck squashes its TDD commits
    into one commit, the component's;
  * from then on every change to the component (martin's refactors, a reopened
    cycle, a resolved conflict, a fix found by the full suite or the static
    checks) is a `fixup!` of the component's commit, the way the user works every
    day;
  * beck autosquashes the component before it lands, and the feature branch
    before presenting it to the user: the user reviews one commit per component,
    never a fixup made before;
  * the changes **the user** asks for in review are fixups, kept so that the
    next round shows what changed; the same will hold for the other
    reviewers' comments (with `gh`);
  * no later review round is autosquashed: every fixup made after the first
    presentation (the user's corrections, and what the full suite or the
    checks fixed after them) stays visible until the close;
  * when the user closes the iteration, beck autosquashes the branch, the
    user's fixups included: a pull request is always presented
    autosquashed, and changes after it arrive only as fixups.
* beck lands the component's commit on the feature branch in the dependency order
  of the architecture; since every component lands as one commit, no single
  commit needs reordering. A landing that conflicts goes back to martin,
  then through the checks again.
* It is not expected to work well at first: smart_audit measures, from the
  events, how long each component waits, the conflicts at landing and the
  divergences found by the integration, and argyris and the user tune it.
* Pushing and opening the pull request stay manual.
* The user's corrections: fixups in a review round, a new iteration, or by
  hand: the user's choice.

## Blocked cycles

* Every attempt of a cycle ends with a **failure signature**, computed
  deterministically from the structured test report and the agent's
  structured output: the normalised set of (test, kind of failure, assertion
  message), without line numbers, addresses and times.
* beck looks at the tail of the sequence of signatures: a block of length p
  repeated 3 times in a row blocks the cycle. p = 1 is the same result three
  times; p ≥ 2 is a repeated pattern (A B A B A B).
* With no repetition, the cycle blocks after 30 attempts.
* The rule is not only for TDD: it applies to every loop between an agent
  and a deterministic check (`DomainInvalid ⟲ WriteDomain`,
  `InterfacesBroken ⟲ WriteInterfaces`, …). A loop triggered by the user
  (`Revise…`) has no limit.
* Both limits are in the configuration. The normalisation is the delicate
  part (too coarse blocks too early, too fine never sees a repetition): it
  starts as above and is tuned with the replays.
* A blocked cycle does not stop the machine: beck parks it and the cycles
  that depend on it, goes on with the independent work, and has kent propose
  a restart, which waits for the user. When nothing else can proceed, the
  machine stops, and that is correct.
* An **interface change** follows the same path. It can be requested during
  the iteration by unclebob (a signature lacks a datum the morphism needs,
  an error case is not in the return type, a property of the spec cannot be
  observed through the public interface), by martin (an interface too wide
  or leaking its implementation: changing a contract changes the tests, so
  martin requests it, never makes it), or by the integration component (a
  divergence between components). The work in flight on the components that
  use that interface is parked too.

## Tests in the projects

* An interface over the machine-readable test report, so that a failure is
  extracted deterministically (test, assertion, line).
* Python: pytest's built-in `--junitxml=<path>`; `pytest-json-report` if
  JSON is needed.
* Elixir: the `junit_formatter` hex package (an ExUnit formatter, test-only
  dependency, configured in `test/test_helper.exs`; latest 3.3.0, little
  maintenance lately).
* C/C++: CTest `--output-junit <file>`; GoogleTest `--gtest_output=xml:<file>`.
  Unity (used by `c-scaffold`) has no native JUnit output: to verify (its
  repository ships a conversion script).
* Go: `go test -json` natively; `gotestsum --junitfile <file>` for JUnit.
* Rust: cargo-nextest, `[profile.<name>.junit] path = "junit.xml"` in
  `.config/nextest.toml` (written under `target/nextest/<profile>/`).
* **Resilience**: when no machine-readable report exists (the runner has
  none, the file is missing or unreadable), beck falls back to the agent
  transparently and logs a warning in the log file.
* **beck does nothing of its own accord**: it runs tests when an agent asks
  (`beck send test`) or a policy does. unclebob runs only the test of its cycle;
  when a component is implemented, a policy has beck run the component's tests and
  the fitness functions; the whole suite runs once, at the end of the
  integration component. smart_audit checks which tests each cycle ran.
* **The agent never reads the console** (reading the runners' whole output
  was one of the costs of the old workflow). `beck send test` sends the output to
  the run's log under `.beck/reports/` and prints only `ALL_OK (n passed)`
  or, from the JUnit XML, one entry per test that did not pass: its id, the
  kind of failure, the first lines of the message, the file and line inside
  the project. Without a readable report it prints `NO_REPORT` and the
  log's path; the summary by Haiku comes with the drivers.
* **The static checks follow the same pattern** (implemented): `beck check`
  runs the checks of `toolchain.yml` (ruff, mypy, credo, dialyzer, or any
  `file:line: message` tool), reads their output deterministically, prints
  `ALL_OK` or one line per finding, and records a `ChecksRun`. They are not
  cheap: the `paths` checks run per component, on the component's files, before the
  component's autosquash; the `project` checks (dialyzer) only with `--all`, at
  the end of the iteration.
* **The report is beck's evidence**, never the agent's word. The agents run
  the tests through `beck send test` (implemented), which runs the command of
  `.beck/toolchain.yml`, writes the JUnit report under `.beck/reports/` and
  records a `TestsRun` event: the tests, the exit code, the report's hash,
  the git tree of the working tree after the run (outside `.beck/`), and as
  its cause `$BECK_CAUSE`, the event that launched the agent. beck accepts
  a declared phase only on a `TestsRun` whose tree is the working tree's
  tree at verification (no file changed since), holding the declared tests
  with the expected outcome.
* **A red** is a declared test that is in the report and did not pass, as a
  failure or as an error (an interface's body raises until it is
  implemented). A syntax or collection error of the test file is not a red:
  it is the implementer's mistake, rejected at once.
* **Shared files** (to try): the model writes the structural ones (the
  dependency manifest from the external libraries, the supervision tree from
  the supervised components) before any component starts; the index marks a file
  holding symbols of several components as shared, and the scheduler never
  runs two components sharing one in parallel; any other need of a component to
  change a shared file is an architecture change. See `docs/dsl.md`.
* **Coverage: it goes up and never down; one line never stops the work.**
  beck measures it at every run of a component's tests, in the report format the
  toolchain names (coverage.py, ExCoveralls, lcov). Two rules:
  * *the ratchet*: the project's coverage (the suite's at the start, then
    each landed component's files as they landed) must not go down with the
    component's files. With TDD it never does, so a drop is an anomaly the user
    decides on (after beck's one try at the gaps): go on, park the component, or
    cover again; kent routes the answer;
  * *the gaps* (the boy scout rule): a line the iteration changed that no
    test runs. beck maps it, through the `spec:` markers, to the requirement
    that names its code and asks unclebob once (`CoverLines`) for the tests
    that run it; beck checks that only tests changed, that they pass, that
    they run the lines, and commits them. What is left (a line no requirement
    names, or a try that failed twice) goes to martin at the review: dead
    code is deleted, live code is a finding for the user.
* **Measures behind interfaces, the tools in the toolchain.** Each measure
  is an interface with backends (`src/beck/measures/`); `.beck/toolchain.yml`
  names them, with a default per language: complexity by lizard (Python, C
  and most languages) or by beck's own counter on tree-sitter (Elixir, which
  lizard does not read); coverage by its report's format, with the options
  or the command that write it. A missing tool is written behind the same
  interface, never worked around. The toolchain is only wiring: when the
  project cannot measure its coverage, kent finds the tool it has or adds
  the language's usual one (`WireCoverage`, phase 2), and beck installs
  the dependencies, runs the suite with it and commits the change.
* **Duplication** is smelled by the agents for now (martin folds duplicated
  helpers in its review); how to make it deterministic comes with time.
* **Flaky tests** (`src/beck/machine/flaky.py`): a test and its code
  unchanged (the same git tree) must give the same result. Like the
  repetition rule, a detector reads the runs in the events: when the last
  runs of a test on one tree (the window, 3) are mixed, beck hunts in a
  worktree of its own, escalating only while it is not sure: the test alone
  N times (`flakiness.runs`, 5), then inside its component's tests (a dependency
  on time or order), then, with the user's permission, inside the whole
  suite. A flaky test of a component reaches martin as a weak test; every one is
  in the notes of the review.
* Whether a project's tests can run is checked once, at the start, by
  kent and the environment checker.

## Interview and framing

* A deterministic tool tells greenfield from brownfield: at least one code
  file means brownfield; a directory with only documents means asking.
* `.beck/` does not count: a directory holding only `.beck/` is empty.
* `scaffold init` stays, deterministic, only in an empty directory. It runs
  `git init`, so in greenfield the branch is created after it.
* The interview is mostly standard questions, kept in the event store; kent
  may add follow-up questions. The branch is created (`linus branch`) only
  after it: an actor proposes its name in the configured format, the user
  confirms it.
* **The first iteration of beck on a brownfield project** has no DSLs: kent
  writes them for the delimited area only, the as-is first, then the
  iteration's changes on top.
* `toolchain.yml` is built deterministically from the stack, with the user's
  help, once per project.

## TUI

Implemented (`beck tui [iteration]`, `src/beck/tui.py`, its read models in
`src/beck/views.py`); the components' graph is a list in landing order with
each component's state and dependencies (`grandalf` not needed so far).
Textual, with tmux-like areas:

* a status bar: global tokens, the state machine's state, the active
  agents;
* a panel with the agents' current activity (a summary of the event
  stream);
* a panel with a horizontal histogram of the average tokens per cycle of
  each agent;
* the main panel: the prompt, the reports, the questions;
* at the end of each component, an ASCII graph of the components in the main
  panel: existing ones in the default colour, done in green, to do in red
  (`grandalf` for the layout, to try).

Driving beck from stdin stays possible, at little cost.

## Metrics

Tokens per agent turn, saved with the events; the average cost of a turn of
each agent, to know where to save; shown in the TUI. The parallelism
measures (waits, conflicts at landing, divergences at integration) come from
the events too.

## Drivers

An abstraction over the CLI running the agents (`beck.machine.core.Agents`):
Claude Code (implemented, `src/beck/drivers/claude.py`) and Copilot later.

* One Claude Code session per beck session (an agent and a session key),
  with a stable uuid: started with `--session-id`, resumed with `--resume`.
* The agents' files are `src/beck/agents/prompts/`; the driver adds the
  common protocol and the examples of each command's output
  (`src/beck/agents/examples.yaml`, checked against the schemas by the
  tests), and passes them with `--agents`.
* The user message is the input envelope; the answer is parsed as JSON.
  Each agent gets its own tools, `Bash` limited to beck's commands, and
  `--permission-prompts none` denies the rest.
* Every tool call becomes a `ToolCalled` event (smart_audit's material);
  a turn the CLI cannot complete is `AgentTurnFailed`, retried under the
  repetition rule, then asked to the user.
* Tried on the real CLI: one reader turn on Haiku summarised a real ExUnit
  log, its answer valid against the schema at the first attempt.

## Running beck

```
beck start [--batch]                  start an iteration in the current project
beck attach <iteration> [--batch]     resume it from its events (Ctrl-C is safe)
beck pending <iteration>              the decisions of the user it waits for
beck answer <iteration> <id> <words>  answer one: yes, or the words
beck replay <iteration> [--agents …]  implement it again: its start and the user's answers as recorded
beck usage <iteration> [--by …]       what the agents consumed (--events: the events it comes from; --json)
beck dashboard --attach               the consumption of the session running here, live, with its log
beck send test|check …                an agent's tests or checks, run once by the session (its socket)
```

On a terminal beck asks each decision and goes on; with `--batch` (or
without a terminal) it stops at the first decision, prints it and exits,
so that another agent can drive it.

## How beck itself is developed

Not in TDD: it is a tool, not production code. Tests written after the code
only on the deterministic core (event store, state transitions, the TDD
checker, the index), since they judge the agents; the TUI is tried by hand.
The tokens of the development sessions are measured to check that this
costs less than TDD. The replays act as integration tests.

## beck's own architecture (approved 2026-11-13)

* **State machines**: one for the session (phases 1, 2, 3 and 6); one per
  component (phases 4 and 5), holding the red/green machine of the cycle
  (unclebob) and the blue phase of the component (martin, once per component, after
  the cycles; it may move into unclebob's cycle later); a scheduler of the
  slots.
* **Policies**: facades of handlers, by event type. A policy that needs an
  agent or the user asks for it (a request, recorded as an event); a
  deterministic one has beck act at once (a commit, a worktree, a test run).
  Every outcome is an event caused by the triggering one.
* **The engine** (implemented, `src/beck/machine/core.py`; the words in
  `docs/glossary.md`, "The core"): commands are never stored, only events.
  Each input (an agent's output, the user's answer, an agent's command on
  the socket, the session's start) is processed in one transaction: its
  facts, then the policies of every new event in order, beck's commands
  executed at once, the commands for the outside recorded as requests
  (`AgentTurnRequested`, `DecisionRequested`). A crash aborts the input's
  transaction and records its facts with a `BeckCrashed`; after the fix
  `beck attach` processes them again, without asking the agent again.
  Recovery rebuilds the state from the events and runs no policy again: a
  requested turn starts (`AgentTurnStarted`: its input and its folder's
  snapshot), a turn beck died in starts again from its snapshot. The agents
  run the tests and the checks through the session's socket (`beck send`,
  `src/beck/socket.py`): beck runs them once and records the evidence.
  A policy sees the events up to its own (the scheduler alone reads the
  present). Implemented: the
  cycle's machine (`src/beck/machine/cycle.py`: the verifiers of red and
  green, the repetition rule) and the components' (`src/beck/machine/component.py`:
  the scheduler of the slots, worktrees, units, parking, restarts) and
  phase 5's (`src/beck/machine/refactor.py`: the component's tests run by beck
  with coverage, regressions reopening their cycles, the squash, the
  measures, martin's review and refactors as fixups verified by beck, the
  trade-off asked to the user, the static checks, the autosquash, the
  landing in dependency order and its conflicts) and phase 6's
  (`src/beck/machine/closing.py`: the integration component last, the full suite
  and the project's checks run by beck, their failures sent back to the
  components or fixed as fixups found with git blame, the branch's autosquash,
  the reviews, the corrections, the close); an agent's question goes to the
  user and the wrapped answer back to the session that asked. The
  session's machine (`src/beck/machine/session.py`, phases 1 to 3: the
  configuration, the classification, the interview, the branch, the
  toolchain, the environment, the suite, the index's reconciliation, the
  shopping list, the domain, the architecture, the interfaces, the fitness
  functions, the anchoring, the plan) closes the chain: a test runs a whole
  iteration, from `SessionStarted` to `IterationClosed`, with scripted
  agents and user. The user's words become events through
  `src/beck/machine/user.py`, one decision per user command.
* **The toolchain is the project's** (`.beck/toolchain.yml` of the main
  tree, `$BECK_PROJECT` for an agent), the commands run in the component's
  worktree. Python runs with `PYTHONDONTWRITEBYTECODE=1`: a stale `.pyc`
  with the same mtime and size as a rewritten file gave a false result.
* **A restart is a real diff**: kent makes its changes (DSLs,
  interfaces) in the feature branch's tree, uncommitted, and describes them;
  the user reviews the diff; beck commits it on approval and discards it on
  rejection, then rebases the parked components on it and reopens their cycles
  at their phase.
* **One asyncio process**; every agent runs as a subprocess of its CLI, read
  as a stream: every tool call becomes an event, the tokens come from the
  result.
* **Agents speak JSON both ways**: the input of a turn and its output are
  JSON documents validated by a schema (an output that fails is
  `SignalRejected`); the protocol, its commands and their schemas are in
  [`docs/protocol.md`](docs/protocol.md) (`src/beck/agents/`). beck's agents get new, lean agent files in
  `beck/agents/`: no commits, no textual signals. The old ones stay with the
  old kent until it fades out.
* **Drivers**: Claude Code first, where the measures are taken; Copilot
  later, behind the same interface.

## Read models

No projection is persisted, and they are not an architectural concept:
they are functions that fold the events into a state, called by the
policies and by the TUI. They are named only where a policy needs one:

* the state of the machine (where `beck attach` resumes);
* the sequence of the failure signatures of a loop (the repetition rule);
* which components are ready to land and which are parked;
* the TUI and the metrics (tokens per agent, the histogram, the graph of the
  components, the parallelism measures for smart_audit);
* the catalog (`beck sessions`).

## Event storming (big picture)

Actors: (U) user, (B) beck deterministic, (A) agent. Each phase has a main
line, read left to right; the branches (`↳`) leave from the event they
hang under and either return to an earlier event (`⟲`) or stop the line.
Every branch becomes a transition of the state machine.

The approved phases also list their commands and policies:
`Command (who) → Event`, and `P: Event → Command` for a policy, which
carries the signal from one actor to the next. `[…]` is a condition read
from a read model; `⟲` returns to an earlier command.

### 1. Session and framing (approved)

```
StartSession (U: beck)                     → SessionStarted (B: event store with a random id, catalog row)
P: SessionStarted → LoadConfig (B)         → ConfigLoaded | ConfigInvalid (stop)
P: ConfigLoaded → ClassifyProject (B)      → ProjectClassified {greenfield | brownfield | undetermined}
P: [undetermined] → AskClassification (B→U)
   ClassifyProject (U)                     → ProjectClassified
P: ProjectClassified → AskQuestion (B: the standard questions; A kent: the follow-ups)
   AnswerQuestion (U)                      → QuestionAnswered
P: [no question pending]                   → InterviewCompleted (B)
P: InterviewCompleted [greenfield] → ScaffoldProject (B) → ProjectScaffolded (git init, toolchain.yml)
P: InterviewCompleted [brownfield] | ProjectScaffolded
   → ProposeBranchName (A)                 → BranchNameProposed
   ConfirmBranchName (U)                   → BranchNamed
P: BranchNamed → CreateBranch (B)          → BranchCreated (iteration name in the catalog)
AttachSession (U: beck attach <id>)        → SessionAttached (replay, resume from the pending command)
```

### 2. Survey (approved)

```
P: BranchCreated [toolchain.yml present] → LoadToolchain (B)   → ToolchainLoaded
P: BranchCreated [toolchain.yml absent]  → DetectToolchain (B) → ToolchainDetected
   ConfirmToolchain (U)                                        → ToolchainConfirmed
P: ToolchainLoaded | ToolchainConfirmed
   → BringEnvironmentUp (B, A kent fallback from the docs)     → EnvironmentUp | EnvironmentDown (stop)
P: EnvironmentUp → VerifySuite (B)       → SuiteVerified {machine-readable report: yes | no} | SuiteFailed (stop)
P: SuiteVerified [brownfield, index present] → ReconcileIndex (B) → IndexReconciled | DslDriftDetected
P: DslDriftDetected → ProposeDslSync (A kent)  → DslSyncProposed
   ApproveDslSync (U)                          → DslSyncApproved
P: DslSyncApproved → RebuildIndex (B)          → IndexRebuilt
P: IndexReconciled | IndexRebuilt | SuiteVerified [brownfield, no index]
   → DelimitCodeArea (A kent)                  → CodeAreaDelimited
P: CodeAreaDelimited | SuiteVerified [greenfield]
   → WriteShoppingList (A kent)                → ShoppingListWritten
   ApproveShoppingList (U)                     → ShoppingListApproved
   ReviseShoppingList (U)                      → ShoppingListRevised ⟲ WriteShoppingList
```

### 3. Model (approved)

Two branches, each a state of kent with a fresh context and its own
approval; in practice the domain comes first. While writing, kent asks the
user (`QuestionAsked` (A) ⟷ `QuestionAnswered` (U)). Every `⟲` triggered by
a check follows the repetition rule of "Blocked cycles".

```
P: ShoppingListApproved → WriteDomain (A kent, fresh context) → DomainWritten
P: DomainWritten → ValidateDomain (B, spec-check)  → DomainValidated | DomainInvalid ⟲ WriteDomain {the errors}
   ApproveDomain (U)                               → DomainApproved
   ReviseDomain (U)                                → DomainRevised ⟲ WriteDomain
P: DomainApproved → WriteArchitecture (A kent, fresh context) → ArchitectureWritten
P: ArchitectureWritten → ValidateArchitecture (B: the schema, every spec id implemented by exactly one component)
                                                   → ArchitectureValidated | ArchitectureInvalid ⟲ WriteArchitecture
P: ArchitectureValidated → WriteInterfaces (A kent) → InterfacesWritten
P: InterfacesWritten → CompileInterfaces (B)       → InterfacesCompiled | InterfacesBroken ⟲ WriteInterfaces
P: InterfacesCompiled → GenerateFitnessFunctions (B) → FitnessFunctionsGenerated {the untranslated assertions}
P: [untranslated ≠ ∅] → WriteFitnessFunctions (A kent) → FitnessFunctionsWritten
P: FitnessFunctionsGenerated [untranslated = ∅] | FitnessFunctionsWritten
   → RunFitnessFunctions (B)                       → FitnessFunctionsPassed | FitnessFunctionsFailed ⟲ WriteInterfaces
   ApproveArchitecture (U)                         → ArchitectureApproved (requires DomainApproved)
   ReviseArchitecture (U)                          → ArchitectureRevised ⟲ WriteArchitecture
P: ArchitectureApproved → BuildIndex (B)           → IndexBuilt {the unanchored ids}
P: [unanchored ≠ ∅] → AnchorIds (A kent)           → IdsAnchored
P: IndexBuilt [unanchored = ∅] | IdsAnchored → DeriveExecution (B)
                                                   → ExecutionDerived (components, units of spec ids, dependency and landing order)
P: ExecutionDerived → CommitModel (B)                  → ModelCommitted (interfaces and fitness functions on the
                                                     feature branch; .beck/ too when versioned)
```

Cross-cutting, from phase 1: `P: AgentTurnEnded | SignalRejected → AuditTurn (smart_audit)`.

### 4. Implementation (approved)

Per component, in its own worktree, components in parallel; one unclebob session per
unit, the cycles of the unit inside it.

```
P: ModelCommitted | ComponentClosed | ComponentParked → ScheduleComponents (B)
   [free slot, component not started]           → StartComponent (B)      → ComponentStarted (worktree from the feature branch)
P: ComponentStarted | UnitClosed [units left] → OpenUnit (B: context from the index) → UnitOpened (new unclebob session)
P: UnitOpened | CycleClosed [requirements of the unit without a test]
   → WriteRedTest (A unclebob: writes the test, runs it with beck send test) → RedDeclared {requirement id, test id}
P: RedDeclared → VerifyRed (B: a fresh TestsRun holds the test, not passed;
                 no syntax or collection error; only test files changed)
                                                                → RedVerified | RedRejected ⟲ WriteRedTest {reason}
P: RedVerified → CommitRed (B)                                  → RedCommitted
P: RedCommitted → WriteGreenCode (A unclebob: runs the cycle's test) → GreenDeclared
P: GreenDeclared → VerifyGreen (B: a fresh TestsRun holds the test, passed;
                   no test file changed since RedCommitted; files in the component's scope)
                                                                → GreenVerified | GreenRejected
P: GreenRejected → SummariseFailure (B parser, Haiku fallback)  → FailureSummarised ⟲ WriteGreenCode
P: GreenVerified → CommitGreen (B)                              → GreenCommitted → CycleClosed
P: CycleClosed [every requirement of the unit has a green test]     → UnitClosed (session ends)
P: UnitClosed [no unit left]                                  → ComponentImplemented

P: [repetition rule] → BlockCycle (B)                           → CycleBlocked
   RequestInterfaceChange | RequestArchitectureChange (A unclebob | martin) → …ChangeRequested
P: CycleBlocked | …ChangeRequested → ParkDependents (B)         → DependentsParked
P: DependentsParked | RestartRejected → ProposeRestart (A kent) → RestartProposed
   ApproveRestart (U) → RestartApproved | RejectRestart (U) → RestartRejected {instructions}
P: RestartApproved → ApplyRestart (B: DSLs and interfaces committed on the feature branch,
                        index rebuilt, units rederived, parked worktrees rebased) → RestartApplied
P: RestartApplied → ReopenCycles (B)                         → CyclesReopened ⟲ OpenUnit
```

After the component's squash (phase 5) a reopened cycle commits its red and green
as fixups of the component's commit.

### 5. Component refactor (approved)

martin, in the component's worktree, beside the other components.

```
P: ComponentImplemented → RunComponentTests (B: the component's tests and the fitness functions, with coverage)
                                                                → CoverageMeasured (+ CoverageDecreased: a note)
                                                                → ComponentVerified | RegressionFound | CoverageGapsFound
P: RegressionFound → CycleReopened (B, on the requirements of the failing tests, green) ⟲ phase 4
P: CoverageGapsFound [the first time] → CoverLines (A unclebob: tests that run the lines a requirement
                  names) → CoverageTestsDeclared → VerifyCover (B: only tests changed, they pass,
                  they run the lines) → CommitCoverage → RunComponentTests
                  | CoverageRejected ⟲ once → GiveUpCoverage → ComponentVerified (the gaps go to martin)
P: ComponentVerified [not squashed] → SquashComponent (B: the TDD commits into the component's commit) → ComponentSquashed
P: ComponentSquashed | ComponentVerified [squashed] → MeasureComponent (B: lines, complexity; durations and
                  flakiness from the cycles' reports)           → ComponentMeasured
P: ComponentMeasured → ReviewComponent (A martin: SOLID, idiom, readability;
                  each test against the requirement it references)        → ComponentReviewed {findings, weak tests}
P: ComponentReviewed | RefactorCommitted [findings or weak tests left]
   → RefactorComponent (A martin: runs the component's tests and the fitness functions;
                   a weak test under the three laws of TDD)     → RefactorDeclared {phase: tests | code | strengthen}
P: RefactorDeclared → VerifyRefactor (B: a fresh TestsRun, the same tests all green;
                      tests phase: no source changed; code phase: no test changed;
                      coverage; measures)                       → RefactorVerified | RefactorRejected ⟲ RefactorComponent
                                                                | MeasureWorsened
P: MeasureWorsened → ParkComponent (B)                               → ComponentParked {measure, martin's reason}
   AcceptTradeoff (U) → RefactorVerified | RejectTradeoff (U) → RefactorReverted (B) ⟲ RefactorComponent
P: RefactorVerified → CommitRefactor (B: a fixup of the component's commit) → RefactorCommitted
P: ComponentReviewed | RefactorCommitted [no findings left] → AutosquashComponent (B) → ComponentReady
P: ComponentReady | ComponentLanded [its dependencies landed]
   → LandComponent (B: rebase on the feature branch)                 → ComponentLanded | LandingConflicted
P: LandingConflicted → ResolveConflict (A martin: a fixup; runs the component's tests) → ConflictResolved
P: ConflictResolved → VerifyRefactor (B) → AutosquashComponent ⟲ LandComponent
P: ComponentLanded → CloseComponent (B: removes the worktree; commits .beck/ when versioned) → ComponentClosed
```

A worsened measure parks the component until the user accepts the trade-off or
rejects it; later iterations will judge it with tools (connascence,
coupling, cohesion, cyclomatic complexity). A clean landing runs no tests:
a component's tests replace its dependencies with doubles, and the integration
component finds the rest.

### 6. Integration and closing (approved)

```
P: ComponentClosed [every other component landed] → StartComponent (B: the integration component) → phases 4 and 5
                                          (its tests use the real components, no doubles)
P: ComponentClosed [the integration component] | CorrectionsApplied
   → RunFullSuite (B)                                           → FullSuiteRun
P: FullSuiteRun → VerifySuite (B)                               → SuiteGreen | SuiteFailed
P: SuiteFailed → MapFailures (B: test → requirement → the component that implements it) → ComponentsReopened
                 → OpenUnit in a worktree of each component ⟲ phases 4 and 5 (fixups of the component's commit)
P: SuiteGreen → RunStaticChecks (B: the toolchain's check)      → StaticChecksPassed | StaticChecksFailed
P: StaticChecksFailed → ProposeStaticFixes (A martin: fixups of the commits
                        that wrote the lines, found with git blame) → StaticFixesCommitted ⟲ RunStaticChecks
P: StaticChecksPassed [first round] → AutosquashBranch (B)      → BranchSquashed (one commit per component)
P: BranchSquashed | StaticChecksPassed [later round] → PresentForReview (B→U)
   RequestCorrections (U) {text}                               → CorrectionsRequested
P: CorrectionsRequested → PlanCorrections (A kent: the components, through the index) → CorrectionsPlanned
P: CorrectionsPlanned → the components' worktrees (A unclebob | martin: fixups, kept) → CorrectionsApplied
   CloseIteration (U) → AutosquashBranch (B) → IterationClosed (B: the catalog's end; the PR is presentable)
   StartNewIteration (U) | FixByHand (U)                       → the user's choice
```

* ↳ a divergence between components in the integration component
  → `InterfaceChangeRequested` (phase 4).

### 7. Cross-cutting

* `AgentTurnEnded` (B, tokens and sha) → the auditor's policy.
* `SignalRejected` (B: an output that does not match its schema) → smart_audit.
* `IndexMarkedDirty` (B) → `IndexVerified` (A kent).
* `IndexDiscrepancyReported` (A) → `IndexRebuilt` (B).
* `TokenBudgetExceeded` (B).
* `SessionInterrupted` (B, Ctrl-C) / `SessionAttached` (B).
* `BeckCrashed` (B: beck's own exception, in a policy, a handler, a
  translator or a turn, with its traceback) → smart_audit, the outbox; the
  session stops, the command stays pending, `beck attach` after the fix
  runs it again.
* `EnvironmentDown` (B), at any time.
* `InterruptionDetected`, `ViolationLogged` (smart_audit, into the outbox).
* `ViolationSettled` (argyris's answer, through `beck audit --settle`).
* `ImprovementProposed` (argyris, woken by the outbox).

Next: try beck on a fictitious project with the user, read the code
together, then the points below.

## Open points

* **TODO: a tool in the TUI to delete old event stores.**
* **TODO: the TUI keeps an answer sent while nothing waits.** Enter with no
  decision pending leaves the text in the input, and the next answer is
  appended to it.
* **TODO, when it gets in the way: the TUI's prompt does not wrap.** A
  long answer runs on one line past the panel's width (`src/beck/tui.py`
  uses Textual's `Input`, a single line). The fix, then: an input that
  wraps and grows with the text, like Claude Code's prompt (a `TextArea`,
  Enter to send).
* **TODO, a next iteration: a debug mode.** On an input (a bug report, a
  failing behaviour), beck writes a shopping list aimed at regression
  tests; the tests reproduce the bug; `git bisect`, driven by those tests,
  finds the commit that brought it; beck reads the pull requests merged in
  the repositories involved to judge what to revert; unclebob writes the
  fix, test first, as in a cycle.
* **TODO, a next iteration: simon comes back, as a mode of work, not as an
  agent that runs beside the others.** Instead of implementing code, the
  mode distils documentation from the existing code: introspection of an
  unknown system. It uses what beck already has to derive the DSLs from
  the code (the brownfield survey, the index, the anchoring), and writes
  the documentation from them.
* **smart_audit, to extend**: its rules today are the forbidden tools and
  commands, unclebob reading outside its unit, test runs that are not the
  cycle's, martin's order on a weak test, malformed outputs, failed turns,
  beck's own crashes (`BeckCrashed`). Not yet: how the parallelism stalls. argyris is woken by hand (`beck
  audit --wake`); waking it at every new record needs a terminal for it.
  argyris's own session is not in the event store: smart_audit cannot
  audit it yet.
* **Phase 5, to refine**: a component resumed after a trade-off retakes its slot
  without the scheduler; a flaky test outside the components is only reported;
  a component parked for its coverage resumes only through a restart.
* **Phase 6, to confirm**: a correction for martin names the component's largest
  file as a hint (`PlanCorrections` has no file).
* **Index staleness**: how to detect a time jump (the code changed under the
  index without any event). Reindexing at the start of each iteration covers
  about 90 %; the rest is handled as it comes.
* The normalisation of the failure signatures: tuned with the replays.
* Unity's JUnit output; `grandalf` for the TUI's graph.
* `CODEOWNERS` for the fitness functions' path (`tests/fitness/`,
  `test/fitness/`): whether beck writes it, and with whose handle.
* Later: PR comments with `gh`, Alloy and TLA+ stages, simon on demand.
