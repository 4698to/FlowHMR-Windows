# 与上游 FlowHMR 的同步状态与移植计划

> 记录时间：2026-11-24
> 对比对象：上游 `https://github.com/flowhmr/flowhmr`（本地参考 `D:\xjq\flowhmr`）
> 本文档只做记录，不代表已实施。

---

## 1. 两个仓库的关系

本仓库是上游的 **Windows 独立分支**，不是简单镜像：

| 项目 | 上游 `flowhmr/flowhmr` | 本仓库 `FlowHMR-Windows` |
| --- | --- | --- |
| 远端 | `flowhmr/flowhmr` | `xiaoerofficial021-collab/FlowHMR-Windows` |
| 共同根提交 | `f12e6a2 Initial release of FlowHMR` | 同左 |
| 提交数 | 23 | 74（当前 v0.3.9） |
| 分叉点 | — | `c5aaeb4`（2026-10-30） |
| 定位 | 论文模型 + CLI / Web demo | 采用上游模型，独立维护便携版、模型与环境管理、桌面工作流 |

本仓库在分叉点之后有约 50 个 Windows 专属提交，重写或大幅扩展了这些部分：

- 桌面工作流与 Web UI：`tools/web/server.py`（+2861 行）、`tools/web/index.html`（+899 行）
- 推理管线与运行时：`flowhmr/pipeline/v2m_inference.py`（+872 行）、
  `flowhmr/utils/runtime_sam_features.py`（+507 行）、
  `flowhmr/utils/runtime_video_processing.py`（+257 行）
- 便携版打包、模型/环境按需安装、ROCm / Intel XPU 支持、导出与重定向、
  多语言、多镜头续跑与场景对齐、MCP 本地服务、稀疏 SAM 手部采样等

上游 README 最新提交（`86e56ee`）已致谢 Windows 扩展作者，说明上游认可本分支的存在。

---

## 2. 未同步的上游提交

上游共有 7 个提交不在本仓库中：

| 提交 | 日期 | 内容 | 是否需要 |
| --- | --- | --- | --- |
| `87ebb48` | 2026-11-01 | README 更新 | 否，本仓库 README 已重写并本地化 |
| `df15cad` | 2026-11-10 | README 引用格式 | 否，同上 |
| `b79fc11` | 2026-11-13 | README 作者列表 | 否，同上 |
| `24c092d` | 2026-11-19 | Windows 下 `.js` MIME 修正 | **已独立实现且更完整**，见 `tools/web/server.py` 中向
`mimetypes` 注册 `text/javascript` 的代码 |
| `03af0bf` | 2026-11-22 | 合并 PR #9 | 无内容 |
| `368bc4a` | 2026-11-22 | 默认 YOLO26m + Pi4，检测器/相机可切换 | **唯一实质功能缺口**，见第 3 节 |
| `86e56ee` | 2026-11-22 | README / `docs/web_demo.md` 说明与致谢 | 否，文档已本地化 |

结论：模型侧核心（FlowHMR 生成、SAM-3D-Body 特征、motion attention 批处理、
相机旋转相对首帧修正、RGBMocap 接触后处理）均已包含在分叉点内，没有落后。
**唯一没同步的核心功能是感知前端（检测器 + 相机估计）的换代。**

---

## 3. 上游 `368bc4a` 内容与本地现状

| 能力 | 上游实现 | 本仓库现状 |
| --- | --- | --- |
| YOLO26m 检测器 | 新增 `UltralyticsHumanDetector`（依赖 `ultralytics`），默认替代 YOLOX，可切回 | 仅 YOLOX，`build_human_detector()` 无后端参数 |
| Pi4 相机 | 新增 `third_party/pi4_camera` + `ckpts/pi4/model.safetensors`；拟合焦距/主点，给 SAM 原始像素内参；上游默认相机 | 仅 VGGT / static |
| SimpleVO 相机 | GVHMR 式 SIFT + `pycolmap` 两视图估计，纯 CPU，每 8 帧估计旋转 | 无 |
| 缓存按后端区分 | bbox 缓存写入 `detector_backend`；特征缓存按相机后端加后缀（`sam3d_feat_pi4.pt` 等） | 缓存无后端标记 |
| 配置入口 | `flowhmr/utils/perception_options.py`（`DETECTOR_WEIGHTS` / `CAMERA_WEIGHTS`）+ CLI/Web 选项 | 权重集中在 `flowhmr/model_files.py`，无后端枚举 |

本仓库对应结构（移植时需要改动的落点）：

- `flowhmr/utils/runtime_video_processing.py:137` `YOLOXHumanDetector` / `build_human_detector`
- `flowhmr/pipeline/v2m_inference.py:1190` 附近 `static_camera` → `camera_mode` 的缓存后缀逻辑
- `flowhmr/model_files.py:19` `YOLOX`、`:25` `VGGT`、`:41` `MODEL_DESTINATIONS`（按需下载登记）
- `flowhmr/mcp/contract.py:31` `static_camera: bool = True`（任务参数需要扩成后端枚举）
- 多镜头/续跑：`flowhmr/multishot/runtime.py`、`stitch_motion.py`、`wrist_alignment.py`
  均以 `static_camera` 布尔值推断缓存名

---

## 4. 判断

**不需要整体合并，也不存在"不同步就跑不了"的硬依赖。**
现有 YOLOX + VGGT 管线等价于上游 2026-10-30 的行为，可继续使用。

按收益排序：

1. **SimpleVO（建议移植）**
   README 已声明支持纯 CPU、Intel XPU、6 GB 显存；当前这些场景只能选 static 相机，
   而 VGGT 是 1B 模型。SimpleVO 用 CPU 估计相机旋转，正好补上空白，代价仅为 `pycolmap` 依赖。
2. **Pi4（可选，中等优先）**
   上游新默认，显存与速度比 VGGT 友好，内参拟合更准；
   但需新增 submodule、权重下载、第二条相机路径的打包与维护成本。
3. **YOLO26m（可选，低优先）**
   精度/速度略优，但引入 `ultralytics` 依赖和额外权重；
   建议做成可选项，保留 YOLOX 作为默认，避免已装用户重新下载。
4. **缓存后端标记（建议一并做）**
   正确性修复：现在 bbox 缓存不带后端标记，一旦新增第二检测器，旧缓存会被错误复用。

---

## 5. 移植方案（不合并）

- 不做 `git merge` / 整体 `cherry-pick 368bc4a`：上游修改的是旧结构的
  `v2m_inference.py`、`server.py`，与本仓库差异巨大，冲突面大且会带回 CLI 式设计。
- 新权重统一登记到 `flowhmr/model_files.py`，沿用现有"按需下载模型/环境"机制，
  不把新权重打进便携 ZIP。
- 后端选择需要同时扩展到任务参数层（`flowhmr/mcp/contract.py`、
  多镜头任务持久化、Web 表单与恢复逻辑），不能只改管线。
- 缓存命名与失效规则要和现有 `output/<视频名>/` 约定保持一致，
  并在切换后端时清理对应缓存。
- 依赖需先验证 Windows / py3.10 wheel 可用性：
  `pycolmap==3.12.6`、`ultralytics`，特别是 ROCm 与 Intel XPU 环境。

## 6. 待办清单

- [ ] 验证 `pycolmap==3.12.6` 在 Windows py3.10（CPU / ROCm / XPU）可用
- [ ] 移植 SimpleVO：`extract_camera` 入口 + 缓存后缀 + CLI/Web 选项
- [ ] bbox 缓存写入 / 校验 `detector_backend`
- [ ] `contract.py` 与多镜头任务参数从 `static_camera: bool` 扩展为后端枚举（含兼容旧任务）
- [ ] 评估 Pi4：submodule、权重按需下载、显存/速度基准
- [ ] 评估 YOLO26m：作为可选检测器，默认仍为 YOLOX
- [ ] 若移植：更新 `docs/inference.md`、`docs/resources.md`、`docs/web_demo.md` 与 README 的对应说明

## 7. 参考

- 上游参考仓库本地副本：`D:\xjq\flowhmr`（仅作为查阅与新功能来源，不做代码合并）
- 可选：在本仓库添加 `upstream` 远端以便 `git fetch upstream` 追踪：
  `git remote add upstream https://github.com/flowhmr/flowhmr.git`
- 感知阶段耗时分析见 `docs/sam-pipeline-performance.md`
