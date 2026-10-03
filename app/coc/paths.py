"""CoC 模式的文件路径常量（自包含：只依赖本包与标准库）。

注意：**不**从 `app.config` 取路径 —— 本包不得耦合既有模块（COC-DESIGN §7.1 B1/B5）。
数据目录固定落在**项目根**下的 `data/coc/`（与 `app/` 同级），
原因是本任务（t48）的白名单只允许写 `app/coc/**`、`data/coc/**`、`tests/coc/**`，
而 `D:/TSF_Galgame_Data/` 被明确列为 outOfScope。

接线任务（等 t37/t38/t39/t46）如需改为写 `D:/TSF_Galgame_Data/data/coc/`，
只改本文件一处即可，引擎其余部分不感知。
"""
from __future__ import annotations

import os

#: app/coc/paths.py → app/coc → app → <项目根>
COC_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(COC_DIR)
ROOT = os.path.dirname(APP_DIR)

#: 内容层：全部数值与数据都在这里
DATA_DIR = os.path.join(ROOT, "data", "coc")
TUNING_PATH = os.path.join(DATA_DIR, "tuning.json")
CARDS_DIR = os.path.join(DATA_DIR, "cards")
MYSTERIES_PATH = os.path.join(DATA_DIR, "mysteries.json")
LOCATIONS_PATH = os.path.join(DATA_DIR, "locations.json")
OLD_GODS_PATH = os.path.join(DATA_DIR, "old_gods.json")
ENDINGS_PATH = os.path.join(DATA_DIR, "endings.json")
ITEMS_PATH = os.path.join(DATA_DIR, "items.json")
TOWN_STATUS_PATH = os.path.join(DATA_DIR, "town_status.json")
ENEMIES_PATH = os.path.join(DATA_DIR, "enemies.json")
CAST_PATH = os.path.join(DATA_DIR, "cast.json")

#: 存档目录（本任务落在项目 data/coc/saves/；接线任务可切到 TSF_Galgame_Data）
SAVES_DIR = os.path.join(DATA_DIR, "saves")
