# 自定义命令线程池关闭 — E2 验收报告

状态：**E2 部分验收；E1 的真实 socket-first drain 和非终态 Client reconnect 路径通过；终态 Client close 与自动重连并发时观察到新连接尝试，E1 的“终态实例不重启”不变量尚未通过。** 本轮没有修改生产代码。

## 验收范围

基线为已审计的 E1 提交：`cf80114`（executor lifecycle）和 `0514115`（重复关闭仍 flush）。用户要求本轮只验收 E1 的实现和边界。运行前将未审工作区改动保存到 stash：`E2-review: unapproved client retry and port reservation draft`；本轮未恢复或使用其中的产品改动。

E2 集成测试只覆盖已批准的真实网络 seam：通过 `register_command(..., run_in_thread=True)` 注册 handler，在真实控制 socket 上等待对端关闭；接收端同时持有由真实 transfer-listener 握手创建、阻塞于 `accept()` 的 listener。没有替换 executor、Future、socket 或 socket 错误。

## 结果

| 执行路径 | 证据 | 结果 |
|---|---|---|
| Threaded Server 外部 `stop()` | 真实控制 socket 读被 shutdown 唤醒；真实 transfer listener 关闭；外部调用等待 handler 返回；Future 完成、worker 退出 | 通过 |
| Asyncio Server 外部 `stop()` | 同一真实 socket/handler/transfer-listener 契约，并确认 accept-loop 线程退出 | 通过 |
| Client 外部 `close()` | 真实控制 socket 读被 shutdown 唤醒；外部调用等待 handler 返回；receive thread、Future、worker、control socket 和 transfer listener 清理 | 通过 |
| Client 非终态 auto-reconnect | 停止第一 Server 后连接第二 Server；旧 socket 关闭；重连前后实际 handler 使用同一 worker；executor 仍可提交任务 | 通过 |
| Client 终态 `close()` 与 auto-reconnect 并发 | 在断线后、重连 runner 已进入 `connect()` 时调用 `close()`；随后本地监听器仍接受到一条控制连接 | **失败：终态 close 后发生了新连接尝试** |

## 实际运行

E2 通过路径：

```bash
PYTHONPATH="$PWD/test/integration${PYTHONPATH:+:$PYTHONPATH}" \
  .venv/bin/python -m pytest -q \
  test/integration/network_api/test_executor_lifecycle.py --tb=short
```

结果：`4 passed in 2.28s`。四个实例对应 Threaded Server、Asyncio Server、Client close 和 Client reconnect 保池。

E1 endpoint 单测及重复关闭落盘回归：

```bash
.venv/bin/python -m pytest -q \
  test/unit/network_api/test_TCP_server.py \
  test/unit/network_api/test_TCP_client.py \
  test/unit/network_api/test_delete_config.py::test_active_transfers_relays_and_pending_logs_prevent_deletion
```

结果：`46 passed in 3.90s`。其中 Server/Client endpoint 两个文件 44 项；重复关闭后 flush 的 Server/Client 参数化回归 2 项。

专项检查：E2 integration test 的 Ruff 选择集通过；相关 Python 文件 `py_compile` 通过。`sphinx-build -b dummy` 完成，输出两条警告：缺失 `_static` 目录，以及未修改文件 `PyFlow/command_control_extension_tcp.py` 第 23 行的 RST 格式诊断；本次修改的 RST 没有构建错误。

终态 reconnect 边界在隔离的 stashed draft 上单独验证：

```bash
PYTHONPATH="$PWD/test/integration${PYTHONPATH:+:$PYTHONPATH}" \
  .venv/bin/python -m pytest -q \
  /tmp/pyflow-verify/test_executor_lifecycle.py::test_terminal_client_close_stops_auto_reconnect \
  --tb=short
```

结果：`1 failed in 2.09s`；失败断言为 `connected after terminal close`。这不是重新引入的产品改动，也没有把失败断言加入通过集；是用于记录终态关闭边界的真实 TCP 证据。该结论说明 E1 的 terminal-state 状态机关闭了 executor，但在自动重连 runner 已进入 `connect()` 的竞态中，尚未保证不会再建立一次控制连接。

## 入口 → 分支 → 出口

真实 Server/Client 接收路径分发已注册的 threaded handler → per-instance admission 返回真实 Future → handler 从同一控制 socket 的 dup 读取 → 外部 `stop()`/`close()` 先关闭实际控制 socket 与 session 所属 transfer listener → handler 观察 EOF/平台闭 socket 错误 → 测试释放 handler 尾段 → 外部 close 返回，Future、worker 和临时 owner 均已退出。reconnect 对照复用同一 Client executor worker；它不是终态关闭。

## 已知边界与未覆盖项

- **终态 reconnect 仍是失败验收项。** E1 计划写有终态实例不支持重启；本轮不修复。该缺口与先前列出的 Client 关闭/连接竞态有关，另立计划，不纳入本轮实现。
- 另一个隔离探针在 `start_TCP_Server()` 的 stopped-check 与 `running=True` 之间注入 `stop()`：listener 已关闭，但 E1 幂等退休使重复 `stop()` 不再重设 `running`，观察到 `running=True`，`delete_config()` 被拒绝。这是另一个 start/stop 并发问题；本 E2 主测试没有覆盖它，且本轮不修复。
- 25 次自然 `connect()`/`close()` 竞争中有 3 次 `connect()` 在 `close()` 返回约 10 秒后才返回；没有观察到最终 `running` 或线程存活异常。10 秒是否由 crypto-mode wait 造成尚未验证。
- E2 socket 路径使用明文 IPv4 loopback；本轮未实测加密控制通道、原生 Windows/macOS、手动端口范围的跨进程竞争或所有 transfer 类型。
- 未测试任意不响应 socket 关闭、永久阻塞于外设/Event/CPU 的 handler；按 E1 契约，池外 stop/close 会无限等待，Python 不会强杀它。
- 本轮没有重跑 A4 全套转发 V4；未修改转发代码，也不把它报告为通过。
- `docs/source/File_Transfer/File_Transfer.rst` 仍描述 semaphore；E2 计划的 TCP Client/Server API 文档之外发现这处旧说明，本轮未扩大文档路径到该文件。

## 结论

E1 的 socket-first drain、外部同步等待、自停不 self-join、Future slot 回收和 reconnect 保池路径都有本机真实 TCP/单测证据。**E1 不能据此标记为全契约通过：终态自动重连竞态失败，且独立的 Server start/stop 并发探针暴露 stale `running`。** 两项均保持为发现，不在本轮改代码。