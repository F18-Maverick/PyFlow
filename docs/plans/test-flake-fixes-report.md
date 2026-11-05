# 测试端修复报告：Windows socketpair 写失败假设 + 加密用例的瞬时态采样

状态：已实施，待人工审查；未创建 git 提交、未合并、未推送。产品代码本轮未改动（ACK 机制重构另行讨论）。

## 1. 提交意图

把三组只在特定平台/时序下失败的测试，改为断言**单调可观测的状态**，使其在任何调度下都给出确定结论，而不是靠轮询撞窗口。

## 2. 改动内容

### 2.1 `test/unit/network_api/test_fid_routes.py`

`test_pending_resume_survives_failed_send_and_prunes_completed_or_missing_routes`：`peer.close()` 之后补 `broken.shutdown(socket.SHUT_WR)`。

原因：测试需要"发送一定失败"来验证 `_flush_pending_originator_resumes` 保留待发项。POSIX 上对已关闭对端的写入必 EPIPE，Windows 上 `socket.socketpair()` 是 TCP loopback 模拟，第一次 `sendall` 会进本地缓冲并**成功**，`WSAECONNRESET` 要到下一次写才出现。于是 `sent` 变成 1、待发项被按设计移除，`== 0` 失败。

`shutdown(SHUT_WR)` 让"本端已不可写"成为 socket 自身的属性。本机实测两种写法（Linux）：

```
write after peer close:      OSError 32 [Errno 32] Broken pipe
write after shutdown(SHUT_WR): OSError 32 [Errno 32] Broken pipe
```

产品侧语义未变：`_flush_pending_originator_resumes` 的 docstring 就是"成功发送才移除"，返回 1 本身是自洽行为。

### 2.2 `test/integration/network_api/test_crypto_tcp.py`

新增两个读取辅助：

- `_server_streak(server, sock)`：读 `_crypto_state[addr]["decode_failures"]`。
- `_server_framed(server, sock)`：读 `channel.secure`。

关键事实（探针实测）：`decode_failures` 跨重握手**累加**（0→1→2→3），因为 `_crypto_is_protocol_line` 让 `/crypto*` 协议行不触发 `_crypto_note_healthy` 清零。而 `channel.secure` / `_encrypted_sockets` 是瞬时态，50 ms 轮询可能整个错过。

三处改写：

1. `_wait_server_reexchanged`：原来是 `wait_until(sock not in _encrypted_sockets)` 再 `wait_until(sock in _encrypted_sockets)`——两段都在采样瞬时态。改为先等 `decode_failures` 非零（单调），再等 `secure` 恢复（稳定态）。
2. `test_decode_failure_burst_closes_connection`：注入前仍等 framed（注入在重握手窗口内会被当 stale 丢弃，这是协议设计），注入改为等 `streak >= 预期值 or not client.running`。原断言 `not server_framed()` 在重握手快速完成时永远采样不到。
3. `test_stale_frame_coalesced_behind_a_plaintext_line_is_consumed`：把 `_crypto_try_server_flip` 的 no-op 替换改成**闸门**（`threading.Event`）。原写法有两个隐患：重握手在替换生效前就完成（注入的帧会被真正解密并二次计数）；以及替换期间若 flip 被调用则被永久吞掉，收尾等 framed 会挂 30 秒。闸门版本在 `finally` 里恢复并 `set()`，两种时序都收敛。

## 3. 数据流（以 burst 用例为例）

```text
wait_until(server framed)            # 帧只会在 framed 态被计数
sendall(garbage frame)
  -> server receive_message -> channel.feed -> _consume_frame -> _decode_rsa
  -> _FrameError("malformed RSA frame") -> _crypto_channel_error -> _crypto_on_decode_failure
  -> channel.disable() + state["decode_failures"] += 1
  -> /crypto_key_exchange 1 -> 对端 ack -> /crypto_ready -> /crypto_frames_start -> enable()
wait_until(streak >= n or not client.running)   # 计数是单调量，不会被错过
n == MAX_DECODE_FAILURES -> do_close -> 连接关闭 -> _wait_disconnected
```

## 4. 局部验证

- `test_crypto_tcp.py` 全量 **22 passed**，连跑 3 轮（83s / 93s / 92s）全绿。
- 目标三个用例连跑 5 轮全绿；另 5 轮（闸门版本）全绿。
- `test_fid_routes.py` + `test_crypto_channel.py` → **38 passed**。
- ruff：改动前后同为 42 条（全为该文件基线既有问题），新增的两个 helper 已避免引入新的 D401。

## 5. 明确未覆盖

- **Windows 未实机验证**。`test_fid_routes` 的修复按 Windows 语义推理（`shutdown(SHUT_WR)` 后 send 必失败，WSAESHUTDOWN 10058），但本机是 Linux；需在 Windows CI 确认。加密三例同理——它们在 Windows 上没失败过，本轮改的是"防错过窗口"，不是针对 Windows 特有行为。
- 未处理 `test_crypto_tcp.py` 里其余瞬时态断言（如 `test_second_connection_passes_tofu` 的 `not in server._encrypted_sockets`）。那条在 `client.close()` 之后，触发是同步的，不在本次失败清单内，故未动。
- 若 `decode_failures` 在某次重握手中途被清零（目前只在"真实对端业务行"到达时发生），burst 用例会提前 30 秒超时而不是静默通过——这是有意的失败方向。

## 6. 风险与待确认

- 风险：低。改动只落在测试文件，未触碰产品行为。
- 需要确认：`_server_streak` 直接读 `_crypto_state` 私有结构。若后续重构该字段名，这两个用例会 `AttributeError` 而不是悄悄失效——比现在的瞬时采样更容易发现，但仍是耦合内部实现的测试写法。是否接受这种耦合，请你定。

## 7. 与协议端待办的关系

终态 ACK 交付缺陷（Linux 3.10 的 4 个 resume 失败）**未在此提交中改动**，按你的计划随 ACK 发送机制重构一起处理。需求卡仍在 `docs/plans/final-ack-delivery-requirement.md`，届时可据此更新路径清单。
