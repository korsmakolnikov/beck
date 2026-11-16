"""beck's configuration: ~/.config/beck/config.yml, extended and overridden by .beck/config.yml.

    versioned: false            # whether beck commits .beck/ (the DSLs, the shopping lists, the toolchain)
    branch_format: "<slug>"     # how kent names the branch; a task code comes from the user
    units_in_parallel: 4
    limits: {times: 3, attempts: 30}   # the repetition rule
    flakiness: {window: 3, runs: 5}    # mixed results in the last `window` runs on one tree: hunt, `runs` each
    driver: claude              # the CLI that runs the agents
    models: {kent: opus, unclebob: sonnet, martin: sonnet, reader: haiku}
    budget: {session: null, iteration: null}   # tokens; null: unlimited (first we measure)
    usage: {window_minutes: 10}   # the windows of the consumption's projection (beck usage, beck dashboard)

Anything tied to a work environment lives only in the global file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "versioned": False,
    "branch_format": "<slug>",
    "units_in_parallel": 4,
    "limits": {"times": 3, "attempts": 30},
    "flakiness": {"window": 3, "runs": 5},
    "driver": "claude",
    "models": {"kent": "opus", "unclebob": "sonnet", "martin": "sonnet", "reader": "haiku"},
    "budget": {"session": None, "iteration": None},
    "usage": {"window_minutes": 10},
}

_TYPES: dict[str, type | tuple[type, ...]] = {"versioned": bool, "branch_format": str, "units_in_parallel": int,
                                              "limits": dict, "flakiness": dict, "driver": str, "models": dict, "budget": dict,
                                              "usage": dict}


class ConfigError(ValueError):
    pass


def global_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "beck" / "config.yml"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: a mapping is expected")
    return data


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(project: Path, global_file: Path | None = None) -> dict[str, Any]:
    config = _merge(_merge(DEFAULTS, _read(global_file or global_path())), _read(project / ".beck" / "config.yml"))
    errors = [f"`{k}` must be a {t.__name__ if isinstance(t, type) else t}" for k, t in _TYPES.items()
              if not isinstance(config.get(k), t)]
    unknown = sorted(set(config) - set(DEFAULTS))
    errors += [f"unknown setting `{k}`" for k in unknown]
    if isinstance(config.get("units_in_parallel"), int) and config["units_in_parallel"] < 1:
        errors.append("`units_in_parallel` must be at least 1")
    minutes = config["usage"].get("window_minutes") if isinstance(config.get("usage"), dict) else None
    if not isinstance(minutes, int) or not 1 <= minutes <= 60 or 60 % minutes:
        errors.append("`usage.window_minutes` must divide an hour (1 to 60)")
    if errors:
        raise ConfigError("; ".join(errors))
    return config
