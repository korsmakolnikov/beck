# beck's DSLs

Status: **approved** (2026-11-13), version 1 of both DSLs. This file is the manifesto of the two
DSLs beck works from; the README records the decisions that led here.

Two YAML files in `.beck/`, one system of ids:

| File | Says | Written by | Schema |
|---|---|---|---|
| `.beck/domain.yaml` | what the domain is and must guarantee | kent, domain state | [`schemas/domain.schema.yaml`](../src/beck/dsl/schemas/domain.schema.yaml) |
| `.beck/architecture.yaml` | which components implement those guarantees, how they depend on each other, and the rules the structure obeys | kent, architecture state | [`schemas/architecture.schema.yaml`](../src/beck/dsl/schemas/architecture.schema.yaml) |

The domain names no component and reads no code. The architecture names the
domain's ids, never restates them. The tests cite the ids (`spec: <id>`).
Both files are design decisions: kent proposes their changes, the user
approves them. The index (`.beck/index.db`) is derived from them and from
the code, never the other way round.

A complete example, valid against both schemas, lives in
[`examples/bookmarks/`](examples/bookmarks/); the snippets below are
abridged from it.

## Why two DSLs and not ADL plus a spec

* They are a **graph**, not prose: beck cuts each agent's context out of it
  with a query, and plans the units, the groups and the cycles from it
  without an LLM. Every element and every clause that a test must prove has
  an id; every edge the graph needs is written, never inferred.
* They are **validated twice**, deterministically: the schema checks the
  shape, the semantic checks (below) the ids and the references between the
  two files. An output that fails either goes back to kent with the errors.
* The architecture keeps ADL's features (the kinds of `DEFINE`, `ASSERT`,
  `FOREACH`), but its assertions are structured, so the fitness functions
  are generated, not written; the free-text assertion is the only one an
  agent translates.
* The components map is gone: `implements` lives in the architecture, the
  rest is the index's job.

## The domain (`.beck/domain.yaml`)

Derived from minime's spec version 2, with two changes the graph needs:
every clause a test proves has an id (the `post` clauses had none), and
every operation lists the entities it `uses`. A constraint lists the
entities it is `about`.

```yaml
domain: 1
system: bookmarks

entities:
  - name: Bookmark                   # PascalCase, unique across entities, events and relations
    kind: entity                     # entity | value_object
    description: A web page a user saved, to be tagged and archived.
    fields:
      - {name: id, type: integer, required: true}
      - {name: status, type: "one of open, archived"}
    invariants:
      - id: archived-bookmark-has-a-tag
        rule: An archived bookmark carries at least one tag.

events:
  - name: BookmarkTagged
    description: A bookmark received one or more tags.
    fields:
      - {name: bookmark_id, type: integer}

relations:
  - from: Bookmark
    to: Tag
    kind: many_to_many               # one_to_one | one_to_many | many_to_one | many_to_many
    description: A bookmark is tagged with any number of tags.
    rules:
      - id: tag-belongs-to-the-same-user
        rule: A bookmark is tagged only with tags of its user.

constraints:
  - id: tagging-is-atomic
    about: [Bookmark, Tag]
    rule: Either every tag of a request is applied or none is.

operations:
  - id: tag-bookmark
    description: A user tags one of their bookmarks.
    uses: [Bookmark, Tag, TagName]
    pre:
      - The bookmark exists.
    post:
      - id: tagged-bookmark-carries-the-requested-tags
        rule: The bookmark carries exactly the requested tags.
    errors:
      - id: tag-missing-bookmark
        when: The bookmark does not exist.
        outcome: Nothing changes and the user learns the bookmark is missing.
    emits: [BookmarkTagged]

properties:
  - id: tagging-twice-equals-tagging-once
    operations: [tag-bookmark]       # one: the operation's group; several or none: the model group
    kind: idempotence
    forall:
      - {name: bookmark, generator: an existing bookmark}
      - {name: tags, generator: a non-empty list of valid tags of its user}
    holds: Tagging twice with the same tags leaves the same state as tagging once.
```

* `id`s are kebab-case and unique across both files.
* Every `rule`, `pre`, `when`, `outcome`, `holds` and `generator` is one
  C2-level English sentence a test can check: no "should", no "for now", no
  TODO, no code. An error's `outcome` says what the domain does, not what a
  function returns.
* A `pre` has no id: it is not proved by a test of its own; the error that
  covers its violation is.
* The **clauses** are what a test proves, one cycle each: an invariant, a
  relation rule, a constraint, a `post`, an error, a property.

### Property kinds

| `kind` | It states | Typical test |
|---|---|---|
| `invariant` | Something always true of the result or the state. | Generate inputs, run, assert the invariant. |
| `roundtrip` | Doing then undoing gives the input back. | `decode(encode(x)) == x` |
| `idempotence` | Applying twice equals applying once. | `f(f(x)) == f(x)` |
| `metamorphic` | How the output changes when the input changes in a known way. | Compare `f(x)` with `f(transform(x))`. |
| `model` | The component behaves like a simpler reference model. | Run the same commands on both, compare. |
| `oracle` | The result equals that of a trusted implementation. | `f(x) == trusted(x)` |

## The architecture (`.beck/architecture.yaml`)

```yaml
architecture: 1
system: {id: bookmarks, name: Bookmarks, physical: bookmarks}
description: Bookmarks saved by users, tagged and archived.
requires: [AdlFitness ExUnit helper]

elements:
  - kind: domain                     # domain | subdomain | component | library | service
    id: tagging
    name: Tagging
    physical: tagging
    elements:
      - kind: component
        id: bookmark-tagger
        name: Bookmark Tagger
        physical: bookmark_tagger
        boundary: interface          # interface | concrete (components only)
        depends_on: [tag-store]
        implements: [tag-bookmark, archived-bookmark-has-a-tag, tag-belongs-to-the-same-user]
      - kind: component
        id: tag-store
        name: Tag Store
        physical: tag_store
        boundary: interface
        implements: [tag-name-is-1-to-40-characters]

constants:
  - {id: max-tags, name: MAX_TAGS, value: 20}

assertions:
  - id: tagging-has-no-dependency-on-archive
    no_dependency: {from: tagging, to: archive}
  - id: classes-only-in-components
    contained_in: {what: classes, within: [component, subdomain]}
  - id: tagging-components-reach-storage-only-through-tag-store
    foreach:
      over: component
      within: tagging
      as: c
      except: [tag-store]
      assert: {no_dependency: {from: $c, to: storage}}
  - id: tagging-is-auditable
    rule: Every write of a tagging component is recorded in the audit log.

integration:
  implements: [tagging-is-atomic]
```

### Elements

* The tree mirrors ADL's `DEFINE` nesting; the path of `physical` names from
  the system down gives the namespace (Elixir `Bookmarks.Tagging.BookmarkTagger`,
  Python `bookmarks.tagging.bookmark_tagger`, C `src/tagging/bookmark_tagger/`).
* Only a `component` (a leaf) has `boundary`, `supervised`, `depends_on`
  and `implements`; only a `library` has `package` and `version`.
  A `service` is a separately deployed unit; a `library` a shared or
  third-party one, which may be a `depends_on` target and implements nothing.
* **`boundary: interface`**: callers depend on an abstraction (an Elixir
  behaviour, a Python `Protocol`); a caller's test may replace it with a
  double, and one contract test runs on the double and on the real
  implementation. **`concrete`**: callers are tested with the real module.
* **The shared files are the model's, not the units'.** The dependency
  manifest (`mix.exs`, `pyproject.toml`) and the supervision tree are
  structure: kent writes them with the interfaces, from the architecture,
  before any unit starts. An external `library` names its `package` (and
  its `version`) for the manifest, and lives outside the system: its
  namespace is its `physical` name alone (`jason`, in Elixir `Jason`), it
  gets no existence case and `contained_in` does not count it; a `supervised: true` component is a
  child of the supervision tree. A unit that still needs to change a file
  shared with other components requests an architecture change.
* **`depends_on`** is the complete list of what the component may call. It
  orders the landings, cuts the signatures handed to unclebob, and is
  enforced: beck generates one implicit assertion per component
  (`<id>-declared-dependencies`, "depends only on its `depends_on`").

### Implementation

* **An operation** is implemented by the component that offers it, and its
  `post`, its errors and the properties that name only that operation come
  with it: they are never listed.
* **Every other clause** (an invariant, a relation rule, a constraint, a
  property over several operations or none) is listed in exactly one
  `implements`: the component that enforces it, or `integration` when it spans
  components and no single component can hold it.
* Every operation and every clause ends implemented exactly once.

### Assertions

Each assertion has an id and exactly one of these forms:

| Form | ADL | Fitness function |
|---|---|---|
| `no_dependency: {from, to}` | `ASSERT(from has NO DEPENDENCY on to)` | generated |
| `only_depends_on: {from, to: [...]}` | `ASSERT(from only DEPENDS ON {…})` | generated |
| `contained_in: {what: classes, within: [kinds]}` | `ASSERT(CLASSES are only CONTAINED within …)` | generated |
| `foreach: {over, within, as, except, assert}` | `FOREACH $c IN … DO ASSERT(…) END` | generated |
| `rule: <sentence>` | a free `ASSERT` | written by kent (`WriteFitnessFunctions`) |

* `from`, `to` and `within` name element ids; inside a `foreach`, `$<as>`
  names the element of the iteration.
* The generator names namespaces, never single modules. martin follows the
  architecture to the letter: no module is moved or split; a change of
  structure is an architecture change (`ArchitectureChangeRequested`).

### The generated fitness functions

`beck fitness generate` expands the assertions into a table of concrete
cases and writes it as one test file, with a helper beside it that reads the
dependencies statically (Python's `import`s with `ast`, the `:imports` chunk
of Elixir's BEAM files). Both files are rewritten at every generation and
never edited.

| Stack | Table | Helper | Kent's rules |
|---|---|---|---|
| Python | `tests/fitness/test_architecture.py` | `tests/fitness/beck_fitness.py` | `tests/fitness/test_architecture_rules.py` |
| Elixir | `test/fitness/architecture_test.exs` | `test/fitness/support/beck_fitness.ex` | `test/fitness/architecture_rules_test.exs` |

* Besides the assertions, the table holds `<id>-is-defined` for every
  element (no assertion passes on an empty tree) and
  `<id>-declared-dependencies` for every component. An assertion id that
  clashes with a generated one is refused.
* A `foreach` becomes one case per element, named `<id>[<element>]`.
* The fitness functions live in a path of their own (`tests/fitness/`,
  `test/fitness/`), apart from the other tests, so that a `CODEOWNERS` rule
  can make no change to them go unnoticed.
* In Elixir, beck adds `test/fitness/support` to the test env's
  `elixirc_paths` of `mix.exs` itself; it reports only a `mix.exs` whose
  shape it does not recognise.
* `contained_in` counts a `library` as a leaf, like a component (a library
  is a component: a black box), and allows
  the modules of the system and of the containers themselves (a Python
  package's `__init__`, an Elixir namespace module).
* `only_depends_on` judges only the modules of the system: the standard
  library and the third parties are free.
* A `rule` is reported as untranslated until kent's rules file cites it
  (`spec: <id>`).
* C is not supported yet.

## Validation

`beck validate domain` and `beck validate architecture` run, in order:

1. **Schema**: the file against its schema (JSON Schema 2020-12, written in
   YAML).
2. **Semantic checks**, which a schema cannot express:
   * domain: ids unique; every name in `uses`, `about`, `from`, `to`,
     `emits` and `operations` exists; `fields` names unique within their
     owner;
   * architecture: ids unique across both files; every `depends_on`, `from`,
     `to`, `within` and `except` names an element of the right kind; no
     cycle in `depends_on`; every `implements` names a domain id that is not
     an operation's `post`, error or single-operation property; every
     operation and every clause implemented exactly once; `$var` used only
     inside its `foreach`.

The architecture's checks need the domain, so it is validated only once the
domain is approved.

## From the DSLs to the graph

`BuildIndex` reads the two files and the code (tree-sitter) and writes two
tables:

```
 node(id, kind, file, hash)
   kind: component | library | container | operation | entity | event | clause | symbol | test
 edge(src, dst, kind)
   contains     container → element               (architecture: the tree)
   depends_on   component → component | library     (architecture)
   implements     component → operation | clause      (architecture, or inherited)
   uses         operation → entity                  (domain)
   binds        clause    → operation | entity      (domain: where the clause sits;
                                                     about, from and to for the others)
   anchored_to  operation | clause → symbol         (the interfaces' docs: spec: <id>)
   tests        test      → clause                  (the tests' comments: spec: <id>)
```

The integration unit is the node `@integration` (`@` keeps it out of the
DSL's ids). A `spec: <id>` marker anchors to the innermost definition that
holds it, or, when it stands in a comment, an attribute (`@doc`) or a
decorator right before a definition, to that definition; in a test file it
marks a test. A file that holds symbols of several components is **shared**:
`beck index plan` lists these files, and the scheduler never runs in
parallel two units that share one (it matters mostly in brownfield). An
operation that no symbol cites goes to kent (`AnchorIds`),
and a marker citing an id the DSLs lack is reported; everything else is
deterministic.

## From the graph to the sessions

```
 graph
  └─ unit  = a component (+ the integration unit)   → one worktree, then martin
      └─ group = an owner node with its clauses       → one unclebob session
          └─ cycle = one clause                       → one test, one red, one green
```

* **Units**: the components in topological order of `depends_on` (the
  landing order); they are developed in parallel, trusting the interfaces.
  The integration unit comes last.
* **Groups**, inside a unit, from the particular to the general: one per
  entity whose clauses the component implements, then one per operation (its
  `post`, errors, single-operation properties), then one `model` group for
  the properties over several operations.
* **The context of a group**, one recursive query from its owner node: the
  text of its clauses; the types of the entities it uses; the signature of
  the operation; the signatures of the dependencies' interfaces; the paths of
  the source and of the tests. Nothing else.
