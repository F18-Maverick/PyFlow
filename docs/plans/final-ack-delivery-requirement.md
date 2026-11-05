# 终态 ACK 交付可靠性：需求卡（待确认，未实施）

状态：需求卡阶段。未写实现代码，未进入计划与拆分。等人类确认执行路径全集与机制判断后再定拆分。

## 1. 目标

接收端在数据传输腿正常收完文件后，要让发送端**可靠地**观察到终态数据 ACK（`server_received_file_data_sign`），再关闭传输连接；发送端据此把该次传输判定为成功并清理上传进度记录。

## 2. 非目标

- 不改终态 ACK 的报文格式、不去重协议（仍是同一行 sign）。
- 不改错误 sign 的语义与发送时机。
- 不改断点续传的匹配规则、进度记录的五项格式、FID 分配。
- 不改加密算法、鉴权、CI、依赖、覆盖率阈值。
- 不引入"等待对端确认关闭"的新握手（见 §6 备选方案，需单独批准）。

## 3. 受影响的执行路径清单（待确认是否为全集）

终态 ACK 的发送点（接收侧）：

1. `TCP_Server_Base.file_transfer_mode_recv`（约 5505）：服务端接收客户端上传。当前代码：`finished_ok = bool(self.send_message(client_file_socket, server_received_file_data_sign))`，紧接着 `close_socket()`，没有 `shutdown(SHUT_WR)`，也没有 drain。
2. `TCP_Client_Base.file_transfer_mode_recv`（约 11024）：客户端接收服务端下发的文件，同一模式。
3. 转发中继 `writer()` 中的 `_forward_read_sign`（约 6786）：中继在目标腿收 ACK 后聚合回传上游，之后 `sock.close()`。

必须逐条给出结论的路径：

| # | 路径 | 触发场景 | 当前证据 |
|---|---|---|---|
| P1 | 服务端接收·普通文件上传 | `/file` 直传 | 3.10 CI 失败 |
| P2 | 服务端接收·文件夹内文件 | `/file_folder` | 3.10 CI 失败（`[exception-folder]`） |
| P3 | 服务端接收·EOF 重放（`resume_offset == file_size`） | 只发 header、`remaining == 0` | 3.10 CI 失败（第三次尝试） |
| P4 | 客户端接收·服务端下发文件 | 下载方向 | 未直接失败，同一代码点 |
| P5 | 客户端接收·下载的 EOF 重放 | 下载续传 | 未直接失败，同一代码点 |
| P6 | 转发中继·目标腿 ACK 聚合 | `/forward_item` | 未直接失败，同一语义 |
| P7 | 服务端接收·`/crypto_pub_key` 推送 | 公钥文件传输 | 未直接失败，走 P1 同一函数 |
| P8 | 错误路径（收不全、size 不符、对端断开） | — | 不涉及终态 ACK，但改 `close_socket()` 时必须回归 |

## 4. 需求方观察到的行为

Linux 3.10 CI：

- `test_server_ack_failure_keeps_received_file_until_eof_confirmation[false-file]`、`[exception-folder]`：第三次 `file_transfer_mode` 期望 `(606, 65573, True)`，实际 `False`。
- `test_server_final_error_keeps_upload_until_successful_resume[initial-file]`：`server.file_upload_progress` 中的行永不被清理。
- `test_reconnected_handshake_rejects_reply_from_old_control_socket[upload]/[download]`：服务端日志显示 `file new_session.bin received ... size 32781 bytes`（已收全），客户端仍以 `break the file transfer connection from server` 收场并返回 False。

三次尝试的服务端日志都打印了 `file ack_failure.bin received ... size 65573 bytes`，即 **ACK 的发送点都被走到了**，失败发生在 ACK 写出之后。

## 5. 机制判断（推断，需运行时证据确认）

服务端 `close_socket()` 只做 `close()`：

- 对端（发送端）在本端接收缓冲里仍有未读数据时，`close()` 触发 RST，内核丢弃已排队但未送达的发送数据 —— 终态 ACK 随之丢失。
- 即使没有 RST，`close()` 之后立刻 FIN，ACK 与 FIN 的相对可见性依赖调度；发送端在 `file_transfer_mode` 里先检查 `file_receive_data_from_server`、再检查 `not file_running`（`connect_tcp.py:10326-10335`），只要 EOF 先被读到就返回 False。

对照：错误路径（`except` 分支）里 `close_socket()` 之后还会再发一次 error sign，同样的问题存在，但本需求只处理终态 ACK。

待确认的两点：

- P3 的第三次尝试 `remaining == 0`，发送端只发了 4+N+8 字节 header，服务端不应有未读数据；因此 RST 假说解释不了 P3，需要考虑"ACK 与 FIN 紧邻导致的读取顺序"这一分支。
- 3.10 之外全部通过，说明窗口极窄。需要能用运行时手段（见 §7）稳定复现，否则无法证明修复有效。

## 6. 备选方案（未选定，需批准）

- A：`send_message` 终态 ACK 之后先 `shutdown(SHUT_WR)`，再继续读取直到 EOF/超时，然后 `close()`。语义：本端不再发送，对端读到 ACK 后读到 EOF。风险：对端可能不发 EOF 就断开，需要超时兜底；改变现有"发完即关"的时延特征。
- B：终态 ACK 后 `close()` 前加一次短 drain（等对端 EOF 或固定短超时）。风险：增加每条腿的收尾时延。
- C：发送端侧兜底 —— `file_receive_data_from_server` 为终态 ACK 时即使 `file_running` 已 False 也判定成功。风险：把竞态从接收侧转移到发送侧，可能掩盖真实的 ACK 丢失。
- D：接收侧发 ACK 后等待对端关闭（读直到 EOF），超时视为成功。与 A 接近。

## 7. 验收证据要求

- 每条路径 P1–P7 各跑一次真实传输，且在**收尾阶段**抓取两侧行为：接收侧确认 ACK 已写出；发送侧确认 `file_transfer_mode` 返回 True。
- 必须能在修复前稳定复现、修复后稳定通过。若无法稳定复现，需先给出"复现手段"作为前置任务，不允许仅凭"看起来修好了"放行。
- 错误路径 P8 回归：收不全、size 不符、对端提前断开，确认不会因为改动变成"假成功"。
- 测试代码不得弱化断言（不得把 `is True` 改成 `in (True, False)`）。

## 8. 禁止事项

- 不删/跳过测试、不弱化断言、不改 CI/依赖/覆盖率。
- 不引入第二套 ACK 机制或第二个结果通道。
- 不绕过既有 `send_message` / 传输入口。
- 不顺手改 `_release_client_socket` 或加密握手（属于其他任务）。
- 不自行提交、合并、推送。

## 9. 风险等级

中。触及所有传输腿的收尾时序；改错会把"失败"误判为"成功"并清理进度记录，造成不可恢复的数据丢失。
