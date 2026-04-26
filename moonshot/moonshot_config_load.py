"""Load daily ``MoonshotConfig`` from JSON (same shape as R24: flat or ``{"params": ...}``).

Resolution when ``path`` is None:
  1. ``MOONSHOT_DAILY_PARAMS`` if set and file exists
  2. Under repo root / cwd: ``config/moonshot_params.json``, then
     ``reports/optimizer/moonshot_phase3_best.json``, then
     ``reports/optimizer/moonshot_best.json`` (legacy)

If multiple exist, the **first** found wins (canonical before optimizer output), same idea as R24.

``main_profit_thresholds`` may be a list of ``[max_pct, threshold]`` pairs.
Sizing alias: ``fixed_margin_usd`` → ``fixed_invest_usd`` when the latter is null.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from moonshot.strategy import MoonshotConfig

ENV_DAILY_PARAMS_PATH = "MOONSHOT_DAILY_PARAMS"
DEFAULT_CANONICAL = Path("config/moonshot_params.json")
DEFAULT_PHASE3 = Path("reports/optimizer/moonshot_phase3_best.json")
DEFAULT_OPTIMIZER_LEGACY = Path("reports/optimizer/moonshot_best.json")


def _moonshot_config_search_roots() -> list[Path]:
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


def moonshot_config_param_names() -> set[str]:
    return {f.name for f in fields(MoonshotConfig)}


def params_dict_from_json_file(file_path: Path) -> dict[str, Any]:
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {file_path}")
    raw = _raw_params_from_parsed_json(data)
    valid = moonshot_config_param_names()
    filtered = {k: v for k, v in raw.items() if k in valid}
    if "main_profit_thresholds" in filtered:
        filtered["main_profit_thresholds"] = _coerce_main_profit_thresholds(
            filtered["main_profit_thresholds"]
        )
    return filtered


def load_moonshot_config(path: Path | str | None = None) -> MoonshotConfig:
    """Merge JSON overrides onto ``MoonshotConfig()`` defaults."""
    base = MoonshotConfig()
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Moonshot config file not found: {p}")
        return replace(base, **params_dict_from_json_file(p))

    candidates: list[Path] = []
    env_p = os.environ.get(ENV_DAILY_PARAMS_PATH, "").strip()
    if env_p:
        candidates.append(Path(env_p))
    for root in _moonshot_config_search_roots():
        candidates.append(root / DEFAULT_CANONICAL)
        candidates.append(root / DEFAULT_PHASE3)
        candidates.append(root / DEFAULT_OPTIMIZER_LEGACY)

    for p in candidates:
        if p.is_file():
            return replace(base, **params_dict_from_json_file(p))
    return base


def moonshot_config_to_json_dict(cfg: MoonshotConfig) -> dict[str, Any]:
    """JSON-serializable flat dict of all ``MoonshotConfig`` fields."""
    d = asdict(cfg)
    mpt = d.get("main_profit_thresholds")
    if isinstance(mpt, list):
        d["main_profit_thresholds"] = [[float(a), float(b)] for a, b in mpt]
    return d


def export_moonshot_flat_params_json(params: dict[str, Any], destination: Path) -> Path:
    """Write ``{"params": clean}`` for ``config/moonshot_params.json`` (optimizer / CLI export)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    valid = moonshot_config_param_names()
    clean = {k: v for k, v in params.items() if k in valid}
    if "main_profit_thresholds" in clean and isinstance(clean["main_profit_thresholds"], list):
        mpt = clean["main_profit_thresholds"]
        clean["main_profit_thresholds"] = [
            [float(a), float(b)] for a, b in mpt
        ]
    payload = {"params": clean}
    destination.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return destination


def export_moonshot_optimizer_json(
    *,
    score: float,
    config: MoonshotConfig,
    destination: Path,
) -> Path:
    """Write ``{"score", "params"}`` for ``load_moonshot_config`` and paper/backtest."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"score": float(score), "params": moonshot_config_to_json_dict(config)}
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination
