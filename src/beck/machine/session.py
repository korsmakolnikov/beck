"""The session's machine: framing, survey and model (README, phases 1 to 3), up to the units' plan.

    SessionStarted → LoadConfig → ConfigLoaded → ClassifyProject → ProjectClassified (or the user's choice)
    → AskInterview (the user, the standard questions) → AskFollowUps (kent, once) → AskInterview … → InterviewCompleted
    → [greenfield] ScaffoldProject → ProposeBranchName (kent) → ConfirmBranchName (the user) → CreateBranch → BranchCreated
    → PrepareToolchain → ToolchainLoaded | ConfirmToolchain (the user) → BringEnvironmentUp (kent as fallback)
    → VerifySuite → SuiteVerified → [brownfield] ReconcileIndex, DelimitCodeArea (kent) → WriteShoppingList (kent)
    → ApproveShoppingList (the user) → WriteDomain (kent) → ValidateDomain → ApproveDomain (the user)
    → WriteArchitecture (kent) → ValidateArchitecture → WriteInterfaces (kent) → CompileInterfaces
    → GenerateFitnessFunctions → WriteFitnessFunctions (kent, the untranslated rules) → RunFitnessFunctions
    → ApproveArchitecture (the user) → BuildIndex → AnchorIds (kent) → CommitModel → ModelCommitted, UnitsPlanned

Every loop between kent and a check follows the repetition rule; a blocked
loop asks the user (ModelBlocked). A revision asked by the user has no limit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from beck import config as beck_config
from beck.dsl.validate import validate_architecture, validate_domain
from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.fitness.generate import detect_stack
from beck.fitness.generate import write as write_fitness
from beck.index import plan as index_plan
from beck.index.anchor import LANGUAGES
from beck.index.build import build as build_index
from beck.index.store import INTEGRATION, connect, data
from beck.machine.closing import workflow
from beck.machine.core import Command, Ctx, Machine, Result, action, ask_routed
from beck.machine.signatures import blocked, signature
from beck.scaffold import LANGUAGES as LAID
from beck.scaffold import ScaffoldError, scaffold
from beck.testrun import run as run_tests
from beck.toolchain import ToolchainError
from beck.toolchain import load as load_toolchain

session = Machine()
S = "session"

STANDARD = [
    ("goal", "What must this iteration deliver?"),
    ("scope", "What is explicitly out of scope?"),
    ("done", "How will you know it is done: what must work, and how will you check it?"),
]
GREENFIELD = [
    ("language", "Which language?"),
    ("name", "The project's name?"),
]


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)


def _last(ctx: Ctx, type_: str) -> Event | None:
    events = ctx.events(types=(type_,))
    return events[-1] if events else None


def _kind(ctx: Ctx) -> str:
    e = _last(ctx, "ProjectClassified")
    return e.payload["kind"] if e else "undetermined"


def _branch(ctx: Ctx) -> str:
    e = _last(ctx, "BranchCreated")
    return e.payload["branch"] if e else "main"


def _config(ctx: Ctx) -> dict[str, Any]:
    e = _last(ctx, "ConfigLoaded")
    return e.payload["config"] if e else dict(beck_config.DEFAULTS)


def _answers(ctx: Ctx) -> list[dict[str, str]]:
    return [{"question": e.payload["question"], "answer": e.payload["answer"]}
            for e in ctx.events(types=("InterviewAnswered",))]


def _answer(ctx: Ctx, key: str) -> str | None:
    found = [e.payload["answer"] for e in ctx.events(types=("InterviewAnswered",)) if e.payload["key"] == key]
    return found[-1] if found else None


def _kent(command: str, context: dict[str, Any], session_name: str, **extra: Any) -> Command:
    return Command(command, "kent", {"session": session_name, "context": context, **extra}, S)


def _ask_user(command: str, **args: Any) -> Command:
    return Command(command, "user", args, S)


def _loop(ctx: Ctx, kinds: tuple[str, ...], since: str) -> str | None:
    """Whether the loop since its last `since` event is blocked by the repetition rule."""
    events = ctx.events(stream=S)
    start = max((e.seq for e in events if e.type == since), default=0)
    sequence = [e.payload["signature"] for e in events if e.seq > start and e.type in kinds]
    return blocked(sequence, *ctx.limits)


def _blocked(step: str, reason: str) -> Command:
    return Command("BlockModel", "beck", {"step": step, "reason": reason}, S)


# --- phase 1: session and framing ---------------------------------------------------------------------


@session.policy("SessionStarted")
def load_config(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("LoadConfig", "beck", {}, S)]


@session.handler("LoadConfig")
def handle_load_config(command: Event, ctx: Ctx) -> list[Result]:
    try:
        return [Result("ConfigLoaded", {"config": beck_config.load(ctx.project)})]
    except beck_config.ConfigError as error:
        return [Result("ConfigInvalid", {"errors": str(error)})]


@session.policy("ConfigLoaded")
def classify(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ClassifyProject", "beck", {}, S)]


CODE = set(LANGUAGES) | {".go", ".rs", ".js", ".ts", ".java", ".rb", ".erl", ".cpp", ".hpp"}


def classify_project(root: Path) -> str:
    """greenfield: nothing but .beck/ and .git/; brownfield: at least one code file; else undetermined."""
    files = [p for p in root.rglob("*") if p.is_file() and not ({".git", ".beck"} & set(p.relative_to(root).parts))]
    if not files:
        return "greenfield"
    if any(p.suffix in CODE for p in files):
        return "brownfield"
    return "undetermined"


@session.handler("ClassifyProject")
def handle_classify(command: Event, ctx: Ctx) -> list[Result]:
    return [Result("ProjectClassified", {"kind": classify_project(ctx.project)})]


@session.policy("ProjectClassified")
def interview_or_ask(event: Event, ctx: Ctx) -> list[Command]:
    if event.payload["kind"] == "undetermined":
        return [_ask_user("ClassifyProject")]
    return _next_question(ctx)


def _questions(ctx: Ctx) -> list[tuple[str, str]]:
    standard = STANDARD + (GREENFIELD if _kind(ctx) == "greenfield" else [])
    follow = _last(ctx, "FollowUpsProposed")
    extra = [(f"follow-up-{i}", q["text"]) for i, q in enumerate(follow.payload["questions"], 1)] if follow else []
    return standard + extra


def _language(ctx: Ctx) -> str | None:
    answer = _answer(ctx, "language")
    return answer.strip().lower() if answer is not None else None


def _next_question(ctx: Ctx) -> list[Command]:
    answered = {e.payload["key"] for e in ctx.events(types=("InterviewAnswered",))}
    language = _language(ctx)
    if language is not None and language not in LAID:   # the answer is free: one beck cannot lay is asked again
        answered.discard("language")
    for key, question in _questions(ctx):
        if key not in answered:
            if key == "language" and language is not None:
                question = (f"beck cannot lay a {language!r} project yet: it lays {', '.join(LAID)}. "
                            f"{question}")
            return [_ask_user("AskInterview", key=key, question=question)]
    if _last(ctx, "FollowUpsProposed") is None:
        return [_kent("AskFollowUps", {"answers": _answers(ctx)}, "interview")]
    return [Command("CompleteInterview", "beck", {}, S)]


@session.policy("InterviewAnswered", "FollowUpsProposed")
def next_question(event: Event, ctx: Ctx) -> list[Command]:
    return _next_question(ctx)


@session.translator("AskFollowUps")
def follow_ups(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("FollowUpsProposed", {"questions": output["result"]["questions"]}, actor="kent")]


@session.handler("CompleteInterview")
def handle_complete(command: Event, ctx: Ctx) -> list[Result]:
    return [Result("InterviewCompleted", {})]


@session.policy("InterviewCompleted")
def scaffold_or_name(event: Event, ctx: Ctx) -> list[Command]:
    if _kind(ctx) == "greenfield":
        return [Command("ScaffoldProject", "beck", {}, S)]
    return [_propose_branch(ctx)]


def _propose_branch(ctx: Ctx) -> Command:
    return _kent("ProposeBranchName", {"format": _config(ctx)["branch_format"], "answers": _answers(ctx)}, "branch")


@session.handler("ScaffoldProject")
def handle_scaffold(command: Event, ctx: Ctx) -> list[Result]:
    language = _language(ctx) or "python"
    name = (_answer(ctx, "name") or ctx.project.name).strip()
    if not (ctx.project / ".git").exists():   # a repository with an empty first commit, for the branch
        _git(ctx.project, "init", "-q")
        _git(ctx.project, "commit", "-q", "--allow-empty", "-m", "start.")
    try:
        scaffold(ctx.project, language, name)
    except ScaffoldError as error:
        return [Result("ScaffoldFailed", {"failure": str(error)})]
    _git(ctx.project, "add", "-A", "--", ".", ":!.beck")
    _git(ctx.project, "commit", "-q", "-m", "chore: scaffold the project")
    return [Result("ProjectScaffolded", {"language": language})]


@session.policy("ScaffoldFailed")
def scaffold_failed(event: Event, ctx: Ctx) -> list[Command]:
    return [ask_routed(f"The project cannot be laid: {event.payload['failure']}", "A greenfield project starts "
                       "from a scaffold.", "scaffold-failed",
                       [action("try-again", "Lay the project again (the user fixed the cause)."),
                        action("go-on", "Go on: the user laid the project by hand.")], S, event.id)]


@session.policy("ProjectScaffolded")
def name_after_scaffold(event: Event, ctx: Ctx) -> list[Command]:
    return [_propose_branch(ctx)]


@session.translator("ProposeBranchName")
def branch_proposed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("BranchNameProposed", {"name": output["result"]["name"]}, actor="kent")]


@session.policy("BranchNameProposed")
def confirm_branch(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ConfirmBranchName", proposed=event.payload["name"])]


@session.policy("BranchNamed")
def create_branch(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CreateBranch", "beck", {"name": event.payload["name"]}, S)]


@session.handler("CreateBranch")
def handle_create_branch(command: Event, ctx: Ctx) -> list[Result]:
    name = command.payload["args"]["name"]
    current = _git(ctx.project, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != name:
        _git(ctx.project, "checkout", "-q", "-b", name)
    Project(ctx.project).name_iteration(ctx.iteration, name)
    return [Result("BranchCreated", {"branch": name})]


# --- phase 2: survey ---------------------------------------------------------------------------------


@session.policy("BranchCreated")
def prepare_toolchain(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("PrepareToolchain", "beck", {}, S)]


@session.handler("PrepareToolchain")
def handle_prepare_toolchain(command: Event, ctx: Ctx) -> list[Result]:
    if (ctx.project / ".beck" / "toolchain.yml").is_file():
        return [Result("ToolchainLoaded", {"language": load_toolchain(ctx.project).language})]
    try:
        return [Result("ToolchainDetected", {"language": detect_stack(ctx.project)})]
    except ValueError:
        return [Result("ToolchainDetected", {"language": "unknown"})]


@session.policy("ToolchainDetected")
def confirm_toolchain(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ConfirmToolchain", detected=event.payload["language"])]


@session.policy("ToolchainConfirmed")
def write_toolchain(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("WriteToolchain", "beck", {"language": event.payload["language"]}, S)]


@session.handler("WriteToolchain")
def handle_write_toolchain(command: Event, ctx: Ctx) -> list[Result]:
    language = command.payload["args"]["language"]
    (ctx.project / ".beck" / "toolchain.yml").write_text(f"toolchain: 1\nlanguage: {language}\n")
    return [Result("ToolchainLoaded", {"language": language})]


@session.policy("ToolchainLoaded")
def bring_up(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("BringEnvironmentUp", "beck", {}, S)]


COMPOSE = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


@session.handler("BringEnvironmentUp")
def handle_bring_up(command: Event, ctx: Ctx) -> list[Result]:
    compose = next((f for f in COMPOSE if (ctx.project / f).is_file()), None)
    if compose is None:
        return [Result("EnvironmentUp", {})]
    done = subprocess.run(["docker", "compose", "-f", compose, "up", "-d"], cwd=ctx.project, capture_output=True,
                          text=True)
    if done.returncode == 0:
        return [Result("EnvironmentUp", {"steps": [f"docker compose -f {compose} up -d"]})]
    return [Result("EnvironmentDown", {"failure": (done.stderr or done.stdout)[-2000:]})]


@session.policy("EnvironmentDown")
def environment_fallback(event: Event, ctx: Ctx) -> list[Command]:
    issued = ctx.store.get(event.causation_id or "")
    if issued.payload["target"] == "kent":   # kent could not either: the user
        return [ask_routed(f"The environment does not come up: {event.payload['failure']}",
                           "Neither beck nor kent could bring it up.", "environment-down",
                           [action("try-again", "Try to bring the environment up again."),
                            action("go-on", "Go on: the user brought the environment up.")], S, event.id)]
    docs = [f for f in ("AGENTS.md", "README.md", "Makefile", *COMPOSE) if (ctx.project / f).is_file()]
    return [_kent("BringEnvironmentUp", {"docs": docs or ["."], "failure": event.payload["failure"]}, "environment")]


@session.translator("BringEnvironmentUp")
def environment_by_kent(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    r = output["result"]
    if r["up"]:
        return [Result("EnvironmentUp", {"steps": r["steps"]}, actor="kent")]
    return [Result("EnvironmentDown", {"failure": "kent: " + "; ".join(r["steps"]) or "kent could not"}, actor="kent")]


@session.policy("EnvironmentUp")
def verify_suite(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifySuite", "beck", {}, S)]


@session.handler("VerifySuite")
def handle_verify_suite(command: Event, ctx: Ctx) -> list[Result]:
    try:
        toolchain = load_toolchain(ctx.project)
    except ToolchainError as error:
        return [Result("SuiteBroken", {"failure": str(error)})]
    roots = toolchain.source_roots(ctx.project)
    result = run_tests(ctx.project, toolchain, [], actor="beck", cause_id=command.id, iteration=ctx.iteration,
                       coverage_of=roots)
    counts = result.event.payload.get("counts", {})
    if result.exit_code == 0 or (not counts.get("tests") and _kind(ctx) == "greenfield"):
        wired = [e for e in ctx.events(types=("CoverageWired",))]
        if not toolchain.coverage and not wired:   # kent finds or adds the tool; the toolchain only wires it
            return [Result("CoverageUnwired", {"language": toolchain.language})]
        if wired and "coverage" in result.event.payload:
            _commit_the_wiring(ctx, wired[-1].payload["tool"])
        return _baseline_coverage(ctx, result.event) + [Result("SuiteVerified", {"tests": counts.get("tests", 0)})]
    return [Result("SuiteBroken", {"failure": result.summary})]


def _commit_the_wiring(ctx: Ctx, tool: str) -> None:
    """The project's changes for the coverage (a dependency), on the feature branch, outside .beck/."""
    git = ["git", "-C", str(ctx.project)]
    subprocess.run([*git, "add", "-A", "--", ".", ":!.beck"], check=True, capture_output=True)
    if subprocess.run([*git, "diff", "--cached", "--quiet"]).returncode != 0:
        subprocess.run([*git, "commit", "-q", "-m", f"build: measure the coverage with {tool}"], check=True,
                       capture_output=True)


@session.policy("CoverageUnwired")
def wire_coverage(event: Event, ctx: Ctx) -> list[Command]:
    manifest = "mix.exs" if event.payload["language"] == "elixir" else "pyproject.toml"
    return [_kent("WireCoverage", {"language": event.payload["language"], "manifest": manifest,
                                   "toolchain": ".beck/toolchain.yml",
                                   "formats": ["coverage.py", "excoveralls", "lcov"]}, "coverage")]


@session.translator("WireCoverage")
def coverage_wired(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("CoverageWired", dict(output["result"]), actor="kent")]


@session.policy("CoverageWired")
def install_dependencies(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("InstallDependencies", "beck", {}, S)]


@session.handler("InstallDependencies")
def handle_install_dependencies(command: Event, ctx: Ctx) -> list[Result]:
    try:
        toolchain = load_toolchain(ctx.project)
    except ToolchainError as error:
        return [Result("SuiteBroken", {"failure": f"kent's toolchain: {error}"})]
    if toolchain.deps:
        argv, env = toolchain.argv(toolchain.deps, [])
        done = subprocess.run(argv, cwd=ctx.project, env={**os.environ, **env}, capture_output=True, text=True)
        if done.returncode != 0:
            return [Result("SuiteBroken", {"failure": f"{' '.join(argv)}: {(done.stderr or done.stdout)[-1500:]}"})]
    return [Result("DependenciesInstalled", {})]


@session.policy("DependenciesInstalled")
def verify_suite_again(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("VerifySuite", "beck", {}, S)]


def _baseline_coverage(ctx: Ctx, run: Event) -> list[Result]:
    """The coverage of the whole suite at the start: the ratchet's first notch."""
    from beck.machine.refactor import _coverage_of
    from beck.measures import totals
    coverage = _coverage_of(ctx, run, ctx.project)
    if coverage is None:
        return []
    return [Result("CoverageMeasured", {"files": totals(coverage, sorted(coverage)), "gaps": {}})]


@session.policy("SuiteBroken")
def suite_broken(event: Event, ctx: Ctx) -> list[Command]:
    return [ask_routed(f"The project's tests do not run green: {event.payload['failure']}",
                       "beck starts from a green suite.", "suite-broken",
                       [action("run-again", "Run the project's suite again (the user fixed it, or it was transient)."),
                        action("go-on", "Go on with the suite as it is: the failing tests stay as they are.")],
                       S, event.id)]


@session.policy("DecisionRouted")
def after_a_session_question(event: Event, ctx: Ctx) -> list[Command]:
    situation, chosen = event.payload["situation"], event.payload["action"]
    if situation == "suite-broken":
        if chosen == "run-again":
            return [Command("VerifySuite", "beck", {}, S)]
        runs = [e for e in ctx.events(types=("TestsRun",)) if not e.payload.get("unit")]
        tests = runs[-1].payload.get("counts", {}).get("tests", 0) if runs else 0
        return [Command("GoOn", "beck", {"as": "SuiteVerified", "payload": {"tests": tests}}, S)]
    if situation == "scaffold-failed":
        if chosen == "try-again":
            return [Command("ScaffoldProject", "beck", {}, S)]
        return [Command("GoOn", "beck", {"as": "ProjectScaffolded",
                                         "payload": {"language": _language(ctx) or "python"}}, S)]
    if situation == "environment-down":
        if chosen == "try-again":
            return [Command("BringEnvironmentUp", "beck", {}, S)]
        return [Command("GoOn", "beck", {"as": "EnvironmentUp", "payload": {}}, S)]
    return []


def index_path(ctx: Ctx) -> Path:
    return ctx.project / ".beck" / "index.db"


@session.policy("SuiteVerified")
def survey(event: Event, ctx: Ctx) -> list[Command]:
    if _kind(ctx) == "greenfield":
        return [_write_shopping_list(ctx, None)]
    if index_path(ctx).is_file():
        return [Command("ReconcileIndex", "beck", {}, S)]
    return [_kent("DelimitCodeArea", {"answers": _answers(ctx)}, "survey")]


@session.handler("ReconcileIndex")
def handle_reconcile(command: Event, ctx: Ctx) -> list[Result]:
    """The files that hold anchored symbols and changed since the last index: the DSLs may have drifted."""
    old = connect(index_path(ctx))
    hashes = {r["path"]: r["hash"] for r in old.execute("SELECT path, hash FROM file")}
    anchored: dict[str, list[str]] = {}
    for r in old.execute("SELECT e.src AS id, n.data FROM edge e JOIN node n ON n.id = e.dst "
                         "WHERE e.kind = 'anchored_to'"):
        anchored.setdefault(data(r)["file"], []).append(r["id"])
    old.close()
    import hashlib
    drift = []
    for file, ids in sorted(anchored.items()):
        path = ctx.project / file
        now = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if now != hashes.get(file):
            drift.append({"file": file, "ids": sorted(set(ids))})
    return [Result("DslDriftDetected", {"drift": drift})] if drift else [Result("IndexReconciled", {})]


@session.policy("DslDriftDetected")
def propose_dsl_sync(event: Event, ctx: Ctx) -> list[Command]:
    return [_kent("ProposeDslSync", {"drift": event.payload["drift"]}, "dsl-sync")]


@session.translator("ProposeDslSync")
def dsl_sync_proposed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("DslSyncProposed", {"summary": output["result"]["summary"]}, actor="kent")]


@session.policy("DslSyncProposed")
def approve_dsl_sync(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ApproveDslSync", summary=event.payload["summary"])]


@session.policy("DslSyncApproved", "DslSyncRejected")
def rebuild_after_sync(event: Event, ctx: Ctx) -> list[Command]:
    commands = [Command("DiscardDslSync", "beck", {}, S)] if event.type == "DslSyncRejected" else []
    return commands + [Command("RebuildIndex", "beck", {}, S)]


@session.handler("DiscardDslSync")
def handle_discard_dsl_sync(command: Event, ctx: Ctx) -> list[Result]:
    for name in ("domain.yaml", "architecture.yaml"):
        _git(ctx.project, "checkout", "--", f".beck/{name}", check=False)
    return []


@session.handler("RebuildIndex")
def handle_rebuild_index(command: Event, ctx: Ctx) -> list[Result]:
    domain, arch = _dsls(ctx)
    conn, _ = build_index(ctx.project, domain, arch, index_path(ctx))
    if conn is not None:
        conn.close()
    return [Result("IndexRebuilt", {})]


@session.policy("IndexReconciled", "IndexRebuilt")
def delimit(event: Event, ctx: Ctx) -> list[Command]:
    return [_kent("DelimitCodeArea", {"answers": _answers(ctx)}, "survey")]


@session.translator("DelimitCodeArea")
def area_delimited(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("CodeAreaDelimited", {"paths": output["result"]["paths"]}, actor="kent")]


def shopping_list(ctx: Ctx) -> str:
    return f".beck/shopping_lists/{_branch(ctx).replace('/', '-')}.md"


def _write_shopping_list(ctx: Ctx, revision: str | None) -> Command:
    context: dict[str, Any] = {"file": shopping_list(ctx), "answers": _answers(ctx)}
    area = _last(ctx, "CodeAreaDelimited")
    if area:
        context["area"] = area.payload["paths"]
    if revision:
        context["revision"] = revision
    (ctx.project / ".beck" / "shopping_lists").mkdir(parents=True, exist_ok=True)
    return _kent("WriteShoppingList", context, "shopping-list")


@session.policy("CodeAreaDelimited")
def shopping(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_shopping_list(ctx, None)]


@session.translator("WriteShoppingList")
def shopping_list_written(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    # a turn resumed with the user's answer carries no context: the file is the iteration's, either way
    return [Result("ShoppingListWritten", {"file": shopping_list(ctx)}, actor="kent")]


@session.policy("ShoppingListWritten")
def approve_shopping_list(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ApproveShoppingList", file=event.payload["file"])]


@session.policy("ShoppingListRevised")
def revise_shopping_list(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_shopping_list(ctx, event.payload["instructions"])]


# --- phase 3: the model ------------------------------------------------------------------------------


def _dsls(ctx: Ctx) -> tuple[Any, Any]:
    def read(name: str) -> Any:
        path = ctx.project / ".beck" / name
        try:
            return yaml.safe_load(path.read_text()) if path.is_file() else None
        except yaml.YAMLError as error:
            return {"__yaml_error__": str(error)}
    return read("domain.yaml"), read("architecture.yaml")


def _write_domain(ctx: Ctx, errors: list[str] | None, revision: str | None) -> Command:
    context: dict[str, Any] = {"file": ".beck/domain.yaml", "shopping_list": shopping_list(ctx)}
    if errors:
        context["errors"] = errors
    if revision:
        context["revision"] = revision
    return _kent("WriteDomain", context, "domain")


@session.policy("ShoppingListApproved")
def write_domain(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_domain(ctx, None, None)]


@session.translator("WriteDomain")
def domain_written(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("DomainWritten", {}, actor="kent")]


@session.policy("DomainWritten")
def validate_the_domain(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ValidateDomain", "beck", {}, S)]


def _errors(doc: Any, problems: list[Any]) -> list[str]:
    if isinstance(doc, dict) and "__yaml_error__" in doc:
        return [f"not valid YAML: {doc['__yaml_error__']}"]
    if doc is None:
        return ["the file was not written"]
    return [str(p) for p in problems]


@session.handler("ValidateDomain")
def handle_validate_domain(command: Event, ctx: Ctx) -> list[Result]:
    domain, _ = _dsls(ctx)
    errors = _errors(domain, validate_domain(domain) if isinstance(domain, dict) and "__yaml_error__" not in domain
                     else [])
    if errors:
        return [Result("DomainInvalid", {"errors": errors, "signature": signature(errors)})]
    return [Result("DomainValidated", {})]


@session.policy("DomainInvalid")
def domain_again(event: Event, ctx: Ctx) -> list[Command]:
    why = _loop(ctx, ("DomainInvalid",), "ShoppingListApproved")
    if why:
        return [_blocked("domain", why)]
    return [_write_domain(ctx, event.payload["errors"], None)]


@session.policy("DomainValidated")
def approve_domain(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ApproveDomain", file=".beck/domain.yaml")]


@session.policy("DomainRevised")
def revise_domain(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_domain(ctx, None, event.payload["instructions"])]


def _write_architecture(ctx: Ctx, errors: list[str] | None, revision: str | None) -> Command:
    context: dict[str, Any] = {"file": ".beck/architecture.yaml", "domain": ".beck/domain.yaml",
                               "shopping_list": shopping_list(ctx)}
    if errors:
        context["errors"] = errors
    if revision:
        context["revision"] = revision
    return _kent("WriteArchitecture", context, "architecture")


@session.policy("DomainApproved")
def write_architecture(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_architecture(ctx, None, None)]


@session.translator("WriteArchitecture")
def architecture_written(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("ArchitectureWritten", {}, actor="kent")]


@session.policy("ArchitectureWritten")
def validate_the_architecture(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ValidateArchitecture", "beck", {}, S)]


@session.handler("ValidateArchitecture")
def handle_validate_architecture(command: Event, ctx: Ctx) -> list[Result]:
    domain, arch = _dsls(ctx)
    readable = isinstance(arch, dict) and "__yaml_error__" not in arch
    errors = _errors(arch, validate_architecture(arch, domain) if readable else [])
    if errors:
        return [Result("ArchitectureInvalid", {"errors": errors, "signature": signature(errors)})]
    return [Result("ArchitectureValidated", {})]


@session.policy("ArchitectureInvalid")
def architecture_again(event: Event, ctx: Ctx) -> list[Command]:
    why = _loop(ctx, ("ArchitectureInvalid",), "DomainApproved")
    if why:
        return [_blocked("architecture", why)]
    return [_write_architecture(ctx, event.payload["errors"], None)]


def _write_interfaces(errors: list[str] | None) -> Command:
    context: dict[str, Any] = {"architecture": ".beck/architecture.yaml", "domain": ".beck/domain.yaml"}
    if errors:
        context["errors"] = errors
    return _kent("WriteInterfaces", context, "interfaces")


@session.policy("ArchitectureValidated")
def write_interfaces(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_interfaces(None)]


@session.translator("WriteInterfaces")
def interfaces_written(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("InterfacesWritten", {"files": output["result"]["files"]}, actor="kent")]


@session.policy("InterfacesWritten")
def compile_interfaces(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("CompileInterfaces", "beck", {}, S)]


@session.handler("CompileInterfaces")
def handle_compile(command: Event, ctx: Ctx) -> list[Result]:
    toolchain = load_toolchain(ctx.project)
    if not toolchain.build:
        return [Result("InterfacesCompiled", {})]
    argv, env = toolchain.argv(toolchain.build, [])
    import os
    done = subprocess.run(argv, cwd=ctx.project, capture_output=True, text=True, env={**os.environ, **env})
    if done.returncode == 0:
        return [Result("InterfacesCompiled", {})]
    lines = [line for line in (done.stdout + done.stderr).splitlines() if line.strip()][-30:]
    return [Result("InterfacesBroken", {"errors": lines or [f"exit {done.returncode}"], "signature": signature(lines)})]


@session.policy("InterfacesBroken", "FitnessFunctionsFailed")
def interfaces_again(event: Event, ctx: Ctx) -> list[Command]:
    why = _loop(ctx, ("InterfacesBroken", "FitnessFunctionsFailed"), "ArchitectureValidated")
    if why:
        return [_blocked("interfaces", why)]
    return [_write_interfaces(event.payload["errors"])]


@session.policy("InterfacesCompiled")
def generate_fitness(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("GenerateFitnessFunctions", "beck", {}, S)]


@session.handler("GenerateFitnessFunctions")
def handle_generate_fitness(command: Event, ctx: Ctx) -> list[Result]:
    _, arch = _dsls(ctx)
    written = write_fitness(ctx.project, arch)
    return [Result("FitnessFunctionsGenerated", {"files": written.files, "untranslated": [
        {"id": i, "rule": r, "file": written.rules_file} for i, r in written.pending_rules.items()]})]


@session.policy("FitnessFunctionsGenerated")
def write_or_run_fitness(event: Event, ctx: Ctx) -> list[Command]:
    pending = event.payload["untranslated"]
    if pending:
        return [_kent("WriteFitnessFunctions", {"file": pending[0]["file"],
                                                "rules": [{"id": p["id"], "rule": p["rule"]} for p in pending]},
                      "fitness")]
    return [Command("RunFitnessFunctions", "beck", {}, S)]


@session.translator("WriteFitnessFunctions")
def fitness_written(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("FitnessFunctionsWritten", {}, actor="kent")]


@session.policy("FitnessFunctionsWritten")
def run_fitness(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("RunFitnessFunctions", "beck", {}, S)]


@session.handler("RunFitnessFunctions")
def handle_run_fitness(command: Event, ctx: Ctx) -> list[Result]:
    dirs = [d for d in ("tests/fitness", "test/fitness") if (ctx.project / d).is_dir()]
    if not dirs:
        return [Result("FitnessFunctionsPassed", {})]
    result = run_tests(ctx.project, load_toolchain(ctx.project), dirs, actor="beck", cause_id=command.id,
                       iteration=ctx.iteration)
    if result.exit_code == 0:
        return [Result("FitnessFunctionsPassed", {})]
    errors = result.summary.splitlines()[:40]
    return [Result("FitnessFunctionsFailed", {"errors": errors, "signature": signature(errors)})]


@session.policy("FitnessFunctionsPassed")
def approve_architecture(event: Event, ctx: Ctx) -> list[Command]:
    return [_ask_user("ApproveArchitecture", file=".beck/architecture.yaml")]


@session.policy("ArchitectureRevised")
def revise_architecture(event: Event, ctx: Ctx) -> list[Command]:
    return [_write_architecture(ctx, None, event.payload["instructions"])]


@session.policy("ArchitectureApproved", "IdsAnchored")
def build_the_index(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("BuildIndex", "beck", {}, S)]


@session.handler("BuildIndex")
def handle_build_index(command: Event, ctx: Ctx) -> list[Result]:
    domain, arch = _dsls(ctx)
    conn, report = build_index(ctx.project, domain, arch, index_path(ctx))
    if conn is not None:
        conn.close()
    return [Result("IndexBuilt", {"unanchored": report.unanchored,
                                  "unknown": [f"{m.path}:{m.line}: {m.id}" for m in report.unknown]})]


@session.policy("IndexBuilt")
def anchor_or_commit(event: Event, ctx: Ctx) -> list[Command]:
    if event.payload["unanchored"]:
        attempts = len(ctx.events(types=("IndexBuilt",)))
        if attempts > ctx.limits[0]:
            return [_blocked("anchoring", f"{', '.join(event.payload['unanchored'])} stay unanchored")]
        return [_kent("AnchorIds", {"ids": event.payload["unanchored"]}, "anchoring")]
    return [Command("CommitModel", "beck", {}, S)]


@session.translator("AnchorIds")
def anchored(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("IdsAnchored", {}, actor="kent")]


def _boundary(conn: sqlite3.Connection, component: str) -> str | None:
    row = conn.execute("SELECT data FROM node WHERE id = ?", (component,)).fetchone()
    return json.loads(row["data"]).get("boundary") if row else None


def plan_units(conn: sqlite3.Connection) -> dict[str, Any]:
    """UnitsPlanned's payload from the index: the units in landing order, their groups, dependencies, sharing."""
    units = index_plan.units(conn)
    exclusive = index_plan.exclusive(conn)
    out = []
    for u in units:
        groups = [{"id": g.id, "clauses": list(g.clauses)} for g in index_plan.groups(conn, u)]
        deps = [r["dst"] for r in conn.execute(
            "SELECT e.dst FROM edge e JOIN node n ON n.id = e.dst WHERE e.src = ? AND e.kind = 'depends_on' "
            "AND n.kind = 'component'", (u,))]
        realizes = [r["dst"] for r in conn.execute("SELECT dst FROM edge WHERE src = ? AND kind = 'realizes'", (u,))]
        entry: dict[str, Any] = {"id": u, "groups": groups, "depends_on": deps, "realizes": realizes,
                                 "exclusive_with": sorted(o for pair in exclusive if u in pair for o in pair if o != u)}
        concrete = [d for d in deps if _boundary(conn, d) != "interface"]
        if concrete:   # the unit's tests use the real modules: it starts once they have landed
            entry["waits_for"] = concrete
        if u == INTEGRATION:
            entry["integration"] = True
            entry["depends_on"] = [x for x in units if x != INTEGRATION]
        out.append(entry)
    return {"units": out}


@session.handler("CommitModel")
def handle_commit_model(command: Event, ctx: Ctx) -> list[Result]:
    versioned = _config(ctx)["versioned"]
    excluded = [] if versioned else [":!.beck"]
    _git(ctx.project, "add", "-A", "--", ".", *excluded)
    if _git(ctx.project, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(ctx.project, "commit", "-q", "-m", f"feat: the interfaces and the fitness functions of {_branch(ctx)}")
    conn = connect(index_path(ctx))
    try:
        planned = plan_units(conn)
    finally:
        conn.close()
    sha = _git(ctx.project, "rev-parse", "HEAD").stdout.strip()
    return [Result("ModelCommitted", {"sha": sha}), Result("UnitsPlanned", planned)]


@session.handler("BlockModel")
def handle_block_model(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("ModelBlocked", {"step": a["step"], "reason": a["reason"]})]


@session.policy("ModelBlocked")
def ask_about_the_model(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return [_ask_user("Ask", question={"text": f"kent's {p['step']} is blocked: {p['reason']}",
                                       "why": "The deterministic check keeps refusing the same output."},
                      turn=event.id)]


# --- the workspace of a real session: the index decides ------------------------------------------------


@dataclass
class IndexWorkspace:
    """Where each unit lives (its UnitStarted), what it may touch and what its sessions get (the index)."""
    store: EventStore
    project: Path
    _cache: dict[str, Path] = field(default_factory=dict)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.project / ".beck" / "index.db")

    def worktree(self, unit: str) -> Path:
        started = list(self.store.read(stream=f"unit:{unit}", types_=("UnitStarted", "UnitReopened")))
        if not started:
            raise KeyError(f"unit {unit!r} has no worktree yet")
        return Path(started[-1].payload["worktree"])

    def scope(self, unit: str) -> list[str]:
        toolchain = load_toolchain(self.project)
        conn = self._conn()
        try:
            if unit == INTEGRATION:
                rows = conn.execute("SELECT data FROM node WHERE kind = 'component' AND id != ?", (INTEGRATION,))
            else:
                rows = conn.execute("SELECT data FROM node WHERE id = ?", (unit,))
            namespaces = [json.loads(r["data"])["namespace"] for r in rows]
        finally:
            conn.close()
        paths: list[str] = []
        for ns in namespaces:
            paths += _component_paths(toolchain.language, ns)
        return paths

    def group_context(self, unit: str, group: str) -> dict[str, Any]:
        conn = self._conn()
        try:
            return index_plan.context(conn, unit, group)
        finally:
            conn.close()

    def clause_text(self, clause: str) -> str:
        conn = self._conn()
        try:
            row = conn.execute("SELECT text FROM node WHERE id = ?", (clause,)).fetchone()
            return row["text"] if row else clause
        finally:
            conn.close()


def _component_paths(language: str, namespace: list[str]) -> list[str]:
    if language == "python":   # a component is a package or a module
        module = [*namespace[:-1], namespace[-1] + ".py"]
        return [str(Path("src", *namespace)), str(Path("src", *module)), str(Path(*namespace)), str(Path(*module))]
    return [str(Path("lib", *namespace)), str(Path("lib", *namespace[:-1], namespace[-1] + ".ex"))]


from beck.machine.audit import audit  # noqa: E402  (smart_audit observes the whole workflow)
from beck.machine.flaky import flaky  # noqa: E402  (and the detector reads every run)

beck_machine = workflow.merge(session).merge(audit).merge(flaky)
