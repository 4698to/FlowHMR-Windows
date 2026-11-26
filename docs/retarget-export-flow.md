# 导出动作时的重定向流程（实现说明）

本文说明在 FlowHMR-Windows 中「导出动作」时，动作重定向（retarget）从配置、求解、烘焙到写盘的完整链路，面向需要改动或排查导出功能的开发者。

只讲操作步骤的读者请看 [动作导出与角色预览](./retarget-export.md)，本文不重复格式选择的使用建议，侧重内部流程。

---

## 1. 结论速览

- 「导出动作」不是单一链路，而是**六条导出通道**，重定向发生的位置各不相同：

  | 通道 | 重定向在哪一侧完成 |
  | --- | --- |
  | 通用 FBX（FlowHMR / Mixamo / Maya ADV / MetaHuman / iClone） | 后端按模板骨架直接写动画，不做预览角色重定向 |
  | 当前角色 FBX（`retarget_fbx`） | 浏览器先烘焙每帧骨骼，再由后端写入目标骨架 |
  | 重定向 GLB（`retarget_glb`） | 全部在浏览器完成并下载 |
  | VMD | 浏览器用 PMX 求解器烘焙，再调用 VMD 编码 |
  | VRMA | 后端 `write_humanoid_motion` 生成人形动作 |
  | 相机配对导出 | 复用上述通道 + `prepareCamera` 产物 |

- 重定向配置有两个来源，可同时存在：
  1. **内置骨架家族校正**（`boneOffsets`）：Mixamo / MetaHuman / VRM / PMX 的内置映射，只需要角度微调。
  2. **手动映射 profile**（`retarget`）：用户逐关节指定目标骨骼，作为求解器的唯一映射来源。
- 导出前必须先「保存并应用」，配置写入角色库；未持久化的编辑器草稿不会进入导出。
- 手动映射角色没有保存 profile 时，导出会直接报 `retarget.mapping_required`，不会静默退化。
- MCP / headless 导出（`flowhmr/mcp/exports.py`）只基于源 SMPL-H 身体，**不烘焙预览角色的重定向偏移**，这是与 UI 导出的重要差异。

---

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| 源动作 | 任务的 `motion.npz`：SMPL-H 52 关节的 `rest`、`joints`、`quaternions`、`contacts`、`floor` |
| 目标角色 | 在「角色」面板应用给某个人物的 FBX / PMX / VRM / GLB 模型 |
| `poseOffsets` | 预览角色的动作校正配置（v2），见第 4 节 |
| `retarget` profile | 手动映射表（v1），SMPL-H 关节索引 → 目标骨骼索引 |
| 预设 / preset | 可跨角色复用的映射 + 校正记录，骨骼用路径编码 |
| 求解器 | `createFbxCharacterRetarget`（FBX/VRM/GLB）与 `createPmxRetarget`（PMX） |
| 烘焙 | 逐帧调用求解器，把结果采样为「骨骼路径 + 位置/旋转/缩放」序列 |

---

## 3. 涉及模块

前端（`tools/web/static/js/`）：

| 文件 | 职责 |
| --- | --- |
| `motion-export.js` | 导出对话框、格式判定、按通道分派 |
| `retarget-fbx.js` | `bakeTargetFbx`：当前角色 FBX 的浏览器端烘焙 |
| `retarget-export.js` | `bakeRetargetGlb`：VRM/GLB 完整烘焙与下载 |
| `fbx-character-retarget.js` | FBX/VRM/GLB 重定向求解器、自动映射 |
| `pmx-retarget.js` | PMX 重定向求解器 |
| `retarget-profile.js` | profile 规范化、合法性校验、比例测量 |
| `pose-offsets.js` | `poseOffsets` 规范化、镜像、版本迁移 |
| `retarget-presets.js` | 预设编码/解码、兼容性筛选 |
| `retarget-editor.js` | 重定向编辑器 UI 与手势交互 |
| `preview-character.js` | 应用角色、自动套用预设、保存入口 |
| `api.js` | 角色库、预设、导出相关 HTTP 调用 |

后端：

| 文件 | 职责 |
| --- | --- |
| `tools/web/server.py` | 导出端点、评分门禁、参数校验 |
| `flowhmr/core/export/smplh2fbx.py` | 通用 FBX 与模板 profile 分派 |
| `flowhmr/core/export/fbx_profiles.py` | Mixamo / MetaHuman 内置骨架写出 |
| `flowhmr/core/export/target_fbx.py` | `write_target_fbx`：把前端烘焙结果写入目标骨架 |
| `flowhmr/core/export/vmd.py` | VMD 编码 |
| `flowhmr/core/export/vrma.py` | `write_humanoid_motion` 等 VRMA/GLB 写出 |
| `flowhmr/character_library.py` | 角色、`poseOffsets`、预设的校验与持久化 |
| `flowhmr/multishot/exports.py` | 多镜头 / 相机配对导出 |

---

## 4. 前置条件

导出动作前，链路上依次检查：

1. **任务本身可用**：`job_body_reference` 能解析出人物与源动作；多人任务要能定位到所选人物。
2. **评分门禁**：`server.py` 的 `rating_gate`（`server.py:2143`）会拦截被判定为低质量的结果，除非用户显式确认。
3. **模型可用性**：模板骨骼导出需要本地模式与已上传模板；`local_model_request`（`server.py:2326`）负责该判断。
4. **角色已应用**：`retarget_fbx` / `retarget_glb` / `metahuman` / 当前角色 Mixamo 必须已加载 actor，否则对话框提示 `retarget_fbx_required` 并禁用导出。
5. **重定向已保存**：`saveEntry` 成功写入角色库（或预设）。仅存在于内存的编辑器草稿不参与导出。
6. **单人限制**：MetaHuman、Maya ADV、当前角色 FBX 均要求所选人物唯一；多人需逐个导出。
7. **导出锁**：同一任务同时只允许一个 FBX 导出请求，避免重复写缓存。

---

## 5. 导出格式矩阵

格式值来自 `motion-export.js` 的 formats 定义，服务端端点见 `server.py`。

| 格式值 | UI 名称 | 端点 | 需要目标角色 | 求解位置 | 主要产物 |
| --- | --- | --- | --- | --- | --- |
| `flowhmr` | FlowHMR FBX（源动作） | `GET/POST /api/jobs/<id>/fbx` | 否 | 后端 | SMPL-H 骨架 FBX |
| `mixamo`（通用） | Mixamo 通用骨架 | `GET/POST /api/jobs/<id>/fbx` | 否 | 后端模板 | Mixamo 骨架 FBX |
| `mixamo`（当前角色） | Mixamo 当前角色 | `POST /api/jobs/<id>/target-fbx` | 是 | 浏览器 + 后端 | 角色原骨架 FBX |
| `maya_adv` | Maya AdvancedSkeleton | `GET/POST /api/jobs/<id>/fbx` | 否 | 后端模板 | ADV 骨架 FBX |
| `metahuman` | MetaHuman FBX | `POST /api/jobs/<id>/target-fbx` | 是 | 浏览器 + 后端 | MetaHuman 身体 FBX |
| `mixamo_iclone` | iClone | `GET/POST /api/jobs/<id>/fbx` | 否 | 后端模板 | iClone 兼容 FBX |
| `retarget_fbx` | 当前角色 FBX | `POST /api/jobs/<id>/target-fbx` | 是 | 浏览器烘焙 | 目标骨架 + 校正后动画 |
| `retarget_glb` | VRM 角色 GLB | 无（本地下载） | 是 | 浏览器 | 含动画的 GLB |
| `vmd` | MMD VMD | `GET /api/jobs/<id>/vmd` 或 `POST /vmd` | 可选 | 浏览器（PMX） | VMD，多人打包 ZIP |
| `vrma` | VRM Animation | `GET /api/jobs/<id>/vrma` | 否 | 后端 | VRMA，多人打包 ZIP |
| `npz` | 源动作 NPZ | 直接下载 | 否 | 无 | `motion.npz` |

相机配对格式集合为 `PAIRED_CAMERA_FORMATS = {flowhmr, vmd, npz}`，需要先调用 `api.prepareCamera` 生成 `camera.npz` 再打包。

---

## 6. 重定向数据模型

### 6.1 `poseOffsets` v2

由 `pose-offsets.js` 的 `normalizePoseOffsets` 规范化，`OFFSET_VERSION = 2`。

```jsonc
{
  "rotations":     { "0": [x, y, z] },      // 仅关节 0..36，单位度，每轴 ±180
  "translations":  { "15": [x, y, z] },     // 每轴 ±5 米
  "mirror":        false,                    // 左右自动镜像
  "ikMode":        "ik" | "fk",
  "rootRotation":  [x, y, z],                // ±180 度
  "rootTranslation": [x, y, z],              // ±5 米，Y 为高度
  "pmxBaselineVersion": 1,                   // PMX 基线已迁移
  "vrmBaselineVersion": 1,                   // VRM 头颈基线已迁移
  "retarget": { /* 见 6.2 */ },              // 手动映射
  "boneOffsets": {                           // 内置骨架校正
    "rotations":    { "12": [x, y, z] },
    "translations": { "0": [x, y, z] }
  }
}
```

要点：

- 旋转项只保留 0..36。37..51 为右手与手指镜像的派生区，通过 `makePoseOffsets` 计算，不直接存储。
- 头部有基础俯仰 `HEAD_X_BASE = -15`，VRM 使用 `normalizeVrmPoseOffsets` 做一次性基线迁移（`vrmBaselineVersion`）。
- 旧版共用手指/身体校正通过 `flattenJointCorrections` 展开到逐关节，并扣除内置中性值。
- `ikMode` 仅在显式保存过 `ik` / `fk` 时保留，避免把默认值写进配置。

### 6.2 `retarget` profile v1

由 `retarget-profile.js` 的 `normalizeRetargetProfile` 规范化：

```jsonc
{
  "version": 1,
  "map": [ 52 个整数 ],       // SMPL-H 关节 -> 目标骨骼索引，-1 表示未映射
  "root": -1,                  // 根位移驱动骨骼
  "ik": {                      // 四组两骨 IK 末端
    "leftFoot": -1, "rightFoot": -1,
    "leftHand": -1, "rightHand": -1
  },
  "rotations":    { "关节": [x, y, z] },   // ±180 度
  "translations": { "关节": [x, y, z] }    // ±5 米
}
```

约束常量：

- `JOINT_COUNT = 52`
- `BODY_REQUIRED = [0,1,2,3,4,5,7,8,15,16,17,18,19,20,21]`：躯干、头、四肢根段必须映射
- `CHAINS = { leftFoot:[1,4,7], rightFoot:[2,5,8], leftHand:[16,18,20], rightHand:[17,19,21] }`

`profileIssue` / `assertRetargetProfile` 校验项：

| 错误键 | 触发条件 |
| --- | --- |
| `retarget.missing` | 必需关节未映射 |
| `retarget.invalid_bone` | 索引越界或指向不存在的骨骼 |
| `retarget.duplicate` | 两根源关节映射到同一目标骨骼 |
| `retarget.hierarchy` | 目标骨骼父子链不符合人体顺序 |
| `retarget.invalid_ik` | IK 末端非法（被占用、是骨骼祖先、PMX IK 目的骨不匹配） |
| `retarget.invalid_root` | 根骨不可动、被 IK 占用或映射不在根骨子树内 |

### 6.3 存储与预设

- 角色级配置：`POST /api/character-library`（`api.saveActorCharacter`），按 actor 与人物 ID 持久化。
- 预设：`api.saveRetargetPreset` / `api.saveBuiltinRetargetPreset`，内置预设可按 profile 覆盖与恢复。
- 无角色库时回退 `localStorage`，键由 `editKey` / `temporaryKey` 生成。
- 预设用**骨骼路径**而非索引编码（`retarget-presets.js:29` 的 `skeletonPaths`），忽略网格名、索引与比例，保留完整父子链；单路径上限 384 字符。
- 路径不唯一时抛 `retarget.preset_ambiguous`，拒绝复用。
- `reusablePresets`（`retarget-presets.js:89`）会检查版本、kind、52 项 links、IK、校正值、必需身体关节是否都能在当前骨架解析，内置预设还要求与当前 native 骨架一致。

---

## 7. 求解器核心流程

### 7.1 FBX / VRM / GLB：`createFbxCharacterRetarget`

入口 `fbx-character-retarget.js:316`，签名：

```js
createFbxCharacterRetarget(root, asset, motion, { poseOffsets = null, libraryId = null })
```

内部按帧执行 `apply(frame)`，主要阶段：

1. **确定映射**
   - 有 `poseOffsets.retarget`：直接 `assertRetargetProfile` 后使用。
   - 内置 profile（`mixamo` / `metahuman` / `vrm`）：`layout()` 按命名规则解析，缺失或歧义抛 `retarget.mapping_required`。
   - `custom` 且无手动映射：拒绝创建，导出层提示 `retarget.mapping_required`。
2. **建立骨架记录**：`hierarchy(root)` 递归收集骨骼，缓存绑定矩阵、局部变换、父子关系，校验缩放为正值且近似均匀（差异 > 0.2% 报错）。
3. **朝向对齐**：用左右肩/髋计算 heading，消除角色朝向与源动作的偏差；角色组的 yaw/平移由调用方负责，不进入求解。
4. **比例测量**：`measureProportions` 得到身高比、步幅比、肢体长度比；任一比值 < 0.002 或身高 ≤ 0.05 抛 `retarget.invalid_length`。
5. **绑定校正**：`restAlignment` 对齐肢体段方向；手掌用腕/食指/中指/小指构造正交基；踝与脚掌只取水平方向，保留绑定的鞋底俯仰。
6. **脊柱与颈**：脊柱三段插值分配躯干旋转；MetaHuman 使用双 neck 分段。
7. **逐关节姿态**：写入目标局部旋转，叠加 `customJointEdits`（profile 的 `rotations` / `translations`）。
8. **镜像**：`mirror` 开启时按身体左右平面反射旋转与位移。
9. **脚步处理**
   - 脚底探针 `cachedSoleProbes` 计算鞋底相对踝的高度，做脚底贴合。
   - 脚部锚点锁定：支撑脚在接触期固定世界位置。
   - `ikMode === "ik"` 时使用两骨 IK 解算腿/臂；否则走 FK。
10. **根位移**：按 `travelScale` 缩放位移，应用 `rootRotation` / `rootTranslation`。
11. **输出**：`apply(frame)` 更新骨骼世界矩阵；`setPoseOffsets`（`fbx-character-retarget.js:701`）规范化配置、更新 `activeIk`、清空 `lastFrame` 缓存。

暴露给烘焙层的接口：`tracked`（参与导出的骨骼）、`editableCorrections`、`nativeIkAvailable`、`nativeControllers`、`proportions`、`lengths`。

### 7.2 PMX：`createPmxRetarget`

入口 `pmx-retarget.js:422`，签名：

```js
createPmxRetarget(mesh, rest, motion, { poseOffsets = null, independentIk = false })
```

- 使用 PMX 原始骨名、肢体比例、鞋底与控制链。
- PMX 自动补 IK 时，脚踝骨需带 `ikEffector` 指向脚掌/脚尖。
- `rootMappingIssue` 对 PMX 放宽：允许 Center 与 IK 柄挂在 all-parent 的不同分支下，由 VMD 求解器分别摆位。

---

## 8. 重定向编辑器操作流程

入口 `openRetargetEditor`（`retarget-editor.js:31`），由 `preview-character.js` 的 `editMapping`（`preview-character.js:704`）打开。

1. **加载上下文**：`api.getCharacterLibrary` 取角色库；读取 `rememberedPose`；`describe` 生成角色描述；筛选 `reusablePresets` 作为预设候选。
2. **自动映射**（`autoMatch`，`retarget-editor.js:621`）
   - 先按 `bodyGuide` 顺序填充空白关节（`suggestBodyAliases`）。
   - 每填一个就校验 `jointFitsChain` 与父子 `descends`，不合法则撤销。
   - 手部缺失时进入手部向导，先绑腕再逐节绑指。
   - PMX 额外自动补 IK 末端。
3. **手动调整**
   - 左侧点 SMPL-H 关节，右侧 3D 视图点目标骨骼，或按名称搜索。
   - `R` / `G` / `S` + 鼠标临时旋转/平移/缩放探查影响范围，结束复位。
   - 填「校正值」，旋转 ±180°，根骨可按米平移。开启「自动镜像」时对侧实时同步。
4. **预设操作**：载入、保存、删除自定义预设；内置预设可覆盖与「恢复默认」。
5. **确认**：`accept(offsets, lifetime, preset)` → 触发 `put(final = true)`。
6. **保存**：`buildEntry` 用当前配置实测求解一次；成功则 `saveEntry`（`preview-character.js:661`）：
   - 原生存档：`saveLibraryCharacter`
   - 角色存档：`saveActorCharacter(actor, id, poseOffsets, true, preset)`
   - 无库：`saveRetargetPreset` + `saveRetargetPresetThumbnail`
   - 失败回退 `localStorage`

保存成功后，导出对话框才会把该人物的配置视为可用。

---

## 9. 各导出通道流程

### 9.1 通用 FBX（后端模板）

`motion-export.js` 在 `actor` 为空且格式不是 `vmd` / `retarget_glb` 时走此路（`motion-export.js:265`）。

1. `api.getMotionDownload` 请求 `GET /api/jobs/<id>/fbx?profile=...`。
2. 服务端 `smplh2fbx.py` 按 profile 分派：
   - `flowhmr`：SMPL-H 原骨架。
   - `mixamo` / `mixamo_iclone` / `maya_adv`：内置模板，可选第 −1 帧 T/A Pose。
   - `metahuman`：走 `fbx_profiles.write_metahuman_animation`。
3. 浏览器拿到 Blob 后直接下载。

此通道不使用预览角色的 `poseOffsets`，只有模板自带的比例与绑定。

### 9.2 当前角色 FBX（`retarget_fbx`）

1. `api.getFbxActor("retarget_fbx")` 取 actor。
2. `bakeTargetFbx({asset, libraryId, motion, poseOffsets, offset, yaw, ...})`（`retarget-fbx.js:8`）：
   - 克隆角色，清除动画。
   - 逐帧 `setPoseOffsets` + `apply`。
   - 收集 `rig.tracked` 骨骼，按世界矩阵分解位置/四元数/缩放。
   - 路径以角色根为基准，去掉 `Armature|` 前缀。
   - 四元数做符号连续化，避免插值翻转。
   - 输出 `{ paths, samples, frameCount, fps }`。
   - 路径数、采样点数与 1600 万数值上限校验，超限抛错。
3. `api.exportTargetFbx(jobId, people, profile, actor.character, baked, withSkin, signal)` → `POST /api/jobs/<id>/target-fbx`。
4. 服务端 `write_target_fbx` 把 tracks/samples 写入目标 FBX 的原始绑定骨架：
   - 校验路径唯一、≤1024 条、数值非 NaN/奇异、剪切矩阵合法。
   - 带蒙皮时一并使用上传的原 FBX（≤64 MB）。
   - MetaHuman 需要额外的水平根处理。
5. 返回文件并下载。

### 9.3 Mixamo 当前角色 / MetaHuman

与 9.2 相同端点，区别在 profile 与后端写出器：

- `mixamo`（当前角色）：`api.getFbxActor("mixamo")`，前端烘焙后 `target-fbx`。
- `metahuman`：`write_metahuman_animation` 处理骨盆水平、双 neck 与手指层级；服务端拒绝带 mesh 的 MetaHuman 导出。

### 9.4 重定向 GLB（`retarget_glb`）

1. 仅对 VRM / GLB 角色启用，FBX 角色禁用该选项。
2. `bakeRetargetGlb(actors, stem, signal)`（`retarget-export.js:10`）：
   - 克隆角色，按人物编号水平摆放，处理间距、朝向、镜像与可见性（轨道名 `flowhmr_visible_<n>`）。
   - 逐帧应用重定向。
   - `GLTFExporter` 以 binary 导出。
   - 纹理 ≤64 MB，总输出 ≤512 MB。
3. 返回 Blob，浏览器直接下载，不经服务端。

### 9.5 VMD

1. 选择目标：`target = current`（当前 PMX）或 `generic`（通用骨架）。
2. `makeVmd`（`motion-export.js:340`）：
   - 为每个人物构建 PMX 求解器（当前 PMX 或通用骨架）。
   - 30 FPS 逐帧烘焙，写入关节校正、根骨调整与脚部 IK/FK。
3. 多人分别编码后打包 ZIP。
4. 可选走 `POST /vmd`（`motion-export.js:346`）或 `GET /api/jobs/<id>/vmd`（`server.py:3925`），缓存版本为 `vmd_v2`。

### 9.6 VRMA

1. 请求 `GET /api/jobs/<id>/vrma`（`server.py:3963`）。
2. 服务端 `vrma.write_humanoid_motion` 写入人形骨骼动作、根位移与默认头颈修正，保留源帧率。
3. 多人物每人一个 VRMA，打包 ZIP，缓存版本 `vrma_v1`。
4. 不包含网格、材质、表情和预览角色的手动骨骼微调。

### 9.7 相机配对导出

- 仅 `flowhmr` / `vmd` / `npz` 支持。
- 先 `api.prepareCamera` 生成相机数据（可复用缓存，`force` 强制重算）。
- 再调用对应导出通道，把动作与 `camera.npz` 一起打包。

---

## 10. 缓存、锁与限制

| 项 | 值 / 行为 | 位置 |
| --- | --- | --- |
| FBX 导出锁 | 同一任务串行 | `server.py` 导出段 |
| VMD 缓存版本 | `vmd_v2` | 服务端 |
| VRMA 缓存版本 | `vrma_v1` | 服务端 |
| 烘焙路径上限 | 1024 条 | `target_fbx.py` |
| 烘焙数值上限 | 1600 万 | `retarget-fbx.js` / `target_fbx.py` |
| 蒙皮源文件上限 | 64 MB | `target_fbx.py` |
| GLB 输出上限 | 512 MB | `retarget-export.js` |
| GLB 纹理上限 | 64 MB | `retarget-export.js` |
| 预设路径长度 | 384 字符 | `retarget-presets.js` |
| 旋转 / 位移范围 | ±180° / ±5 m | `pose-offsets.js` |
| 比例合法性 | 比值 ≥ 0.002 且身高 > 0.05 | `retarget-profile.js` |
| 单人限制 | MetaHuman / Maya ADV / 当前角色 FBX | `motion-export.js` |
| 模板上传 | 仅本地模式 | `server.py` `local_model_request` |

---

## 11. 端到端时序

```
用户点击「导出动作」
  └─ motion-export.js 判定格式与 actor
      ├─ 通用 FBX / VRMA ──> GET /api/jobs/<id>/{fbx|vrma} ──> 后端模板写出 ──> 下载
      ├─ retarget_fbx / mixamo(当前) / metahuman
      │     ├─ bakeTargetFbx：逐帧求解 -> { paths, samples }
      │     ├─ POST /api/jobs/<id>/target-fbx
      │     └─ write_target_fbx 写入原骨架（可含蒙皮） -> 下载
      ├─ retarget_glb
      │     └─ bakeRetargetGlb -> GLTFExporter -> 本地下载
      ├─ vmd
      │     ├─ 前端 PMX 求解 30 FPS -> makeVmd
      │     └─ POST /vmd 或 GET /api/jobs/<id>/vmd -> ZIP 下载
      └─ 相机配对
            ├─ prepareCamera -> camera.npz
            └─ 对应通道导出 -> 动作 + 相机打包
```

每次烘焙前都会经过：角色库读取 → profile 校验 → 比例测量 → 逐帧求解 → 路径/数值上限校验。

---

## 12. 常见错误与排查

| 报错键 / 现象 | 原因 | 处理 |
| --- | --- | --- |
| `retarget.mapping_required` | 自定义角色没有保存 profile；或内置骨架缺必需骨骼/命名歧义 | 打开重定向编辑器完成映射并「保存并应用」 |
| `retarget.missing` | 必需身体关节未映射 | 补齐 `BODY_REQUIRED` 中的关节 |
| `retarget.duplicate` | 两根源关节指向同一骨骼 | 重新分配目标骨骼 |
| `retarget.hierarchy` | 目标骨骼父子顺序不符合人体链 | 检查是否绑到了辅助骨或错误层级 |
| `retarget.invalid_ik` | IK 末端被占用或是祖先/后代关系不合法 | 换用正确的脚踝/手腕末端骨 |
| `retarget.invalid_root` | 根骨不可动或不在映射子树内 | 指定可动的根骨，或清空根映射 |
| `retarget.invalid_length` | 肢体长度比过小或身高异常 | 检查模型缩放是否为 0 或非均匀 |
| `retarget.preset_ambiguous` | 骨骼路径在同一骨架内不唯一 | 改用唯一路径的预设或删除冲突预设 |
| `target_fbx_ambiguous_path` | 烘焙结果里有重复骨骼路径 | 检查角色是否有重名骨骼 |
| 导出按钮置灰，提示 `retarget_fbx_required` | 未应用对应角色 | 先在「角色」面板应用模型 |
| 评分门禁拦截 | 结果评分过低 | 按提示确认或重新生成 |
| 仅本地模式可用 | 模板上传/本地模型限制 | 切换到本地模式或改用通用骨架 |
| MCP 导出没有角色校正 | headless 只基于源 SMPL-H | 需要角色校正时走 UI 导出 |

---

## 13. 代码索引

前端：

- `tools/web/static/js/motion-export.js`：`initMotionExport`（15）、格式与 actor 判定（56–106）、`retarget_fbx` 烘焙（212–224）、通用导出分派（265）、`retarget_glb`（281）、蒙皮选项（290）、`makeVmd`（340）、`POST /vmd`（346）
- `tools/web/static/js/retarget-fbx.js:8`：`bakeTargetFbx`
- `tools/web/static/js/retarget-export.js:10`：`bakeRetargetGlb`
- `tools/web/static/js/fbx-character-retarget.js`：`suggestedFbxMap`（102）、`createFbxCharacterRetarget`（316）、`pose`（439）、`fk`（463）、`plants`（617）、`setPoseOffsets`（701）
- `tools/web/static/js/pmx-retarget.js:422`：`createPmxRetarget`
- `tools/web/static/js/retarget-profile.js`：常量、`normalizeRetargetProfile`、`profileIssue`、`assertRetargetProfile`、`jointFitsChain`、`measureProportions`
- `tools/web/static/js/pose-offsets.js`：`normalizePoseOffsets`、`normalizeVrmPoseOffsets`、`flattenJointCorrections`、`makePoseOffsets`、`readStoredPose` / `storedPose`
- `tools/web/static/js/retarget-presets.js`：`skeletonPaths`（29）、`makeRetargetPreset`（77）、`reusablePresets`（89）
- `tools/web/static/js/retarget-editor.js:31`：`openRetargetEditor`；`autoMatch`（621）；`closeEditor`（751）
- `tools/web/static/js/preview-character.js`：`automaticCharacter`（345）、`saveEntry`（661）、`editMapping`（704）
- `tools/web/static/js/api.js`：`getCharacterLibrary`（107）、`saveActorCharacter`（121）、`saveRetargetPreset`（127）、`saveBuiltinRetargetPreset`（133）、`prepareCamera`（152）、`getMotionDownload`（179）、`exportTargetFbx`（196）

后端：

- `tools/web/server.py`：`rating_gate`（2143）、`local_model_request`（2326）、`/api/jobs/<id>/fbx`（3658）、`/api/jobs/<id>/target-fbx`（3778）、`/api/jobs/<id>/vmd`（3925）、`/api/jobs/<id>/vrma`（3963）
- `flowhmr/core/export/target_fbx.py`：`write_target_fbx`
- `flowhmr/core/export/smplh2fbx.py`：通用 FBX 与模板分派
- `flowhmr/core/export/fbx_profiles.py`：`write_metahuman_animation`、`write_mixamo_animation`
- `flowhmr/core/export/vmd.py`：VMD 编码
- `flowhmr/core/export/vrma.py`：`write_humanoid_motion`
- `flowhmr/character_library.py`：角色与预设持久化
- `flowhmr/mcp/exports.py`：headless 导出（不含预览角色校正）
- `flowhmr/multishot/exports.py`：相机配对导出

---

## 14. 与现有文档的关系

- [动作导出与角色预览](./retarget-export.md)：面向使用者的格式选择、外部软件导入步骤、编辑器操作建议。
- 本文：面向开发者的配置结构、校验规则、求解阶段、烘焙协议与错误键索引。

修改导出逻辑时，优先确认三件事：`poseOffsets.retarget` 是否被 `assertRetargetProfile` 接受、烘焙路径是否能在目标骨架中唯一解析、产物是否超过第 10 节的限制。
