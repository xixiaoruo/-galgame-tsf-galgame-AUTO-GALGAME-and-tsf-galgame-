# 自动化AI Galgame 测试工具（只随开发机，不进分发包）

本目录只在本机使用，**绝不会被打进 `TSF_Galgame.zip` 分发包**。

| 文件 | 用途 | 依赖 |
|---|---|---|
| `test_tsf_143.py` | 隔离单测：角色库注入/立绘还原与限量/对局归档回环与安全/生图三线路重试/脸部-换装图生图几何（全部走临时目录，不碰真实数据） | python + 项目依赖（Pillow/httpx/rembg…） |
| `test_live_refine.py` | 真实素材出图测试：用 `D:\TSF_Galgame_Data\data\cache\学院_*` 真实立绘 + 本机 SD WebUI（7860）跑脸部细化/换装掩膜，产物写到 `..\测试样本\` | SD WebUI 在线 |
| `pack_tsf_zip.py` | 重打分发 zip：纳入有效文件与`角色库`，排除 config.json（Key）/data/update/备份/测试件/含 Key 文档；打包后自动做 Key 前缀泄漏校验 | 无 |

## 相关路径

- 样本图库（四阶段渐变/换装成品/对比图等）：`D:\TSF_Galgame\测试样本\`
- 数据目录：`D:\TSF_Galgame_Data\data\`（与本目录无关，永不随更新丢失）

## 快速使用

```bat
python D:\TSF_Galgame\测试工具\test_tsf_143.py
python D:\TSF_Galgame\测试工具\test_live_refine.py   :: 需先开 SD WebUI
```
