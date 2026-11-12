# 轻量增量报告：固定 client_port 的 connect 阶段端口冲突降级

状态：已实施，待人工审查；未创建 git 提交、未合并、未推送。

## 1. 提交意图

配置了固定 `client_port` 的 `TCP_Client_Base.connect()` 在 `bind()` 成功、`connect()` 因本端端口冲突失败时（Windows `WSAEADDRINUSE` 10048），改为用新 socket 从 OS 分配端口重试一次，而不是直接返回 False。

## 2. 改动内容

- `PyFlow/network_api/connect_tcp.py`
  - 新增模块级 `_is_local_port_conflict(exc)`：识别 `errno.EADDRINUSE` 与 Windows `winerror == 10048`。与既有 `_is_closed_socket_error` 同层、同风格。
  - `TCP_Client_Base.connect()`：`bind` + `connect` 收进一个固定端口重试循环。任一步失败且命中端口冲突时，关闭当前 socket、记录日志、丢弃 `local_address`，下一轮从 OS 分配端口重连；非端口冲突或已无固定端口时原样抛出。

## 3. 为什么这样改

对应失败用例 `test_reconnected_handshake_rejects_reply_from_old_control_socket` 在 Windows CI 的 `assert client.connect()`：

```
client_port 固定 -> SO_REUSEADDR 允许 bind 已被 TIME_WAIT 占用的端口
                 -> connect 才报 WinError 10048
                 -> 原实现只降级 bind 失败（8803-8814），connect 失败落入通用 except -> return False
```

日志中没有 "client port ... is not available" 一行，证明 `bind` 已通过，缺陷在 `connect` 这一侧。POSIX 上同一冲突由 `bind` 报出，原逻辑已覆盖，所以这是 Windows 独有的缺口。

## 4. 数据流

```text
connect()
  local_address = (client_host, client_port)   # client_port 为 None 时跳过 bind
  loop:
    new socket + SO_REUSEADDR + timeout
    bind(local_address)?          -> OSError 且 _is_local_port_conflict -> close, local_address=None, 重试
    connect((host, port))         -> OSError 且 _is_local_port_conflict -> close, local_address=None, 重试
                                  -> 其它 OSError -> 抛出，沿用外层 timeout/refused/通用分支
    break
  之后: _stopped 检查 -> running=True -> 控制 socket 登记 -> receive 线程 -> 模式协商
```

固定端口可用时行为完全不变（首轮 bind 成功、connect 成功，循环只跑一次）。

## 5. 局部验证

1. **新增分支冒烟**（临时脚本，已删除）：真实 server + 真实 client，`client_port` 固定；monkeypatch `socket.socket.connect` 使**首次** connect 抛 `OSError(EADDRINUSE)`。

```
connect(): True
attempts: [('127.0.0.1', 50970), ('0.0.0.0', 0)]
final local: ('127.0.0.1', 39008)
fallback used: True
bound fixed port on 1st attempt: True
```

第一次尝试绑定了固定端口 50970 并在 connect 处失败；第二次不 bind、由内核分配 39008 后连接成功。

2. **既有回归**：
   - `test/integration/network_api/test_tcp_transfer_resume.py -k "configured_port_is_taken or upload_resumes_after_a_reconnect or download_resumes_after_connect"` → 3 passed。
   - `test/unit/network_api/test_TCP_client.py` + 整个 `test_tcp_transfer_resume.py` → 50 passed in 107s。
   - 修复前同两套件在本机 3.14 也已通过（该缺陷只在 Windows 触发），故本轮新增的唯一证据是上面的 1。

## 6. 明确未覆盖

- **Windows 真机未验证**：本机是 Linux，10048 由 monkeypatch 模拟，不是真实内核行为。合入前建议在 Windows CI 上跑 `test_reconnected_handshake_*` 确认。
- 只降级一次。若 OS 分配端口后仍冲突（极不可能），原错误照抛。
- 未改动 `_release_client_socket()`。它依赖 "socket 尚未 shutdown" 才能靠 `SO_LINGER(1,0)` 触发 RST；测试用 `shutdown(SHUT_RDWR)` 前置，那条路径仍会留下 TIME_WAIT，本修复让客户端能绕过去，但没有消除根因。
- 从 OS 分配端口后，本端地址变化，按地址匹配的断点续传不会命中旧记录（与既有 bind 失败降级语义一致，已有注释说明）。

## 7. 风险与待确认

- 风险：低。单函数、单路径、~40 行，固定端口可用时零行为变化。
- 待确认：是否接受 "只降级一次、不重试固定端口"。另一种做法是固定端口冲突时先短退避再重试固定端口，但那会把重连延迟拉长到 TIME_WAIT 量级（Windows 约 2 分钟），不合适。
