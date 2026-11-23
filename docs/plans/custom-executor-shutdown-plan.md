# 自定义命令线程池关闭 — 修复计划

状态：**需求方已选择 Server.stop() + Client.close() 对称修复范围，并选择外部关闭无界等待已接纳任务退出；尚未授权实现。** A4 验收发现 Server 执行并完成一次落盘扩展后，`stop()` 返回时仍有一个空闲的 `ThreadPoolExecutor` worker。这不是转发数据线程或在途任务泄漏；同样的空闲 worker 在 A1 前的 `ddb71c9` 上也可复现。

## 1. 目标与不变量

- Server 完成 `stop()`、Client 完成终态 `close()` 后，不再接受新的自定义任务；`submit_task` 等待名额的调用者也能及时醒来并被拒绝。
- 外部关闭先关闭 listener/control sockets 以解除网络任务，然后无界等待已接纳 handler 结束并关闭实例 executor；Client auto-reconnect 不关闭 executor。
- `max_custom_workers` 的每实例上限不变；成功、异常、取消和 submit 失败均精确归还任务名额。
- `stop()` / `close()` 幂等，终态实例不支持重启。保持已有构造器、`submit_task(...) -> Future`、handler 注册、wire/进度格式不变。
- Admission/lifecycle 锁不可跨用户 handler、网络 I/O 或 executor/Future 等待。
- Pool worker 自身调用 stop/close 时不得 self-join；完成资源退休并发起非等待 shutdown，让 handler 返回后 worker 自行退出。
- 任意 Python handler 无法安全强杀。若它不因 socket 关闭而退出，无界等待会使外部 stop/close 一直等待；明确记录此限制。

## 2. 数据流与并发决策

Server inbound/console custom command 或 Client inbound server custom command → per-instance bounded admission → `_custom_executor.submit` → handler → Future 完成并归还名额。Client socket 断线重连不是实例关闭，不触碰 executor。

并发关闭需线性化“已接受 Future”和“STOPPING”：stop/close 取得 admission 锁并转入 STOPPING 后，新提交拒绝；正在容量门处等待的提交者被唤醒并拒绝；在 OPEN 状态内成功提交的 Future 属于 teardown 必须处理的工作。维持最多 `max_custom_workers` 个已接受任务；不得让提交者永远卡在 `Semaphore.acquire()`。

推荐一个实例级 Condition + `in_flight < max_custom_workers` 取代独立 Semaphore 阻塞与 executor.submit 之间的竞态窗口。持锁期间只检查状态、计数并提交 Future；submit 异常则原子归还名额；done callback 唯一归还并 notify。锁不跨 handler、I/O 或等待。

外部终态关闭顺序：

1. OPEN → STOPPING，拒绝新任务并唤醒 admission waiter。
2. 停止 listener/接收循环，关闭控制 socket 并退休 transfer owner，先解除网络 handler 的真实 I/O 阻塞。
3. 持久化中断进度与已有状态。
4. `executor.shutdown(cancel_futures=True, wait=True)`：取消未运行工作，等待已运行 Future 完成。
5. flush 受 handler 影响的记录并标记 STOPPED；重复关闭幂等。

若终态关闭由 pool worker 自身调用，第 4 步使用 `wait=False`，不能 self-join；执行资源退休后返回，当前 handler 返回时 worker 再退出。状态锁不得覆盖第 2–5 步的 I/O/等待。并发进入者不能重复释放端口/存储，也不能在首个 stop 尚未结束时误报 pool drained。
## 3. 已确认范围与停止契约

需求方已选择 Server `stop()` + Client 终态 `close()` 对称修复，以及**外部关闭无界同步等待**。Client 的 `_release_client_socket()` 是 auto-reconnect 的临时会话更替，不关闭 executor。stop/close 关闭实例资源，不会关闭宿主 Python 进程。

- socket-first 使常见的网络型 handler 收到 EOF/OSError 并正常退出；若自定义 handler 阻塞于 Event、外设或无限计算，则无界 drain 会令外部 stop/close 等待。Python 不能安全强杀运行线程。
- handler 在自己的 pool worker 中调用 stop/close 时执行 socket 退休和非等待 shutdown，不 join 当前 worker；该 handler 返回后 worker 退出。这种自停情形不能承诺 stop 返回时整个池已 drained。
- 新 submit 在 OPEN→STOPPING 后抛 RuntimeError；终态实例不支持重启。Client reconnect 期间池保留。

## 4. 文件与原子拆分建议

预期文件（实现批准后定稿）：

| 类型 | 路径 | 意图 |
|---|---|---|
| 修改生产代码 | `PyFlow/network_api/connect_tcp.py` | 两种实例的 admission/Future 名额归还与 shutdown；只在 Server.stop 和 Client.close 终态关闭。 |
| 修改单测 | `test/unit/network_api/test_TCP_server.py`、`test/unit/network_api/test_TCP_client.py` | 关闭后 submit 拒绝、容量等待者唤醒、异常/取消归还名额、幂等和 worker 自 stop。 |
| 新增集成测试 | `test/integration/network_api/test_executor_lifecycle.py` | 真实控制 socket 上阻塞的 threaded handler，在 Server.stop/Client.close 后退出；确认 Future 完成、workers/owner/socket 清理，Client reconnect 保留池。 |
| 修改文档/变更记录 | `docs/source/Network_APIs/TCP_Server_APIs.rst`、`TCP_Client_APIs.rst`、新增 `docs/source/changes/server-executor-shutdown.md`、本计划/报告 | 生命周期语义和无法安全终止任意 Python handler 的边界。 |

原子阶段建议（实施获批后再开始）：
- E1：双端 admission/Future slot 和终态状态机；先以单测证明 stop-submit 竞态、容量 waiter 退出、异常/取消的唯一名额归还和自停不 self-join。
- E2：真实 TCP socket-first close/drain、client reconnect 对照，补文档和实测报告；不改转发生产逻辑。

## 5. 验收命令草案

- 单测：`.venv/bin/python -m pytest -q test/unit/network_api/test_TCP_server.py test/unit/network_api/test_TCP_client.py <新增 executor lifecycle 节点>`。
- 集成：`.venv/bin/python -m pytest -q test/integration/network_api/test_executor_lifecycle.py`；场景必须通过原 API 注册 `run_in_thread` handler，真实阻塞/关闭 socket；不 fake executor、Future 完成或 socket 错误。
- 回归：A4 V4 的 `test_forward_extension.py` 真实上传/目标 ACK；Threaded/async Server，Server stop-before-start，Client auto-reconnect/终态 close。
- 可机械核对：stop/close 后不得新增任务；等待 admission 的调用返回拒绝；已运行网络任务从真实 socket 关闭中退出；外部 stop/close 返回后相关 executor workers、Future、task slots、控制/临时 socket 及手动端口归属归零。池内自停检查无 self-join，handler 返回后最终 worker 退出。
- 不删/跳过测试，不加 `xfail`/`|| true`，不调低 timeout/断言或改 CI/依赖/coverage；fixture 可以关闭 executor 做最终兜底，但不得用 fixture 清理冒充生产 stop/close。

## 6. 风险 / 非目标

- 已选择无界同步 drain：若插件 handler 永久卡在非 socket 等待，stop/close 无法返回。换成有限等待也不能终止该线程，只能返回时仍有 worker；需另行选择子进程隔离/协作取消机制，超出本次修复范围。
- 池内 worker 自 stop/close 特殊路径避免 self-join，但返回时当前 handler 未结束；仅调用者在池外时保证同步等待池全部退出。
- 不改文件/消息协议、转发预算、transfer owner、process-level exit hooks、插件实现或线程类型；不新增 timeout 参数、不声称可以强杀用户线程。
- Python executor 的进程退出清理不是实例 `stop()`/`close()` 的替代品；验收分别证明实例终态同步回收和解释器自然退出。
