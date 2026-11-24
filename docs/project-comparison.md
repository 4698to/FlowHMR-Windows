# 三个仓库的定位、关系与差异

> 记录时间：2026-11-24
> 对比对象：
> - 本项目 `d:\xjq\FlowHMR-Windows`（FlowHMR-Windows v0.3.9）
> - 上游 `https://github.com/flowhmr/flowhmr`（本地副本 `D:\xjq\flowhmr`）
> - 多模型动捕应用 `https://github.com/toyxyz/toyxyz_RGBMocap`（本地副本 `D:\xjq\toyxyz_RGBMocap`）

---

## 1. 一句话结论

三者是**三个独立的 Git 仓库，运行时互不调用**：

- 本项目是上游 FlowHMR 的 **Windows 独立分支**，负责把 FlowHMR 做成便携桌面应用；
- 上游是**论文模型与 CLI/Web demo** 的来源，本项目从它分叉后自行演进；
- RGB MoCap 是**另一个多模型动捕工作站**，它把 FlowHMR 当作 12 个后端之一临时 vendored 进来；
  反过来，本项目只从 RGB MoCap 抽取了**接触修正算法**（`third_party/RGBMocapCorrection`）。

## 2. 关系图

```
                    flowhmr/flowhmr（上游，研究代码 + CLI/Web demo）
                       │                               │
        分叉于 f12e6a2（共同根提交）           vendored 副本 b23040f + query attention 回补
                       │                               │
                       ▼                               ▼
        FlowHMR-Windows（本项目）            toyxyz_RGBMocap（多模型工作站）
          ▲         │                            │  ExMR / FlowHMR / GVHMR / MAMMA / PEAR /
          │         │ 抽取 mocap/ 修正算法        │  SAM 3.1 / HaMeR / FootMR / HTD-Refine /
          │         ▼                            │  HandFlow / RIFE / OSNet
          └── third_party/RGBMocapCorrection @941acff
              third_party/KimodoMotionCorrection @e1c219c（两边都用）
```

## 3. 三方对照

| 维度 | 本项目 FlowHMR-Windows | 上游 flowhmr | toyxyz_RGBMocap |
| --- | --- | --- | --- |
| 远端 | `xiaoerofficial021-collab/FlowHMR-Windows` | `flowhmr/flowhmr` | `toyxyz/toyxyz_RGBMocap` |
| 提交数 / 版本 | 74 提交，v0.3.9 | 23 提交 | 最新提交 2026-11-24（main 干净） |
| 定位 | 单一模型（FlowHMR）的 Windows 便携桌面版 | 论文模型 + 推理 demo | 多模型动捕工作站（GUI + CLI + 插件） |
| 模型后端 | FlowHMR（0.05B / 0.46B）+ HaMeR 手指精修 + SAM 2.1 分割 | 仅 FlowHMR | ExMR、FlowHMR、GVHMR、MAMMA、PEAR、SAM 3.1、HaMeR、FootMR、HTD-Refine、HandFlow、RIFE、OSNet |
| 输出骨骼 | 中性 SMPL-H | 中性 SMPL-H | 依后端而定（SMPL-X / SMPL-H / MAMMA 等） |
| 导出 | Mixamo、MetaHuman、AdvancedSkeleton、VMD、VRMA、GLB、NPZ | SMPL-H 结果与示例导出 | FBX、BVH、Blender 工程、Unreal Live Link、角色重定向 |
| 界面 | 桌面（pywebview）+ Web + 本地 MCP 服务 | CLI、Gradio/Web demo | Qt GUI + CLI + Blender 插件 + Unreal 插件 |
| 模型/环境管理 | 内置按需下载与校验、环境隔离、纯 CPU / CUDA / ROCm / Intel XPU | 手动准备 | Model Manager，多运行时虚拟环境 |
| 许可 | 源码 Apache-2.0（第三方组件各自许可） | Apache-2.0 | MIT |

## 4. 版本锚点与同步状态

| 关系 | 锚点 | 状态 |
| --- | --- | --- |
| 本项目 ← 上游 | 共同根 `f12e6a2`；本项目包含上游到 `c5aaeb4`（2026-10-30） | 上游有 7 个提交未进入本项目，其中仅 `368bc4a`（YOLO26m / Pi4 / 可选后端）为实质功能缺口，详见 [upstream-sync.md](upstream-sync.md) |
| 本项目 ← RGB MoCap | `third_party/RGBMocapCorrection` 锁定 toyxyz 提交 `941acff`（2026-10-30），仅抽取 `mocap/` 下的修正算法 | 14 个抽取文件的 sha256 与 RGB MoCap 当前 HEAD **完全一致**；即 `941acff..HEAD` 的 52 个 `mocap/` 提交只改了 GUI/CLI/导出层，算法未变，无需重新抽取 |
| 本项目 ← Kimodo | `third_party/KimodoMotionCorrection`，`nv-tlabs/kimodo` @ `e1c219c` + 本地补丁 | RGB MoCap 也使用同一组件，两边各自维护补丁 |
| RGB MoCap ← 上游 | vendored `third_party/FlowHMR` @ `b23040f`（2026-10-20），另回补 query-batched attention | 注：其 `RGB_MOCAP_PROVENANCE.json` 把回补标注为 `87ebb48`，该哈希在上游实际是 README 提交；该特性真实来源是上游 `51cd1c2`。属于对方仓库的标注偏差，不影响本项目 |

## 5. 会不会互相影响

- **不会。** 三者没有包依赖或运行时调用关系；本项目运行时不 import RGB MoCap 的任何模块，反之亦然。
- 唯一需要人工关注的两条线：
  1. 上游 FlowHMR 的新功能（当前待评估：SimpleVO / Pi4 / YOLO26m）；
  2. RGB MoCap 是否改动 `correction.py`、`anatomical_ik.py`、`contact_*.py` 等被抽取的 14 个文件——一旦算法变动，才需要更新 `third_party/RGBMocapCorrection` 与 `UPSTREAM.json` 的哈希。
- 合并结论：**不与上游合并、不与 RGB MoCap 合并**。两边代码结构已大幅分叉
  （`tools/web/server.py` +2861 行、`v2m_inference.py` +872 行），只做定向移植或定向抽取。

## 6. 附录：FlowHMR 与 GVHMR 的模型差异

GVHMR 只出现在 RGB MoCap 的后端列表中，本项目未集成；它是上游 FlowHMR 的 `SimpleVO` 相机模块来源。

| 维度 | GVHMR | FlowHMR |
| --- | --- | --- |
| 论文 | arXiv 2409.06662（ZJU3DV，SIGGRAPH Asia 2024） | arXiv 2610.03691 "Physically Plausible Motion Capture from Video" |
| 范式 | 判别式回归，单次前向 | 生成式 flow matching，Euler 采样 20 步 + CFG |
| 输出 | SMPL-X（不推理手指） | 中性 SMPL-H，30 fps，最长 360 帧 |
| 2D 前端 | ViTPose-H + HMR2 图像特征 | SAM 3D Body 特征 |
| 相机 | VGGT / 静态 / SimpleVO（SIFT + pycolmap，纯 CPU） | VGGT-Omega / 静态（上游新增 Pi4） |
| 模型规模 | 多 ckpt 组合：GVHMR + HMR2 + ViTPose-H | 0.05B / 0.46B 两档 |
| 物理合理性 | 依赖后处理（HTD-Refine、FootMR、接触修正） | 训练内建接触/根速度/FK 一致性损失，动作更平滑 |
| 典型弱点 | 抖动、脚滑、穿地 | 采样步数多、显存与耗时更高；极端姿态可能被过度合理化 |
| 适用场景 | 罕见/非常规动作（如后空翻）有时更稳，适合交叉验证 | 常规素材直接出物理合理的干净动作 |

## 7. 相关文档

- [upstream-sync.md](upstream-sync.md)：与上游 FlowHMR 的提交级差异与移植计划
- [third_party/RGBMocapCorrection/UPSTREAM.json](../third_party/RGBMocapCorrection/UPSTREAM.json)：抽取范围与文件哈希
- [sam-pipeline-performance.md](sam-pipeline-performance.md)：感知阶段耗时分析
