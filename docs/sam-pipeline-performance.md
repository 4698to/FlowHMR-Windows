# FlowHMR 中 SAM 流程详解与性能分析

> 结论先行：这里说的 "SAM" 不是分割模型 SAM2，而是 Meta 的 **SAM 3D Body** 大模型。
> 每帧视频要做 **3 次** 512×512 的 ViT-H 前向（人体 + 左手 + 右手），
> 实测 151 帧耗时 **26.7 s**，约占整段 56.1 s 流程的 **48%**，是单阶段最大开销。

---

## 1. 整体流水线位置

```
输入视频
  │
  ├─ Step 0 预处理：转码 30 fps、按 track 裁剪片段（ffmpeg）
  ├─ Step 1 Bbox   ：YOLOX 检测 + ByteTrack 跟踪 → bbox.npz（帧级 1 人框）
  ├─ Step 2 VGGT   ：VGGT-1B 估计逐帧相机内参 K → vggt_camera.npz（峰值显存 ~18.6 GB）
  ├─ Step 3 SAM    ：SAM 3D Body 提取每帧 3072 维特征 → sam3d_feat.pt   ← 本文重点
  └─ Step 4 生成   ：FlowHMR 扩散/流匹配（默认 20 步）→ 姿态 npz → 可选 FBX、接触修正
```

每一步都会写缓存文件（`output/<视频名>/`），命中缓存时该步直接跳过：

| 阶段 | 缓存文件 |
|---|---|
| Bbox | `*_bbox.npz` |
| VGGT 相机 | `*_vggt_camera.npz` |
| SAM 特征 | `*_sam3d_feat.pt`（`(T, 3072)`） |
| 生成结果 | `flowhmr_latest.npz` / `.fbx` |

对应代码：`flowhmr/pipeline/v2m_inference.py`（Step 3 在 1013–1032 行附近），
`flowhmr/utils/runtime_sam_features.py`，`flowhmr/utils/lite_sam_infer.py`。

---

## 2. SAM 阶段具体在算什么

### 2.1 单帧的完整计算

对每一帧（已有 bbox + 相机内参 K）：

1. **人体裁剪**：按 bbox 中心/尺度 + 相机内参做 `TopdownAffine`，缩放到 **512×512**
   （`models/sam-3d-body-dinov3/model_config.yaml` 的 `IMAGE_SIZE: [512, 512]`）。
2. **Backbone**：DINOv3 `dinov3_vith16plus`（ViT-H/16+，约 0.8B 参数），
   CUDA 上用 **BF16**；512/16 = 32 → 每张图 1024 个 patch token。
3. **Prompt 编码 + Mask Decoder**：SAM 风格 decoders（`DIM: 1024, DEPTH: 6, HEADS: 8`）。
4. **MHR Head**：输出人体 pose / shape / 相机，得到 3D 关键点。
5. **取特征 token**：body decoder 输出 1024 维（由 forward hook 捕获，见
   `lite_sam_infer.py` 的 `capture_body`）。
6. **推导左右手框**：用人体姿态关键点算出左右手 bbox（`model._get_hand_box`），
   左手镜像翻转后，左右手各做一次 **hand decoder** 前向，各取 1024 维 token。
7. **拼接**：`[body 1024 | left 1024 | right 1024] = 3072` 维，转 CPU float32 后存盘。

所以**一帧 = 3 张 512×512 图像过 ViT-H**（1 人体 + 2 手），
手的裁剪同样缩放回 512×512（`sam_3d_body_estimator.py` 的 `transform_hand`）。

### 2.2 批处理方式

`runtime_sam_features.py` 默认 `frame_batch_size = 16`（网页端对应 `sam_chunk_size`）：

- 一个 chunk（16 帧）内：**1 次 body 前向（batch=16）+ 1 次合并手部前向（batch=32）**
  （`lite_sam_infer.py` 187–251 行；`merge_hands=True` 时左右手合并成一次 forward）。
- 批大小只并行了"帧"，**不是把一帧的 3 个 crop 折进同一个 ViT 批**；
  批与批之间严格串行。
- `sam_batch.py` 的 `SAMBatchPlanner` 会根据显存余量动态缩小 batch；
  显存不足时会先拆分左右手为两次前向，再减小 batch。
- 阶段化驻留：`v2m_inference.py` 699–706 行里 `stage_sam` 打开时，
  非当前阶段的模型会被挪到 CPU/内存，SAM 独占 GPU，但每次换阶段有搬运开销。
- 该路径显式 **关闭了 torch.compile**（`use_compile=False`），因为编译开销与显存占用不可控。

### 2.3 手部稀疏采样（可选项）

`sam_hand_stride`（`sample_hand_frames`，`runtime_sam_features.py` 53–98 行）：
每隔 N 帧才真正跑手部 decoder，未采样帧的手部 token 置 NaN，
事后用相邻采样帧线性插值（`fill_hand_slots`）。网页默认 `sam_hand_stride = 1`，即逐帧。

---

## 3. 实测数据（RTX 3090 24 GB，whisper-v2 环境）

来自 `logs/smoke-run.log`（`assets/example/example.mp4`，1920×1080，151 帧）：

```
=== Step 2: VGGT Camera ===
  [vggt] sampled 151/151 frames (interval=1, resolution=512, profile=full)
  camera saved (T=151, image_size=(1080, 1920), CUDA peak reserved=18568 MiB)

=== Step 3: SAM Feature ===
  [sam] batch=16, requested=16
  SAM extract: 16/151  | 4.0s  | 4.00 fps
  SAM extract: 64/151  | 11.6s | 5.51 fps
  SAM extract: 151/151 | 26.7s | 5.66 fps
  saved -> example_sam3d_feat.pt  (shape=(151, 3072))

=== Step 4: Generation ===
[ok] 56.1s -> flowhmr_latest.npz
```

换算：

| 指标 | 数值 |
|---|---|
| SAM 阶段耗时 | **26.7 s** |
| 视频帧率吞吐 | 5.66 fps |
| 等效图像吞吐 | 151 × 3 ÷ 26.7 ≈ **17 张 512² / s** |
| 单帧平均 | ≈ 177 ms（3 次 ViT-H 前向 + 3 个 decoder + 2 个 head） |
| 整个流程 | 56.1 s（SAM 占 ≈48%，其余为 bbox/VGGT/生成） |

---

## 4. 为什么慢

1. **模型本身重**：ViT-H/16+（约 0.8B）+ 1024 维、6 层 decoder + MHR head，
   这是"单图 3D 人体重建"模型，不是轻量分割网络，单张 512² 前向就不是毫秒级。
2. **每帧 3 次前向**：人体 1 次 + 左右手各 1 次，成本几乎 ×3。
   手部 bbox 依赖人体姿态（要先跑完 body 才能出），所以 3 次前向天然串行。
3. **只按帧并行、批间串行**：151 帧 / 16 = 10 个 chunk，chunk 之间无重叠，
   每批都有调度、H2D 拷贝、hook 捕获与 CPU 拼装开销。
4. **没有 torch.compile**：`stage_sam` 路径强制 `use_compile=False`，
   否则编译时间和显存都不划算；因此吃不到 kernel fusion/图优化收益。
5. **显存压力限制 batch**：VGGT 阶段峰值 18.6 GB，整机 24 GB；
   SAM 阶段虽然独占 GPU，但 `SAMBatchPlanner` 仍会保守选择 batch，
   显存不足时自动拆手部为两次前向、甚至逐帧退化成 batch=1。
6. **阶段间的模型搬移**：为了省显存，非当前阶段模型被挪到 CPU 再挪回，
   视频越长、切换越多，固定开销占比越高。
7. **与其他阶段叠加**：VGGT（1B 参数、逐帧 512）本身也很重；
   网页端若再勾选 HaMeR 手指精修，还会额外加载 HaMeR + MANO 并逐帧跑手部回归。
8. **帧数线性放大**：SAM 成本与帧数严格线性，151 帧 → 5 s 视频；
   1080p 长视频（如 30 s、900 帧）光 SAM 就要约 2.6 分钟。

---

## 5. 如何加速（按性价比排序）

| 手段 | 做法 | 预期效果 | 代价 |
|---|---|---|---|
| **手部稀疏采样** | `sam_hand_stride=2` 或 `4` | 每帧从 3 张降到 2 / 1.25 张，SAM 约降到 17–18 s / 13–14 s（估算） | 手部细节靠插值，手指动作快时略糊 |
| **加大 chunk** | `sam_chunk_size` 16 → 24/32（显存允许时） | 减少批间空转，约 5–15% | 显存峰值升高，可能触发 planner 回退 |
| **减少帧数** | `vggt_interval` 抽帧、按时间段裁剪、`max_frames` | 线性收益，最直接 | 时间分辨率下降 |
| **只看身体不要手** | 关闭手部 token（当前网页未直接暴露） | 最多降到 1/3 成本 | 手部特征缺失，手指质量下降 |
| **不要重复跑** | 只改生成参数时复用 `*_sam3d_feat.pt` 缓存 | 二次调参省掉整个 SAM 阶段 | 仅限同一视频/同一 bbox |
| **HaMeR 按需开启** | 首轮 stride=2 试，满意后再 stride=1 | 省 HaMeR 额外开销 | 手指精度略降 |
| **torch.compile** | 代码里允许 `use_compile=True`，仅 backbone | 可能 10–20%，但编译耗时长、显存涨 | 首次启动慢，遇显存错误会自动回退 |
| **更快硬件** | 4090 / A100 | 线性提升，24 GB→48/80 GB 可加大 batch | 需换机器 |

> 注意：**分辨率不能调低**。512×512 是 SAM 3D Body 的训练输入尺寸，
> 改小会直接破坏特征分布与后续 FlowHMR 的输入语义。

---

## 6. 复现与调试

```powershell
cd D:\xjq\FlowHMR-Windows
$env:TORCH_HOME = 'D:\xjq\FlowHMR-Windows\.cache\torch'

# 全流程（命中缓存会跳过对应阶段）
.\runtime\python.exe run_v2m_demo_generation.py assets\example\example.mp4

# 手部隔帧采样，观察耗时变化
.\runtime\python.exe run_v2m_demo_generation.py assets\example\example.mp4 --sam_hand_stride 2

# 改 chunk 大小
.\runtime\python.exe run_v2m_demo_generation.py assets\example\example.mp4 --sam_chunk_size 24

# 强制重算全部阶段
.\runtime\python.exe run_v2m_demo_generation.py assets\example\example.mp4 --overwrite
```

网页端对应参数在任务面板的"高级参数"：`采样间隔（sam_hand_stride）`、
`SAM 批大小（sam_chunk_size）`、`相机采样（vggt_interval）`。

---

## 7. 相关代码索引

| 内容 | 位置 |
|---|---|
| 流水线阶段 | `flowhmr/pipeline/v2m_inference.py` |
| SAM 构建 / 批处理 / 稀疏手部 | `flowhmr/utils/runtime_sam_features.py` |
| 单批 body+hand 前向细节 | `flowhmr/utils/lite_sam_infer.py` |
| 显存自适应 batch | `flowhmr/utils/sam_batch.py` |
| 模型配置（512、ViT-H+、decoder） | `models/sam-3d-body-dinov3/model_config.yaml` |
| 预处理尺寸（body / hand 均 512） | `third_party/sam-3d-body/sam_3d_body/sam_3d_body_estimator.py` |
| 网页默认参数 | `tools/web/server.py`（`DEFAULT_PARAMS`） |
