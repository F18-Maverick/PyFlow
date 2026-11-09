# Windows 本地端口 WSAEACCES 回退修复计划

状态：需求卡与扩展计划已批准并实施。定向 smoke 22 passed；最终受影响套件 97 passed。未在 Windows runner 上验证真实 WSAEACCES；未创建提交、未合并、未推送。

## 1. 需求卡

### 目标

当固定非零本地端口因 Windows `WSAEACCES`（`winerror == 10013`，或 Windows `errno.EACCES`）不可用时，退回 OS 分配端口，并保持连接/传输成功。

适用路径：

- `TCP_Client_Base.connect()` 控制连接：固定非零 `client_port` 的 `bind()` 或 `connect()`。
- `TCP_Server_Base.file_transfer_mode()` 与 `TCP_Client_Base.file_transfer_mode()` 传输发送 socket：固定非零 `client_port` 的 `bind()` 或 `connect()`。
- `TCP_Server_Base.file_transfer_mode_recv()` 与 `TCP_Client_Base.file_transfer_mode_recv()` 传输接收 listener：固定非零 `server_file_port` 的 `bind()`。回退端口必须通过 `getsockname()` 取得并公告给发送方。
- `TCP_Server_Base._forward_relay()` 的上游 `up_sock` listener：固定非零 palloc 端口 `bind()`。回退后的实际端口公告给 forwarder；端口分配器必须释放原申请端口，而不是 OS 端口。

既有 `errno.EADDRINUSE` 与 `winerror == 10048` 回退保持不变。`None`/`0` 表示未固定端口，不把 WSAEACCES 当成固定端口冲突。发送 socket 的固定端口冲突只触发一次切换到 `port=0`，之后沿用原有传输重试；不增加最大重试次数。

### 非目标与禁止项

- 不修改通用 `create_temporary_client()` / `create_temporary_server()` side-channel API、服务端主 listener、无固定本地端口的 forwarding target `connect()`、端口分配算法、传输协议/FID/恢复身份、公共函数签名、CI、依赖或覆盖率策略。
- 不将未固定端口上的 10013 当成固定端口冲突；不对其他 socket 错误做新降级；不新增重试次数。
- 不删除或弱化现有测试断言。

### 数据与风险边界

本次只改变本地端口失败时的选择。控制协议、远端 listener 地址、文件内容、加密、FID 和 ACK 不变。文件接收与转发 listener 回退后必须公告 `getsockname()` 的真实端口；`_forward_relay` 还必须释放原 palloc 端口，避免手动分配表泄漏。风险：中，涉及控制、直传和转发的 socket 生命周期。

## 2. 数据流与路径全集

```text
控制连接：固定非零 client_port -> 新 socket -> bind -> connect
  本地端口冲突 -> close -> 不再绑定固定地址 -> connect 一次
  控制端口为 None/0、文件传输 palloc 返回 0，或其它错误 -> 原路径

直传发送端（server/client）：palloc client_port -> track_socket -> bind -> connect
  本地端口冲突 -> release_socket + close -> 后续重试用 port=0 -> OS 源端口

直传接收端（server/client）：palloc server_file_port -> listener bind
  本地端口冲突 -> 同 socket bind(host, 0) -> getsockname -> 公告真实 listener 端口

转发发送 listener：palloc -> track up_sock -> bind
  本地端口冲突 -> bind(host, 0) -> 公告 getsockname 端口 -> finally 释放原 palloc 端口
```

| 路径 | 代码落点 | 必须保持/新增行为 |
|---|---|---|
| 控制连接固定端口 bind/connect 冲突 | `TCP_Client_Base.connect()` | 10013 与既有 10048/EADDRINUSE 一样改用 OS 端口并连通 |
| 控制连接本地端口为 None/0 | `TCP_Client_Base.connect()` | 不进入固定端口回退；原错误/等待语义不变 |
| 服务端/客户端发送文件：固定源端口 bind/connect | 两端 `file_transfer_mode()` | 释放已登记失败 socket，再以 port 0 继续原重试；传输内容和 ACK 不变 |
| 服务端/客户端接收文件：固定 listener bind | 两端 `file_transfer_mode_recv()` | listener 回退到 port 0；公告真实端口，发送端仍完成传输 |
| 转发 relay 的 upstream listener bind | `TCP_Server_Base._forward_relay()` | listener 公告真实端口；`pfree()` 释放原申请端口 |
| 自动端口 0、其它错误 | 上述路径 | 不进入固定端口专用降级；沿用原处理/重试 |
| 无固定源端口的 forwarding target connect | `_forward_relay()` | 不变；connect 不绑定固定本地端口 |
| 通用 temporary client/server API、主 listener | 两个类的 API | 本次不修改；不声称覆盖 |

## 3. 文件与契约影响

计划修改：

- `PyFlow/network_api/connect_tcp.py`：识别 `EADDRINUSE`、Windows 10048/10013 与 Windows-only `errno.EACCES`；新增固定非零端口判定及共用 listener bind 回退；更新主连接、两端发送 socket、两端接收 listener、转发 upstream listener 的错误/资源处理；forward relay 记录并释放原 palloc 端口。
- `test/integration/helpers.py`：加入一次性注入 `OSError(errno.EACCES)` + `winerror=10013` 的 socket 测试工具。
- `test/unit/network_api/test_TCP_client.py`：覆盖 Windows-only `errno.EACCES` 识别边界。
- `test/integration/network_api/test_tcp_transfer_resume.py`：控制连接 10013 bind/connect 回退、无固定端口不回退；在真实转发续传中覆盖 relay listener 回退及原 palloc 端口释放；保留现有真实“配置端口被占用”用例。
- `test/integration/network_api/test_tcp_file_transfer.py`：真实 loopback 覆盖服务端/客户端发送 socket 的 bind/connect 回退；覆盖两端接收 listener bind 回退并断言实际端口公告与文件字节。
- `docs/source/Network_APIs/TCP_Client_APIs.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`：记录固定本地传输端口不可用时回退到 OS 端口；更新控制连接说明。
- 本计划及 `docs/plans/winerror-10013-local-port-fallback-report.md`：记录需求、路径证据与平台限制。

不新增/修改公共 API。CI、依赖、数据模型均不变。

## 4. 原子变更与局部验收

一个原子意图：统一固定本地端口冲突识别及控制/文件传输出站 socket 回退。预计总 diff 不超过约 400 行；若实现需要修改列出的范围之外的公共 API、主 listener 或通用 side-channel，先退回本计划，不边写边扩围。

局部验证：

1. **错误识别**：`winerror=10013` 在当前 Linux 测试进程也被识别；单独 `errno.EACCES` 只在 `os.name == "nt"` 时匹配；EADDRINUSE/10048 保持匹配。
2. **控制连接**：真实 loopback server/client；注入 10013 到固定端口 bind 与 connect，均断言 `connect()` 成功且实际本地端口不同于配置值。无固定端口时注入 10013，断言不执行固定端口回退且保持失败。
3. **直传发送**：真实 loopback 两方向；对 server/client sender 的固定源端口 bind/connect 注入 10013，断言成功传输、文件逐字节相等、失败 socket 从 handshake registry 释放。
4. **直传接收 listener**：真实 loopback 两方向；对固定 listener bind 注入 10013，断言公告端口不同于固定端口且客户端连通并完成文件传输。
5. **转发 listener**：真实转发续传中对 relay upstream listener 注入 10013；断言目标收到完整文件、回退端口可连接、原 palloc 端口在 finally 中释放。
6. **原回归与套件**：运行 `test_client_connects_when_its_configured_port_is_taken`、相关上传/下载/续传用例及三个受影响测试文件。
7. 测试必须通过真实 TCP listener 观察连接与文件内容；不 mock 掉关键连接/传输路径。

建议命令：

```sh
uv run pytest test/unit/network_api/test_TCP_client.py \
  test/integration/network_api/test_tcp_transfer_resume.py \
  test/integration/network_api/test_tcp_file_transfer.py -q
```

当前环境为 Linux：合成 `winerror=10013` 可验证产品分支；真实 Windows WSAEACCES/Hyper-V/WinNAT 与 Windows CI 尚不能由本机证明，报告必须标明，不得声称已通过。

## 5. 人工审查点

- 全部覆盖路径为：控制连接、两端直传发送、两端直传接收 listener、转发 upstream listener；主 listener 与通用 side-channel 明确不在范围。
- 10013/`errno.EACCES` 只在固定非零本地端口时触发；端口 0 和未绑定连接不做固定端口降级。
- 直传发送失败 socket 必须先从 `_transfer_handshakes` 释放再关闭；重建 socket 后方可重试。
- 监听回退需公告真实 `getsockname()` 端口；转发 listener 失败回退后必须 `pfree()` 原 palloc 请求值。
- OS 分配端口后协议远端地址、加密协商、数据与 ACK 保持原样。
- 不提交、不合并、不推送；最终工作区与 Windows CI 证据交由人类审查。

## 6. 实施结果与验证

### 变更结果

- `connect_tcp.py`：控制连接、server/client 文件发送 socket 均只在固定非零源端口冲突时改用 OS 端口；两端文件接收 listener 与 `_forward_relay` listener 在固定 bind 失败时 bind 到端口 0，并公告实际端口。转发 relay 在清理时释放原始 palloc 端口。
- 错误识别：保留 `errno.EADDRINUSE` 与 WinError 10048；新增 WinError 10013；仅 Windows 上的 `errno.EACCES` 被视为端口冲突。端口 `None` 和 `0` 不符合固定端口回退条件。
- 更新客户端/服务端 TCP API 文档，说明固定 transfer 端口是 best-effort 及接收 listener 端口公告行为。
- 新增 loopback 集成回归：控制连接 bind/connect 10013；控制连接未绑定与端口 0 不回退；两端文件 sender bind/connect 10013；两端接收 listener bind 10013；真实 relay resume listener bind 10013 与原 palloc 端口释放。传输测试比较收件文件实际 bytes。

### 验证证据

1. 定向 smoke：

```text
uv run pytest test/unit/network_api/test_TCP_client.py \
  test/integration/network_api/test_tcp_transfer_resume.py::test_client_falls_back_when_fixed_port_returns_wsaeacces \
  test/integration/network_api/test_tcp_transfer_resume.py::test_client_does_not_fallback_on_wsaeacces_without_fixed_port \
  test/integration/network_api/test_tcp_transfer_resume.py::test_client_connects_when_its_configured_port_is_taken \
  test/integration/network_api/test_tcp_transfer_resume.py::test_forwarded_file_resumes_through_the_server \
  test/integration/network_api/test_tcp_file_transfer.py::test_sender_falls_back_when_fixed_source_port_returns_wsaeacces \
  test/integration/network_api/test_tcp_file_transfer.py::test_receiver_listener_falls_back_when_fixed_port_returns_wsaeacces -q
22 passed in 14.34s
```

2. 最终受影响套件：

```text
uv run pytest test/unit/network_api/test_TCP_client.py \
  test/integration/network_api/test_tcp_transfer_resume.py \
  test/integration/network_api/test_tcp_file_transfer.py -q
97 passed in 190.14s
```

3. 新增 `None`/`0` 端口边界与 errno 单测：3 passed in 0.35s。

### 明确未覆盖

- Linux loopback 用例注入了带 `winerror=10013` 的 `OSError`，并真实连接本机 TCP listener/收发文件；这验证应用分支与 socket 生命周期，不证明 Windows 内核的 Hyper-V/WinNAT 真实 WSAEACCES 行为。
- Windows runner / 实际 Hyper-V/WinNAT 端口排除范围未测试；提交或合并前仍需 Windows CI 运行时证据。
- 通用 `create_temporary_client()` / `create_temporary_server()` 与服务端主 listener 未修改，未声称覆盖。
- `uv run ruff check` 的一次尝试对 5 个文件给出来自 56 个文件的诊断，输出范围与参数不符，不能作为 lint 结果；已向 `xd://report_issue` 报告该工具行为。未据此声称 lint 通过。

### 总验收对照

| 路径 | 结果 | 证据 |
|---|---|---|
| 控制连接固定端口 bind 10013 | 覆盖 | 定向集成用例成功连接并检查实际源端口变化 |
| 控制连接固定端口 connect 10013 | 覆盖 | 定向集成用例成功连接并由 server 接收端确认客户端 |
| 控制连接 None/0 | 覆盖 | 两种配置分别注入 10013；无第二次 connect；不绑定或只绑定 `0` |
| errno.EACCES 平台边界 | 覆盖 | Linux 原生判断为 false；模拟 Windows 时为 true；None/0 不触发 fallback |
| 服务端/客户端 sender bind/connect | 覆盖 | 四种组合真实传输文件并逐字节比较 |
| 服务端/客户端 receive listener bind | 覆盖 | 两个方向验证 fallback 端口公告与真实文件内容 |
| forward relay listener bind 与 palloc cleanup | 覆盖 | 真实续传完成；观察到 bind `0` 和原申请端口释放 |
| 真实 Windows WSAEACCES / Hyper-V/WinNAT | 未覆盖 | 当前验收环境无法提供 Windows runner 证据 |
| 通用 side-channel 与主 server listener | 未覆盖（非目标） | 本次未修改 |
