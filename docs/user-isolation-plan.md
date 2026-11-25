# 用户隔离与共享任务池改造计划

> 状态：方案文档（仅设计，不含代码改动）
> 范围：`tools/web`（Flask 后端 + 原生 JS 前端）、任务运行时（`flowhmr/pipeline`）、角色库
> 目标环境：局域网多客户端访问同一个 FlowHMR 服务（`--host 0.0.0.0`）

---

## 1. 目标与验收口径

### 1.1 目标

1. **用户隔离**：每个浏览器（用户）打开页面只看到自己的场景/任务列表与 3D 结果，刷新、换页签、重开窗口后仍回到自己的目录。
2. **共享任务池**：所有用户的任务进入同一份队列，由同一个 GPU 工作线程按公平策略依次处理；任务池对所有用户**只读可见**（能看到谁的任务、状态、进度、排队位置），但不能操作别人的任务。
3. **操作边界**：选人、取消、重跑、删除、导出、修改角色、查看缩略图/轨迹，只有任务属主可以执行；其他人访问一律 403/404。
4. **最小侵入**：不改变现有推理流水线（`process_video`）与任务产出格式；任务串行执行、单 `ENGINE` 复用的现状保持不变。
5. **管理员后台**：提供独立的 `/admin` 管理控制台，掌握全池任务、用户、队列与存储，可暂停派发、调整优先级、取消/删除任意任务并留审计日志；管理员身份由启动参数中的密钥授予，普通页面不出现入口。
6. **评分门禁**：任务完成后，属主必须先完成评分（1–5 星，可带标签/评语）才能下载资源，未评分不可下载；评分与产物绑定并留痕，后续按高分筛选构建资源库。

### 1.2 明确不做

- 不做公网级账号体系（不引入密码、OAuth、邮件验证）；内网信任模型 + 属主校验。
- 不引入数据库；沿用 `job.json` 文件持久化。
- 不改动 GPU 并发策略（仍为单 worker 串行），只改排队与归属。

---

## 2. 现状（改造依据）

| 事项 | 现状 | 位置 |
| --- | --- | --- |
| 任务存储 | 进程内 `JOBS = {}`，key 为 job id，无 owner/user 字段 | `tools/web/server.py:262` |
| 持久化 | `<job_dir>/job.json`，原子写；`waiting_user` 时保留 `candidates` | `tools/web/server.py:551-567` |
| 任务目录 | `ARGS.work_dir/<job_id>/`（默认 `output/web_jobs`），内含 `input/`、`persons/`、`mesh/`、`members/` | `tools/web/server.py:532-533, 3364` |
| 队列 | `JOB_QUEUE = queue.Queue()`，元素为 `job_id` 或 `("scene"/"camera", id)`；**1 个 worker 线程** | `tools/web/server.py:267, 743-822, 3389` |
| 执行模型 | 全局唯一 `ENGINE`，`body_reference`/`sam_backend` 变化会整模型重载 → 任务必须串行 | `tools/web/server.py:259, 346-376` |
| 取消 | `CANCEL_REQUESTS` 集合 + `_raise_if_cancelled`，运行中任务在安全点退出 | `tools/web/server.py:825-828, 2379-2402` |
| 多人/场景 | 父任务保留第一条轨迹，其余人各生成子任务（`person_batch=<父id>`），聚合为场景 | `tools/web/server.py:875-945, 990-1041` |
| 身份/会话 | **无 Cookie / Session / token 映射**；`local_model_request()` 仅判断"本机浏览器"，`require_desktop_session`/`require_mcp_service` 是进程级共享密钥 | `tools/web/server.py:1672-1680, 2032-2038, 1872-1879` |
| 接口防护 | 上传、列表、详情、取消、重跑、选人、删除、结果下载**均无属主校验**（部分仅 `@requires_runtime`） | `tools/web/server.py:2273-2646, 2778, 3042, 3077, 3125-3197` |
| SSE | `/api/ui/events` 仅本机可用，事件为全局广播，无按用户过滤 | `tools/web/server.py:115-137` |
| 前端 | `currentJob` 全局单值；`localStorage: flowhmr-last-job`；任务列表在 `history.js` | `static/js/main.js:53, 328`, `static/js/history.js:33-194` |
| 角色库 | 全局单库，绑定 `actor=job_id`，所有用户共享 | `tools/web/server.py:1739`, `flowhmr/character_library.py:339-340` |

结论：当前是"单用户工具"的服务器模型，局域网多人同时使用时，任务列表、选人、取消、导出全部互相串台。本次改造的核心是把 **任务归属** 与 **池内可见性** 分开建模。

---

## 3. 总体设计

### 3.1 身份模型（推荐方案 A：设备身份 Cookie）

| 角色 | 身份来源 | 说明 |
| --- | --- | --- |
| 浏览器用户 | `HttpOnly` Cookie `flowhmr_uid`（随机 UUID，长期有效） | 首次访问由服务端签发；页面顶部显示可编辑的昵称（默认"用户-XXXX"） |
| 桌面壳 | 同上（桌面壳内嵌浏览器复用 Cookie） | 不单独建身份 |
| MCP 客户端 | 服务端固定系统身份 `mcp` | 由 `X-FlowHMR-MCP-Key` 鉴权（已有机制），任务 owner=mcp |
| 管理员 | 启动参数 `--admin-key <secret>`（或环境变量 `FLOWHMR_ADMIN_KEY`）+ `/admin` 登录 | 登录后签发 `HttpOnly` Cookie `flowhmr_admin`（签名令牌，12 小时滑动过期）；可管理全池，不绑定 owner |
| 历史任务 | `owner_id = "legacy"` | 升级前的任务统一归入"历史"，全池可见、只读 |

- 昵称与**来源 IP 特征**存放在服务端 `output/web_jobs/users.json`：
  `{uid, name, created, last_seen, first_ip, last_ip, ip_history: [{ip, first_seen, last_seen, hits}], last_agent}`。
- **IP 默认不作为身份依据，仅在丢失 Cookie 时兜底**：有效 `flowhmr_uid` Cookie 优先；没有 Cookie 时，按来源 IP 复用该 IP 上最近活跃（同 User-Agent 优先）且未被禁用的 uid，并重新签发 Cookie，使清缓存/换浏览器的同一台机器仍回到同一用户（实现见 `ip_fallback_user()`）。由此带来的边界：DHCP 换 IP 会新建用户；同一 NAT 出口或共用一台机器会合并到最近活跃的 uid；禁用用户不会被兜底复活。后台仍展示同一 IP 多账号提示用于排障。
- IP 其余用途：后台用户识别与排障、登录/上传限流、审计记录。
- 采集方式：`request.remote_addr`；仅当部署在已配置的可信反向代理后面时，才读取 `X-Forwarded-For` 的最右一跳（启动参数 `--trust-proxy`），默认忽略该头，防伪造。
- 记录粒度：每个 uid 保留最近 10 个 IP（去重、带首末次时间），任务额外记录创建时的 `client_ip`；不记录端口、不做地理位置解析（P2 可选）。
- 未配置 `--admin-key` 时，`/admin` 及 `/api/admin/*` 一律 404，管理能力完全关闭；密钥只用于登录校验，不写入日志、不下发前端。
- 备选方案 B：所有人共用一个页面口令后各自填昵称 —— 仅防陌生人，不防互看；不推荐。
- 备选方案 C：真实账号 + 密码 —— 超出内网演示需求，不推荐。

### 3.2 归属与可见性（两级视图）

| 视图 | 内容 | 操作权限 |
| --- | --- | --- |
| 我的场景 | 自己的任务/场景完整数据：参数、候选人、3D 结果、下载（需先评分）、角色绑定 | 全部 |
| 任务池 | 全体任务的**完整任务数据**（已确认）：昵称、来源 IP、文件名、参数、帧数、状态、阶段进度、候选人数量与缩略图、排队位置、创建/耗时、评分（1–5 星） | 只读；自己的行可执行操作；产物下载仅属主与管理员 |
| 管理员后台 | 全池 + 用户 + 队列 + 存储 + 审计（见第 7 节） | 全部（含取消/删除/置顶/暂停派发/改名/清理） |

约束：任务池虽然展示完整任务字段，但不返回他人产物的下载/预览直链（`/download`、`/fbx`、`/track` 等仍校验属主），也不返回 `SELECTIONS` 运行时内部结构。任务池中属主是自己的行复用现有行组件并点亮操作按钮。

### 3.3 场景归属

`person_batch`/`members` 是多任务聚合。规则：

- 父任务 owner 决定整个场景 owner；子任务强制继承父任务 owner（创建时写入，之后不可变）。
- 场景列表、场景聚合结果、`btnAlignScene` 等只对属主展示。
- 末尾 `_assemble_scene` 由服务端触发，与用户身份无关。

---

## 4. 数据模型变更

### 4.1 `job` 字段

新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `owner_id` | string | Cookie 身份 / `mcp` / `legacy`；**不可被请求体覆盖** |
| `owner_name` | string | 创建时快照的昵称（用于任务池展示，改名不回溯） |
| `client_ip` | string | 创建任务时的来源 IP（经可信代理规则归一化），后台/池展示与审计用 |
| `queued_at` | float | 入池时间，用于排队位置与公平调度 |
| `queue_seq` | int | 单调自增序号，稳定排序 |
| `visibility` | `"pool"`（默认） | 预留：将来支持 `"private"`（不出现在池里） |
| `rating` | object | `{score: 1–5, tags: [], comment, uid, created_at, updated_at}`；任务完成（`completed`）后由属主提交，**下载门禁依据** |
| `rating_history` | array | 评分变更历史（最近 10 条，记录分数/时间/uid） |
| `allow_library` | bool | 固定 `true`：评分即视为同意作为资源库素材（界面不再提供开关） |

`job.json` 落盘字段同步扩展；启动恢复（`server.py:614-628`）时缺失 `owner_id` 的旧任务补 `owner_id="legacy"`。

### 4.2 索引

- 保持 `JOBS` 以 id 为 key（内存量小，直接过滤即可）。
- 新增 `USERS`（uid → 昵称、首末次访问、`first_ip/last_ip/ip_history`）与 `PENDING`（有序待执行列表，取代直接塞 `queue.Queue` 的做法，见 5.2）。
- 新增 `IP_INDEX: dict[ip, set[uid]]`，用于后台"同 IP 多用户"提示与按 IP 限流；仅内存索引，重启后由 `users.json` 重建。
- 新增 `JOBS_BY_OWNER: dict[str, set[str]]` 可选；如无性能压力，用过滤实现，减少一致性负担。

### 4.3 文件系统

推荐 **保持扁平目录** `work_dir/<job_id>/`，以 `job.json.owner_id` 为准做访问控制：

- 优点：`MEMBER_DIRS`、恢复扫描、子任务移动逻辑全部不动。
- 替代方案：`work_dir/users/<owner_id>/<job_id>/`，隔离更直观，但需改 `job_dir`、恢复扫描、`members/` 迁移与旧数据搬迁，收益不足。
- 新增 `work_dir/users.json` 存昵称与 IP 特征；`work_dir/ratings.jsonl` 追加写评分事件（`job_id/owner/score/tags/allow_library/ts/ip`），用于统计与资源库筛选；缩略图与 persons 目录访问前统一走属主校验。
- 资源库目录（P2）：`work_dir/library/index.jsonl` + `library/assets/<job_id>/`（复制的 `motion.npz`/`motion.fbx`/封面），与任务目录解耦，任务删除不影响已入库素材。

---

## 5. 权限与调度设计

### 5.1 接口权限矩阵

约定：`owner` = 请求身份等于 `job.owner_id`；`any` = 所有已识别用户；`local` = 现有 `local_model_request()`。

| 接口 | 现行为 | 改造后 |
| --- | --- | --- |
| `POST /api/upload` | 无校验 | 登录态（懒签发 Cookie）→ 创建任务时写入 owner |
| `GET /api/jobs` | 全员全部 | **只返回自己的**任务/场景（含完整字段） |
| `GET /api/pool`（新增） | — | 任何用户；返回全体**完整任务数据** + 排队位置（不含运行时可下载直链） |
| `GET /api/jobs/<id>` | 无校验 | `owner`；非属主 404（不暴露存在性） |
| `POST /api/me`、`GET /api/me`（新增） | — | 懒签发 Cookie / 读写昵称 |
| `POST /api/admin/login`、`/api/admin/logout`（新增） | — | 校验 `--admin-key`，签发/清除管理员 Cookie；失败限流 |
| `GET /api/admin/pool`、`/api/admin/queue`（新增） | — | `admin`；全池详情、排队/运行/暂停状态 |
| `GET /api/admin/users`、`PATCH /api/admin/users/<uid>`、`/api/admin/settings`、`/api/admin/audit`、`/api/admin/storage`（新增） | — | `admin`；用户改名/禁用、全局配额与设置、审计、磁盘占用 |
| `GET /api/jobs/<id>/scene` | 无校验 | `owner`（父任务） |
| `POST /api/jobs/<id>/select` | 无校验 | `owner` + slot 属主二次校验（防竞态） |
| `POST /api/jobs/<id>/cancel` | 无校验 | `owner`；支持取消**排队中**任务（从 `PENDING` 移除） |
| `POST /api/jobs/<id>/rerun`（重跑/换人） | 无校验 | `owner`；新任务继承 owner |
| `DELETE /api/jobs` | 无校验 | `owner`；场景批量删除按父 owner 校验 |
| `POST /api/jobs/<id>/rating`（新增）、`DELETE /api/jobs/<id>/rating`（新增） | — | `owner`；仅 `completed` 任务可评分（1–5 星 + 标签 + 评语 + 是否可入库）；可更新，历史留痕 |
| 导出/下载（npz/fbx/vmd/vrma/thumb/person/track/skeleton） | 无校验 | `owner`（**必须先有 `job.rating`**）或 `admin`；未评分返回 409 `rating_required`；同源 Cookie 自动携带，`<img>`/`<a>` 无需改 URL |
| `POST /api/mcp/*` | MCP key | 不变，owner=`mcp` |
| `GET /api/ui/events` | 仅本机全量广播 | 允许局域网；按连接身份过滤：自己的任务事件 + 任务池事件；管理员连接接收全量事件 |

实现要点：

- 统一依赖：`identify_user()` 读取/签发用户 Cookie；`require_owner(job_id)` 属主校验；`require_admin()` 校验管理员 Cookie（未配置密钥时直接 404）。
- 属主校验必须在 `JOBS_LOCK` 内完成"读取 + 判定"，避免任务被删除后悬挂；管理员操作同样先取锁再改状态。
- 非属主一律 `404`（而非 403），避免探测任务存在性；任务池已公开完整数据，无需 403 语义。
- 管理员所有写操作必须带自定义请求头 `X-FlowHMR-Admin: 1`（同源校验），防跨站提交；审计日志记录 `admin` 身份、动作、目标与结果。
- 优先级字段 `priority` 只能由管理员修改，普通用户请求体中的该字段一律忽略。
- 每次请求经 `identify_user()` 时顺带 `touch_user(uid, ip)`：更新 `last_seen/last_ip/ip_history`（内存 + 节流写盘，避免每请求 IO）；创建任务时把当时 IP 固化到 `job.client_ip`。
- 限流维度：管理员登录按 IP；上传与提交任务按 uid + IP 双维度（同一出口 IP 下多用户不会互相误伤，单用户刷 IP 也会被 uid 限制）。
- **评分门禁在后端强制**：所有产物下载路由统一调用 `require_rating(job)`，前端禁用按钮只是体验优化；未评分返回 `409 rating_required`，提示"请先评分后下载"。
- 豁免范围：仅管理员下载与 `owner=mcp` 的程序化导出；`legacy` 历史任务不再豁免，普通用户同样需要评分后才能下载。
- 评分幂等可更新（最后一次为准），每次变更写 `rating_history`、`ratings.jsonl` 并广播 UI 事件；场景整体下载以父任务评分为准，成员单人下载以该成员任务评分为准。

### 5.2 任务池与公平调度

现状 `queue.Queue` 无法移除排队项，且无法表达"按用户轮转"。改为：

```
PENDING: list[QueueItem]        # QueueItem = (priority, seq, owner_id, job_id | ("scene"/"camera", id))
QUEUE_COND: threading.Condition # 通知 worker 与等待位置变化
RUNNING_JOB: str | None         # 当前执行任务，任务池/后台展示用
QUEUE_PAUSED: bool              # 管理员"暂停派发"开关（运行中任务继续跑完）
```

- **调度策略**：先按 `priority` 降序（默认 0，管理员可置顶/降级），再按 owner 轮转（每个用户最多 1 个"连续执行名额"，取该用户最早的一个任务），同 owner 内 FIFO。目的：一个用户提交 10 条时不会把别人堵死。
- **暂停派发**：`QUEUE_PAUSED=True` 时 worker 在 `QUEUE_COND` 上等待，不取新任务；页面与后台显示"已暂停派发（管理员）"，恢复后继续。
- **位置计算**：按上述规则模拟出每个待执行项的预计位置，供任务池/后台显示"第 N 位"。
- **取消**：`queued` 状态直接从 `PENDING` 移除并置 `cancelled`；`running` 仍走 `CANCEL_REQUESTS`；管理员可强制取消运行中任务（提示"将在当前阶段的安全点退出"）。
- **并发上限**：单用户 `PENDING` 上限（20，已确认）与总上限（200，已确认），超出返回 429；管理员可在后台调整。
- **worker 不变**：仍单线程消费，`ENGINE` 串行；换模型行为不变。
- 子任务（`members`）入池时继承父 owner，并标记 `scene_child=True`，与父任务相邻调度（同一 owner 连续名额内保持父子相对顺序）。

### 5.3 事件与前端刷新

- `UI_EVENTS` 事件补充 `owner_id`；SSE 端按连接身份过滤：普通用户收到自己的完整事件 + 池内其他任务的状态事件（不含下载直链），管理员连接收到全量事件与队列控制事件。
- 当前前端在 SSE 不可用时用 2s 轮询（`history.js:179-194`），需同时改轮询接口：
  - 自己的列表：`GET /api/jobs`
  - 池：`GET /api/pool?since=<revision>` 增量拉取。

### 5.4 评分与资源库准入

- **评分时机**：仅 `completed` 任务可评分；场景整体评分记在父任务，成员任务可各自评分。
- **字段校验**：`score` 必填整数 1–5；`tags` 白名单（动作质量/手部/物理/相机/服装/其他），最多 3 个；`comment` ≤ 200 字；`allow_library` 恒为 `true`（评分即同意入库，弹窗不再提供开关）。
- **下载门禁**：后端 `require_rating` 强制；未评分返回 `409 rating_required`。前端在下载窗口内完成评分后解锁按钮，无需刷新页面。
- **资源库准入（P2）**：`score >= 分数线（默认 4）` 且 `allow_library` 且未被管理员标记"质量存疑"的任务进入候选池；管理员复核后批量入库（复制 `motion.npz`/`motion.fbx`/封面到 `library/assets/<job_id>/`，写入索引：视频内容哈希、参数摘要、评分、标签、属主昵称），同视频重复提交按哈希去重，保留最高分版本。
- **隐私边界**：资源库只收录动作数据与参数摘要，不收录评语原文、IP、uid；评语与评分明细仅属主本人与管理员可见（任务池只显示星级）。
- **统计**：`ratings.jsonl` 支撑后台评分分布、按用户/标签聚合；管理员可导出候选清单（不含个人信息）。

---

## 6. 前端改造

| 位置 | 改动 |
| --- | --- |
| 顶部栏（新增） | 用户区：昵称 + "编辑昵称"；显示本机 IP 与访问方式（本机/局域网）；任务池表格显示各任务属主昵称与来源 IP |
| 历史/场景列表 `history.js` | 分成两个区块：**我的场景**（可操作）与 **任务池**（只读，显示属主昵称、状态、排队位置；自己的行高亮并启用按钮） |
| `main.js` | `currentJob` 校验 owner：若恢复的 job 不属于当前用户则清空；`localStorage` 键改为 `flowhmr-last-job:<uid>`，uid 由服务端 `/api/me` 下发并缓存 |
| 选人卡片 `person-select.js` | 逻辑不变；`waiting_user` 任务只有属主能收到，非属主在池中只看到"等待选人（他人）" |
| 下载资源窗口（改造） | 窗口顶部新增评分区：5 星 + 场景标签（动作质量/手部/物理/相机/服装）+ 可选评语 + "允许作为资源库素材"勾选；**未评分时下载按钮禁用**并提示"评分后可下载"；评分后立即解锁，可随时在窗口内修改评分 |
| 任务卡片/列表 | 已完成任务显示星级徽标；`completed` 且未评分显示"待评分"角标，点击直达评分窗口 |
| 错误提示 | 新增"无权操作该任务"、"排队已满"、"任务已被属主删除"等文案，补齐 `locales/*.js` 七种语言 |
| 删除/取消按钮 | 任务池中非属主行禁用，悬浮提示"仅属主可操作" |

---

## 7. 管理员任务池管理后台

### 7.1 定位与登录

- 独立页面 `/admin`（`static/admin.html` + `static/js/admin.js`），主站导航不提供入口，仅文档/运维手册说明地址。
- 未配置 `--admin-key`（或 `FLOWHMR_ADMIN_KEY`）时，`/admin` 与 `/api/admin/*` 一律 404。
- 登录流程：输入密钥 → `POST /api/admin/login` → 常量时间比较 → 签发 `flowhmr_admin` Cookie（HMAC 签名，含签发时间与过期时间），12 小时滑动过期；`POST /api/admin/logout` 清除。
- 登录失败限流：同 IP 每分钟 5 次，连续失败 10 次锁 5 分钟；成功/失败都写审计。
- 管理员 Cookie 与用户 Cookie `flowhmr_uid` 相互独立：管理员仍可用普通身份上传任务，便于排障。

### 7.2 功能模块

| 模块 | 能力 | 接口 |
| --- | --- | --- |
| 全池总览 | 全部任务/场景（子任务可折叠），owner（含来源 IP）、文件名、参数、状态、阶段进度、排队位置、耗时、失败原因、评分；顶部 KPI：运行中 / 排队数 / 今日完成 / 失败数 / 平均评分 / 当前引擎与模型切换状态 | `GET /api/admin/pool` |
| 队列控制 | 暂停/恢复派发；置顶/降级（`priority`）；取消排队任务；强制取消运行中任务；重新入队；清空排队（二次确认） | `GET/POST /api/admin/queue` |
| 任务管理 | 查看详情与候选人缩略图；下载任意产物；重跑/换人；删除任务（目录、`persons/`、`members/`、角色绑定一并清理）；整场景删除 | 复用 `/api/jobs/*`（admin 放行）+ `DELETE /api/admin/jobs/<id>` |
| 用户管理 | 列表：昵称、uid 短码、**最近 IP / 全部 IP 记录（首末次时间）**、任务数、排队数、磁盘占用、最后活跃、是否禁用；改名、禁用/启用（禁用后不能提交，已提交任务跑完）、清空某用户任务；同一 IP 下多个用户高亮提示 | `GET/PATCH /api/admin/users`、`GET /api/admin/users/<uid>/ips` |
| 配额与全局设置 | 单用户排队上限、全池上限、上传大小、公平调度开关、"任务池显示他人完整数据"开关（默认开）、历史任务保留天数、**资源库入选分数线（默认 ≥4 星）**、是否强制评分下载 | `GET/PATCH /api/admin/settings` |
| 评分与资源库 | 评分分布与明细（按用户/分数/标签筛选）、评语查看；复核并标记"质量存疑"（不计入资源库）；高分候选预览与批量入库/剔除；资源库目录浏览、去重（按视频内容哈希）与封面临理 | `GET /api/admin/ratings`、`GET/POST /api/admin/library` |
| 存储与清理 | `work_dir` 总占用、按用户/状态排行；清理已完成超 N 天任务；清理 `legacy`/`mcp` 遗留；扫描无 `job.json` 的孤立目录 | `GET /api/admin/storage`、`POST /api/admin/cleanup` |
| 审计日志 | `output/web_jobs/admin_audit.jsonl` 追加写，字段 `ts/actor/target/action/result/ip`；页面可分页筛选；按 10 MB × 5 轮转 | `GET /api/admin/audit` |

### 7.3 界面与交互

- 单页表格 + 右侧详情抽屉；状态色、阶段进度条与主站保持一致；排队位置实时刷新（SSE 优先，断线 2s 轮询）。
- 危险操作二次确认：删除、清空排队、强制取消运行中任务需二次弹窗确认（输入任务文件名后 6 位）。
- 所有写请求带 `X-FlowHMR-Admin: 1` 自定义头，配合同源校验防跨站提交。
- 操作结果即时反馈并刷新对应行；失败原因直接展示，不弹原始 traceback。

### 7.4 与调度、数据的一致性

- 管理员操作与 worker 通过同一把锁（`JOBS_LOCK` / `QUEUE_COND`）修改 `PENDING` 与 `JOBS`，禁止直接操作 `queue.Queue`。
- 删除运行中任务 = 先强制取消（在阶段安全点退出）→ 标记删除 → worker 结束后统一清理目录，避免"删一半"。
- `priority` 只影响派发顺序，不改变 GPU 串行执行与"按 owner 轮转"的公平性下限（管理员置顶项最多连续执行 1 条）。
- 后台与主站任务池共用同一个快照函数（`pool_view()`），保证数字、状态、排队位置一致。

---

## 8. 角色库与产物

- 角色库全局共享（角色资源本身可共享），但**绑定关系**（哪个任务用了哪个角色/姿势）需要按用户隔离：
  - P0：绑定记录不改，接受"所有人可见别人任务绑定的角色"（因为别人任务本身不可见，泄露面有限）。
  - P1：`characters/.preview.json` 增加 `by_user` 覆盖层（`{actor_id: {uid: preset}}`），读取时优先本用户，写入写本用户；导出时保持最终生效值。
- 下载产物：URL 不变，靠同源 Cookie 校验 owner；如需给第三方分享，P2 再做一次性签名链接（`?token=` + 过期时间）。

---

## 9. 安全、配额与运维

1. **威胁模型**：内网互信 + 防误操作/防窥探。明确"不防恶意内网成员"（可嗅探/伪造 Cookie）；如需要，P2 增加启动口令。
2. **防越权（IDOR）**：所有 `job_id` 路径参数先 `require_owner`（管理员走 `require_admin` 旁路）；任务池只返回任务数据与缩略图，不返回可直接下载产物的 URL。
3. **Cookie 属性**：用户 `HttpOnly; SameSite=Lax; Path=/; Max-Age=1年`；管理员 `HttpOnly; SameSite=Strict; Max-Age=12小时`；不做 HTTPS 时不加 `Secure`。
4. **配额**：单用户排队上限 20、总排队上限 200、单用户磁盘占用统计（`job_dir` 求和）、上传大小沿用 1 GB；后台可调。
5. **清理**：属主删除任务时清理 `persons/`、`members/`、角色绑定；`legacy`/`mcp` 任务由管理员或自动过期清理。
6. **日志**：请求日志增加 `uid`、`ip`、`job_id`、`owner`、`action`；管理员操作另写 `admin_audit.jsonl`，密钥本身永不落日志。
7. **管理员密钥**：通过启动参数或环境变量提供；不提供默认值（缺失即关闭后台）；登录接口常量时间比较 + IP 限流；建议运维侧限制后台仅内网访问。
8. **IP 数据的使用边界**：仅内网地址，属于运维可见信息；不对外展示、不写入导出产物、不做地理位置解析；`users.json` 只保留最近 10 个 IP 与首末次时间。反向代理部署时默认**不信任** `X-Forwarded-For`（需显式 `--trust-proxy`），避免客户端伪造 IP 绕过限流或污染审计。
9. **IP 变更提示**：后台对"同一 uid 在短时间内从多个 IP 访问"标记为提示（非阻断），用于发现共享 Cookie 或异常访问；不自动封禁。

---

## 10. 兼容与迁移

- 启动恢复：旧 `job.json` 无 `owner_id` → 置 `legacy`，任务池可见、任何人只读。
- 未带 Cookie 的旧页面：首次请求 `/api/me` 时签发 Cookie；前端在拿到 uid 前不渲染"我的场景"，避免闪现他人数据。
- MCP/桌面壳：不改协议；MCP 任务 owner=`mcp`，桌面壳走浏览器 Cookie。
- 现有 `local_model_request()` 保留用于桌面/MCP 的敏感接口，不再作为任务 API 的鉴权手段。
- 新增启动参数 `--admin-key`（默认不设置 = 后台关闭）；`start-web.cmd`/`start-web.ps1` 透传可选参数，不改变双击即用的默认行为。
- 评分门禁上线时，升级前已完成的任务没有 `rating`：`legacy` 任务由管理员代下；非 legacy 的旧任务允许属主一次性补评分后下载，不回溯历史评分。

---

## 11. 分阶段实施与验收

### P0：身份 + 归属 + 属主校验（核心）

1. `/api/me` 签发/读取 Cookie；`users.json` 昵称读写（`GET/PUT /api/me`）；`touch_user()` 记录 IP 特征（`first_ip/last_ip/ip_history/last_agent`，节流写盘）。
2. `job.owner_id/owner_name/client_ip/queued_at/queue_seq` 写入、落盘、恢复补 `legacy`。
3. 全部任务生命周期与下载接口加 `require_owner`；列表只返回自己的。
4. 前端：用户区、`localStorage` 按 uid 分键、`currentJob` owner 校验。

验收：
- 两个浏览器（不同 Cookie）各自提交 1 条任务：各自列表只有自己那条；互相访问对方 `/api/jobs/<id>`、`/download`、`/persons/0.jpg` 均 404。
- A 取消/删除 B 的任务被拒绝；B 的任务不受影响。
- 重启服务后归属保持；旧任务出现在池中且不可操作。
- `users.json` 记录 `last_ip/ip_history`，任务的 `client_ip` 与提交端一致；重启后仍保留；任务池展示属主 IP。

### P1：任务池 + 公平调度

1. `GET /api/pool` 完整任务数据；SSE/轮询事件按 owner 分类过滤。
2. `PENDING` + 条件变量 + 按 owner 轮转调度；排队位置展示；排队中取消。
3. 前端任务池区块（只读、可展开看阶段进度、自己行可操作）。
4. 评分门禁：`POST/DELETE /api/jobs/<id>/rating`、下载路由 `require_rating`、`ratings.jsonl`、下载窗口评分区与星标角标；管理员/MCP 豁免（`legacy` 不豁免）。
5. 评分 UI 与文案（七种语言），任务池与"我的场景"显示星级。

验收：
- A 连提 5 条、B 提 1 条：执行顺序为 A1、B1、A2、A3…（轮转），池中所有人可见进度与位置。
- 池中他人任务可见参数/缩略图，但下载与操作按钮不可用；刷新页面位置正确。
- 排队中取消立即从池中消失，不占用 GPU。
- 任务完成后未评分：点击任何下载项返回 409 且窗口提示"请先评分"；提交评分后立即可下载；修改评分后以最新分为准，历史可查。
- 管理员下载未评分任务成功；MCP 导出不受影响；`legacy` 任务未评分同样返回 409，完成评分后普通用户可下载。

### P1.5：管理员任务池管理后台

1. 启动参数/环境变量 `--admin-key`；`/admin` 登录页 + `flowhmr_admin` Cookie；未配置时后台整体 404；失败限流。
2. 后台接口：全池总览、队列控制（暂停/恢复、置顶/降级、取消排队、强制取消运行中、清空排队）、任务删除/重跑/下载、用户列表与禁用、配额设置、存储统计与清理、审计日志。
3. 前端 `admin.html`：KPI + 全池表格 + 详情抽屉 + 二次确认；SSE 优先、轮询兜底。
4. `admin_audit.jsonl` 记录所有管理员写操作（含失败）。

验收：
- 未配置密钥：`/admin` 与 `/api/admin/*` 均 404；配置后错误密钥登录被限流，正确密钥可登录并 12 小时后过期。
- 管理员可置顶 B 的任务，执行顺序变为该任务紧随当前任务；暂停派发后队列不再取新任务，恢复后继续。
- 管理员删除运行中任务：任务在安全点取消、目录被清理、审计有记录；删除排队中任务立即生效。
- 禁用某用户后该用户提交返回 403，已提交任务正常跑完。
- 后台"运行中/排队/失败"数字与主站任务池一致；管理员下载他人产物成功，普通用户仍 404。
- 用户列表显示最近 IP 与 IP 历史；同一 IP 下多个用户有提示；审计日志包含操作者 IP。

### P2：资源库与可选增强

1. 资源库：高分候选筛选（分数线/标签/去重）、管理员复核与批量入库、`library/index.jsonl` 与资产目录、浏览页与下架操作。
2. 普通用户内网口令、角色库按用户覆盖、分享链接、自动过期清理策略。

验收：
- 完成 ≥4 星且允许入库的任务出现在候选列表；不满足条件（低分/取消勾选/被标记存疑）的不出现。
- 入库后任务目录被删除，资源库资产仍可下载；同一视频哈希重复入库只保留最高分版本。
- 下架后资源库不再出现，但原任务评分记录保留。

---

## 12. 风险与已确认决策

| # | 事项 | 影响 | 建议 |
| --- | --- | --- | --- |
| 1 | Cookie 可被同内网伪造 | 隔离仅防误操作 | 接受；P2 加速口令或反向代理 |
| 2 | 单 `ENGINE` 换模型耗时，长任务阻塞全池 | 体验 | 任务池显示"正在切换模型"；后续可做引擎缓存 LRU（不在本次范围） |
| 3 | `person_batch` 聚合涉及父子任务与场景导出 | 越权面 | 子任务强制继承 owner，聚合接口按父任务校验 |
| 4 | 任务池展示完整任务数据 | 隐私 | 已确认开放；后台提供"任务池显示他人完整数据"开关，必要时可关闭 |
| 5 | 角色库绑定共享 | 轻度信息泄露 | P1 做 `by_user` 覆盖层 |
| 6 | 旧任务无 owner | 兼容 | 归 `legacy`，只读；提供管理员清理 |
| 7 | 管理员密钥泄露 / 误删任务 | 高影响 | 密钥经启动参数或环境变量传入、不落日志；危险操作二次确认 + 审计；删除运行中任务走"先取消后清理" |
| 8 | 管理员后台暴露在局域网 | 攻击面 | 建议反代/防火墙限制；上线前默认后台 404，需显式配置密钥 |
| 9 | 评分仅为下载门禁，用户可能随手打高分 | 资源库质量 | 评分不等于质量结论：入库必须管理员复核 + 标签/评语参考 + 视频哈希去重；低分任务不主动回收，仅不入库 |

**已确认的决策**：

1. 身份方案采用"设备 Cookie + 昵称"（方案 A）；普通用户口令不在 P0 引入（P2 可选）。
2. 任务池展示"哪个用户 + 任务具体数据"均可见（完整任务数据、缩略图），仅操作与产物下载仍限属主。
3. 排队上限：单用户 20、全池 200。
4. 任务目录保持扁平 `work_dir/<job_id>/`，以 `job.json.owner_id` 做访问控制。
5. 管理员后台必须提供；管理员身份由启动参数 `--admin-key`（或 `FLOWHMR_ADMIN_KEY`）授予，未配置时后台关闭。
6. 收集用户 IP 作为用户特征之一：写入 `users.json`（最近 10 个 IP + 首末次时间）与任务 `client_ip`，在任务池与后台展示，并用于审计与限流；**不作为身份依据**，不支持按 IP 登录或按 IP 归属任务。
7. 下载前必须评分：任务 `completed` 后，属主需提交 1–5 星评分（可带标签/评语/是否允许入库）才能下载；未评分返回 409，仅管理员、MCP 豁免（`legacy` 同样需要评分）；评分可修改并留痕；高分（默认 ≥4 星且同意入库）任务进入资源库候选，经管理员复核后入库。

**待确认**：

1. 管理员密钥的提供方式：启动参数 `--admin-key`（推荐，`start-web.cmd` 增加可选参数透传）还是首次启动自动生成并打印到控制台？
2. 管理员后台是否需要"重启服务"入口（当前前台模式为关窗即停，建议不提供，避免进程自杀）。
3. 普通用户是否需要"隐藏我的任务"（`visibility="private"`，不进任务池）？默认不进范围。
