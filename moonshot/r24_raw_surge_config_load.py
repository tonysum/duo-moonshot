"""Load ``RawSurgeR24Config`` from JSON (same shape as ``r24_config_load``).

Resolution when ``path`` is None:
  1. ``MOONSHOT_R24_RAW_SURGE_PARAMS`` if set and file exists
  2. ``config/r24_raw_surge_params.json`` by walking parents of ``Path.cwd()``（根目录运行时通常与下面重复，可忽略）
  3. Same walk from ``moonshot/`` 包目录
  4. ``_rolling_config_search_roots()`` 各根下的相对路径
  5. ``RawSurgeR24Config()`` defaults + ``logging.warning``

在仓库根目录执行时，(2)(3)(4) 往往指向同一文件；若仍像未加载 JSON，请看是否被 ``MOONSHOT_R24_RAW_SURGE_PARAMS`` 抢先、或 ``get_last_loaded_raw_surge_config_path()`` 为 None。

JSON: ``{"params": {...}}`` or flat object.
``fixed_margin_usd`` maps to ``fixed_invest_usd`` when the latter is null.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from moonshot.r24_config_load import (
    _coerce_main_profit_thresholds,
    _raw_params_from_parsed_json,
    _rolling_config_search_roots,
)
from moonshot.r24_raw_surge_config import RawSurgeR24Config

ENV_RAW_SURGE_PARAMS_PATH = "MOONSHOT_R24_RAW_SURGE_PARAMS"
DEFAULT_CANONICAL = Path("config/r24_raw_surge_params.json")
_CANONICAL_NAME = "r24_raw_surge_params.json"

# 最近一次成功从 JSON 合并的配置文件路径（显式 path 或自动发现）；仅用 dataclass 默认则为 None
last_loaded_raw_surge_config_path: Path | None = None


def get_last_loaded_raw_surge_config_path() -> Path | None:
    """供回测/脚本打印：是否真正读到了 ``r24_raw_surge_params.json``。"""
    return last_loaded_raw_surge_config_path


def _find_canonical_raw_surge_json(start: Path) -> Path | None:
    """从 ``start`` 起沿父目录向上查找 ``config/r24_raw_surge_params.json``（解决 cwd 在子目录时相对路径失效）。"""
    cur = start.resolve()
    for _ in range(12):
        cand = cur / "config" / _CANONICAL_NAME
        if cand.is_file():
            return cand
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def raw_surge_r24_param_names() -> set[str]:
    return {f.name for f in fields(RawSurgeR24Config)}


def params_dict_from_json_file(file_path: Path) -> dict[str, Any]:
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {file_path}")
    raw = _raw_params_from_parsed_json(data)
    valid = raw_surge_r24_param_names()
    filtered = {k: v for k, v in raw.items() if k in valid}
    if "main_profit_thresholds" in filtered:
        filtered["main_profit_thresholds"] = _coerce_main_profit_thresholds(
            filtered["main_profit_thresholds"]
        )
    return filtered


def load_raw_surge_r24_config(path: Path | str | None = None) -> RawSurgeR24Config:
    """Merge JSON overrides onto ``RawSurgeR24Config()`` defaults."""
    global last_loaded_raw_surge_config_path
    base = RawSurgeR24Config()
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"R24 raw-surge config file not found: {p}")
        last_loaded_raw_surge_config_path = p.resolve()
        logger.info("R24 raw-surge 配置: %s", last_loaded_raw_surge_config_path)
        return replace(base, **params_dict_from_json_file(p))

    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add_candidate(p: Path) -> None:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if rp in seen:
            return
        seen.add(rp)
        candidates.append(p)

    env_p = os.environ.get(ENV_RAW_SURGE_PARAMS_PATH, "").strip()
    if env_p:
        _add_candidate(Path(env_p))
    hit = _find_canonical_raw_surge_json(Path.cwd())
    if hit:
        _add_candidate(hit)
    hit_pkg = _find_canonical_raw_surge_json(Path(__file__).resolve().parent)
    if hit_pkg:
        _add_candidate(hit_pkg)
    for root in _rolling_config_search_roots():
        _add_candidate(root / DEFAULT_CANONICAL)

    for p in candidates:
        if p.is_file():
            last_loaded_raw_surge_config_path = p.resolve()
            logger.info("R24 raw-surge 配置: %s", last_loaded_raw_surge_config_path)
            return replace(base, **params_dict_from_json_file(p))
    last_loaded_raw_surge_config_path = None
    logger.warning(
        "未找到 %s（已查 MOONSHOT_R24_RAW_SURGE_PARAMS、cwd 祖先、包目录祖先、_rolling_config_search_roots）。"
        "使用 RawSurgeR24Config 默认值；请设置环境变量、把 JSON 放在仓库 config/ 下，或传入 path/--config。",
        _CANONICAL_NAME,
    )
    return base


def export_raw_surge_params_json(params: dict[str, Any], destination: Path) -> Path:
    """Write ``{"params": ...}`` for use with this loader."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    valid = raw_surge_r24_param_names()
    clean = {k: v for k, v in params.items() if k in valid}
    payload = {"params": clean}
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination
