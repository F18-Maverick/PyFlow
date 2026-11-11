# 文件转发/resume 身份与暂停清扫修复 — 需求卡、计划与报告

状态：**Commit 1（R1）与 Commit 2（R3）已实现，等待人类审查**。审计证据来自只读复核与 `/tmp/pyflow_audit/` 下的真实 socket 复现脚本；提交结果见 `docs/plans/forward-resume-identity-and-pause-report.md`。

---

## 0. 审计基线（已复现）

### R1：resume 用配置地址比较，`localhost` / `0.0.0.0` 下双向失效

实测（server 绑 `0.0.0.0`，client 配 `localhost`）：

| 来源 | 值 |
|---|---|
| `server.host, server.port`（配置） | `('0.0.0.0', 47397)` |
| server 已接受控制 socket 的 `getsockname()` | `('127.0.0.1', 47397)` |
| `client.host, client.port`（配置） | `('localhost', 47397)` |
| client 的 `getpeername()` | `('127.0.0.1', 47397)` |

端口相同，host 字符串不同。失败点：

- server `_request_resumes`（`connect_tcp.py:6336`）把 `target=(self.host, self.port)` 写上线；
- client `resume_trans`（`10623-10635`）用 `target != (self.host, self.port)` 判定“转发”，误走 `/forward_item`；
- server `_resume_request`（`6399`）用 `origin == (self.host, self.port)` 判定“直传”，失配；
- client `_command_peer`（`10979-10996`）把直传 peer 记为 `(self.host, self.port)`。

**不是回环专属**：server 在 `192.168.1.10` 上绑 `0.0.0.0`、client 在另一台机器配 `192.168.1.10` 时，server 通告 `('0.0.0.0', P)`，client 的 `(self.host, self.port)` 是 `('192.168.1.10', P)`，同样失配。DNS 别名（`nas.lan` vs `192.168.1.10`）、多网卡、IPv6 `localhost=::1` 同理。

### R3：清扫关闭 socket 但不解除 pause，暂停 leg 永久阻塞并占住 per-fid 发送锁

server 侧端到端复现（`/tmp/pyflow_audit/repro_r3_server.py`）：

```
A ready sockets: True  row: True  pause: True  leg_alive: True
B swept active: False  sockets: False  pause: True  state_users: 1  leg_alive: True
C resume_queued: True  send_called: 0  leg_alive: True  targets: {('127.0.0.1', 59594)}
D after_release leg_alive: False  result: False  send_called: 1
```

- `_drop_stale_transfers`（`2935-2970`）只 `pop` socket、`discard` active fid，不删 `_transfer_pause`、不通知；
- `_wait_transfer_paused`（`3008-3026`）重新查登记表，pop 后 `sock is None`，`fileno()<0` 退出条件永不成立；server 的 `self.running` 不随单客户端断开而变 False；
- leg 不退出 ⇒ `_sending_transfer` 不释放 state 锁 ⇒ 已“接受”的 resume 在 `run()` 里永久等待；只有人工 `start_trans` 或实例 stop/close 才解除。

### R2（不纳入本次）

`_forward_one` 预注册进度行在 leg 启动前失败时残留。现有 `delete_config` 可清理，且复核确认不会自动重发；按评审决定作为已接受风险，不改。

---

## 1. 需求卡

### 目标

- **R1**：断线重连后的 resume 在“配置地址与实际连接地址不同名”的部署下仍能完成，且**不改变** `/resume_trans` 线上格式、进度文件 schema、命令语义、公共方法签名。
- **R3**：断线清扫遇到已暂停的发送 leg 时，leg 必须在有限时间内结束并释放 per-fid 发送锁；排队中的 resume 不得依赖人工 `start_trans`。

### 非目标

- 不解决 NAT 下的端点身份（记录为已知限制，见 §7）。
- 不解决 `client_port` 变化导致的地址不可匹配（既有约束）。
- 不新增命令、字段、状态文件；不做旧 progress 文件格式迁移（仅内存归一化旧行，见 §2.1）。
- 不改 CI、依赖、覆盖率策略、公共方法签名。
- 不改 R2 的现有行为。

### 受影响范围

- `PyFlow/network_api/connect_tcp.py`
  - server：`_request_resumes`、`_resume_request`、`handle_command` 的 `RESUME_LINE` 分派、`_drop_stale_transfers`、`_wait_transfer_paused`、`file_transfer_mode` 的 pause 调用；
  - client：`_request_resumes`、`resume_trans`、`_command_peer`、`_drop_stale_transfers`、`_wait_transfer_paused`、`file_transfer_mode` 的 pause 调用。
- 测试：`test/unit/network_api/` 与 `test/integration/network_api/test_tcp_transfer_resume.py`。
- 文档：`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/changes/`。

### 执行路径清单（全集）

| # | 路径 | 期望 |
|---|---|---|
| 1 | 直传 client→server 上传 resume（server 为接收端发起） | 完成，行清理 |
| 2 | 直传 server→client 下载 resume（client 为接收端发起） | 完成，行清理 |
| 3 | 转发 target resume（target→server→source，source 在线） | 完成，route 按现有语义清理 |
| 4 | 转发 target resume（source 离线，pending 保留后重连 flush） | 保留后执行 |
| 5 | 转发零偏移 / EOF 重放（`resume=0`、`offset==size`） | 复用 route，不新分配 fid |
| 6 | `localhost` vs `127.0.0.1` 双向 | 完成 |
| 7 | server 绑 `0.0.0.0`、client 用具体 IP 双向 | 完成 |
| 8 | 旧持久化行（`entry[4]` 为配置地址） | 内存归一化后完成 |
| 9 | 控制 socket 已消失 / `getsockname()` 抛 `OSError` | 不崩溃，保留进度 |
| 10 | 非 resume 的新传输 | 行为不变 |
| 11 | 外部调用 `resume_trans(peer_addr=None)` | 行为不变 |
| 12 | paused + 清扫 + 排队 resume（server 发送端） | leg 结束，resume 自动执行 |
| 13 | paused + 清扫 + 排队 resume（client 发送端） | 同上 |
| 14 | 清扫后 pause 被重新 `/pause_trans` 置位 | leg 仍能退出 |
| 15 | 普通 `pause_trans`/`start_trans`（无断线） | 行为不变 |
| 16 | 转发回压暂停 + 清扫（`_forward_pump` 路径） | leg 结束，不阻塞后续 resume |

### 安全与数据边界

- `/resume_trans` 的 `origin`/`target` 仍是线上自报字段；server 仍以**实际控制 peer** 做 route 作用域校验，不放宽跨 peer 访问。
- 直传判定从“单一配置字符串”扩展为“实际端点或配置端点”，两者都必须指向当前已连接 peer；不引入“0.0.0.0 等于任意主机”这类宽规则。
- 不在日志中新增密钥/明文文件内容；地址本就在线上与日志中出现。
- 不改进度文件 JSON schema；旧行只在内存中替换直传 peer。

### 验收证据

- 每条路径有真实 socket 测试或复现脚本；R1 至少覆盖 #6、#7、#8；R3 至少覆盖 #12、#13。
- 改前失败、改后通过的同一命令输出；线程/锁释放有断言。
- 既有 resume/forward 套件不回退；不删除、不弱化断言。

### 禁止事项

- 禁止改线格式、进度 schema、公共签名、CI、依赖。
- 禁止用硬编码 `{'localhost','0.0.0.0','127.0.0.1'}` 表替代端点判定（理由见 §2.1）。
- 禁止删除/弱化测试、加 `|| true`、降低覆盖率。
- 禁止绕过控制 peer 作用域。
- 禁止把 R2 混入本次提交。

### 风险等级

中。R1 触及身份判定（跨 peer 误判风险）；R3 触及线程/锁生命周期（并发回归风险）。

---

## 2. 方案

### 2.1 R1：实际端点优先，配置端点作为同一 peer 的别名

判定规则（两侧同构）：

- 直传 server 端点（client 视角）= `control_socket.getpeername()` **或** `(self.host, self.port)`。
- 直传 client 端点（server 视角）= `control_socket.getsockname()` **或** `(self.host, self.port)`。
- 转发端点（target/origin）继续使用线上实际地址，路由作用域不变。

具体改动：

| 位置 | 现状 | 改为 |
|---|---|---|
| server `_request_resumes`（`6336`） | `target = (self.host, self.port)` | `target = info["socket"].getsockname()`；`OSError` 时回退配置值并记录 |
| client `resume_trans`（`10623-10635`） | `target != (self.host, self.port)` 判转发 | `target` 命中 `getpeername()` 或配置值时判直传；upload 行 key 仍为 `(self.host, self.port)`，不改 schema |
| client `_command_peer`（`10979-10996`） | 直传返回 `(self.host, self.port)` | 直传返回 `control_socket.getpeername()`；socket 已消失时回退配置值 |
| client `_request_resumes`（`10575-10607`） | `origin = entry[4]` | 直传旧行（`entry[4] == (self.host, self.port)`）先内存归一化为 `getpeername()`，再发送 |
| server `_resume_request`（`6395-6409`） | `origin == (self.host, self.port)` | `origin` 命中 `client_socket.getsockname()` 或配置值判直传；`handle_command` 把实际 `client_socket` 传入 |
| `format_resume_line` / `parse_resume_command` | — | 不改 |
| 进度 JSON | — | 不改；新行 `peer` 记录实际端点，旧行读入后按上述规则归一化 |

**为什么不用三字符串表**（评审提案的复核结论）：

- LAN 反例：server 在 `192.168.1.10` 绑 `0.0.0.0`，client 配 `192.168.1.10`。表里只有 `0.0.0.0`，无法知道应等同 `192.168.1.10`；若把 `0.0.0.0` 视为“任意主机”，则是比现有行为更宽的身份规则。
- DNS 别名：`nas.lan` vs `192.168.1.10` 不在表内。
- 多网卡：同一 `0.0.0.0` 对应多个本地 IP，不能归一成单个回环地址。
- `localhost` 可能解析为 `::1`，`127.0.0.1` 不覆盖。
- 实际端点由内核给出，天然覆盖上述非 NAT 场景；配置端点作为回退只用于 socket 不可用/旧行兼容。

NAT 明确不在本次范围：client 的 `getpeername()` 是公网地址，server 的 `getsockname()` 是私网地址；二者不可比。现有实现同样不支持，记录为已知限制。

### 2.2 R3：清扫时解除 pause，并让暂停等待持有自己的 socket

| 位置 | 改动 |
|---|---|
| `_drop_stale_transfers`（server `2935-2970` / client `8475-8510`） | 收集被清扫的上传 key；循环结束后 `with _transfer_pause_cond: pop(key); notify_all()`。放在 `_transfer_progress_lock` 之外，避免锁序变化 |
| `_wait_transfer_paused`（server `3008-3026` / client `8511-8529`） | 增加可选参数 `leg_socket=None`；退出条件增加 `leg_socket is not None and leg_socket.fileno() < 0`；两处 `file_transfer_mode` 传入 `client_file_socket` |
| `file_transfer_mode`（两处） | 调用改为 `_wait_transfer_paused(transfer_key, client_file_socket)` |
| 公共 API | 无变化；`_wait_transfer_paused` 为内部方法 |

双保险：即使 pause 标志被后续 `/pause_trans` 重新置位，已关闭的 leg socket 也能让等待退出；清扫同时清掉遗留 pause，避免同一 fid 的后续 leg 无端暂停。

---

## 3. 数据流

### 3.1 R1 直传上传（server 接收，client 发送）

```
client 断开 → server _unregister_client 持久化 download 行（peer=client 实际地址）
client 重连 → server _register_client → _resume_after_connect
  → _request_resumes(client_addr)
      target = accepted_control.getsockname()          # 实际本地端点
      /resume_trans <fid> <disk_size> <client_addr> <server_actual>
  → client handle_server_command
      origin == client_socket.getsockname()?            # 现有校验，实际地址
      resume_trans(target=server_actual)
          target == getpeername() or configured?        # 判直传
          /file <basename> <fid> → file_transfer_mode(resume_offset=offset)
  → server file_transfer_mode_recv：_resumable_entry(fid, peer=client_addr) 命中
```

### 3.2 R1 直传下载（client 接收，server 发送）

```
client 断开 → client _persist_transfer_progress（download 行 peer=实际 server 端点）
client 重连 → connect() → _request_resumes()
  origin = entry[4]（旧行先归一化为 getpeername）
  target = client_socket.getsockname()
  /resume_trans → server _resume_request
      target == client_address ✓
      origin == client_socket.getsockname() or configured? → 直传
      resume_trans(client_addr, fid, offset) → /file → file_transfer_mode
```

### 3.3 转发 resume（不变）

```
target → /resume_trans <child_fid> <offset> <source_addr> <target_addr>
server _resume_request：target==client_address，_lookup_fid_route(target, child)
  route.originator == origin → _queue_resume_to_originator
  → /resume_trans <upstream_fid> <offset> <source_addr> <target_addr> → source
source resume_trans：target != server → _forward_one(resume_offset, upstream_fid)
```

### 3.4 R3 暂停清扫

```
leg 在 _sending_transfer(key) 内，停在 _wait_transfer_paused(key, leg_sock)
重连/清扫 → _drop_stale_transfers(peer)
  pop socket / discard active / close
  with pause_cond: pop(key); notify_all()
leg 醒来：leg_sock.fileno() < 0 或 pause 已清 → 返回
  → send 失败 → finally：_file_progress_finish(keep=True)
  → _sending_transfer 释放 state 锁
排队 resume 的 run() 取得锁 → send(entry) 执行
```

---

## 4. 路径映射

| # | 路径 | 生产改动 | 测试 |
|---|---|---|---|
| 1 | 直传上传 resume | `_request_resumes`、`_resume_request`、`_command_peer` | `test_tcp_transfer_resume.py` 新增/现有 |
| 2 | 直传下载 resume | `resume_trans`、`_request_resumes`、`_command_peer` | 同上 |
| 3 | 转发在线 resume | 无（仅判定辅助） | 现有 `test_forwarded_file_resumes_through_the_server` |
| 4 | 转发离线 pending | 无 | 现有 `test_pending_resume_survives_failed_send...` |
| 5 | 零偏移/EOF | 无 | 现有零偏移与 EOF 测试 |
| 6 | localhost 别名 | 全部 R1 改动 | 新增 `test_resume_across_localhost_alias` |
| 7 | 0.0.0.0 绑定 | 全部 R1 改动 | 新增 `test_resume_across_wildcard_bind` |
| 8 | 旧行归一化 | `_request_resumes` | 新增 `test_resume_normalizes_legacy_configured_peer` |
| 9 | socket 已消失 | `getsockname/getpeername` 回退 | 新增 OSError 注入 |
| 10 | 新传输 | 无 | 现有文件/文件夹套件 |
| 11 | `resume_trans(None)` | 默认 target = `getpeername()` | 单测 |
| 12 | server paused + sweep | `_drop_stale_transfers`、`_wait_transfer_paused` | 新增 `test_paused_server_leg_released_by_sweep` |
| 13 | client paused + sweep | 同上 | 新增 `test_paused_client_leg_released_by_connect` |
| 14 | 清扫后重新 pause | socket 捕获退出 | 上述测试内加断言 |
| 15 | 普通 pause/start | 无 | 现有 pause 测试 |
| 16 | 转发回压 + 清扫 | 同 #12 | 转发 pause 测试扩展 |

---

## 5. 原子提交拆分

### Commit 1：R1 — resume 身份改用实际控制端点（别名/通配绑定）

- **意图**：断线重连后，直传与转发的 resume 不再因配置 host 字符串差异失败；线格式、schema、公共签名不变。
- **文件**：`PyFlow/network_api/connect_tcp.py`、`test/integration/network_api/test_tcp_transfer_resume.py`、`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/changes/transfer-fid-routes.md`（或新变更记录）、`docs/plans/forward-resume-identity-and-pause-report.md`。
- **预估**：生产 60–120 行；测试 180–300 行；文档 30–60 行。
- **局部验证**：
  ```bash
  .venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k "resume_across or legacy_configured or download_resumes or upload_resumes" -q
  .venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py -q
  ```
  以及 `/tmp/pyflow_audit/repro_hostname.py`、`repro_bindaddr.py` 的改前/改后对照（改前失败、改后完成）。
- **明确未覆盖**：NAT；`client_port` 变化；跨版本旧 server 通告。
- **报告**：见 §7.1。

### Commit 2：R3 — 清扫暂停 leg 的退出与锁释放

- **意图**：断线清扫后，暂停中的发送 leg 在有限时间内结束；同一 fid 的 resume 自动执行，无需人工 `start_trans`。
- **文件**：`PyFlow/network_api/connect_tcp.py`、`test/integration/network_api/test_tcp_transfer_resume.py`、`docs/source/File_Transfer/File_Transfer.rst`、`docs/plans/forward-resume-identity-and-pause-report.md`。
- **预估**：生产 25–50 行；测试 100–180 行；文档 20–40 行。
- **局部验证**：
  ```bash
  .venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k "paused and sweep" -q
  .venv/bin/python -m pytest test/integration/network_api/test_tcp_file_transfer.py -k "pause" -q
  ```
  `/tmp/pyflow_audit/repro_r3_server.py` 改前（C 处 `send_called=0`、leg 存活）改后（resume 自动执行、leg 结束）。
- **明确未覆盖**：暂停持久化（进程重启后 pause 不恢复）；NAT。
- **报告**：见 §7.2。

### Commit 3：文档收尾与规则沉淀（仅文档）

- **意图**：总报告、变更记录、`get_transfer_progress` 中 `peer` 字段语义说明（新行记录实际端点）、已知限制（NAT、client_port 变化）。
- **文件**：`docs/plans/forward-resume-identity-and-pause-report.md`、`docs/source/changes/`、必要时 `AGENTS.md` 检查项。
- **局部验证**：`git diff --stat` 仅文档；链接/标题检查。
- **报告**：总报告 §8。

> 拆分依据是单一意图，不是行数。两个代码提交均低于 300 行生产改动；若实现超出 300 行，退回重拆而不是合并意图。

---

## 6. 验收命令

```bash
# 编译/静态检查（不新增告警）
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py

# 定向回归
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py -q
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -q
.venv/bin/python -m pytest test/integration/network_api/test_tcp_file_transfer.py -k "pause or forward or folder" -q

# 相关全量（提交前）
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py \
  test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py \
  test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py \
  test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

运行时证据（两条，必须改前/改后各跑一次）：

```bash
.venv/bin/python /tmp/pyflow_audit/repro_hostname.py
.venv/bin/python /tmp/pyflow_audit/repro_bindaddr.py
.venv/bin/python /tmp/pyflow_audit/repro_r3_server.py
```

---

## 7. 提交报告模板

### 7.1 Commit 1（R1）

```text
提交意图：resume 身份判定改用实际控制端点，配置端点仅作同一 peer 的别名；线格式/schema 不变。
改动内容：<文件与关键行>
为什么这样改：对应需求卡路径 #1/#2/#6/#7/#8/#9/#11；修复 R1。
局部验证：<命令 + 实际输出，含改前失败/改后通过>
明确未覆盖：NAT；client_port 变化；跨版本旧 server。
风险与待确认：身份判定放宽为“实际或配置”两值；route 作用域未放宽；旧行仅在内存归一化。
```

### 7.2 Commit 2（R3）

```text
提交意图：断线清扫时解除 pause，并让暂停等待以 leg 自身 socket 的关闭为退出条件；排队 resume 自动执行。
改动内容：<文件与关键行>
为什么这样改：对应需求卡路径 #12/#13/#14/#16；修复 R3。
局部验证：<命令 + 实际输出，含改前 leg 卡死/resume 不执行、改后自动执行>
明确未覆盖：暂停持久化；进程重启后的 pause 恢复。
风险与待确认：pause 清理与 notify 的锁序；leg socket 捕获的调用点覆盖。
```

---

## 8. 总体报告模板

```text
需求卡路径清单逐条对照：1–16 覆盖/未覆盖/部分覆盖
总体数据流与架构影响：仅身份判定与暂停生命周期；无新命令/字段
所有原子提交汇总：Commit 1 / 2 / 3
明确未覆盖项与已知风险：NAT、client_port 变化、暂停持久化、跨版本兼容
对 CI、测试、依赖、公共接口的影响：无 CI/依赖；公共签名不变；get_transfer_progress 的 peer 字段对新行记录实际端点
建议的验收命令与证据清单：§6
```

---

## 9. 合并前闸门

1. 红旗复查：无删测试/弱化断言/CI 改动/依赖改动/绕过控制 peer 作用域。
2. 规则沉淀：新增 R1/R3 回归测试；在 `docs/source/changes/` 记录“身份用实际端点、配置端点仅别名”“清扫必须解除 pause”两条约束。
3. 文档收尾：`File_Transfer.rst` 的 resume 段说明身份来源与已知限制；变更记录更新。
4. 人工最终确认后才允许提交远端。

---

## 10. 明确未覆盖与已知限制

- **NAT**：client `getpeername()` 与 server `getsockname()` 不在同一地址空间；本次不解决。
- **client_port 变化**：地址变化后 server 无法把持久化行匹配到新 peer；既有约束。
- **多网卡 + 通配绑定**：实际端点能匹配“本次连接使用的本地 IP”；若同一客户端跨会话换网卡，仍按新地址匹配，旧行可能无法续传。
- **跨版本**：旧 server 仍通告配置地址时，新 client 的“实际或配置”判定在配置字符串一致时可兼容；不一致时不保证。
- **已运行**：Commit 1/2 的定向回归与相关套件，结果见提交报告；本轮未额外做跨平台 CI 验证。
