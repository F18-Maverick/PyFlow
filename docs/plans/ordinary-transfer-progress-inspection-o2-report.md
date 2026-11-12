# O2 报告：Server 最终 ACK 明确失败后保留接收进度

状态：最小实现、定向回归、独立真实运行与完整相关套件均完成，待人类审查。只提交 O2，不自动进入 O3/O4 或原 FID 大计划更改 4–6；未创建 git 提交、未合并、未推送。

## 提交意图与批准范围

人类明确“审计通过，开始02”。O1 已获审计通过，本项仅修复 Server 普通接收端在最终 ACK 明确写出失败后仍清理接收行的问题，不增加 ACK 收据、自动重试或额外等待协议。

## 改动内容与数据流

- `PyFlow/network_api/connect_tcp.py`：删除文件写完后的提前 `finished_ok=True`；最终 ACK 改为 `finished_ok = bool(self.send_message(...))`，与 Client 接收端既有模式一致。
- `test/integration/network_api/test_tcp_transfer_resume.py`：新增 `test_server_ack_failure_keeps_received_file_until_eof_confirmation`，覆盖文件/目录项 × ACK 返回 False/抛异常；每例验证初次完整接收失败、EOF 再确认仍失败、下一次 EOF 再确认成功及落盘清理。
- 文件传输说明、变更记录、补充计划、原 FID 计划和本报告同步 O2 范围与证据。

数据流：Client 文件/目录项上传或既有恢复 → Server 写入完整目标文件并截断旧尾部 → 接收通知、事件更新及公钥 TOFU 注册 → 发送既有最终 ACK → 返回真才成功清理；False/异常时沿用原 `finally` 保留并持久化可恢复接收行。

保留的是原 fid 对应的 `[bytes, total, speed, full_path, peer]`，其中停止后的 speed 为 None。完整文件不删除；原路径、来源和总大小继续用于后续稳定 fid 恢复。已有 active fid/socket 释放、owning-entry 判断及 peer/进度开关边界不改。接收通知仍表示本地文件已写完，不升级为对端已收到 ACK。

无新公共签名、持久化字段、协议标志、发送旁路或重复工具函数；O1 的两处发送错误分支保持。未修改 Client 接收逻辑、CI、依赖或覆盖率策略。

## 改前复现与改后回归

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k server_ack_failure_keeps_received_file_until_eof_confirmation -q
```

| 阶段 | 实际结果 |
|---|---|
| 生产修复前 | 4 failed，27 deselected，9.45s，退出码 1 |
| 最小修复后 | 4 passed，27 deselected，26.03s，退出码 0 |

改前四种组合都已实际写完 65573 字节，Client 发送腿返回 False，Server 接收 worker 已收尾；但 `file_recv_progress[606]` 不存在，回归在读取原接收行时出现 `KeyError: 606`，不是连接或装配超时。

修复后，第一次和 EOF 第二次 ACK 明确失败均保留原接收行及磁盘 `download` 行，原文件内容相同；第三次 ACK 成功后两端记录清理，持久化 `download` 为空。发送结果顺序为 `(原 fid, 0, False)`、`(原 fid, 65573, False)`、`(原 fid, 65573, True)`。目录项继续使用嵌套完整路径，没有在接收根目录创建同名文件。所有受追踪 socket worker 在 teardown 中结束。

测试仅在最终 ACK 的发送边界注入 False/异常，不 mock 掉收发核心、磁盘或恢复分派；没有删除或弱化已有测试。

## 编译与静态检查

```bash
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py
ruff check --select F,E9 test/integration/network_api/test_tcp_transfer_resume.py
```

- 编译退出码 0。
- 联合 Ruff 退出码 1，仅既有控制台分支 `response` 未使用（F841），当前行号 6729；本次不修改无关代码，不宣称完整 lint 通过。
- 测试文件单独 F/E9：`All checks passed!`，退出码 0。

## 明确未覆盖与风险

- O3/O4 未实现：没有新增进度查询、`/transfers` 或 `delete_config`。原 FID 大计划更改 4–6 仍未进入。
- ACK 成功写入 TCP、但对端应用没有收到的窗口仍存在；本项只保守处理明确 False/异常，不证明应用层收到，不改变最终 ACK 等待时限。
- 进度关闭或无可恢复 peer 时不会凭空创建接收行，不升级为支持自动恢复；接收文件与进度记录的生命周期仍按原边界区分。
- 没有跨端点身份迁移、旧断点迁移、源文件外部修改处理、全仓测试、完整 lint、文档构建、跨平台或负载审计。
- 没有 UI 或远程管理入口变化；公钥注册与握手顺序必须在实际运行中验收，不能仅凭代码顺序声称通过。

## 规则沉淀与人工闸门

永久行为回归固化“文件完整不等于最终 ACK 写出成功”“EOF 再确认再次失败也不能丢掉恢复记录”“成功后清理持久化接收行”。不改变默认持久化策略，不新增僵尸判定或自动清理。

O2 局部验收已完成，现交付人类审查；未获明确放行不进入 O3。

## 独立真实运行与新进程读回

```bash
.venv/bin/python /tmp/pyflow-o2-smoke.py
```

脚本不调用 pytest 测试函数。实际启动 Server/Client，以既有接收入口、文件发送和恢复分派完成全部路径；仅在最终 ACK 边界注入指定 False/异常，真实读写文件。输出 `O2_SMOKE_COMPLETE`，正常退出，143.26s。

| 运行路径 | 实际数量 | 验收观察 |
|---|---:|---|
| 明文/加密 × 文件/目录项 × 初次/从 4096 字节恢复 × False/异常 | 16 组 | 每组发送结果为 False、EOF False、EOF True；原路径、fid、peer、总大小和内容不变；成功后清理内存及磁盘接收行 |
| 失败状态新进程读回 | 32 次 | 每组两次失败后均另起 Python 进程，从真实产生的快照调用 `_load_transfer_progress`，恢复数量为 1，五项接收行与真实文件完全对应 |
| 关闭进度记录时 ACK 失败 | 4 组 | 明文/加密 × False/异常，完整文件保留，不虚构上传/接收行，不生成可恢复进度文件 |
| 公钥最终 ACK 失败 | 2 组 | 在连接前注入 False/异常；实际 ACK 尝试前已有接收通知、TOFU 记录和 `peer_pub_ok=True`；两端最终进入 framed 状态，公钥任务没有普通进度行 |

每组文件失败都比较完整 payload，EOF 再确认后的文件 inode 与完整路径保持；目录项没有在接收根目录出现新同名文件。本地通知发生在每次 ACK 尝试之前，没有被 ACK 失败跳过，也不被解释成对端确认完成。

加密文件场景等待真实握手完成并确认 `enc:` stamp，记录实际传入帧解码器的 wire bytes，检查当前发送区间的 128 字节明文片段未出现在原始帧流中。这是具体运行路径的观察，不是全局加密审计。

例：加密目录项从 4096 字节恢复、最终 ACK 抛异常，fid 为 40917317460，实际三次发送结果：

```text
[[40917317460,4096,false],[40917317460,65573,false],[40917317460,65573,true]]
```

该组新进程读回两次，原路径及 inode 保持，最终 SHA-256 为 `6a141060e2ab02e4b57c6a84c97958d9e1cc3b7d54f2c76fa0823d142cafa2ea`。同组实际捕获加密帧 62458 字节；其恢复不是重新构造一份期望 JSON 来代替失败快照。

两次公钥失败均输出 `both_framed=true`、`registered_before_ack=true`、`tracked_key_rows=0`；钩子在 `connect()` 前安装且实际命中一次，不依赖“注入时握手可能已经完成”的竞态。

## 完整相关套件与证据收尾

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

结果：**241 passed，286.12s，退出码 0**。包含新增 O2 回归、O1 普通发送回归、双向普通文件/目录、恢复、加密和公钥、转发、异步及现有 Web 消费者。定向回归属于该总数，不重复相加；不挪用 O1 的历史 237 passed 作为本次结果。

一次性 smoke 脚本在记录证据后删除，其自建临时目录由 `TemporaryDirectory` 成功退出时清理；已检查 `/tmp/pyflow-o2-smoke.py` 与 `/tmp/pyflow-o2-smoke-*` 均不存在。保留永久行为回归及本报告，不保留临时测试装配。

