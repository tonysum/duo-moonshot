"""Load Moonshot-R24 `RollingConfig` from JSON files or env-specified path.

Resolution order when `path` is None:
  1. ``MOONSHOT_R24_PARAMS`` (path to JSON) if set and file exists
  2. Under each search root (clone root next to the ``moonshot`` package, then cwd):
     ``config/r24_params.json``, then ``reports/optimizer/r24_phase3_best.json``, if exist
  3. ``RollingConfig()`` defaults

Search roots dedupe when the package lives inside the cwd. When installed from PyPI
the package parent may not contain these files; behavior falls through safely.

JSON may be optimizer shape ``{"phase", "score", "params": {...}}`` or a flat object
of RollingConfig fields. ``main_profit_thresholds`` may be a JSON list of pairs.

Sizing alias: ``fixed_margin_usd`` is accepted and mapped to ``fixed_invest_usd`` when the
latter is omitted or null (use with ``position_sizing_mode``: ``"fixed_usd"``).

Sell surge (AND with R24, hm1l-aligned): optional keys ``enable_sell_surge_gate``,
``sell_surge_threshold``, ``sell_surge_max``.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from moonshot.rolling_strategy import RollingConfig

ENV_R24_PARAMS_PATH = "MOONSHOT_R24_PARAMS"
DEFAULT_CANONICAL = Path("config/r24_params.json")
DEFAULT_PHASE3 = Path("reports/optimizer/r24_phase3_best.json")


def _rolling_config_search_roots() -> list[Path]:
    """Prefer repo layout (directory containing ``moonshot/``) over cwd."""
    roots: list[Path] = []
    pkg_parent = Path(__file__).resolve().parent.parent
    roots.append(pkg_parent)
    roots.append(Path.cwd())
    seen: set[Path] = set()
    out: list[Path] = []
    for r in roots:
        rp = r.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _raw_params_from_parsed_json(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("params"), dict):
        raw = dict(data["params"])
    else:
        raw = {k: v for k, v in data.items() if k not in ("phase", "score")}
    if raw.get("fixed_invest_usd") is None and raw.get("fixed_margin_usd") is not None:
        raw["fixed_invest_usd"] = raw["fixed_margin_usd"]
    raw.pop("fixed_margin_usd", None)
    return raw


def _coerce_main_profit_thresholds(val: Any) -> list[tuple[float, float]]:
    if not isinstance(val, list):
        raise TypeError("main_profit_thresholds must be a list of [max_pct, threshold] pairs")
    out: list[tuple[float, float]] = []
    for item in val:
        if isinstance(item, list | tuple) and len(item) == 2:
            out.append((float(item[0]), float(item[1])))
        else:
            raise TypeError(f"Invalid main_profit_thresholds entry: {item!r}")
    return out


def rolling_config_param_names() -> set[str]:
    return {f.name for f in fields(RollingConfig)}


def params_dict_from_json_file(file_path: Path) -> dict[str, Any]:
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {file_path}")
    raw = _raw_params_from_parsed_json(data)
    valid = rolling_config_param_names()
    filtered = {k: v for k, v in raw.items() if k in valid}
    if "main_profit_thresholds" in filtered:
        filtered["main_profit_thresholds"] = _coerce_main_profit_thresholds(
            filtered["main_profit_thresholds"]
        )
    return filtered


def load_rolling_config(path: Path | str | None = None) -> RollingConfig:
    """Merge JSON overrides onto ``RollingConfig()`` defaults."""
    base = RollingConfig()
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"R24 config file not found: {p}")
        return replace(base, **params_dict_from_json_file(p))

    candidates: list[Path] = []
    env_p = os.environ.get(ENV_R24_PARAMS_PATH, "").strip()
    if env_p:
        candidates.append(Path(env_p))
    for root in _rolling_config_search_roots():
        candidates.append(root / DEFAULT_CANONICAL)
        candidates.append(root / DEFAULT_PHASE3)

    for p in candidates:
        if p.is_file():
            return replace(base, **params_dict_from_json_file(p))
    return base


def export_params_json(params: dict[str, Any], destination: Path) -> Path:
    """Write ``{"params": ...}`` for use with this loader and paper/backtest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    valid = rolling_config_param_names()
    clean = {k: v for k, v in params.items() if k in valid}
    payload = {"params": clean}
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination
