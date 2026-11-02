# 自动化AI Galgame 测试工具（只随开发机，不进分发包）

本目录只在本机使用，**绝不会被打进 `TSF_Galgame.zip` 分发包**。

| 文件 | 用途 | 依赖 |
|---|---|---|
| `test_tsf_143.py` | 隔离单测：角色库注入/立绘还原与限量/对局归档回环与安全/生图三线路重试/脸部-换装图生图几何（全部走临时目录，不碰真实数据） | python + 项目依赖（Pillow/httpx/rembg…） |
| `test_cards_190.py` | 1.9.0 单测：随机制服库/场景归一/角色卡背景/关系阶段与缺席衰减/换装摘制服锁/旧档案字段适配（不联网、不写真实数据） | python（标准库 + app 模块） |
| `test_perf_180.py` | 1.8.0 绘画线路：成品缓存命中、节能模式重试次数、阶段判定器跳过 | python |
| `ui19_cdp_smoke.mjs` | 1.9.0 真实 UI 冒烟（CDP）：随机制服、随机角色/AI 背景、角色库、旧档案、关系面板、换装随机；截图到 `测试样本/1.9.0/` | Node 22+、已起隔离服务 + CDP 浏览器 |
| `ui191_choice_check.mjs` | 1.9.1 显示回归：弹窗无全屏 backdrop-filter、选项拖动无文本选区、正常点击能推进下一幕 | Node 22+、已起隔离服务 + CDP 浏览器 |
| `test_slots_194.py` | 1.9.4 存档单测：同秒连存两份不覆盖、列表两份、裸读回读、删除 | python（隔离 TSF_DATA_DIR） |
| `ui194_slots_check.mjs` | 1.9.4 真实 Qt 回归：UI 存档后列表立即出现、读 B 档后 S.sid/localStorage 切换、下一幕仍写 B、select change 后失焦 | Node 22+、Qt remote debugging |
| `ui18_cdp_smoke.mjs` | 1.8.0 UI 冒烟：UI2 绑定、命令面板、六段工坊、性能面板 | Node 22+、已起隔离服务 + CDP 浏览器 |
| `test_live_refine.py` | 真实素材出图测试：用 `D:\TSF_Galgame_Data\data\cache\学院_*` 真实立绘 + 本机 SD WebUI（7860）跑脸部细化/换装掩膜，产物写到 `..\测试样本\` | SD WebUI 在线 |
| `test_consistency_1921.py` | 1.9.21 单测：写作风格/自定义提示词注入、一致性证据包与归一化、修复指令去重限长、自动检查、ComfyUI 权重清洗/工作流/颜色崩坏、换装线路分派（隔离数据根，不联网） | python（隔离 TSF_DATA_DIR） |
| `test_api_1921.py` | 1.9.21 API 冒烟：`/api/version`、`/api/prompt-presets`、开局、一致性三端点、prompt-style、换装端点、index.html 新页签（TestClient + 隔离数据根） | python（隔离 TSF_DATA_DIR） |
| `test_consistency_live_1921.py` | 1.9.21 实机：用真实 DeepSeek 对合成会话跑一次一致性检查（校验请求/JSON/结构，不碰玩家存档） | DeepSeek Key |
| `test_comfy_i2i_live.py` | 1.9.21 实机：ComfyUI 线路换装/脸部/渐变链真实出图 + 像素断言（alpha 不变、头部区小改动、躯干有变化、掩膜外≈0、颜色不崩） | ComfyUI 在线（8188） |
| `comfy_probe_img2img.py` | ComfyUI img2img/inpaint 接口探针：色卡 VAE 往返、上传取图、掩膜内外差异，产物在 `测试样本/comfy_probe/` | ComfyUI 在线 |
| `pack_tsf_zip.py` | 重打分发 zip：纳入有效文件与`角色库`，排除 config.json（Key）/data/update/备份/测试件/含 Key 文档；打包后自动做 Key 前缀泄漏校验 | 无 |

## 相关路径

- 样本图库（四阶段渐变/换装成品/对比图等）：`D:\TSF_Galgame\测试样本\`
- 数据目录：`D:\TSF_Galgame_Data\data\`（与本目录无关，永不随更新丢失）

## 快速使用

```bat
python D:\TSF_Galgame\测试工具\test_tsf_143.py
python D:\TSF_Galgame\测试工具\test_live_refine.py   :: 需先开 SD WebUI
```
