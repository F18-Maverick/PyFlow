# 终态 ACK 交付可靠性：需求卡与机制结论

状态：代码与确定性回归已实施，运行证据及审查报告见 [final-ack-delivery-report.md](final-ack-delivery-report.md)。未创建 git 提交、未合并、未推送，人工审查仍未放行。原会话将修复按轻量增量执行；本次恢复其未完成的边界测试、验收与文档收尾，不重构 ACK 发送时机。

## 1. 目标

接收端仍在原先的时机发送终态数据 ACK（`server_received_file_data_sign`）并关闭传输连接；发送端必须正确解析 TCP 字节流中的 ACK，不能因粘包、拆包或紧随其后的 EOF 而误判失败、遗留上传进度记录。不保证 TCP 写成功但实际传输丢失的 ACK 交付。

## 2. 非目标

- 不改终态 ACK 的报文格式、不去重协议（仍是同一行 sign）。
- 不改错误 sign 的语义与发送时机。
- 不改断点续传的匹配规则、进度记录的五项格式、FID 分配。
- 不改加密算法、鉴权、CI、依赖、覆盖率阈值。
- 不引入 ACK-of-ACK、新结果通道、延迟关闭、drain 或新的超时。

## 3. 受影响的执行路径清单

终态 ACK 的既有发送点（不改变发送时机）：

1. `TCP_Server_Base.file_transfer_mode_recv`：服务端收完上传后，通过 `send_message` 发送 ACK 并关闭传输 socket。
2. `TCP_Client_Base.file_transfer_mode_recv`：客户端下载接收侧，同一模式。
3. `TCP_Server_Base._forward_pump`：每个目标的 writer 读到真实 ACK，全部已接受目标完成后，上游仍通过 `send_message` 回传该 ACK；本次修正此处的换行终止。

必须逐条给出结论的路径（下表保留最初的故障证据；本次验收见报告）：

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

## 5. 已确认的根因与修复决策

原会话用真实传输探针对照确认：不合并读时 `file_transfer_mode=True`，把 header sign 与 final ACK 合并到一次读后为 `False`。原实现用 `data.decode("utf-8").strip()` 的全部结果与单个 sign 做 `==` 比较；两行一起到达时匹配失败，随后 EOF 导致返回 `False`。这不是“只保留最后一行”，而是把整次读误当一条消息。

此前的 RST、ACK/FIN 顺序假说并未证明是本缺陷根因，不能据此增加 shutdown/drain 或改变发送时机。原会话回归测试还记录了修复前必失败、修复后通过；本次扩展为上传/下载与初传/EOF 重放四种确定性粘包场景。

决策：两端发送腿均累积完整 sign 行；未遇换行的尾部以 bytes 保留到下次读，完整行才 UTF-8 解码。错误 sign 仍走既有失败路径，真实 ACK 才能成功；不以 EOF 刷出未终止的尾部。

中继的 `_forward_read_sign` 返回不带换行的 marker bytes，而 `send_message` 只给 str 加换行。上游改为发送该已确认 marker 的文本形式，继续走同一发送/加密入口；不生成第二个确认信号，不改变全目标完成门槛。

## 6. 实施范围与审查边界

- 产品实现只改 `PyFlow/network_api/connect_tcp.py` 内的分行器、两处发送腿接收循环和中继 ACK 的文本发送。
- 验证文件：`test/integration/network_api/test_tcp_transfer_resume.py` 的真实 socket 粘包回归；`test/unit/network_api/test_wire_lines.py` 的所有读边界、空行及未终止 ACK 契约。
- 公共 API、FID/进度数据模型、依赖、CI、加密握手、接收端 ACK 时机均不改动。
- 人工最终审查与平台 CI 仍是交付闸门；不能把本地通过写成 Windows 已验收。

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

中。影响两端发送腿的 sign 识别与中继 ACK 的换行终止，不改变接收端收尾时序。若把未完成/错误传输误识别为成功，会清理进度记录并破坏恢复；必须保留错误路径与未终止 ACK 的失败证据。
