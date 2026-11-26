# 接入公共动作参考库资源库（上传 / 浏览）方案

> 状态：方案文档（仅设计，不含代码改动）
> 依据接口文档：`http://anim.nd.com.cn/api/doc-help/reference_lib_api.md`（原始 Markdown 追加 `?raw=1`，本次新增 §4.7 上传资源、§4.10 上传 3D 资源并绑定）
> 前置文档：[接入公共动作参考库用户管理（OAuth2 登录）方案](reference-lib-user-integration.md)（登录链路已实现，本文只增量设计资源库）
> 范围：`tools/web`（Flask 后端 + 原生 JS 前端）；不改推理流水线、不改 `job.json` 产出结构、不改 MCP、不改 GPU 调度

---

## 1. 结论与范围

对端新增了两个上传接口后，FlowHMR 的结果可以直接变成参考库档案，形成闭环：

1. **发布**：任务完成 → 上传成片视频（§4.7）→ 上传由该视频生成的动作文件并绑定档案（§4.10）。
2. **浏览**：在 Web 端检索公共参考库、看详情、预览/下载图片与视频（§4.2–§4.5）。
3. **回写**：后续补充或替换关联动作（§4.8 / §4.9）。

关键判断：

- **必须走 §4.7 二进制上传，不走 §4.6 URL 登记**：FlowHMR 产物在本地/内网磁盘（`work_dir/<job>/...`），没有公网可访问 URL，对端无法回源抓取；§4.6 只适合"已经有对象存储 URL"的场景。
- **上传接口只需用户 Bearer，不需要应用签名**：§4.7 / §4.10 属于"用户级接口"，因此不需要 `X-App-Id / X-Signature`，也不需要处理 multipart 的 body 哈希签名。当前 [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2317) 的 `reference_lib_request` 只做 JSON + 应用签名，上传需要新增独立的 multipart 调用路径。
- **写操作需要 `reference_lib:write`**：当前部署配置只申请了 `reference_lib:read`（[auth_config.json](file:///d:/xjq/FlowHMR-Windows/tools/web/auth_config.json#L7)），发布功能必须让用户重新授权；**只读浏览可以先上线，不需要改动授权**。
- **资源归属按任务创建人，而不是当前操作者**：上传一律使用 `job["owner_id"]`（参考库侧为 `rl:<id>`）对应账号的 token；平台记录的 uploader、资源归属、`external_ref` 都指向创建人。操作者（包括管理员）不能换成自己的身份上传，也不能转移归属；创建人没有可用会话或没有写权限时直接拒绝发布，并提示创建人重新登录授权。
- **发布不可撤回**：对端没有删除资源接口，一旦上传即进入公共参考库（`anims_public` 可见）。因此采用"评分提交即自动上传"（用户不可选择），评分弹窗必须明确提示会自动备份。

### 1.1 明确不做

- 不做参考库的删除/下架（对端无接口），不伪造 `source=svn` 的同步数据。
- 不上传用户原始输入视频：只上传**成片**与**动作文件**，避免把用户素材发布到公共库。
- 不做"以操作者身份代传"：默认只允许创建人本人发布；`allow_admin_publish=true` 时管理员可代为触发，但凭证与归属固定为任务创建人，也不提供归属转移。
- 不发布没有参考库身份的任务：`mcp` / `legacy` / 本地 uid（非 `rl:<id>`）创建的任务一律不可发布。
- 不改 `job.json` 字段结构；发布状态独立存储（见 §7）。

---

## 2. 对端接口增量摘要

| 能力 | 接口 | scope | 本期用途 |
| --- | --- | --- | --- |
| 资源列表 | `GET /resources`（`media_type/keyword/child_type/child_id/page/page_size`） | read | 资源库首页、搜索 |
| 资源详情 | `GET /resources/{id}` | read | 详情面板 |
| 下载资源 | `GET /resources/{id}/download?kind=image\|video` | read | 下载按钮（302 到对象存储） |
| 批量取链接 | `POST /resources/links`（≤200 个 ID） | read | 列表缩略图/播放地址 |
| 登记资源 | `POST /resources`（已有 URL） | write | **不用**（本地文件无公网 URL） |
| 上传资源 | `POST /resources/upload`（multipart） | write | **发布成片** |
| 回写 child_ids | `POST /resources/{id}/children`（append/replace） | write | 补充/替换关联动作 |
| 解绑 child_ids | `POST /resources/{id}/children/remove` | write | 更正误绑 |
| 上传 3D 并绑定 | `POST /resources/{id}/children/upload`（multipart） | write | **发布动作文件** |

### 2.1 §4.7 上传资源（本方案核心）

- `multipart/form-data`，用户级 Bearer。
- 表单字段：`file`（必填）、`name`、`description`、`task_id`（写入 `external_ref.task_id`）、`child_ids`（**JSON 字符串**，仅 `model/anims/folders`）。
- 支持格式：`.png/.jpg/.jpeg/.webp/.gif/.mp4/.avi/.mov/.ts`；其他 → `415 unsupported_media_type`。
- 文件写入应用配置的 `upload_root_id` 目录（为 `0` 回退参考库根目录 `326353497`）。
- 返回 `data` 为登记后的资源对象（`source=third_party`，`resource_id` 即资源中心 ID）。
- **不支持自定义 `resource_id`，也没有幂等键**：重复调用会产生重复档案。

### 2.2 §4.10 上传 3D 资源并绑定

- 路径带父档案：`POST /resources/{resource_id}/children/upload`。
- 表单：`file`（可多份）、`related_type`（`model` 默认 / `anims`）、`task_id`、`remark`。
- 上传后按"**父档案同名 + 原后缀**"重命名：父档案 `行走.mp4` + 上传 `character.fbx` → 存储为 `行走.fbx`。
- 返回更新后的资源对象，并附带 `uploaded[]`（`resource_id/name/original_name/file_extension`）。
- 档案不存在 `404 not_found`；缺文件/超限/`related_type` 非法 `400 invalid_request`；上传失败 `502 upload_failed`；绑定失败 `500 update_failed`。

### 2.3 读接口要点

- 列表 `page_size` 默认 20、最大 100；列表项只有 `resource_id/name/media_type`，缩略图需再调 §4.5 取链接。
- §4.4 下载是 **302**，`Location` 指向对象存储（图片 `.webp`、视频 `.mp4`），不是文件流。
- §4.5 返回顺序与请求一致，不存在的项返回 `{resource_id, not_found: true}`。
- 资源对象固定包含 8 类 `child_ids` 键（`folders/anims/model/texture/audio/video/effect/other`），只有前三类可写。

---

## 3. 资源映射：FlowHMR 产物 → 参考库档案

| 参考库对象 | 来源（现有产物） | 说明 |
| --- | --- | --- |
| 视频档案 | `work_dir/<job>/input/input_30fps.mp4`（`SUB_30fps.mp4`） | 成片主档案，`media_type=video`；作为后续 3D 绑定的父档案 |
| 预览图 | 评分时前端抓取的 3D 视口截图（canvas → JPEG，≤1280 宽） | 随动作一起以 `preview_file` 传给 §4.10，作为该 3D 资源的缩略图；视口未加载时省略 |
| 动作文件 | 优先：评分时前端烘焙的当前角色 FBX（带蒙皮，`POST /api/jobs/<id>/library-motion` 落盘）；回退：`/api/jobs/<id>/fbx?mesh=1` 服务端 SMPL-H 产物 | 通过 §4.10 绑定到视频档案 |
| 任务标识 | `job["id"]` | 写入 `external_ref.task_id`，便于溯源与去重排查 |
| 上传身份 | `job["owner_id"]`（参考库账号 `rl:<id>`，见 [job_owner](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2755)） | 用创建人会话 token 上传；平台 uploader 即创建人，与谁点击"发布"无关 |
| 名称 | `export_stem(job)`（文件名去扩展名 + 人物编号） | 自动使用默认值，不提供修改 |
| 备注 | 自动取评分弹窗中的「备注」（写入 `rating.comment`） | 写入视频 `description` 与动作 `remark` |
| 评分 | 自动取任务本地评分 `rating.score`（0–5） | 写入上传请求 `rating` |
| 标签 | 自动取评分弹窗中的「标签」（写入 `rating.tags`，逗号分隔） | 写入上传请求 `tags` |

### 3.1 动作文件的可用边界（重要）

| 动作格式 | 是否可后台自动发布 | 原因 |
| --- | --- | --- |
| `flowhmr` / `mixamo` / `maya_adv` / `mixamo_iclone` / `metahuman` FBX | 可 | 服务端按模板写出（[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L4365) `api_fbx`） |
| VRMA | 可 | 服务端生成（[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L4670)） |
| VMD | 可（通用骨架） | 服务端接口存在（[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L4632)），但"当前 PMX 角色"结果依赖浏览器烘焙 |
| `retarget_fbx` / 当前角色 Mixamo / MetaHuman 角色版 | 可（已有角色时） | 评分时前端逐帧烘焙并 `POST /target-fbx` 得到带蒙皮 FBX，再上传到 `/api/jobs/<id>/library-motion` 暂存，发布时优先使用；未应用角色则回退服务端 `mesh=1` FBX |

发布动作时：视口已应用角色则上传带蒙皮的当前角色 FBX（`retarget_fbx` 烘焙结果）；未应用角色则回退服务端 `mesh=1` 的 SMPL-H FBX。`mixamo`/`metahuman` 模板仍只在 UI 手动导出，不进入自动发布。

### 3.2 `related_type` 语义

- `anims`：FlowHMR 输出的**动画**（带动作的 FBX/VRMA/VMD），默认用这个。
- `model`：带蒙皮的角色模型文件；仅当用户上传"角色模型 + 动画"时才用。
- `related_type` 固定为 `anims`，避免把纯动画误标为模型。

---

## 4. 发布流程与状态机

```
浏览器                       FlowHMR Flask                        参考库
  │ 勾选"同时发布动作" + 确认     │                                     │
  │ POST /api/library/publish    │                                     │
  │─────────────────────────────▶│ 校验：创建人会话/评分/角色   │
  │                              │ 记 publish 记录(state=queued)        │
  │◀── 202 {state:"uploading"} ──│ 后台线程/任务                        │
  │                              │ 取 job.owner_id 对应会话的 token     │
  │                              │ 1) 读取 input_30fps.mp4（只读句柄）  │
  │                              │ POST /resources/upload (Bearer)────▶│
  │                              │◀── resource_id / 资源对象 ──────────│
  │                              │ 2) 生成动作文件到临时目录             │
  │                              │ POST /resources/<id>/children/upload▶│
  │                              │◀── uploaded[] / 资源对象 ───────────│
  │ GET /api/library/publish/<id>│ 落盘 state=done/partial/failed       │
  │◀── 状态与 resource_id ───────│                                     │
```

说明：图中 Bearer 始终是**任务创建人**的 access token（由服务端从其会话中取出），不是发起请求的浏览器用户。

状态：`queued → uploading → uploaded_video → uploading_motion → done | partial | failed`

- `partial`：视频成功、动作失败（可单独重试动作，不重复传视频）。
- 幂等策略：视频步骤成功即写 `video.resource_id`，重试从"第一个未完成步骤"继续；**已成功的步骤永不重传**（对端无删除接口，重传即脏数据）。
- 并发：同一 `job_id` 同时只允许一个发布任务（进程内锁 + 落盘状态）。
- 失败不自动重试：上传类错误可能已部分生效，自动备份失败只记录状态与原因，不阻塞评分、不打扰用户。

### 4.1 发布门禁

按顺序校验，任一失败直接返回，不做任何上游调用：

1. 请求者已登录；创建人 = `job_owner(job)` 且必须是 `rl:<id>`（`mcp`/`legacy`/本地 uid 直接拒绝）。
2. 请求者是创建人本人；若非本人，必须是管理员且 `allow_admin_publish=true`（代发布凭证仍用创建人的，见 §5.2）。
3. 任务 `status=done`，且成片文件存在。
4. `rating_gate(job)` 通过（复用 [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2651) 语义）。
5. 评分保存成功后自动发布：`rating` 取本地评分 `rating.score`，`tags` 取 `rating.tags`，`description`/`remark` 取 `rating.comment`；`job["allow_library"]` 字段保留但前端不再暴露（恒为 `true`）。
6. **创建人**存在可用会话（`AUTH_SESSIONS` 中有其 `owner_id` 且可续期），否则 `library_owner_session_missing`。
7. **创建人**会话的 `scope` 含 `reference_lib:write`，否则 `library_owner_scope_missing`，提示"请任务创建人重新登录并授权发布"。
8. 创建人账号仍有 `anims_public`（对端写操作也会二次校验，这里提前拦）。

---

## 5. 服务端设计（FlowHMR 侧）

### 5.1 新增路由

| 接口 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/library/config` | GET | 登录 | `{enabled, max_upload_mb}`；带 `?job_id=` 时额外返回 `{owner_id, owner_name, owner_has_write, owner_session_ok, can_publish, reason}` 供发布按钮置灰判断 |
| `/api/library/publish` | POST | 登录（创建人本人或管理员） | `{job_id, name?, description?, publish_motion?, motion_format?, related_type?, consent:true}` → 202；上传凭证固定取创建人 |
| `/api/library/publish/<job_id>` | GET | 登录 + 属主 | 发布状态、已生成 `resource_id`、错误码 |
| `/api/library/resources` | GET | 登录 | 透传 §4.2 参数，补 5 分钟内存缓存 |
| `/api/library/resources/<rid>` | GET | 登录 | 透传 §4.3 |
| `/api/library/resources/links` | POST | 登录 | 透传 §4.5，校验 ≤200 |
| `/api/library/resources/<rid>/file` | GET | 登录 | 服务端请求 §4.4（**不跟随重定向**），把 `Location` 以 302 返回浏览器 |

说明：

- 浏览器永远不接触 `access_token`；所有读接口由服务端带 Bearer 转发。
- §4.4 不能把链接缓存太久（对象存储地址可能带时效），`Location` 直接 302，不落库。
- 列表/详情做短 TTL 缓存，降低上游调用量（应用级 60 次/分钟的限制虽针对签名接口，用户级保守按同量级处理）。
- 新增上传调用封装（与现有 [reference_lib_request](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2317) 并列）：`reference_lib_upload(endpoint, file_path, fields, access_token)`，用 `requests` 的 `files=` 流式读盘，**不要**把整段视频读进内存；显式 `timeout`（建议连接 10 秒、读 600 秒）。

### 5.2 session 与 token（按创建人取凭证）

当前 [auth_session()](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2245) 只读浏览器 Cookie，返回**操作者**会话，不能直接用于发布。需要新增按属主查找会话的能力：

```python
def owner_auth_session(owner_id, require_scope="reference_lib:write"):
    """任务创建人的可用会话：取该 owner 最近活跃的 sid，必要时续期。"""
    # 1) 在 AUTH_SESSIONS 中筛 owner_id 匹配的记录，按 last_seen 取最新
    # 2) 会话过期(AUTH_SESSION_TTL) 或 refresh 失败 → 清理并返回 None
    # 3) access_expires_at 不足 margin → 复用 refresh_auth_session(sid, record)
    # 4) 校验 require_scope ∈ scope.split()（read/write 都可能，按需传）
    # 5) 返回 (sid, record)，record 中带 access_token
```

- 发布任务在 `queued` 阶段就解析一次创建人会话并**固定 `sid`**；上传全程使用该会话的 token，绝不使用请求者的 `auth_session()`。
- 长视频上传中途 token 过期：用固定 `sid` 续期后重试**一次**（用户级接口无 nonce，可安全重取）；续期失败 → `library_owner_session_missing`，任务置 `failed`，视频步骤可继续重试。
- 写权限判定基于**创建人会话的 scope**，不是配置项也不是操作者 scope：配置升级后创建人未重新登录时，提示"请任务创建人重新登录并授权发布"。
- 管理员代发布：请求者通过 `admin_session_valid()`，但 token 仍取创建人；发布记录同时保存 `owner_id`（归属）与 `published_by`（操作者），审计两条身份。
- 读接口（浏览/预览/下载）仍用操作者自己的会话，与发布身份解耦；这也保证"能看到什么"遵循操作者权限。

### 5.3 文件与临时产物

- 动作文件按需生成到 `work_dir/<job>/library_tmp/`，上传成功后清理；失败保留供排查（带 TTL 清理）。
- 上传句柄只读打开，避免与正在播放/导出的文件竞争；文件被占用时给出可读错误。
- 大小上限：视频与 3D 文件分别配置（默认 512 MB / 128 MB），超限在本地先拦，不发上游。

---

## 6. 配置变更

### 6.1 `auth_config.json`

| 键 | 现值 | 目标值 | 说明 |
| --- | --- | --- | --- |
| `scope` | `reference_lib:read` | `reference_lib:read reference_lib:write` | 发布功能必需；**老用户需重新登录授权** |
| `upload_root_id` | 未配置 | `326360534` | 已登记的应用上传根目录；`0` 时对端回退 `326353497` |
| `library_enabled` | 未配置 | `true` | 关闭后资源库入口与发布接口返回 404 |
| `publish_require_rating` | 未配置 | `true` | 是否强制先评分 |
| `allow_admin_publish` | 未配置 | `false` | 是否允许管理员以创建人身份代发布；关闭时仅创建人本人可发布 |
| `max_video_mb` / `max_motion_mb` | 未配置 | `512` / `128` | 本地预检 |

**注意**：现有登录回调用 `REFERENCE_LIB_SCOPE not in scope.split()` 校验（[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2934-L2938)）。`scope` 变成多 token 后，该校验必须改为"配置里的每个 scope 都必须在授权结果中"，否则登录会一律失败。这是实现时的必改点。

### 6.2 部署前置（平台侧）

- `REFERENCE_LIB_APPKEYS['anim-ops'].scopes` 已包含 `write`（文档 §2.1 已登记），但需确认 `upload_root_id=326360534` 已配置且目录存在。
- `redirect_uris` 不变，登录回调无需重新登记。
- 已登录用户升级后必须退出并重新登录，才能拿到 write scope；未升级用户仍可浏览（read）。

---

## 7. 数据模型

新增 `work_dir/library_publish.json`（原子写，与 `jobs.json` 解耦）：

```json
{
  "20261125_101500_ab12cd": {
    "owner_id": "rl:123",
    "owner_name": "张三",
    "published_by": "rl:123",
    "state": "done",
    "consent": true,
    "request": {
      "name": "walk_person1.mp4",
      "description": "FlowHMR 单人动作，30fps",
      "publish_motion": true,
      "motion_format": "mixamo",
      "related_type": "anims"
    },
    "video": { "resource_id": "326400001", "uploaded_at": 1790000000.0 },
    "motion": {
      "format": "mixamo",
      "related_type": "anims",
      "uploaded": [{ "resource_id": 326400002, "name": "walk_person1.fbx" }],
      "uploaded_at": 1790000060.0
    },
    "error": null,
    "updated": 1790000060.0
  }
}
```

- 不保存 `access_token`、不保存对象存储 URL（时效不可控）。
- 只记录资源 ID 与上传结果，用于状态展示、幂等重试与审计。
- 审计沿用 `admin_audit.jsonl`：记录 `library.publish`（`target=job_id`、`detail=resource_id`、actor=`rl:<id>`），不记录文件内容。

---

## 8. 前端设计

| 位置 | 改动 |
| --- | --- |
| 文件菜单（[index.html](file:///d:/xjq/FlowHMR-Windows/tools/web/index.html#L21-L29)） | 新增「浏览公共参考库…」；无权限时置灰并提示原因 |
| 评分弹窗 | 星级 + 标签 + 备注 + 一行提示"提交评分后，成片与动作会自动上传到公共参考库备份"；评分保存成功后自动调用发布接口，不提供任何发布选项 |
| 自动备份 | 前端调用 `POST /api/library/publish`（`{job_id, consent:true}`），后端按门禁执行上传并记录状态；失败静默 |
| 资源库面板 | 新 dialog/抽屉：搜索框、`media_type` 筛选、分页（≤100/页）、缩略图列表（§4.5 批量取链接）、点击看详情（§4.3）、播放/下载（§4.4 经服务端 302）、按 `child_type/child_id` 筛"有动作的档案" |
| 评分弹窗 | 只保留星级 + 标签 + 备注 + 自动备份提示；不提供发布按钮与发布选项 |
| `api.js`（[api.js](file:///d:/xjq/FlowHMR-Windows/tools/web/static/js/api.js#L43-L49)） | 新增 `getLibraryConfig/publishLibraryJob/listLibraryResources/getLibraryResource/getLibraryLinks/libraryFileUrl` |
| 错误文案 | 新增 `library.*` 文案并补齐七种语言（`locales/*.js`），上游错误只显示可读摘要 |

交互细节：

- 自动备份不跳转、不弹出进度，保持当前任务；仅在评分弹窗展示一行提示。
- 资源库预览视频用原生 `<video>` + `poster`（缩略图），不引入新依赖。
- write scope 缺失或创建人无可用会话时，后端拒绝入队并记录原因；评分保存与下载不受影响。

---

## 9. 安全与合规

1. **发布即公开**：参考库对 `anims_public` 可见；采用"评分提交即自动上传"，评分弹窗固定展示自动备份提示。
2. **权限与归属**：资源归属固定为任务创建人，上传凭证也是创建人的；请求者只能是创建人本人或管理员（管理员仅"代为触发"，记录 `published_by`），`mcp` / `legacy` / 本地 uid 任务不可发布。
3. **凭证**：token 只在服务端；浏览/下载全部经服务端转发，禁止把 `access_token` 拼进浏览器 URL；不得把创建人 token 返回给发起人。
4. **内容边界**：只传成片与动作文件，不传原始上传视频、不传 `job.json`、不传用户昵称等元数据（备注/标签只取用户在评分弹窗填写的内容）。
5. **上传来源**：服务端只读打开 `work_dir` 内受控路径，路径由 `job_id` 推导，不接受前端任意路径。
6. **限流**：发布接口每用户每分钟 2 次、全局并发 1；浏览接口走缓存，避免上游限流。
7. **不可撤回**：评分弹窗需写明"提交评分会自动备份到公共参考库"；误传只能通过管理侧平台处理（对端无接口）。
8. **错误脱敏**：不把上游原始 message 直接抛给浏览器，按 §10 映射为本地错误码。
9. **代发布合规**：管理员用创建人身份上传，属于"以创建人名义发布"，必须同时满足创建人 `allow_library`、创建人 write 权限与管理员审计；创建人可随时退出登录使会话失效，从而阻止继续代发布。

---

## 10. 错误码映射

FlowHMR 侧新增错误码（沿用 `api_error(error_code, message, status)` 风格）：

| 上游 code | FlowHMR error_code | HTTP | 前端处理 |
| --- | --- | --- | --- |
| —（本地门禁） | `library_disabled` | 404 | 隐藏入口 |
| — | `library_owner_session_missing` | 409 | "任务创建人登录已失效，请创建人重新登录后发布" |
| — | `library_owner_scope_missing` | 403 | 指引创建人重新授权（write scope） |
| — | `library_not_owner` | 404 | 不暴露任务存在性 |
| — | `library_owner_unlinked` | 409 | 任务无参考库身份（`mcp`/`legacy`/本地 uid） |
| — | `library_not_done` | 409 | 提示任务未完成 |
| `rating_required`（本地） | `rating_required` | 409 | 复用现有评分引导 |
| — | `library_consent_required` | 409 | 勾选"允许加入公共库" |
| `unsupported_media_type` | `library_unsupported_file` | 415 | 提示格式不支持 |
| `upload_failed` | `library_upload_failed` | 502 | 可重试；区分视频/动作步骤 |
| `update_failed` | `library_bind_failed` | 500 | 视频已成功，仅重试动作 |
| `insufficient_scope` | `library_owner_scope_missing` | 403 | 创建人重新授权 |
| `role_required` | `auth_role_required` | 403 | 联系管理员开通角色 |
| `not_found` | `library_not_found` | 404 | 资源已不存在 |
| `storage_unavailable` / `service_unavailable` | `library_unavailable` | 503 | 稍后重试 |
| `rate_limited` | `library_rate_limited` | 429 | 带 `Retry-After` |

---

## 11. 分阶段实施与验收

### P0：只读资源库（无需改 scope）

1. `/api/library/config|resources|resources/<rid>|resources/links|resources/<rid>/file`。
2. 资源库面板：列表、搜索、筛选、详情、预览、下载。
3. i18n 与错误映射。

验收：

- 当前 `read` scope 用户可直接浏览，不触发重新授权。
- 列表分页/关键词/`media_type`/`child_*` 筛选结果与对端一致；不存在的 ID 显示 `not_found` 占位。
- 视频可播放、图片可预览、下载 302 到对象存储且响应中不出现 token。
- 上游 503/429 时页面给可读提示，不白屏。

### P1：发布成片（需 write scope）

1. `scope` 配置升级 + 登录校验改为多 scope；`upload_root_id` 配置。
2. `/api/library/publish`（仅视频）+ 状态查询 + 落盘记录 + 审计；上传凭证从 `job.owner_id` 的会话取，不使用请求者会话。
3. 评分提交后自动发布（后端门禁 + 状态记录）；评分弹窗仅一行提示。

验收：

- 未评分 / 无创建人会话 / 创建人无 write scope / 重复发布四类请求均被拦截且不发上游调用。
- 用 B（管理员或协作者）触发发布 A 的任务：成功时平台 uploader 为 A，`external_ref.task_id=job_id`；本地记录 `owner_id=A`、`published_by=B`。
- `mcp` / `legacy` / 本地 uid 任务发布返回 `library_owner_unlinked`。
- 成功发布后，在参考库列表以 `keyword` 能搜到。
- 视频步骤成功后手动重试，不会重复上传视频（`video.resource_id` 不变）。
- 上传中断/失败后状态为 `failed`/`partial`，错误码与 §10 一致。

### P2：动作绑定与回写

1. §4.10：按 `motion_format` 生成动作文件并绑定，默认 `related_type=anims`。
2. §4.8/§4.9：补充、替换、解绑动作（仅限本应用已上传的资源）。
3. 资源库详情展示 `child_ids`，支持跳转"同动作的其他档案"。

验收：

- 发布动作后，档案 `child_ids.anims` 含新 ID，`uploaded[]` 名称符合"父档案同名 + 原后缀"。
- 视频成功 + 动作失败时状态为 `partial`，重试只补动作。
- `retarget_fbx` 等浏览器烘焙格式不出现在自动发布选项中，并有说明文案。

---

## 12. 风险与待确认

| # | 事项 | 影响 | 建议 |
| --- | --- | --- | --- |
| 1 | §4.7 无自定义 `resource_id`、无幂等键 | 重试可能产生重复档案 | 本地状态机保证"成功步骤不重传"；`task_id` 用于事后排查 |
| 2 | 无删除/下架接口 | 误发布不可撤回 | 强制二次确认；文案写明不可撤回 |
| 3 | 视频是否会生成预览图（`image_url`） | 决定视频卡是否有封面 | §4.10 已支持 `preview_file`，3D 缩略图由本地上传的视口截图提供 |
| 4 | 单文件大小上限、上传超时 | 大视频发布失败 | 文档未给具体值，按 512 MB 预检 + 600 秒超时；实测后调整 |
| 5 | 用户级接口是否也受 60 次/分钟限制 | 浏览页可能限流 | 按受限处理：列表/详情短 TTL 缓存、`links` 批量取 |
| 6 | `scope` 从 read 变 read+write | 老会话无法发布 | 登录页提示重新授权；未升级用户仍可浏览 |
| 7 | `upload_root_id=326360534` 是否已配置 | 上传目录可能落到参考库根目录 | 平台侧确认；为 0 时行为可接受但不隔离 |
| 8 | `related_type` 选 `anims` 还是 `model` | 影响参考库检索与复用 | 默认 `anims`（FlowHMR 产物是动画）；模型中转场景允许用户切换 |
| 9 | 当前角色重定向 FBX 无法后台发布 | 功能预期不一致 | 已支持：评分时前端烘焙带蒙皮 FBX 暂存到 `library-motion`，发布优先使用；无角色时回退 SMPL-H FBX |
| 10 | 公共库可见性合规 | 用户素材外泄风险 | 只传成片+动作、强制同意开关、不传原始输入 |
| 11 | 创建人会话不存在/已过期（退出登录、超过 `AUTH_SESSION_TTL`） | 任务无法发布，且不能回退用操作者身份 | 明确提示"请创建人重新登录"；发布记录显示创建人登录状态，避免误以为无权限 |
| 12 | 管理员以创建人身份代发布 | 越权/错误归属风险 | 仅管理员可代理，强制审计双身份；创建人退出登录即失效；提供配置开关默认关闭 |
| 13 | `auth_sessions.json` 落盘含明文 access/refresh token | 服务器被读即凭证泄露 | 维持现有文件权限（`work_dir` 受限）；不新增日志输出；后续可考虑加密存储 |

**待确认问题**

1. 发布是否默认"同时发布动作"，还是默认只传成片？
2. `anims` 之外是否需要把带蒙皮角色模型按 `model` 上传（涉及体积与版权）？
3. 资源库入口放在顶部文件菜单，还是独立一级面板？
4. 是否允许管理员批量发布历史优质任务（涉及评分阈值与审计策略）？
5. 对端是否计划提供删除/下架接口；在此之前是否需要本地"发布黑名单"。
6. 管理员代发布（以创建人身份）是否开放？若开放，`allow_admin_publish` 默认值是否设为 `false`；是明确需要该能力才会开启，还是必须创建人本人操作。
