# connect_tcp 监听器生命周期与锁范围修复计划（最终版）

状态：待人类批准；本文只定计划，不写代码。

## 1. 需求卡（Agent 复述）

### 目标

1. **P2-1**：`TCP_Client_Base.close()` 内 join 接收线程；`close()` 返回后本实例不再投递新监听器事件；`close()/stop()` 被调用时停止所有监听器投递（置 stopped、清空消息/发送/文件监听器列表）。
2. **P1-1**：`broadcast()` 不在 `client_lock` 内同步调用发送监听器，消除监听器重入 `broadcast()/stop()` 的确定性死锁。
3. **P2-4**：更新监听器契约文档：文本行 / 原始 `bytes` / 发送 / 文件事件；线程、并发、close/stop、ID 语义。
4. **P2-2（仅文档）**：明确发送监听器回调顺序不保证等于线序。该问题只影响监听器的观察顺序，不影响 socket 写线顺序、接收顺序、文件数据完整性或任何协议功能，因此本轮不改代码。
5. **P1-2（最后执行）**：Web 端两个消息监听器显式忽略/处理侧信道 `bytes`，消除每块一次 `TypeError` 和日志放大。

### 非目标

- 不实现发送事件 notifier，不改变发送监听器的执行线程/同步语义。
- 不修改 `_SecureChannel.send` 的发送锁范围，不修改加密/传输协议。
- 不改变 P2-2 的任何运行时行为，只在文档中声明不保证发送回调线序。
- 不改变 P2-3、P2-5、P3-1 的现有行为（均已撤回）。
- 不修改公共方法签名、依赖、CI、分支保护。
- 不让 `TCP_Server_Base.stop()` join 每客户端线程；本轮只停止新监听器投递。
- 不改变 `is_send_raw=True` 与 `publish_pub` 等内部协议行不通知监听器的设计。

### 受影响范围（文件）

修改：

- `PyFlow/network_api/connect_tcp.py`
  - `TCP_Server_Base`：`__init__`、`stop`、`broadcast`、`_notify_*`
  - `TCP_Client_Base`：`__init__`、`close`、`_notify_*`
  - `add_message_listener` / `add_send_message_listener` / `add_file_listener` docstring
- `docs/source/Network_APIs/TCP_Server_APIs.rst`
- `docs/source/Network_APIs/TCP_Client_APIs.rst`
- `PyFlow/transfer_web/web_backend/server_backend.py`（仅最后提交）
- `PyFlow/transfer_web/web_front/client_backend.py`（仅最后提交）

新增/扩充测试：

- 新增 `test/integration/network_api/test_listener_lifecycle.py`
- 扩充 `test/integration/network_api/test_tcp_broadcast_alloc.py`
- 扩充 `test/unit/transfer_web/` 下的 Web 监听器类型测试（最后提交）

### 执行路径清单（本轮必须覆盖）

关闭/停止：

1. `close()` 时接收线程正阻塞在 `recv`：shutdown 唤醒，join 成功，返回后无新事件。
2. `close()` 时接收线程已 `recv` 到一块数据但尚未通知：join 等待本轮处理结束；返回后不得出现该块的 `bytes` 或 `str` 事件。
3. `close()` 从消息监听器内调用（接收线程自身）：跳过 self-join，不得抛 `RuntimeError`；当前回调返回后接收循环退出。
4. `close()` 未连接 / 接收线程为 `None`：幂等返回。
5. `close()` 连续调用两次：幂等。
6. `is_auto_reconnect` 的 `_release_client_socket()` 路径：不清空监听器、不 join；重连后监听器仍有效。
7. `close()` 时发送监听器已在其他线程执行：停止标志阻止新投递；当前回调允许结束。
8. `TCP_Server_Base.stop()`：停止新监听器投递并清空列表；不 join 客户端线程。
9. `stop()` 从发送监听器内调用：不得因 `client_lock` 重入死锁。

广播：

10. `broadcast()` 正常发送全部客户端。
11. `broadcast(exclude_client=...)` 排除指定客户端。
12. `broadcast()` 发送失败：关闭并删除对应客户端。
13. 发送监听器内再次调用 `broadcast()`：不得死锁；监听器自身需防递归。
14. 广播快照期间客户端被并发移除 / 同地址新客户端加入：删除时按 socket 身份校验，不误删新客户端。
15. `is_asynic_clients_io=True` 时广播行为不变。

文档：

16. 消息监听器：控制行 `str` 与侧信道 `bytes` 的区别、线程、并发。
17. 发送监听器：只覆盖非 raw `send_message`；内部协议行不投递；**回调顺序不保证等于线序**；该顺序不影响数据正确性。
18. 文件监听器：完整文件落盘后触发；早于完成 sign；逐块事件由消息监听器投递；不等待 ACK。
19. ID 语义：按实际执行 I/O 的 socket 对端记录；文件转发/传输时是临时 TCP 的 `ip:port`，不是控制连接 ID。
20. close/stop：停止新投递、清空监听器、自动重连不受影响。

Web：

21. 服务端 `_on_incoming_message` 收到 `bytes`：不再抛 `TypeError`，不产生聊天事件。
22. 客户端 `_on_incoming_message` 收到 `bytes`：同上。
23. 真实文件传输时不再每块产生一条监听器错误日志。

### 安全与数据边界

- 监听器仍然看到解密后的明文；本轮不改变可见数据范围。
- 发送监听器仍在原发送线程同步执行；本轮不改变回调线程，也不改变同步语义。
- 不在 `_event_listeners_lock` 内调用用户回调。
- 不在 `client_lock` 内调用 `send_message` 或通知监听器。
- `close()/stop()` 后不再发起新投递；已进入回调的线程允许自然结束。
- 内部协议行（`is_send_raw`、`publish_pub`）仍不进入发送监听器。
- 侧信道事件 ID 继续按实际 socket 对端记录，不改成控制连接 ID。
- P2-2 只是观察顺序问题：TCP 写线由发送锁串行化，接收顺序由接收线程串行化，文件数据块不会因监听器回调顺序不同而乱序或损坏。

### 验收证据（运行时 / 机械证据）

1. **关闭生命周期**：
   - `close()` 返回后 `receive_thread.is_alive()` 为 False；
   - `_listeners_stopped` 为 True，三个监听器列表为空；
   - 关闭后直接调用四个 `_notify_*`，回调计数不增加；
   - 强制在 `recv` 与 `_notify_transfer_chunk` 之间暂停接收线程，`close()` 必须等待；释放后返回，且该块不再产生 `bytes` 或 `str` 事件。
2. **广播**：
   - 发送监听器内调用 `broadcast()`，`broadcast("trigger")` 在超时前返回；
   - 现有死客户端删除测试继续通过。
3. **Web 类型守卫**：
   - 直接以 `bytes` 调用两个 `_on_incoming_message` 不抛异常、不产生事件；
   - 真实文件传输期间无监听器 `TypeError` 日志。
4. **回归**：
   - `test_message_listeners.py`
   - `test_side_channel_records.py`
   - `test_send_message_store.py`
   - `test_tcp_broadcast_alloc.py`
   - `test_tcp_async_server.py`
   - 加密/文件传输相关测试
5. **文档**：`make -C docs html` 成功。

### 禁止事项

- 不得删除、跳过或弱化测试；不得加 `|| true`；不得降低 coverage。
- 不得改 CI、依赖、分支保护。
- 不得改公共方法签名。
- 不得在 `_event_listeners_lock` 内调用用户回调。
- 不得在 `client_lock` 内调用 `send_message` 或通知监听器。
- 不得把 P2-2 的运行时行为修改混入本任务；只允许文档声明。
- 不得修改加密/传输协议逻辑。
- 不得在 `is_auto_reconnect` 的 `_release_client_socket()` 路径清空监听器或 join。
- `close()` 的 join 不设静默超时；用 self-join guard 处理在接收线程内调用 `close()` 的情况。
- Web 修复必须最后执行，不得混入前面的提交。

### 风险等级

中。高风险片段：`close()` 的 shutdown/join 顺序、监听器停止标志、`broadcast()` 的锁边界与失败客户端删除。

## 2. 数据流

### 2.1 close

```text
调用方
  -> running = False
  -> _stopped = True
  -> _stop_listeners(): 置 stopped，清空 message/send/file 列表
  -> client_socket.shutdown(SHUT_RDWR)  # 唤醒 recv
  -> join(receive_thread)                # 非当前线程才 join
  -> free_port / flush stores / persist progress
  -> 丢弃 channel、encrypted、control socket 标记
  -> client_socket.close()
  -> 返回
```

返回后：

- 接收线程已退出，不再产生消息 / 原始块 / 文件事件。
- 新发送不会触发发送监听器。
- 其他线程中已经进入的发送回调可以继续执行到返回。

### 2.2 stop

```text
调用方
  -> running = False
  -> _stopped = True
  -> _stop_listeners()
  -> free_port / flush / persist
  -> close 所有 client sockets、server socket
  -> 唤醒 async accept loop
  -> 返回
```

不 join 客户端线程；只保证不再投递新事件。

### 2.3 broadcast

```text
broadcast
  -> with client_lock: 快照 [(addr, socket), ...]
  -> 释放 client_lock
  -> 逐目标 send_message()   # 监听器在此同步触发，但不在 client_lock 内
  -> 收集失败 (addr, socket)
  -> with client_lock:
       若 clients[addr]["socket"] is socket: close + del
  -> 返回
```

## 3. 原子提交拆分

### 提交 1：broadcast 不在 client_lock 内通知发送监听器（P1-1）

- 意图：消除发送监听器重入 `broadcast()/stop()` 的确定性死锁。
- 文件：`connect_tcp.py`，`test_tcp_broadcast_alloc.py`。
- 变更：
  - `client_lock` 下只做 `(addr, socket)` 快照；
  - 释放锁后逐个 `send_message`；
  - 失败目标重新加锁，按 socket 身份校验后关闭/删除；
  - 保持 `exclude_client` 与“失败即断开删除”的现有语义。
- 预估：代码约 20 行，测试约 50 行。
- 验证：新增重入测试；现有 broadcast / async server 测试。

### 提交 2：close/stop 停止监听器投递，close join 接收线程（P2-1）

- 意图：兑现“close 被调用时停止所有监听器”，消除关闭窗口内的在途事件和 `bytes`/`str` 重复。
- 文件：`connect_tcp.py`，新增 `test_listener_lifecycle.py`。
- 变更：
  - 两个类新增 `_listeners_stopped` 与 `_stop_listeners()`；
  - 四个 `_notify_*` 在取快照前检查 stopped；
  - `TCP_Client_Base.close()` 按 2.1 顺序 shutdown + join；
  - `TCP_Server_Base.stop()` 在关闭客户端前调用 `_stop_listeners()`；
  - `_release_client_socket()` 不调用 `_stop_listeners()`。
- 预估：代码约 50 行，测试约 120 行。
- 验证：关闭生命周期测试；现有监听器/广播测试。

### 提交 3：监听器契约文档修正（P2-4 + P2-2 声明）

- 意图：文档与实现一致；明确发送回调不保证线序且不影响功能。
- 文件：两份 RST + `connect_tcp.py` 三个 `add_*` docstring。
- 内容：
  - 消息监听器：控制行 `str` / 侧信道 `bytes`、线程、并发；
  - 发送监听器：只覆盖非 raw `send_message`；内部协议行不投递；**并发发送同一 socket 时，回调顺序不保证等于线序；该顺序仅影响观察顺序，不影响 socket 写线、接收顺序或文件数据完整性**；需要严格顺序的调用方应自行串行化发送；
  - 文件监听器：完整落盘后、完成 sign 前、逐块由消息监听器负责；
  - ID：实际 I/O socket 对端；文件转发/传输是临时 TCP `ip:port`；
  - close/stop：停止新投递、清空列表、自动重连不受影响。
- 预估：文档约 70 行。
- 验证：`make -C docs html`。

### 提交 4：Web 监听器忽略原始 bytes（P1-2，最后执行）

- 意图：消除 Web 端每个侧信道块一次 `TypeError` 和日志放大。
- 文件：`server_backend.py`、`client_backend.py`、Web 单元测试。
- 变更：两个 `_on_incoming_message` 入口判断 `isinstance(text, bytes)`，原始块直接返回（或进入独立处理分支，不生成聊天事件）。
- 预估：代码约 8 行，测试约 30 行。
- 验证：直接 `bytes` 调用不抛异常；真实文件传输无监听器错误日志；现有 Web 测试。

## 4. 路径到提交映射

| 执行路径 | 提交 | 验证 |
|---|---|---|
| close 阻塞在 recv | 2 | `test_close_joins_receive_thread_and_clears_listeners` |
| close 时有在途 recv 块 | 2 | `test_close_does_not_deliver_inflight_chunk_after_return` |
| close 从接收监听器内调用 | 2 | `test_close_from_receive_listener_does_not_self_join` |
| close 未连接 / 重复调用 | 2 | 生命周期测试幂等用例 |
| 自动重连路径 | 2 | 现有 auto-reconnect 测试 + 监听器仍在断言 |
| close 时发送回调已在执行 | 2 | 停止标志测试 + 文档声明 |
| stop 停止新投递 | 2 | `test_stop_clears_listeners_and_blocks_new_delivery` |
| stop 从监听器内调用 | 1 + 2 | 广播重入测试变体 |
| broadcast 正常 / exclude / 死客户端 | 1 | `test_tcp_broadcast_alloc.py` |
| async server broadcast | 1 | `test_tcp_async_server.py` |
| 发送回调线序 | 3 | 文档声明；不写行为测试 |
| Web bytes 类型守卫 | 4 | Web 单元测试 + 文件传输日志检查 |
| 文档契约 | 3 | `make -C docs html` |

## 5. 明确未覆盖

- P2-2 运行时顺序：不修；文档声明。原因：只影响监听器观察顺序，不改变 socket 写线、接收顺序、文件数据完整性或协议功能。
- P2-3、P2-5、P3-1：已撤回，不改行为。
- `TCP_Server_Base.stop()` 不 join 客户端线程；若要求 stop 返回后零回调，需先跟踪并 join 所有客户端线程，属独立任务。
- 已进入的监听器回调无法被 Python 强制终止；close/stop 只保证不再发起新投递。
- 发送监听器内的 `close()/stop()` 仍可能在业务锁上重入；本轮只修复 `broadcast` 这条已知锁路径，其他锁路径不在范围内。

## 6. 合并前检查

- 红旗复查：无删测试、无弱化断言、无 CI/依赖改动、无公共签名改动。
- 规则沉淀：新增关闭生命周期、广播重入回归测试；文档明确发送回调线序边界。
- 文档收尾：两份 RST 与三个 `add_*` docstring 同步更新。
- 人工批准后才开始提交 1；每个提交附提交报告并等待审查。
