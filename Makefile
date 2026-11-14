# beck's task runner. Every Python target runs in beck's uv environment.
.PHONY: help deps sync install uninstall reinstall test lint format typecheck check build clean

help:
	@echo "make deps       system packages: Python, uv, git, make; Elixir for its tests and projects"
	@echo "make sync       beck's environment: the dependencies and the development tools (uv sync)"
	@echo "make install    the beck command on the PATH, editable: it runs this source (uv tool)"
	@echo "make uninstall  remove the beck command"
	@echo "make test       the tests (pytest)"
	@echo "make lint       ruff"
	@echo "make format     ruff format"
	@echo "make typecheck  mypy"
	@echo "make check      lint, typecheck, test"
	@echo "make build      the wheel and the sdist, in dist/"
	@echo "make clean      the caches and dist/"

# System packages, on Arch Linux and Ubuntu. Elixir is optional: beck's tests skip what needs it.
deps:
	@. /etc/os-release; case "$$ID $$ID_LIKE" in \
		*arch*) sudo pacman -S --needed --noconfirm python uv git make elixir ;; \
		*ubuntu*|*debian*) sudo apt-get update && sudo apt-get install -y python3 python3-venv git make curl elixir \
			&& (command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh) ;; \
		*) echo "Unsupported distribution: install Python 3.12+, uv, git, make (and Elixir) by hand." >&2; exit 1 ;; \
	esac
	@command -v claude >/dev/null || echo "beck drives its agents through Claude Code: install it (https://claude.com/claude-code)"
	@command -v gh >/dev/null || echo "argyris opens its pull requests with gh: install the GitHub CLI"

sync:
	uv sync

install:
	uv tool install --editable --force .
	@echo "beck: $$(command -v beck || echo 'not on the PATH: run uv tool update-shell')"

uninstall:
	uv tool uninstall beck

reinstall: uninstall install

test: sync
	uv run pytest -q

lint: sync
	uv run ruff check src tests

format: sync
	uv run ruff format src tests

typecheck: sync
	uv run mypy

check: lint typecheck test

build:
	uv build

clean:
	rm -rf dist .pytest_cache .ruff_cache .mypy_cache
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
