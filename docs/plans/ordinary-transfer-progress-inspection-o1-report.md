# O1 报告：Server 普通发送错误符号不得成功收尾

状态：实现、定向回归、独立运行时烟测与完整相关套件均完成，提交人类审查，不自动推进 O2–O4。未创建 git 提交、未合并、未推送；未进入原 FID 大计划的更改 4–6。

## 提交意图与批准范围

人类明确“审计完毕，开始01的更改”。本项只修复 `TCP_Server_Base.file_transfer_mode` 在等待开始符号、等待最终数据 ACK 时收到错误符号的退出行为，不改变接收端的 ACK 生命周期。

## 改动内容

| 文件 | 关键变更 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 两处错误符号分支的 `break` 改成 `return False`；生产逻辑仅此两行变化 |
| `test/integration/network_api/test_tcp_transfer_resume.py` | 新增 `test_server_final_error_keeps_upload_until_successful_resume`，文件/目录项 × 初次/从 4096 字节恢复，验证真实错误符号、失败返回、上传及磁盘记录保留、原 ID EOF 再确认与原路径内容 |
| `docs/source/File_Transfer/File_Transfer.rst` | 写明普通发送的最终 ACK 成功条件、开始阶段拒绝与失败保留边界 |
| `docs/source/changes/transfer-fid-routes.md` | 增加 O1 变更记录，不覆写更改 3 的历史范围 |
| `docs/plans/ordinary-transfer-progress-inspection-plan.md`、`docs/plans/transfer-fid-and-resume-unify-plan.md` | 记录 O1 授权、当前状态与后续人工闸门 |
| 本报告 | 改前/改后命令、真实运行时证据、未覆盖与风险 |

Client 相应两个分支原本已经返回 False，本次未重复改写。未修改其他调用点、测试断言、CI、依赖、覆盖率策略、公共签名、持久化五项值或线路格式。

## 数据流与为什么这样改

入口：Server 普通文件或目录项发送，或既有恢复入口，最终调用同一个 `file_transfer_mode`。

1. 建立传输连接，继续使用既有加密附着和 `send_message` / `receive_message`。
2. 等待开始符号：错误符号立即 False，不再读取并发送后续文件元数据；首次尝试不新建进度，已有中断记录保持。
3. 开始符号成功后发送文件头、从 `resume_offset` 读取并发送内容，按既有方式记录进度。
4. 等待最终数据确认：错误符号立即 False，不能执行 `file_sent=True`、成功日志或成功清理；只有既有最终成功 ACK 才进入成功分支。
5. `finally` 不变：当前 owning-entry 的失败上传具备 peer 时保留并持久化；活动 socket、活动 fid 及本次暂停项仍按原范围释放。源文件不存在的既有不可恢复处理不变。

“发送字节数已等于总大小”不是接收成功证据。该修复兑现计划 2.1 的普通发送契约，不新增完成通道或状态副本。

## 改前复现

在生产修复前新增并运行真实 socket 回归：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k server_final_error_keeps_upload_until_successful_resume -q
```

结果：**4 failed，23 deselected，9.30s，退出码 1**。四种组合均在接收端已写入 65573 字节后，由既有 `send_message` 真正发出错误符号替代最终 ACK；Server 收到错误符号，却返回 True。捕获输出先有 `Error sign received...`，随后仍有 `Success: file ... sent successfully`。失败断言为 `assert result is False`，不是测试装配或超时失败。

## 改后永久回归

修复后同一命令结果：**4 passed，23 deselected，17.72s，退出码 0**。

回归没有 mock 整条传输。仅在 Client 最终 ACK 边界写出真实错误符号并抛出指定异常，使 Client 按既有失败路径保留接收行；通过 Event 分离文件头确认和最终错误，避免无帧 TCP 合并两种符号造成错误覆盖不到分支。

验证内容：Server 返回 False；完整接收内容保持；上传完整 scoped key 和五项磁盘记录保留；Client 沿既有恢复驱动以原 fid 在 EOF 再确认；最终两端 owning 记录清理，目录项不变成接收根目录下的新文件。全部 socket worker 在 teardown 中 join，未留下活动线程。

## 独立真实运行时证据

另建一次性 `/tmp/pyflow-o1-smoke.py`，不调用 pytest 测试函数，运行真实 Server/Client、普通文件/目录项接收入口和 `file_transfer_mode`。每种场景都覆盖明文/加密、文件/目录项、offset=0/4096。仅在接收端开始符号或最终 ACK 边界注入真实错误符号，不替换文件传输实现。

```bash
.venv/bin/python /tmp/pyflow-o1-smoke.py
```

结果：**退出码 0，62.32s，输出 `O1_SMOKE_COMPLETE`**。

| 路径 | 场景数 | 观察结果 |
|---|---:|---|
| 开始阶段错误 | 8 | 返回 False；Server 二进制发送尝试为 0；首次不新建上传/目标文件，恢复尝试不破坏原记录与原部分文件；无成功日志 |
| 文件体完成后错误 | 8 | 返回 False；65573 字节内容相同；原上传 key 与磁盘五项记录保留；无错误尝试的成功日志 |
| 上述失败后的 EOF 再确认 | 8 | `resume_trans` 使用同一 fid、offset=65573；实际发送腿返回 True；两端记录清理，原文件内容不变 |
| 正常最终 ACK | 8 | 初次/恢复发送返回 True；上传/接收 owning 记录清理，文件内容相同 |

这对应 24 个独立初始场景以及其中的 EOF 恢复，不将恢复次数再算成新增初始场景。

加密场景等待两端真实握手完成，确认每次传输 stamp 为 `enc:`。记录接收端实际送入帧解码器的 wire bytes，验证所发送区间的 128 字节明文片段不出现在原始帧流中；body/error 及其恢复均经过既有安全入口。这是被执行场景的观察，不是全局加密覆盖审计。

输出同时记录内容 SHA-256。例如加密目录项从 4096 字节恢复、最终被拒绝后再确认，原 fid 恢复成功，最终文件 SHA-256 为：

```text
8566a6558862ad8797dcf42e6a2b53f71d6d40f9c57c8e8eb50b61e185554fc7
```

该行的 `result=false`、`error_sent=true`、`eof_recovered_original_fid=true`，实际捕获加密帧共 62164 字节（含失败尝试和 EOF 再确认）；脚本内部比较完整接收内容与源 payload，而非仅比较长度或非空。

## 编译与静态检查

```bash
.venv/bin/python -m py_compile PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py
ruff check --select F,E9 test/integration/network_api/test_tcp_transfer_resume.py
```

- 编译：退出码 0。
- 生产与测试联合 Ruff：退出码 1，仅既有 `connect_tcp.py:6728` 的 `response` 未使用（F841）；前序原子报告已记录。本次不修改无关控制台分支，不宣称全量 lint 通过。
- 测试文件单独 F/E9：`All checks passed!`，退出码 0。

## 明确未覆盖与风险

- O2 未实施：Server 接收端在最终 ACK 明确写出失败后仍可能清理接收记录；Client 对应修复保持，但不以此声称 Server 已修复。
- O3/O4 未实施：没有新增 `get_transfer_progress`、`/transfers` 或 `delete_config`。
- 不解决 ACK 成功写入 TCP 而对方应用没收到的不确定窗口，不新增 ACK 收据/去重/重试协议，不扩展现有最终 ACK 等待时限。
- 不改固定 client_port 依赖、端点迁移、源文件外部修改/删除、旧断点迁移、kind/tfid、现行转发恢复线路或 ID 分配权。
- O1 没有新增 UI、HTTP、远程管理入口。没有全仓测试、完整 lint、文档构建、跨平台或负载验证；完整相关套件仅覆盖所列命令。

## 规则沉淀与人工闸门

已补永久行为回归：“错误符号不得成功返回或丢失可恢复上传；完整文件依然可用原 ID/原路径在 EOF 再确认。”没有删除用例、弱化断言、增加跳过或修改 coverage/CI。

本项只提交审查；未获人类明确放行，不进入 O2。O4 是补充计划的第四项，不是原 FID 大计划的原子更改 4。

## 完整相关套件与证据收尾

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

结果：**237 passed，254.28s，退出码 0**。覆盖相关网络单测、普通两向文件/目录、恢复、加密/公钥、原生/扩展转发、异步和现有 Web 消费者。新增定向回归已包含在该总数中，不相加为唯一用例数量；本轮结果不挪用更改 3 的历史数量。

一次性 smoke 脚本在记录证据后删除，脚本自建的临时目录由 `TemporaryDirectory` 在成功退出时清理。以上结果仅证明 O1 已实施范围；Server 接收 ACK 缺口、查询和清理 API 仍待 O2–O4。

