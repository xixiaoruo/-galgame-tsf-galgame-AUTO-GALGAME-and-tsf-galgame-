"""CoC 模式的路径常量。**明确区分两类路径**（这是 t51 缺陷的根因）。

## 两类路径（务必分清）

| 类别 | 含义 | 落点 | 谁写 | 冻结后 |
|---|---|---|---|---|
| **随包只读数据** | 引擎要读的静态内容：`tuning.json` / 卡池 / 结局表 / 古神表… | **包内** `<app 上级>/data/coc/`（冻结后即 `_internal/data/coc/`） | 只读（由 `TSF_Galgame.spec` 收集） | 随包走 |
| **玩家可写数据** | 对局存档、存档槽 | **`app.config.DATA_DIR`** 之下 | 运行时写 | 跟随 `DATA_DIR` |

## 为什么必须分开（t51 的真实缺陷）

第一版把**两类都**塞进"由 `__file__` 推出来的 `<项目根>/data/`"。非冻结下 `__file__` 推出的根就是项目根，
看起来"能用"，于是掩盖了三个问题：

1. **更新即丢档（红线）**：冻结 onedir 下 `__file__` 推出 `ROOT = _internal`，
   于是存档写进 **安装目录** `_internal/data/saves/`；替换目录更新时**连带丢掉存档** ——
   直接违反项目红线「游戏数据永不因更新丢失」。
2. **只读安装位置写失败**：装到 Program Files 类目录时无法写入。
3. **隔离验收覆盖不到**：`TSF_DATA_DIR` 重定向对 `__file__` 推出的路径无效，
   隔离样本里根本看不到 coc 的写盘，排查时会误以为"没写盘"。

另有一处**非冻结下也错**的问题：既有模式的存档槽在 `config.DATA_DIR/"slots"`
（`app/game.py::_slots_dir`），而第一版把 coc 槽写到了 `<项目根>/data/saves/` ——
**既不是既有模式的位置、也不是同一个目录**，所以 `game.list_slots()` 其实**看不到** coc 槽
（"共用"只在我的注释里成立，不在磁盘上成立）。本文件现已对齐到 `config.DATA_DIR/"slots"`。

## 为什么读取也**不**跟随 `DATA_DIR`

`data/coc/**` 是**随包数据**（spec 显式收集），它的位置由包布局决定，不由玩家数据根决定。
若把读取也改到 `DATA_DIR/data/coc`，则未预置内容的玩家目录会让引擎启动即抛
`TuningMissingError`。所以：**读取留包内，写入走 `DATA_DIR`**。

（若将来需要"玩家可覆盖的卡池"，正确做法是**两个来源叠加**：包内默认 + `DATA_DIR/data/coc`
用户覆盖层，而不是二选一。本任务不做，仅记录方向。）

## 动态取值

`configured_data_dir()` / `saves_dir()` / `slots_dir()` 都是**函数**，每次调用重新向
`app.config` 取当前值。理由：`TSF_DATA_DIR` 是**进程启动时**读的，而 `app.config` 的取值可能
随启动顺序变化；用函数取当下值可避免"在 import 时缓存了一个陈旧值"这一类难查的问题。
顶层常量 `SAVES_DIR` / `SLOTS_DIR` 保留为**当前值**，供既有调用方（`session.py`）与
报告脚本直接读。
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------- 包内布局

#: app/coc/paths.py → app/coc → app → <包根>；冻结 onedir 下 <包根> 即 `_internal`
COC_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(COC_DIR)
ROOT = os.path.dirname(APP_DIR)

# ---------------------------------------------------------------- 随包只读内容层

#: 内容层（**只读**，随包分发；spec 收集 `data/coc/**` 到包内同位置）
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


# ---------------------------------------------------------------- 玩家可写数据层

def configured_data_dir() -> str:
    """当前的数据目录（**跟随 `app.config.DATA_DIR`**，因而跟随 `TSF_DATA_DIR`）。

    拿不到 `app.config` 时才退回包内推法 —— 这是**降级**而不是等价路径：
    正常运行时 `app.config` 一定可导入（它也在同一个包里）。
    """
    try:
        from .. import config as _cfg
        return str(_cfg.DATA_DIR)
    except Exception:                      # pragma: no cover - 仅极端降级
        return os.path.join(ROOT, "data")


def saves_dir() -> str:
    """对局自主存档目录（`DATA_DIR/saves`）。"""
    return os.path.join(configured_data_dir(), "saves")


def slots_dir() -> str:
    """全局存档槽目录：**与既有两个模式同一个**（`app/game.py::_slots_dir` = `DATA_DIR/slots`）。

    这是"共用"的**唯一**正确落点：只有对齐到这里，`game.list_slots()` 才能列出 coc 槽，
    且在冻结形态下跟随 `DATA_DIR`（因而不随更新丢失）。
    """
    return os.path.join(configured_data_dir(), "slots")


#: 顶层常量 = 调用时的当前值（供既有调用方与报告脚本直接读）
SAVES_DIR = saves_dir()
SLOTS_DIR = slots_dir()


def refresh() -> dict:
    """重新解析并刷新顶层常量；返回新值。

    用途：`TSF_DATA_DIR` 在**同一进程内**变更后（测试/宿主切换）要让常量跟上。
    """
    global SAVES_DIR, SLOTS_DIR
    SAVES_DIR = saves_dir()
    SLOTS_DIR = slots_dir()
    return {"DATA_DIR": DATA_DIR, "SAVES_DIR": SAVES_DIR, "SLOTS_DIR": SLOTS_DIR}


def classify() -> dict:
    """给报告/自检用：把两类路径一次性列出（含类别标注）。"""
    return {
        "bundled_readonly": {
            "root": DATA_DIR,
            "tuning": TUNING_PATH,
            "cards": CARDS_DIR,
            "note": "随包分发（spec 收集 data/coc/**）；不跟随 DATA_DIR",
        },
        "player_writable": {
            "data_dir": configured_data_dir(),
            "saves": saves_dir(),
            "slots": slots_dir(),
            "note": "跟随 app.config.DATA_DIR（含 TSF_DATA_DIR 逃逸舱）",
        },
    }
