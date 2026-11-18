# Benchmark: beck against the agent workflow

beck exists to cost fewer tokens than minime's workflow of agents
(README, first section). Until both have run on the same problem, that is
a hypothesis, not a measure. This file holds what the pilots have measured
so far, what went wrong in them, and the plan to settle the question.

State as of 2026-11-18, branch `event_sourced_core`.

## The plan

1. **Make beck reliable on the hello world pilot**: the refinements below,
   until a run closes the iteration with a proportionate domain, a
   proportionate architecture and no loop.
2. **Benchmark minime against beck on hello world**: the same shopping
   list, one run with minime's kent, one with beck, both outside `/tmp`;
   measured by a deterministic command (below).
3. **Refine beck on the other pilots** (ping/pong first, then the
   larger ones).
4. **Benchmark every pilot** the same way.

## How to measure

* **Weights** (the same as minime's cost analysis, so the numbers stay
  comparable): input 1, cache write 1.25, cache read 0.1, output 5. The
  result is in input-equivalent tokens.
* **beck** saves the tokens of every agent turn with its `AgentTurnEnded`
  event (`tokens`: `input`, `output`, `cache_read`, `cache_creation`).
* **minime** keeps them only in the Claude Code transcripts
  (`~/.claude/projects/<project>/`, `message.usage` of each assistant
  message, deduplicated by message id) and in the stop hook's
  `kent-events.jsonl`, under `/tmp`.
* **Proposal**: extend `beck replay`, which already compares turns, tokens
  and time per agent between two beck runs, to read a run of minime from
  its transcripts, and report both with the same weights, per agent and
  per component. The measure then reruns at every change of beck, with
  nobody counting by hand.
* **Keep the pilots outside `/tmp`**: a reboot wiped the ping/pong pilot
  (repository and event store) on 2026-11-17, as it had wiped minime's
  `/tmp/kent-loop` before. Only the transcripts survived.

## What has been measured

| Run | Weighted tokens | Where it went |
|---|---|---|
| beck, hello world (from the event store) | ~33.4 M | 99.2 % martin's refactor loop: 1,228 turns, 306 M tokens read from the cache |
| beck, hello world without the loop (estimate) | ~0.4–0.5 M | kent 186 k (12 turns), martin's reviews 140 k, unclebob 87 k (7 turns) |
| beck, ping/pong, interrupted at the game runner's review (from the transcripts) | ~1.2 M | unclebob 54 %, kent 33 % over its phases, martin 13 % |
| minime, an earlier session on another project | no comparable total | unclebob 50 %, joshua 20 %, ford 13 %, kent 9 %, simon 6 %, martin and hughes 3 %; unclebob made 231 calls, about 9 per cycle, each rereading a context of 109 k tokens (median) |

What the numbers say, and what they do not:

* The usage limits of the hello world run came from martin's loop, a bug
  (fixed, below), not from the workflow's design.
* The ping/pong run consumed little; the usage limit it hit was most
  likely spent by the interactive session developing beck, which shares
  the same quota. That session's transcript was lost, so this is not
  verified.
* beck gives every command a fresh agent turn with a small context, the
  lever minime's analysis named for unclebob. Whether it costs less is
  still unmeasured: no problem has run on both.

## Pilot 1, hello world: what went wrong

Iteration `5ca546dd`, branch `hello-world-cli`. Outcome `IterationClosed`,
one component (`greeter`); `hello` and `python -m hello` print
`Hello, world!` and exit with 0.

| | |
|---|---|
| Duration | 2026-11-17 09:55 → 22:44 UTC, mostly waiting for usage limits |
| kent's turns | 12 (interview, list, domain, architecture ×2, interfaces ×2, corrections, routing) |
| unclebob's turns | 7 (5 red, 2 green) |
| martin's turns | 1,232, of which 1,228 refactors in an endless loop (bug, fixed) |
| User decisions | 17 (7 interview questions, 3 approvals, 2 reviews, 3 retries after the usage limit…) |
| Final tests | 11, for a program that prints a fixed sentence |

### 1. The domain is too fine: four requirements for one behaviour

kent split "print `Hello, world!` and exit with 0" into four requirements:
`greeting-text-is-hello-world`, `greeting-is-written-on-stdout`,
`greeting-exits-with-status-zero`, `greeting-writes-nothing-on-stderr`.
One TDD cycle each.

The evidence that they were one behaviour: three of the four reds were
already green at their first run (`RedVerified` with `already_green`); the
first green had implemented them all. Two of those tests were also vacuous,
as martin rightly reported: `main` writes to an injected `StringIO`, so
`capsys` sees nothing, and the stderr test would pass even if `main` wrote
to stderr. Many small tests over one behaviour turn vacuous easily.

* *kent's prompt*: a requirement is an observable behaviour that a
  distinct test can make fail on its own; postconditions that always fail
  together are one requirement; the domain is proportionate to the problem.
* *beck, deterministic*: a red already green because another requirement's
  green of the same unit implemented it signals a requirement that is not
  distinct (the same kind of signal as the name collision between
  domains). beck reports it in the review and proposes that kent merge the
  requirements; beyond a threshold (say, half of the unit's reds) it asks
  before going on.

### 2. A constant modelled as a value object

`Greeting(text=...)` and `the_greeting()` to return a fixed sentence: an
entity and an operation with no invariant of their own, which still
produce interfaces, requirements and tests.

* *kent's prompt*: no entity for a constant or for a value with no
  invariant of its own; an entity exists when it has at least one
  invariant a test can make fail.

### 3. The entry points lost between the shopping list, the domain and the interfaces

The shopping list asked for the `hello` script, `python -m hello` and, under
"Done when", a test that launches the program as a process. Neither the
domain nor the interfaces had them: the architecture went back to kent
(`[project.scripts]` and `__main__.py` were missing), and the process test
came only as a review correction. With no requirement, no cycle writes it.

* *Deterministic traceability*: every item of the shopping list's
  "Deliver" and "Done when" cites the requirement (or the morphism) that
  covers it, and `ValidateDomain` rejects an item that cites none. Launching
  the program (script, `-m`) is the morphism of the entry component, with a
  requirement checked by launching the process: a single test, which also
  covers stdout, stderr and the exit status.

### 4. Generated fitness functions are noise on a single component

Four of the eleven tests are generated fitness functions (two
"is-defined", the declared dependencies, and kent's
`greeter-depends-on-no-system-module`) for a single component that depends
on nothing.

* Generate "is-defined" and "declared-dependencies" as one test,
  parametrised by component, and only when the architecture has more than
  one component. kent's assertions stay.

### 5. martin's endless loop (bug, fixed in `b26ce83`)

On a weak test martin answered `phase: none`, naming in `addressed` the
requirement instead of the test; beck compared it with the test's id, never
closed the item and handed it back: 1,228 turns, about 300 M tokens read
from the cache, the cause of the repeated usage limits.

* *Done*: the item closed is the one beck handed over, not the one the
  agent names; an item handed over more times than the repetition rule
  allows blocks the refactor.
* *Still to do*: a ceiling per component (turns or tokens, from the
  configuration's `budget`, today not enforced) that stops any loop the
  rules do not foresee.

### 6. The usage limit retried three times (bug, fixed in `065e80d`)

A turn that failed on the five-hour limit was retried three times before
asking. beck now asks at once.

### What worked

* The interview: relevant questions (launch form, tooling), a faithful
  shopping list.
* The architecture's restart with a correction: kent added the entry
  points.
* The event-sourced core: unclebob's tests go through the socket
  (`beck send test`), and resuming after the usage limits and after an
  interruption took up martin's turn without losing anything.
* The review corrections: kent planned them, unclebob wrote the process
  test, martin fixed two items (`out or sys.stdout`, a duplicated import),
  and the fixups were folded into the component's commit.

## Pilot 2, ping/pong: where it stopped

Two actors exchanging messages through mailboxes for a number of rounds;
components `messages`, `mailbox`, `rounds`, `actor`, `ping`, `pong`,
`game-runner`.

* `ping` and `pong` were reviewed by martin and refactored.
* The game runner finished its cycles but failed a fitness function: it
  types both mailboxes as `Mailbox[Message]`, so it depends on `messages`,
  which the architecture did not declare. kent's restart added the
  dependency; the generated table kept the old one, and kent proposed a
  second restart only to have it regenerated. Fixed in `52244d7`: a
  restart regenerates the fitness functions of the architecture it changed.
  On the way, two fixes to the fitness checks (`251609b`, `0ed954d`).
* martin's review of the game runner (`runner.py`: 7 lines, one function,
  worth a look) hit the five-hour usage limit at once; then a power cut,
  and the reboot wiped `/tmp/pilots/pingpong`. The run starts again from
  the beginning, after the hello world refinements.

## Where the material is

* The hello world pilot: `~/code/pilots/hello` on the machine that ran it,
  with its event store (`.beck/events/5ca546dd-….db`); it is not in this
  repository, so copy it when changing machine.
* The ping/pong transcripts: `~/.claude/projects/-tmp-pilots-pingpong*`, on
  the same machine.
