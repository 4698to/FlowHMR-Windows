# 99U 任务结果通知接入文档

任务进入终态（完成 / 失败 / 取消）时，FlowHMR 通过 99U 消息服务给任务所属用户的登录账号 ID 发送一条文本消息。

## 1. 总体流程

```
worker_loop / 取消接口
  └─ 任务落盘（job.json）
       └─ notify_job_outcome(job)          # 只接受 done / failed / cancelled
            ├─ 解析接收人：owner_id = rl:<id> -> accounts.json 的登录名 code
            ├─ 渲染文本：任务名 + 状态 + 耗时 + 链接（失败附原因）
            ├─ 后台线程 POST /messages       # HMAC 签名，不阻塞任务队列
            └─ 轮询 /jobs/<job_id> 直到 success / failed / 超时，只记日志不重试
```

- 通知在任务状态落盘之后触发，发送失败不影响任务本身。
- MCP / legacy / 本地 cookie 会话没有平台登录 ID，不发送。
- 多人物场景任务只在父任务（`person_batch == id`）上发送一次，成员任务不重复发送。
- 服务重启时的历史任务恢复不会补发通知。

## 2. 接收人规则

| owner_id | 接收人 |
| --- | --- |
| `rl:<id>`（参考库登录） | `accounts.json` 中该账号的 `code`（登录名），缺失时回退 `id`，再回退 `rl:` 后的数字 |
| `mcp` / `legacy` / 本地会话 uid | 不发送 |

接收人即用户登录名校验 ID，直接作为 99U 请求的 `receiver` 字段；如 99U 账号体系使用平台数字 ID，可在配置中把 `receiver_field` 改为 `id`。

## 3. 配置

写在 `tools/web/auth_config.json`，为可选对象，缺省表示关闭：

```json
{
  "notify": {
    "enabled": true,
    "app_id": "anim-ops",
    "secret": "<向 99U 服务管理员申请的应用密钥>",
    "base_url": "http://anim.nd.com.cn/api/99u/v1",
    "sender": "10030473",
    "org": "ND",
    "receiver_field": "code"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 总开关，默认 `true` |
| `app_id` | 调用方应用 ID，签名 `X-App-Id` |
| `secret` | 应用密钥，用于 HMAC-SHA256；为空则整体关闭 |
| `base_url` | 99U API 前缀；签名路径为剥离 `/api` 后的 path（如 `/99u/v1/messages`） |
| `sender` | 发件人 99U ID，可省略，由服务端决定 |
| `org` | 组织标识，默认 `ND` |
| `receiver_field` | 接收人取自 `accounts.json` 的字段，默认 `code`（登录名），可改为 `id` |

任务链接使用已有的 `public_base_url`，格式为 `<public_base_url>/?job=<job_id>`；为空时消息不带链接。

## 4. 触发点

| 状态 | 代码位置 | 场景 |
| --- | --- | --- |
| `done` | 任务成功收尾（`finish_runtime_job`） | 推理与产物生成完成 |
| `failed` | `worker_loop` 异常分支 | 推理异常、显存不足等 |
| `cancelled` | `worker_loop` 的 `JobCancelled` 分支 | 运行中任务被取消 |
| `cancelled` | 用户取消排队任务接口 | 排队中被取消 |
| `cancelled` | 管理端清空队列 / 取消排队任务接口 | 管理员操作 |

## 5. 消息模板

```
【FlowHMR】任务「<文件名>」已完成，耗时 3 分 07 秒。
查看：http://<public_base_url>/?job=<job_id>
```

- 失败时追加一行：`原因：<error 前 300 字符>`。
- 取消时状态文案为 `已取消`。
- 耗时取 `finished - (started 或 created)`。
- 文本截断到 4000 字符；换行统一为 `\n`；以 `Content-Type:` 等头字段开头的行会前置空格，避免被 99U 服务判为请求头注入。

## 6. 签名与请求

请求头：

| Header | 值 |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-App-Id` | 应用 ID |
| `X-Timestamp` | Unix 秒 |
| `X-Nonce` | 单次随机串（32 位） |
| `X-Signature` | 见下 |

签名串（每行一个字段，`\n` 连接）：

```
app_id
timestamp
nonce
METHOD          # 大写，如 POST
PATH            # 剥离 /api 后的路径，如 /99u/v1/messages
sha256(body)    # 十六进制，GET 时为 sha256("") = e3b0c442...
```

> 注意：nginx 会先把 `/api` 前缀剥掉再转发，99U 服务按剥离后的路径验签。签名串里必须写 `/99u/v1/messages`；写 `/api/99u/v1/messages` 会返回 `401 invalid_signature`（99U 在线文档的示例与实际部署不一致）。

`X-Signature = HMAC-SHA256(string_to_sign, secret)` 的十六进制小写，签名有效期 ±300 秒，同一 nonce 不能复用。

发送：

```bash
curl -X POST "http://anim.nd.com.cn/api/99u/v1/messages" \
  -H "Content-Type: application/json" \
  -H "X-App-Id: anim-ops" \
  -H "X-Timestamp: 1750000000" \
  -H "X-Nonce: 4f8c2e1a9b7d4c3e8a1f6b2d9c0e7a35" \
  -H "X-Signature: <signature>" \
  -d '{"receiver":"10030473","sender":"10030473","org":"ND","text":"【FlowHMR】任务「demo.mp4」已完成，耗时 3 分 07 秒。"}'
```

响应（异步任务）：

```json
{"ok": true, "job_id": "<id>", "status": "queued", "dedupe": false}
```

轮询 `GET /api/99u/v1/jobs/<job_id>`（同样签名，GET body 为空），直到 `status` 为 `success` / `failed` 或超时（60 秒，间隔 2 秒）。

## 7. 失败与限流

- 发送或轮询失败只写 `WARNING` 日志（关键字 `99U notify`），不重试、不回滚任务状态。
- 同一任务同一终态只发一次（内存去重键 `job_id + status + finished`）。
- 99U 服务按应用限流约 30 次/分钟，本集成由任务终态触发，正常量级不会触达。
- 回复 `/help` 可查看 99U 服务命令；文件、图文、Markdown 等消息类型本集成未使用。

## 8. 排查

1. 确认 `notify.enabled` 为 `true` 且 `secret` 非空。
2. 确认任务 `owner_id` 形如 `rl:<id>`，且 `accounts.json` 中存在对应账号记录。
3. 服务日志搜索 `99U notify`，可看到签名失败、`status=failed` 或超时原因。
4. 用第 6 节 curl 手工验证应用 ID / 密钥与网络连通性。
