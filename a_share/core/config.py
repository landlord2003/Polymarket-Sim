# -*- coding: utf-8 -*-
"""策略参数配置加载（Phase 2：配置驱动参数外置，改策略不动代码）。

零依赖：用标准库 json 读 a_share/config/strategies.json。
webui 启动即加载 CONFIG；面板滑杆范围/默认值、参数扫描网格均从此读取；
改动策略只需编辑 JSON，无需改代码。set_config 可运行时持久化回写。
"""
from __future__ import annotations

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "config", "strategies.json")

_DEFAULT_CONFIG = {
    "backtest": {
        "default_days": 30,
        "default_every_min": 1440,
        "default_size": 100,
        "half_spread": {"min": 0.001, "max": 0.05, "step": 0.001,
                        "default": 0.005},
        "fee_rate": {"min": 0.0, "max": 0.1, "step": 0.001, "default": 0.01},
        "sweep_half_spreads": [0.002, 0.005, 0.01, 0.02, 0.03],
        "sweep_fee_rates": [0.0, 0.005, 0.01, 0.02, 0.03],
    },
    "arb": {
        "default_fee_rate": 0.01,
        "default_max_skew": 300,
    },
    "a_share_sim": {
        "init_cash": 100000.0,
        "lot": 100,
    },
}


def load_config(path=None):
    """读取 JSON 配置，缺字段时回退到内置默认，确保不崩溃。"""
    p = os.path.abspath(path or _CONFIG_PATH)
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(_DEFAULT_CONFIG)
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception:
        return dict(_DEFAULT_CONFIG)


def save_config(cfg, path=None):
    """把配置写回 JSON 文件；成功返回 True。"""
    p = os.path.abspath(path or _CONFIG_PATH)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# 模块级单例：webui 启动即加载，后续直接引用。
CONFIG = load_config()
