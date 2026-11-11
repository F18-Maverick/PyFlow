# 文件转发/resume 身份与暂停清扫修复 — 提交报告

状态：Commit 1（R1）实现完成，等待人类审查；Commit 2（R3）未开始。
计划与需求卡见 `docs/plans/forward-resume-identity-and-pause-plan.md`。

---

## Commit 1（R1）：resume 身份改用实际控制端点

### 提交意图

断线重连后的 resume 不再因“配置 host 字符串与实际连接地址不同名”而失败；`/resume_trans` 线格式、进度 JSON schema、命令语义、公共方法签名均不变。

### 改动内容

| 文件 | 关键变更 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 新增 `_socket_local_endpoint` / `_socket_peer_endpoint`；server `_request_resumes` 用控制 socket 的 `getsockname()` 作为 `target`；server `_resume_request` 改为接收实际 `client_socket`，`origin` 命中实际本地端点或配置端点即判直传；client `_request_resumes` 将旧直传行的 `entry[4] == (host, port)` 在内存中归一化为 `getpeername()`；client `resume_trans` 用 `getpeername()` 或配置端点判定直传，upload 行 key 仍为 `(host, port)`；client `_command_peer` 直传时记录 `getpeername()` |
| `test/integration/network_api/test_tcp_transfer_resume.py` | 新增参数化回归：`localhost` 别名、`0.0.0.0` 绑定 × 旧行/新行，双向 resume；新增关闭 socket 回退测试 |
| `test/unit/network_api/test_fid_routes.py` | `_resume_request` 新签名的调用点适配（传入服务端本地端点） |
| `docs/source/File_Transfer/File_Transfer.rst` | resume 身份来源、别名/通配绑定行为、旧行内存迁移、NAT/client_port 限制 |
| `docs/source/changes/transfer-fid-routes.md` | 追加本次变更记录 |

### 为什么这样改

对应需求卡路径 #1、#2、#6、#7、#8、#9、#11。旧实现比较的是两侧各自的**配置字符串**，server 绑 `0.0.0.0` 或 client 配 `localhost` 时，端口相同但 host 不同，导致直传被误判为转发或 server 拒绝 origin，resume 静默失效。实际端点由内核给出，天然覆盖通配绑定、host 别名、LAN 地址；配置端点作为同一 peer 的别名保留，兼容旧持久化行与 socket 已消失的回退。

### 局部验证

```bash
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py
# 通过；ruff --select F,E9 仍只有既有 console `response` F841（无关存量）

.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py -q
# 28 passed in 0.27s

.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -q
# 57 passed in 150.33s（含新增 5 条参数化/回退用例）

.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py \
  -k "resume_identity_uses_the_connected_endpoint or closed_control_socket_falls_back" -q
# 5 passed, 52 deselected in 15.33s

.venv/bin/python -m pytest test/unit/network_api \
  test/integration/network_api/test_tcp_file_transfer.py \
  test/integration/network_api/test_crypto_tcp.py \
  test/integration/test_forward_extension.py \
  test/integration/test_forward_msg_attribution.py -q
# 339 passed in 179.61s
```

运行时对照（真实 socket；`/tmp/pyflow_audit/`）：

- 改前：server 绑 `0.0.0.0` + client 配 `127.0.0.1`，以及 server `127.0.0.1` + client `localhost`，双向 resume 均失败，partial 停在 4096 字节；
- 改后：同一脚本两种配置双向均完成，内容与源文件一致，接收行与发送行清理。

```text
localhost upload_started: 1 row_cleared: True size: 8192 content_ok: True
localhost download_started: 1 row_cleared: True size: 8192 content_ok: True
wildcard  upload_started: 1 row_cleared: True size: 8192 content_ok: True
wildcard  download_started: 1 row_cleared: True size: 8192 content_ok: True
```

（`sender_row_cleared` 在脚本采样时刻尚未完成最终清理，属采样时序；完整套件中的对应断言已通过。）

### 明确未覆盖

- NAT：client 的 `getpeername()` 与 server 的 `getsockname()` 不在同一地址空间，本次不解决。
- `client_port` 变化：地址变化后 server 无法把持久化行匹配到新 peer，既有约束。
- 跨版本：旧 server 仍通告配置地址时，新 client 的“实际或配置”判定在配置字符串一致时兼容；不一致不保证。
- 本轮未改 Commit 2（R3）的任何行为。

### 风险与待确认

- 身份判定从单一配置值扩展为“实际端点或配置端点”，两者都指向当前已连接 peer；转发 route 仍以实际控制 peer 作用域，未放宽跨 peer 访问。
- 新直传下载行的 `peer` 字段记录实际 server 端点（`get_transfer_progress` 可见）；旧行读入后按需原地归一化，不写回文件。
- `_resume_request` 的内部签名增加 `client_socket`；已适配仓库内全部调用点与测试 monkeypatch。

---

## Commit 2（R3）：待实现

按计划实现后再补本文件对应章节；当前未开始，不报告任何结果。
