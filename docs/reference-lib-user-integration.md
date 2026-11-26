# 接入公共动作参考库用户管理（OAuth2 登录）方案

> 状态：方案文档（仅设计，不含代码改动）
> 依据接口文档：`http://anim.nd.com.cn/api/doc-help/reference_lib_api.md`（原始 Markdown 追加 `?raw=1`）
> 范围：`tools/web`（Flask 后端 + 原生 JS 前端）、用户身份与归属模型；不改动推理流水线、任务产出格式与 GPU 调度
> 关联文档：[用户隔离与共享任务池改造计划](user-isolation-plan.md)

---

## 1. 目标与边界

### 1.1 目标

1. **真实身份**：FlowHMR 页面支持通过公共动作参考库（`anim.nd.com.cn`）OAuth2 授权码流程登录，拿到平台用户 `id / name / code` 与角色 `roles`，用真实账号替代当前"匿名设备 Cookie"作为任务属主。
2. **最小改动**：沿用现有 `owner_id` 归属模型与 `require_owner` 校验，不重做任务池、评分门禁、管理后台；只替换"uid 从哪来"。
3. **凭据不出服务端**：APPKEY 签名密钥、`access_token`、`refresh_token` 只存在于 Flask 进程与本地会话文件，浏览器只拿到不透明的会话 Cookie。
4. **可回退**：保留现有匿名设备身份作为 `local` 模式，默认行为不变；登录能力通过启动参数开启，出问题可一键退回。
5. **角色门禁**：仅 `anims_public` 角色可完成授权（由对端 `/authorize` 强制）；FlowHMR 侧在登录后复核 `roles`，角色被收回时降级为只读并提示。

### 1.2 明确不做

- 不做 FlowHMR 自己的注册/密码体系，用户主数据仍以参考库平台为准。
- 不同步平台用户列表：对端接口**只提供"当前登录用户"信息**，没有用户查询/管理接口，因此管理后台的用户列表仍以"本地出现过的账号"为准。
- 不接入参考库的资源读写接口（4.2–4.10）；本方案只取 `user/info`，资源库（上传/浏览）另见 [接入公共动作参考库资源库方案](reference-lib-resource-library-plan.md)。
- 不改 SSE 协议、不改 `job.json` 产出结构、不改 MCP 协议。

---

## 2. 对端接口能力摘要

| 能力 | 接口 | FlowHMR 是否使用 |
| --- | --- | --- |
| 发起授权（浏览器） | `GET /api/openapi/v1/reference_lib/authorize` | 使用（登录入口跳转） |
| 确认授权（浏览器表单） | `POST /api/openapi/v1/reference_lib/authorize` | 使用（对端页面完成） |
| 授权码换 Token | `POST .../token`（需应用签名） | 使用（服务端） |
| 刷新 Token | `POST .../token/refresh`（需应用签名） | 使用（服务端） |
| 吊销 Token | `POST .../token/revoke`（需应用签名） | 使用（退出登录） |
| 当前用户信息 | `GET .../user/info`（Bearer，`reference_lib:read`） | 使用（登录后取身份） |
| 资源列表/详情/链接/登记/回写 | §4.2–4.7 | 本期不使用 |

关键约束（均来自对端文档，直接影响设计）：

- **两层鉴权**：应用签名（`X-App-Id / X-Timestamp / X-Nonce / X-Signature`，HMAC-SHA256）只用于应用级接口；用户级接口用 `Authorization: Bearer`。签名必须由 FlowHMR 服务端完成，浏览器不能持有 secret。
- **签名串**：`app_id\ntimestamp\nnonce\nMETHOD\npath\nsha256_hex(raw_body)`，`path` 为网关剥掉 `/api` 后的真实路径（如 `/openapi/v1/reference_lib/token`），GET 的 body 哈希为 `sha256(b"")`。
- **nonce 一次性**，重复返回 `409 duplicate_nonce`；重试必须重新生成 nonce 与签名。单 APPKEY 默认 60 次/分钟，超限 `429 rate_limited`。
- **时间窗** ±300 秒，部署机需保证时钟同步（NTP），否则一律 `expired`。
- **Token 时效**：`access_token` 3600 秒、`refresh_token` 7 天、授权码 300 秒且一次性。
- **redirect_uri 必须与应用登记白名单完全一致**，否则 `400 invalid_redirect_uri`；这是部署层面最硬的约束（见 §8）。
- 未登录、无 `anims_public` 角色、应用不存在或回调不在白名单时，`/authorize` 返回 400 授权失败页；正常返回授权确认页（HTML），由用户在平台侧点击确认。

### 2.1 本应用已登记的凭据（平台侧 `REFERENCE_LIB_APPKEYS`）

| 项 | 值 | 说明 |
| --- | --- | --- |
| `app_id` | `anim-ops` | 出站签名头 `X-App-Id` 与 `/authorize` 的 `app_id` |
| `secret` | `c52b714cc655f8d2b8d2d8819d34b147041861dbd66d87f6` | HMAC-SHA256 密钥；只从 `tools/web/auth_config.json` 的 `secret` 字段读取 |
| `scopes` | `reference_lib:read`、`reference_lib:write` | 本期只申请 `read`；P2 资源登记再申请 `write`（需用户重新授权） |
| `rate_per_min` | `60` | 应用级接口合计限流，该 app_id 下所有终端共享 |
| `upload_root_id` | `326360534` | P2 上传文件的资源中心独立根目录；为 `0` 时回退参考库根目录 `326353497` |
| `redirect_uris` | 当前仅有占位值 `https://third-party.example.com/callback` | **实施前必须由平台管理员加入 FlowHMR 实际回调**（见 §7），否则一律 `invalid_redirect_uri` |

说明：

- 凭据只允许在 FlowHMR 服务端出站调用中使用；浏览器、前端、MCP 客户端一律不可见。
- 上表明文 secret 仅用于交付交接；运行时放在独立配置文件 `tools/web/auth_config.json`（已加入 `.gitignore`，不进仓库），并确认 git 历史未包含。若已外泄，需平台侧轮换 secret，并同步更新部署机的配置文件与本文档。
- `app_id` 与 `redirect_uris` 是绑定的；换机、换端口、换域名都等于换回调，需同步更新平台登记（见 §7）。

---

## 3. 现状（改造依据）

| 事项 | 现状 | 位置 |
| --- | --- | --- |
| 用户身份 | `flowhmr_uid`：32 位十六进制随机串，HttpOnly Cookie，惰性签发 | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L280-L296)、[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2207-L2219) |
| 身份兜底 | 无 Cookie 时按来源 IP 复用最近活跃 uid（`ip_fallback_user`） | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2191-L2204) |
| 请求身份 | `request_identity()`：MCP key 走 `mcp`，其余走设备 uid | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2222-L2234) |
| 归属校验 | `job_owner()` / `can_access_owner()` / `require_job_access()`，非属主 404；管理员放行 | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2237-L2267) |
| 用户档案 | `work_dir/users.json`，仅接受 32 位十六进制 uid；昵称/首末 IP/最近 10 个 IP/UA/禁用 | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L1873-L1953) |
| 用户接口 | `GET/PUT /api/me` 返回 `uid/name/ip/local`，昵称可自由编辑 | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2294-L2309) |
| 管理员 | `--admin-key` / `FLOWHMR_ADMIN_KEY`；`flowhmr_admin` HMAC Cookie，12 小时滑动 | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2010-L2047)、[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L4779) |
| 前端用户区 | 顶部 `#btnUser` 显示昵称，点击 `prompt` 改名；`localStorage` 键为 `flowhmr-last-job:<uid>` | [main.js](file:///d:/xjq/FlowHMR-Windows/tools/web/static/js/main.js#L144-L167)、[api.js](file:///d:/xjq/FlowHMR-Windows/tools/web/static/js/api.js#L39-L42) |
| 启动参数 | 已有 `--host/--port/--work_dir/--trust-proxy/--admin-key` | [server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L4752-L4773) |

结论：现有模型已经把"归属"抽象成 `owner_id` 字符串，登录接入只需提供**可信、稳定的 owner_id 来源**，并把昵称改为平台昵称（只读），其余权限矩阵全部复用。

---

## 4. 总体方案

### 4.1 方案对比

| 方案 | 做法 | 评价 |
| --- | --- | --- |
| A（推荐）`owner_id = "rl:<user.id>"` | 登录后以平台用户 ID 作为归属主键，`users.json` 之外新增 `accounts.json` 保存账号快照 | 身份不可伪造（Cookie 不可 DIY 成他人），跨设备/清缓存后任务仍在；与现有 `owner_id` 模型天然兼容 |
| B 把平台账号绑定到现有 `flowhmr_uid` | 继续用设备 uid 做 owner，登录只是绑定展示名 | 改动最小，但 uid Cookie 可被内网伪造/复制，绑定关系也会随浏览器丢失，**不满足"真实身份归属"** |
| C 自行维护账号表 + 密码 | FlowHMR 内置账号 | 与平台重复，且违背"平台为账号主数据"的前提，不推荐 |

采用 **A**，并在平台身份之外保留 `mcp`（程序化任务）与 `legacy`（升级前历史任务）两类特殊 owner。

### 4.2 身份与归属映射

| 身份 | `owner_id` | 来源 | 说明 |
| --- | --- | --- | --- |
| 平台用户 | `rl:<user.id>`（如 `rl:123`） | 登录后 `/user/info` 的 `user.id` | 稳定的主键；`user.code`（登录名）与 `user.name` 仅作展示 |
| 匿名设备用户（仅 `local`/`optional` 模式） | 32 位十六进制 uid | 现有 `flowhmr_uid` | 行为与当前完全一致 |
| MCP | `mcp` | 现有 `X-FlowHMR-MCP-Key` | 不变 |
| 历史任务 | `legacy` | 升级前 `job.json` 无 `owner_id` | 不变 |

- `job.owner_name` 在创建任务时快照：平台用户取 `user.name`（缺省回退 `user.code`），匿名用户取本地昵称；改名不回溯。
- `owner_display_name()` 扩展为：`mcp` → `MCP`，`rl:*` → `accounts.json` 中的昵称，其余走 `users.json`。
- `accounts.json` 键即 `owner_id`（`rl:<id>`），结构：`{id, code, name, roles, scope, created, last_login, last_ip, disabled}`；不保存 token。
- **平台没有禁用/改名的推送**：`roles` 与昵称在每次登录和每次"会话续期校验"时刷新；本地管理员对 `rl:*` 的禁用只作用于 FlowHMR（不写回平台）。

### 4.3 会话模型

```
浏览器  ── flowhmr_auth=<sid> (HttpOnly/Lax) ──▶  Flask
                                                   │ AUTH_SESSIONS[sid]（内存 + auth_sessions.json）
                                                   │   ├─ owner_id / ref user 快照
                                                   │   ├─ access_token / expires_at / scope
                                                   │   └─ refresh_token / session_created
                                                   └─ 出站：应用签名 + Bearer
```

- `sid` = 32 字节随机串，Cookie `flowhmr_auth`：`HttpOnly; SameSite=Lax; Path=/; Max-Age=7天`（与 refresh_token 同寿），滑动续期由会话校验时重写。
- 服务端会话记录 `AUTH_SESSIONS: dict[sid, record]`，持久化到 `work_dir/auth_sessions.json`（原子写，权限依赖本机文件系统；仅存本机）。
- 会话校验通过后写 `g.auth_owner`；`request_identity()` 优先返回 `rl:<id>`，否则回落到设备 uid（仅允许匿名模式）。
- **MCP 与管理员 Cookie 不变**；管理员可同时用个人账号上传任务（与 §7 现状一致）。
- 退出登录：`POST /api/auth/logout` → 调对端 `token/revoke`（access/refresh 各一次，失败不阻断）→ 删除服务端会话与 Cookie。

---

## 5. 认证流程

### 5.1 登录（授权码模式）

```
浏览器                     FlowHMR Flask                     anim.nd.com.cn
  │ GET /api/auth/login        │                                  │
  │───────────────────────────▶│ 生成 state + 记录原始目标页        │
  │ 302 authorize?app_id&redirect_uri&state&scope                │
  │──────────────────────────────────────────────────────────────▶│
  │                    平台登录 / 授权确认（用户点击）              │
  │◀── 302 redirect_uri?code=..&state=.. ────────────────────────│
  │ GET /api/auth/callback     │                                  │
  │───────────────────────────▶│ 校验 state（一次性、绑会话）      │
  │                            │ POST /token（应用签名）─────────▶│
  │                            │◀── access/refresh/scope ────────│
  │                            │ GET /user/info（Bearer）────────▶│
  │                            │◀── user/roles/scope ────────────│
  │                            │ 建会话、写 accounts.json          │
  │◀── 302 /（或原始目标页）────│ Set-Cookie: flowhmr_auth          │
```

要点：

1. **state**：32 字节随机串，存 `AUTH_STATES[state] = {created, next_url, browser_bind}`，TTL 300 秒、一次性；回调比较使用 `hmac.compare_digest`。`browser_bind` 使用登录发起时下发的短期 Cookie（`flowhmr_auth_pending`），防"别人把 code 塞给我"的登录 CSRF。
2. **redirect_uri 由服务端按配置拼装**（`auth_config.json` 的 `public_base_url` + 固定路径 `/api/auth/callback`），**不接受请求参数覆盖**，也不按请求 Host 推导，避免 Host 头注入与开放重定向。
3. **scope 请求 `reference_lib:read`**（只读用户信息）。应用已登记 `read` + `write`（§2.1），本期只申请 read；P2 登记资源时再申请 write，用户需重新授权。
4. **非属主任务不因登录变化**：登录只是让 `request_identity()` 返回 `rl:<id>`；任务池、评分、下载门禁逻辑不动。
5. **`/user/info` 返回的角色**必须包含 `anims_public`，否则视为授权无效（对端 `/authorize` 已拦一次，这里二次校验防配置漂移）。
6. 若用户从平台侧直接访问 `/api/auth/callback`（无 state），统一 302 到首页并显示"登录已失效，请重新登录"。

### 5.2 Token 续期

- 收到业务请求且会话存在时：若 `expires_at - now > 300` 直接放行，不产生对端调用（避免 60 次/分钟的 APPKEY 限流）。
- 否则用 refresh_token 调 `/token/refresh`（应用签名）：成功则更新 access/refresh/expires_at；失败（`invalid_token`）则销毁会话并清 Cookie，前端跳登录。
- 建议后台/首屏另起低频任务，每 10 分钟对活跃会话调一次 `/user/info` 刷新 `roles` 与昵称（仅活跃会话、同一账号去重、单次批量），角色被收回时标记会话 `role_missing`。
- **长时间任务不受影响**：任务属主在创建时固化，token 过期不影响排队/运行/下载；下载鉴权只依赖会话身份，不依赖 access_token。

### 5.3 注销与吊销

- 主动退出：`POST /api/auth/logout`（带 `X-FlowHMR-Auth: 1` 同源头校验），服务端先删会话再调 `revoke`，`revoke` 失败仅记日志。
- 本地管理员可"强制下线某个 `rl:*` 账号"：删除该 owner 的所有会话（管理后台操作 + 审计）。
- 会话 Cookie 被清除/设备更换：重新登录即可，任务归属不变。

---

## 6. 服务端接口设计

新增路由（均位于 `tools/web/server.py`，命名沿用现有 `api_*` 风格）：

| 接口 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/api/auth/config` | GET | 无 | 返回 `{mode, login_url, logged_in, provider_name}`，供前端决定是否显示登录入口 |
| `/api/auth/login` | GET | 无 | 生成 state → 302 到对端 `/authorize`；`?next=` 仅接受同源相对路径 |
| `/api/auth/callback` | GET | state | 校验 state → 换 token → 取用户 → 建会话 → 302 回跳 |
| `/api/auth/status` | GET | 会话 | `{logged_in, owner_id, user:{id,name,code}, roles, expires_at}`；供桌面壳轮询 |
| `/api/auth/logout` | POST | 会话 + 同源头 | 删除会话、吊销 token、清 Cookie |
| `/api/me` | GET/PUT | 现有 | GET 扩展字段（见下）；PUT 仅匿名模式允许改名，`rl:*` 返回 409 `name_managed` |

`GET /api/me` 响应扩展（保持旧字段不删，前端可渐进升级）：

```json
{
  "uid": "rl:123",
  "owner_id": "rl:123",
  "name": "张三",
  "ip": "10.0.0.8",
  "local": false,
  "auth": {
    "mode": "reference",
    "logged_in": true,
    "managed_name": true,
    "user": {"id": 123, "code": "zhangsan"},
    "roles": ["anims_public"],
    "expires_at": "2026-11-25T12:00:00+00:00"
  }
}
```

错误码（FlowHMR 侧，沿用 `api_error(error_code, message, status)` 风格）：

| error_code | HTTP | 触发 |
| --- | --- | --- |
| `auth_disabled` | 404 | 当前模式未开启登录（`local`） |
| `auth_required` | 401 | `reference` 模式下未登录访问任务接口 |
| `auth_state_invalid` | 400 | state 缺失/过期/已用/绑定不符 |
| `auth_denied` | 403 | 对端返回 400 授权失败（无角色/应用不存在/回调未登记） |
| `auth_role_required` | 403 | `/user/info` 的 `roles` 缺 `anims_public` |
| `auth_upstream_unavailable` | 503 | 对端超时、`rate_limited`、`storage_unavailable` 等 |
| `name_managed` | 409 | 平台账号尝试改昵称 |
| `auth_scope_missing` | 403 | `scope` 缺 `reference_lib:read` |

上游错误统一翻译：`invalid_code`/`invalid_token` → 重新登录；`insufficient_scope` → 提示重新授权；`role_required` → 联系平台管理员开通角色；`rate_limited` → 稍后重试（页面提示，不暴露内部细节）。

---

## 7. 配置项

登录相关的全部配置放在**独立配置文件** `tools/web/auth_config.json`（不读环境变量），路径可用 `--auth-config <path>` 覆盖；`--auth-mode` 只用于临时覆盖文件里的 `mode`。

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `mode` | string | `local` | `local`（现状，`/api/auth/*` 404）/ `optional`（可匿名，登录后升级）/ `reference`（强制登录） |
| `app_id` | string | 空（`mode!=local` 时必填） | 已登记应用，见 §2.1 |
| `secret` | string | 空（`mode!=local` 时必填） | 已登记密钥，见 §2.1；只存于本文件，不落日志、不下发前端 |
| `base_url` | string | `http://anim.nd.com.cn/api/openapi/v1/reference_lib` | 参考库 API base，便于指向测试环境/桩服务 |
| `public_base_url` | string | 空（`mode!=local` 时必填） | 浏览器访问 FlowHMR 的 origin（如 `http://192.168.139.125:60550`），用于拼装回调地址 |
| `scope` | string | `reference_lib:read` | 申请的 OAuth scope；P2 资源登记再改为含 `write` |
| `role` | string | `anims_public` | 登录后必须拥有的平台角色，缺失则授权失败 |
| `session_ttl` | int | `604800`（7 天） | 本地会话与 Cookie 的有效期（秒），最小值 300 |

示例（`tools/web/auth_config.json`）：

```json
{
  "mode": "reference",
  "app_id": "anim-ops",
  "secret": "<platform secret>",
  "base_url": "http://anim.nd.com.cn/api/openapi/v1/reference_lib",
  "public_base_url": "http://192.168.139.125:60550",
  "scope": "reference_lib:read",
  "role": "anims_public",
  "session_ttl": 604800
}
```

- 文件缺失或 JSON 损坏时按默认值（`mode=local`）启动并打印警告，不影响现有匿名模式。
- 该文件含 secret，已在 `.gitignore` 中忽略；部署时由运维在目标机器上创建/替换。
- 管理员密钥 `--admin-key` / `FLOWHMR_ADMIN_KEY` 与登录无关，仍按原方式配置。

`redirect_uri` 规则：

- 生产形态：`{public_base_url}/api/auth/callback`，如 `http://10.1.2.3:8080/api/auth/callback`。
- `public_base_url` 只填协议 + 主机 + 端口（即可在浏览器直接打开的 origin），**不要带结尾 `/`，也不要带 `/api/auth/callback`**；服务端负责拼接路径。
- 该完整 URI 必须加入平台 `REFERENCE_LIB_APPKEYS[app_id].redirect_uris`；**端口、协议、路径任一不同都会 `invalid_redirect_uri`**；平台侧按字符串精确匹配，写 `localhost` 与写 `127.0.0.1` 视为不同。
- 多机/多网段部署：为每台机器登记各自 URI，或统一走反向代理域名（推荐后者，只需登记一个 URI）。不建议把 FlowHMR 挂在子路径下（如 `/flowhmr/`），因为回调路径固定为 `/api/auth/callback`。
- 桌面壳（`tools/windows/desktop.py`）与 LAN 浏览器共用同一 `public_base_url`，避免"壳内 localhost、浏览器 LAN IP"导致回调不一致。

启动时校验：`mode != local` 时若缺少 `app_id`/`secret`，或 `base_url`/`public_base_url` 不是绝对 http(s) URL，直接打印错误（指明是哪个字段、哪个配置文件）并拒绝启动（fail fast），而不是运行到登录时才发现。

---

## 8. 数据模型与持久化

### 8.1 `accounts.json`（新增）

```json
{
  "rl:123": {
    "id": 123, "code": "zhangsan", "name": "张三",
    "roles": ["anims_public"], "scope": "reference_lib:read",
    "created": 1790000000.0, "last_login": 1790000000.0,
    "last_ip": "10.0.0.8", "disabled": false
  }
}
```

- 读取时校验键格式 `rl:\d+`；与 `users.json` 完全分离，避免破坏现有 32 位 uid 校验（[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L1922-L1926)）。
- 账号禁用（FlowHMR 本地禁用）后：可登录，但提交任务返回 403，已提交任务正常跑完（沿用现有 `user_disabled()` 语义，[server.py](file:///d:/xjq/FlowHMR-Windows/tools/web/server.py#L2111-L2113)）。

### 8.2 `auth_sessions.json`（新增）

- `{sid: {owner_id, created, last_seen, access_token, access_expires_at, refresh_token, scope}}`。
- 启动时只恢复未过期（`refresh_token` 7 天窗口内）的会话；损坏则整文件忽略并告警，不影响任务恢复。
- 不写入 `job.json`；`admin_audit.jsonl` 只记账号 ID 与动作，不记 token。

### 8.3 任务字段

无需新增字段；仅 `owner_id` 取值域扩展为 `rl:<id>`。历史任务保留原 uid / `legacy`，不做隐式重写。

---

## 9. 前端改造

| 位置 | 改动 |
| --- | --- |
| 顶部栏 `#btnUser` | `auth.mode=reference` 且未登录时显示"登录"；已登录显示平台昵称 + 头像占位，点击菜单：当前账号（`code`）、退出登录；`managed_name=true` 时隐藏"编辑昵称" |
| 启动流程 [main.js](file:///d:/xjq/FlowHMR-Windows/tools/web/static/js/main.js#L159-L167) | `/api/me` 返回 `auth`：未登录且 `reference` 模式 → 渲染登录遮罩（不加载任务列表）；`localStorage` 键由 `me.uid` 派生，天然按账号隔离 |
| `api.js` | 新增 `authStatus/loginUrl/logout`；`getMe` 保留；对 401 `auth_required` 统一跳登录 |
| 下载/评分/任务池 | 不改逻辑，仅属主名字改为平台昵称；`legacy`/`mcp` 规则不变 |
| 错误文案 | 新增登录相关文案，补齐 `locales/*.js` 七种语言；上游错误只显示人类可读摘要 |
| 桌面壳 | 登录按钮用系统浏览器打开或壳内新窗口；壳内轮询 `/api/auth/status`，登录成功后刷新页面。需确认壳的 Cookie 持久化与第三方跳转行为 |

---

## 10. 与现有功能的关系

| 功能 | 影响 |
| --- | --- |
| 任务归属/任务池 | `request_identity()` 优先返回 `rl:*`；`accessible_owners()` / `can_access_owner()` 不改 |
| 管理员后台 | 用户列表按 owner 展示，`rl:*` 额外显示 `code` 与角色；密钥登录方式不变；建议 P2 支持"平台角色 → 管理员"映射（需产品确认） |
| 评分门禁 | 不变；平台账号同样必须先评分才可下载 |
| MCP | 不变；MCP 任务 owner 仍为 `mcp` |
| SSE | 事件 `owner_id` 取值变化，过滤逻辑不变 |
| IP 兜底 `ip_fallback_user` | 仅在 `local` 模式启用；`reference` 模式禁用（避免按 IP 串号） |
| `--trust-proxy` | 保持不变；登录失败限流与审计仍用 `client_ip()` |
| 旧匿名用户任务 | 见 §11 迁移策略 |

---

## 11. 兼容与迁移

1. **默认关闭**：`auth_config.json` 的 `mode=local`（默认值）时所有 `/api/auth/*` 返回 404，前端不显示登录入口，行为与当前完全一致。
2. **`optional` 模式**：匿名用户可继续使用（提交任务、查看自己的任务），但不能写入资源库（P2）且任务池显示"未登录用户"；登录后新任务归属 `rl:*`，旧匿名任务回到用户自己名下需走认领。
3. **历史任务认领**（`reference` 模式必需，建议 P1 实现）：
   - 登录后若浏览器仍持有原 `flowhmr_uid` 且该 uid 下有任务，页面提示"发现 N 个旧任务，是否迁移到当前账号"。
   - 确认后服务端在 `JOBS_LOCK` 内把 `owner_id` 从 uid 改为 `rl:<id>`，写 `owner_name` 快照、追加审计；一次性操作，迁移后旧 Cookie 失效。
   - 未认领的 uid 任务保留原 owner（登录用户看不到），管理员可在后台查看/清理。
4. **`legacy` 任务**：不变，普通用户在桌面壳内可见只读、下载需评分（与现状一致）。
5. **降级**：把 `auth_config.json` 的 `mode` 改回 `local`（或启动时加 `--auth-mode local`）并重启即可回退；`accounts.json`/`auth_sessions.json` 保留不参与逻辑。

---

## 12. 安全设计

1. **密钥管理**：secret 只从 `tools/web/auth_config.json` 的 `secret` 读取（该文件已被 `.gitignore` 忽略，不读环境变量）；不写日志、不回显、不下发前端；启动日志只打印模式、app_id 与回调地址，不打印 secret。§2.1 的明文仅用于交付交接；若已外泄，需平台侧轮换 secret，并同步更新部署机配置文件。
2. **state / 登录 CSRF**：一次性 + 300 秒 TTL + 浏览器绑定 Cookie；回调只接受 GET 且无 `code` 时回到首页。
3. **开放重定向**：`redirect_uri` 服务端拼装；`next` 参数只允许以 `/` 开头的相对路径，且不接受 `//`、`\`。
4. **Token 存储**：access/refresh 仅服务端；`auth_sessions.json` 为本机文件，依赖操作系统账户权限；不进入导出包、不进 Git（`work_dir` 已在忽略范围）。
5. **同源防护**：登录发起为顶层跳转；退出登录与管理操作要求自定义头（沿用 `X-FlowHMR-Admin` 模式，新增 `X-FlowHMR-Auth: 1`）。
6. **限流**：登录发起按 IP 每分钟 10 次；回调失败按 IP 每分钟 20 次；对端 `rate_limited` 时对本应用的登录请求退避（返回 503 并带 `Retry-After`）。
7. **时钟**：启动时若与响应头 `Date` 偏差 > 120 秒，启动日志告警（签名窗口 300 秒）。
8. **日志脱敏**：请求日志记录 `owner_id`、`user.id`、`code`、`ip`，**永不记录** token/code/state 原文。
9. **角色校验**：以 `/user/info` 的 `roles` 为准，不能只信任 `/authorize` 的结果；角色失效时允许浏览自己的任务，但拒绝写操作（任务提交）并提示。
10. **管理员密钥不变**：仍为 `--admin-key`，不因接入平台账号而弱化；`reference` 模式下管理员若用个人账号上传任务，权限按普通账号处理。

---

## 13. 分阶段实施与验收

### P0：登录链路（只读身份）

1. 配置与启动校验（§7）；`accounts.json`、`auth_sessions.json` 读写。
2. `/api/auth/login|callback|logout|status|config`；`/api/me` 扩展；
3. `request_identity()` / `owner_display_name()` 支持 `rl:*`；`ip_fallback_user` 在非 local 模式禁用。
4. 前端登录入口、登录遮罩、退出登录；七语言文案。

验收：
- `local` 模式：所有 `/api/auth/*` 404，页面与现在一致。
- `reference` 模式：未登录访问任务接口 401，页面出现登录遮罩；登录后 `/api/me` 返回平台昵称与 `roles`，`owner_id=rl:<id>`。
- 两个不同平台账号各自提交任务：互相看不到、互不可操作；重启服务后仍保持。
- state 复用/过期/伪造 → 400；回调不在白名单 → 对端 400 授权失败页，FlowHMR 不建会话。
- 平台账号改名后重新登录，新任务用新昵称，旧任务 `owner_name` 不变。
- 断网/对端 503：已登录用户不受影响（token 未过期时），登录入口返回可读错误。
- 退出登录后 Cookie 失效，对端 `revoke` 记录成功（可用桩服务核对）。

### P1：会话续期、迁移认领、管理后台联动

1. refresh 续期与失败降级；活跃会话角色巡检。
2. 历史匿名任务认领（含审计）；管理员用户列表显示 `code` 与 `roles`，可强制下线。
3. 桌面壳登录体验与 Cookie 持久化验证。

验收：
- access_token 过期后业务操作无感续期；refresh 失效后跳登录且不丢任务归属。
- 平台侧收回 `anims_public` 后，最迟 10 分钟内写操作返回 `auth_role_required`，浏览不受影响。
- 认领后旧 uid 任务出现在新账号下，其他账号不可见；审计有记录。

### P2（可选，不在本次范围）

- 平台角色 → 管理员映射；资源库登记（`reference_lib:write`）：导出完成后自动登记图片/视频与 `child_ids`，上传根目录用 `326360534`（P2 实现时在 `auth_config.json` 增加 `upload_root_id`，`0` 时回退 `326353497`）；`library` 资源库与平台参考库联动。详细设计见 [接入公共动作参考库资源库方案](reference-lib-resource-library-plan.md)。

---

## 14. 风险与待确认

| # | 事项 | 影响 | 建议 |
| --- | --- | --- | --- |
| 1 | `redirect_uri` 白名单精确匹配 | LAN/多机部署易配错 | 固定 `public_base_url`，启动校验；多机走统一反代域名 |
| 2 | 单 APPKEY 60 次/分钟 | 多人同时登录/续期可能超限 | 缓存会话、按需续期、登录限流；必要时向平台申请更高配额 |
| 3 | 平台无用户列表接口 | 后台无法全量同步账号 | 以本地 `accounts.json` 为准，文档中明确该边界 |
| 4 | 角色收回延迟 | 最长 10 分钟窗口 | 写操作前对 `role_missing` 会话即时校验（可加 1 次/分钟的 `/user/info`） |
| 5 | 断网/平台不可用 | 新登录不可用 | 已登录会话按 token 有效期继续；不提供离线登录 |
| 6 | 历史匿名任务 | 登录后"消失" | 提供认领流程 + 管理员可见；默认不自动迁移 |
| 7 | 服务重启 | 内存会话丢失 | `auth_sessions.json` 恢复 refresh 窗口内的会话；失败则重新登录 |
| 8 | 桌面壳内嵌浏览器跳转 | 登录体验不一致 | 优先系统浏览器 + 轮询 `/api/auth/status`；实现前先验证壳的 Cookie 行为 |

**待确认问题**

1. 登录是否强制？配置项默认 `local`（不启用）；当前交付配置文件为 `reference`（强制登录），内网演示可改为 `optional`。
2. `redirect_uri` 的正式取值（机器 IP:端口 还是统一反向代理域名）？这决定平台侧白名单登记方式；当前配置文件用 `http://192.168.139.125:60550`，需平台把 `http://192.168.139.125:60550/api/auth/callback` 加入白名单。
3. 是否允许平台 `anims_public` 之外的角色登录（例如只读参观角色）？当前设计为"无 `anims_public` 提示授权失败"。
4. 管理员是否要与平台角色打通（例如平台管理员角色自动进入 `/admin`）？当前保持 `--admin-key` 不变。
5. 历史匿名任务认领是否默认开启？建议开启但需用户二次确认。
6. 桌面壳是否接受"跳系统浏览器登录"？若必须壳内完成，需要先验证 Cookie 与第三方跳转。
