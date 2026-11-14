# beck

beck is the deterministic orchestrator of the architecture-first workflow:
the human is the architect, beck the engineer that implements. The README
holds the design (phases, policies, measures) and the TODOs.

## Working on this repository

* **Language**: talk to the user in Italian; code, comments, agent prompts
  (`src/beck/agents/prompts/`) and docs in C2-level English.
* **Checks**: `make check` (ruff, mypy, pytest) must pass before every
  commit; CI runs the same target on every push and pull request.
* **Tests first**: every behaviour comes with its test; the tests drive
  real git repositories and real test runs, with scripted agents.
* **Commits**: Conventional Commits; never push without being asked.
* **Branches**: never delete a branch, local or remote, unless told to
  delete it; an abandoned branch stays.
* **Public repository**: never write the names of the user's work systems,
  tools or business domain into it (files, tests, commits, pull requests);
  use neutral names (`shop`, `app`, the `bookmarks` domain).
* **Independence**: beck depends on nothing of minime.
