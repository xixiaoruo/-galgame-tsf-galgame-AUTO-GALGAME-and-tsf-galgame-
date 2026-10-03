"""CoC 模式的数值溯源机制（PROVENANCE）。

设计依据：`COC-DESIGN.md` §0.2「实现铁律」——
> `tuning.json` 中所有来自 §12 不确定项的数值，必须带 `"_unverified": true` 与 `"_source"` 字段。

本模块把这条铁律**变成可执行的机制**，而不是事后补的注释：
`data/coc/tuning.json` 里任何一个**数值叶子节点**缺 `_source`（或 `_unverified` 用了非布尔真值），
`validate_provenance()` 就会抛 `ProvenanceError`，加载即失败。

配套测试：`app/coc/tests/test_tuning_provenance.py`（含"构造缺来源的输入证明它会红"）。

本模块**不导入** app/tsf.py、app/vn.py、app/game.py，也不导入任何 LLM 客户端。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from . import paths

#: 值节点内允许出现的元数据键（不参与"叶子"判定）
_META_KEYS = {"_source", "_unverified", "_note"}

#: 结构性节点：本身不是"数值"，只是分组（其子节点各自必须有来源）
#: `_probe` = 仅验收探针使用的刻度，不参与正式规则（COC-DESIGN §9.2 V-08）
_GROUP_KEYS = {"_meta", "_aliases", "_probe"}

#: 一个值节点必须带的来源元数据
_REQUIRED_LEAF_KEYS = ("_source", "_unverified")


class ProvenanceError(ValueError):
    """数值缺少来源标注（或标注形态非法）。"""


class TuningMissingError(RuntimeError):
    """tuning.json 不存在（引擎需要它才能工作）。"""


def _has_provenance(node: Any) -> bool:
    """该 dict 是否是**带来源的值节点**。

    两种合法形态：
      A. `{"value": ..., "_source": ..., "_unverified": ...}` —— 单值节点；
      B. `{"sta": 21, "rea": 21, ..., "_source": ..., "_unverified": ...}` —— 复合值节点
         （例如 `difficulty.Sceptic`：一张难度小表的多个字段共用一条来源）。

    判定只看 `_source` / `_unverified` 是否出现 —— 这正是设计要的：
    **数值必须带来源**，所以来源标记本身就是"这是值节点"的标志。
    """
    return isinstance(node, dict) and ("_source" in node or "_unverified" in node)


def _group_children(node: dict) -> list[tuple[str, Any]]:
    """分组节点的子节点：排除元数据键（它们在**值节点**里才是内容）。"""
    return [(k, v) for k, v in node.items() if k not in _META_KEYS]


def validate_provenance(data: Any, *, path: str = "$") -> int:
    """递归校验：每个**值节点**必须带 `_source`（非空字符串）与 `_unverified`（bool）。

    - 值节点（形态 A/B，见 `_has_provenance`）：校验两项元数据后停止下探；
    - 分组节点（无来源标记的 dict）：递归校验子节点；
    - `_meta` / `_aliases` / `_probe`：结构性节点，跳过；
    - 其余（int/str/list 直接当节点）：结构非法。

    返回**已校验的值节点个数**（供测试断言"确实遍历到了东西"——
    否则一个空文件也能"通过"，那类断言正是"构造不出反例"的典型）。
    """
    count = 0

    def walk(node: Any, at: str) -> None:
        nonlocal count
        if isinstance(node, dict):
            if _has_provenance(node):
                count += 1
                for key in _REQUIRED_LEAF_KEYS:
                    if key not in node:
                        raise ProvenanceError(
                            f"{at} 是数值节点但缺 {key!r}；"
                            f"COC-DESIGN §0.2 要求所有 tuning 数值带 _source 与 _unverified"
                        )
                src = node["_source"]
                if not isinstance(src, str) or not src.strip():
                    raise ProvenanceError(f"{at}._source 必须是非空字符串（现在 {src!r}）")
                unv = node["_unverified"]
                if not isinstance(unv, bool):
                    raise ProvenanceError(
                        f"{at}._unverified 必须是 bool（现在 {unv!r}）；"
                        f"字符串 'true' / 0 / 1 都不算已标注，否则会掩盖缺标注"
                    )
                return
            for key, child in _group_children(node):
                if key in _GROUP_KEYS:
                    continue
                walk(child, f"{at}.{key}")
            return
        raise ProvenanceError(
            f"{at} 的结构非法：期望 dict（分组或值节点），实际 {type(node).__name__}"
        )

    walk(data, path)
    return count


def walk_value_nodes(data: Any, *, path: str = "$") -> list[tuple[str, Any]]:
    """列出全部值节点的 `(路径, 节点)` —— 供 `unverified_leaves` 与审计使用。"""
    out: list[tuple[str, Any]] = []

    def walk(node: Any, at: str) -> None:
        if isinstance(node, dict):
            if _has_provenance(node):
                out.append((at, node))
                return
            for key, child in _group_children(node):
                if key in _GROUP_KEYS:
                    continue
                walk(child, f"{at}.{key}" if at != "$" else key)

    walk(data, path)
    return out


def load_tuning() -> dict:
    """读取并校验 tuning.json。缺文件 → TuningMissingError；缺来源 → ProvenanceError。"""
    if not os.path.isfile(paths.TUNING_PATH):
        raise TuningMissingError(
            f"缺少 {paths.TUNING_PATH}；CoC 引擎的全部数值都来自它，不能有代码内兜底常量"
        )
    with open(paths.TUNING_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    validate_provenance(data)
    return data


def t(data: dict, dotted: str) -> Any:
    """按点号路径取**值**（自动脱 `value` 外壳）。

    >>> t(cfg, "threat_mod.LOW")          # -1
    >>> t(cfg, "combat.time_budget_turn") # 200

    取不到直接 KeyError —— 与"代码里写个默认值"相比，这是有意的：
    数值只能来自 tuning.json。
    """
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"tuning.json 里没有路径 {dotted!r}（在 {part!r} 处断了）")
        node = node[part]
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def meta(data: dict, dotted: str) -> dict:
    """取某个值节点的元数据（`_source` / `_unverified` / `_note`）。"""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"tuning.json 里没有路径 {dotted!r}")
        node = node[part]
    if not isinstance(node, dict):
        raise KeyError(f"{dotted!r} 不是节点")
    return {k: node[k] for k in _META_KEYS if k in node}


def unverified_leaves(data: dict) -> list[str]:
    """列出所有 `_unverified: true` 的数值路径（报告与审计用）。"""
    return sorted(at for at, node in walk_value_nodes(data) if node.get("_unverified") is True)


def deep_copy(data: dict) -> dict:
    """给测试用的深拷贝（在副本上注入错误，不污染真实 tuning）。"""
    return copy.deepcopy(data)
