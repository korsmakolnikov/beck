# beck's ubiquitous language

The words beck, its documentation, its code, its events and its agents use.
The code mirrors the domain: when a word here changes, the code, the
events, the DSLs and the prompts change with it.

## The graph and its nodes

* **Node**: any element of the DSLs (or of the code) in beck's graph: a
  domain, a component, a library, an entity, an event, a relation, a
  morphism, a requirement, a symbol, a test.
* **Architectural node**: a node of `.beck/architecture.yaml`: a **domain**
  (a container of components), a **component**, a **library** (a component
  of another kind).
* **Domain node**: a node of a domain's spec: an **entity** (or value
  object), an **event**, a **relation**, a **morphism**, a **requirement**.
* **Domain spec**: one per domain of the architecture,
  `.beck/domains/<domain>.yaml`. Two domains may hold entities with the same
  name and different invariants; a node's identity is `<domain>/<id>`.
* **Morphism**: what the domain does to its entities: what the domain spec
  used to call an *operation*. A **transaction** is a group of morphisms
  applied atomically (not in the DSL yet).
* **Requirement**: a node a test proves.
  * **functional**: a requirement of a domain spec: an invariant, a
    relation's rule, a constraint, a morphism's `post`, an error, a property.
    A TDD cycle proves it. A **property** is an invariant: of its entity, or,
    when it spans several entities, of the component.
  * **non-functional**: an assertion of the architecture. A fitness
    function proves it.

  Components, libraries and entities are nodes, not requirements: they hold
  requirements.
* **Reference to the specs**: the `spec: <id>` line in a definition's doc or
  in a test, which ties that code to a requirement (or a morphism). A
  reference never points to a requirement another component implements.

## The work

* **Iteration**: one piece of work on one branch, from the shopping list to
  the landing; one event store, `.beck/events/<id>.db`. `beck attach`
  resumes it.
* **Execution** (`ExecutionDerived`): what beck derives, deterministically,
  from the graph: for each component its units, and their order. kent never
  writes it; kent writes the DSLs and the interfaces.
* **Component** (in the work): implemented in its own worktree, landed as
  one; it holds N units.
* **Unit**: what one unclebob session implements inside a component:
  * a **model**: an entity and its requirements;
  * a **morphism**: a morphism and its requirements (`post`, errors);
  * a **component**: the component's own properties, those spanning several
    entities (e.g. untagging a tag just added leaves the bookmark as it was).
* **Cycle**: the TDD of one requirement: a red test, the green code, the
  commits.
* **Interfaces**: the files kent writes before any cycle: signatures, types,
  docs with their references to the specs.
* **Unimplemented**: a definition of the interfaces whose body is still to
  be written (`raise NotImplementedError` in Python, `todo!()` in Rust). A
  green replaces it. Not a stub: stubs and mocks are test doubles.
* **Parking**: beck stops a component that cannot go on (`ComponentParked`).
* **Restart**: the change kent proposes to the DSLs or the interfaces to
  unblock a parked component; the user approves it, beck applies it,
  derives the execution again and resumes the components.

## The core

* **Command**: a request to change the state, from the user, from an
  agent's output or from a policy. Never stored: a command handler validates
  it.
* **Command handler**: validates a command and hands it to its state
  machine.
* **State machine**: `execute(state, command) → events` validates the
  command against the state; `apply(state, event) → state` is the callback
  that folds each event. A new state that is not semantically valid is
  dropped and the command handler gets an error.
* **Event store wrapper**: persists the events of a command atomically, then
  hands them to the router. If it cannot persist them it returns the error
  and no policy runs.
* **Router**: hands each event to the policies that listen to its type.
* **Policy**: a facade over handlers, with a map from event type to handler
  (e.g. the dashboard's policy: on a turn's events, signal the socket).
* **Handler**: `handle(context, event) → message`; the message holds the
  outcome (ok, abort the transaction, ignore the failure) and may wrap a
  value. The context injects what the side effect needs (a connection, the
  socket, the driver).
* **Side effect / side input**: an agent. A policy hands it its input (the
  prompt) and is done; minutes later its structured output comes back as a
  side input, distilled into a new command.
* **Read model**: a table derived from the events, dropped and projected
  again at will (e.g. `usage_turns`, `usage`, `usage_window`).
* **Evidence**: what proves a phase of a cycle (a `TestsRun`, a commit).
  beck produces it, and the tests run once: an agent's `beck test` (or
  `beck check`) is a client of the session's socket. It sends the command
  to the session running in its folder; beck runs the tests inside, records
  the `TestsRun`, reads the result (the JUnit summary, haiku when it cannot)
  and sends it back through the socket; the agent goes on with it.
* **Socket**: `.beck/beck.sock`, served by the session running in the
  folder. The dashboard attaches to it (an handshake names the read models'
  database and the log; then a signal says "read again"); the agents send
  their commands through it (`beck send`). A step towards one daemon for
  every session.
* **Recovery**: after a crash beck rebuilds its state from the events
  (`apply`), without running the policies again. A turn started
  (`AgentTurnStarted`) whose output never came back is in flight: beck puts
  the worktree back to the turn's snapshot and hands the agent the same
  input again.
