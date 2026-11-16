# Later iterations

What was decided to come later, and what was dropped with the knowledge to
bring it back (git history keeps the code).

## argyris and smart_audit (dropped on 2026-11-17, branch event_sourced_core)

How they were:

* **smart_audit** (`src/beck/machine/audit.py`): a deterministic observer.
  After every agent turn (`AgentTurnEnded`, `SignalRejected`, `BeckCrashed`
  → `AuditTurn`) it read the turn's `ToolCalled` events and logged the
  violations a rule could decide: forbidden commands, explored files outside
  the scope, malformed outputs, tests run outside the cycle, failed turns,
  beck's crashes. Each went to an `outbox` table of `.beck/beck.db` (the
  event it was about, its correlation, `handled_at`) and was recorded as a
  `ViolationLogged` event.
* **argyris** (`src/beck/argyris.py`, `src/beck/agents/prompts/argyris.md`):
  beck's agent that improved beck's agents. The user woke it by hand
  (`beck audit --wake`) in beck's source, with a structured message of the
  pending records; it settled them with the user and proposed each change
  as a pull request to beck (`beck contrib start | diff | submit`); its
  answer (`beck audit --settle`) marked the records handled and recorded
  `ViolationSettled`.

To rethink: argyris gets the signals that interest it like the dashboard,
through the socket, or better runs inside beck as a policy's side effect,
with no socket and no outbox at all. The pending/handled state of a violation is a read
model of `ViolationLogged` and `ViolationSettled`, not an outbox.

## One socket for every session

Today each running session serves its own socket in its project's folder
(`.beck/beck.sock`): the folder names the session. One socket for all would
need a daemon, always on, where every session registers and that routes the
dashboards' connections (like `dockerd` and `/var/run/docker.sock`): worth
it the day one view over several projects is wanted.

## Outboxes and policy cursors

An outbox is a queue (a Kafka topic, in a table): a projector writes its
records, a worker reads them after its cursor (one per policy, in a cursors
table outside the event store) and issues a command for each. Nothing in
today's domain needs one: the dashboard's signal is a plain policy, the
agents' turns in flight are known from the state, and argyris inside beck
would be a plain policy's side effect too. They come back only if a side
effect must survive a crash on its own and the state cannot tell it.

## Others

* A driver for local models (ollama) for the humbler tasks: a hybrid team
  of online and local agents, chosen by A/B replays.
* Transactions in the domain spec: groups of morphisms applied atomically.
* Rewriting beck through beck: its architecture and its domain in its own
  DSLs.
