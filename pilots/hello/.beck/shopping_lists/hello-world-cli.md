# hello-world-cli

## Goal

A Python command-line program, `hello`, that prints `Hello, world!` on
stdout and exits with status 0. Nothing else.

## Delivers

- Running `hello` (console script, `[project.scripts]` entry point in
  `pyproject.toml`) writes exactly `Hello, world!\n` on stdout and exits 0.
- Running `python -m hello` (`src/hello/__main__.py`) behaves the same.
- Nothing is written on stderr.

## Out of scope

- Arguments and options (none are parsed).
- Localisation, configuration files.
- Any output other than the greeting.

## Done when

- A pytest test runs the program as a subprocess, both as `hello` and as
  `python -m hello`, and checks stdout is exactly `Hello, world!\n` and the
  exit status is 0.

## Stack

- Python 3.12, uv, pytest (already in the project's dev group).
- Package layout: `src/hello`, built with hatchling.
