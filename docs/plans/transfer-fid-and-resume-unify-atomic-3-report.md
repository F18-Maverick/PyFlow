# 原子更改 3 报告：回传到达端原有最终 ACK

状态：按最新人类要求完成第二结果通道的部分回退；其他已实施修复保留。单 ACK 实现、定向/完整回归及独立真实程序验收完成，待人类审查。未创建 git 提交、未合并、未推送；未进入原子更改 4–6。

## 最新决策与本次边界

人类要求：Server 直接等待到达端既有最终 ACK，再向源端回传同一种 ACK，保持协议简洁；其他问题的修改暂不回退。

因此本次删除独立结果控制行、额外结果等待与结果状态，恢复单一最终 ACK 的成功语义。没有整体回退更改 3，也没有撤销逐目标完成、稳定 ID 恢复、原路径衔接、并发控制、持久化或失败资源清理。

本报告以当前单 ACK 版本为准。此前双层确认版本的 231 passed 是历史证据，不作为本次切换的验证结果。

## 回退与保留清单

| 项目 | 当前处理 |
|---|---|
| 第二结果控制通道及其分派/处理函数 | 删除，不留别名或兼容入口 |
| `TRANSFER_RESULT_TIMEOUT`、`result`、`result_event` | 删除，不再有第二阶段等待 |
| 上传字节读完即发送的本跳成功 ACK | 删除；流结束不等于目标交付成功 |
| Server 逐目标 `completed` 与完整接受目标映射 | 保留；只有真实目标最终 ACK 才推进 |
| 稳定上游/下游 fid、`relay_fid=`、原始源端校验 | 保留；成功目标不重发 |
| 同 fid 发送锁及同目标重复合并 | 保留；完成后排队的旧 owning 记录不能重开任务 |
| 原路径、磁盘 offset、file/folder 匹配及 EOF 再确认 | 保留；不恢复已删除的新 ID/同路径桥接 |
| 完成集合 JSON、源上传行读回、快照锁 | 保留；五项进度值与 client JSON 字段不变 |
| 队列 receiver 作用域、失败缓冲释放、读取超时、资源收尾 | 保留 |

## 最终实现与数据流

### Server：收到全部目标 ACK 才向源端回传

- `_forward_read_sign` 仍经 `receive_message` 有界读取，现在返回已识别的既有 marker 字节或 None，而不是只返回布尔值。开始握手继续按真假判断；最终确认调用保留到达端的实际 marker。不会把可能合并在同次读取中的 header ACK 一并作为完成信号转发。
- 每个 writer 收到真实最终 ACK 后，调用原有 owning-route 检查的 `_complete_fid_target`。完整 `targets` 保持不变；`completed` 和未完成反向索引保持原修复。
- `_forward_pump` 先等待本次 writer 收尾，再同时核对：本次上游输入完整、主表仍属于该 route、原接受目标全集均已完成、确实读到了本次目标 ACK。
- 条件满足后，通过既有 `send_message` 在上传数据连接上回传该 ACK marker；否则发送既有错误符号。成功 ACK 写出后才调用 `_drop_fid_route`。写出返回 False 或抛异常都保留完成路由。
- 这是转发同一种应用层 ACK，不是复制某一跳 TCP/密文帧；每跳继续经过既有发送与加密入口。
- 中继最终清理仍释放 socket、端口和本次 owning 运行状态，但不再发送任何额外控制结果。

### 源端：只使用原有最终 ACK

- `file_transfer_mode` 删除 forwarding record/result/event 的额外等待。既有最终数据 ACK 是成功的唯一完成标志。
- 收到既有错误符号的两处 Client 发送分支改为立即返回 False，不能通过 `break` 落入 `file_sent=True`。这是聚合失败使用原有错误符号后所必需的修正；普通 Client 上传也共享这个分支。
- 断线、错误或超时且源文件仍存在、进度可恢复时，既有 `_file_progress_finish(..., keep=True)` 保留上传行并持久化。没有新增另一份进度表。
- `_forward_one` 仅在真实发送函数返回 True 后删除自己的 forwarding metadata，并写回已有进度文件。失败仍保留原 metadata 和完整目标列表。
- 运行时 forwarding record 只保留 `send_lock` 与 `resuming` 等重放控制，不保存结果同步对象。
- 最终 ACK 等待仍为原有 30 秒加文件大小额度，没有第二个 20 秒结果等待，也没有新收据或自动重试协议。

### 跨恢复闭环保持

目标恢复请求仍通过现行 `/resume_trans`、`/forward_resume` 驱动；源端用原 `relay_fid` 重放。先前成功目标留在完成集合中，不重发；本次仅恢复剩余目标。最后一个目标确认后，同一个原始最终 ACK 在本次上传连接回到源端，使原任务的上传行、metadata 和 route 清理。

原始源端、目标归属、child fid 和 route 对象所有权检查没有回退。Client 接收端最终 ACK 写出失败时仍保留原路径与进度，允许在 EOF 位置重试确认。

## 本次改动文件

- `PyFlow/network_api/connect_tcp.py`：第二通道删除、原始最终 ACK 回传和 owning metadata 清理；保留其他修复。
- `test/integration/network_api/test_tcp_transfer_resume.py`：适配单 ACK 成功条件，替换已删除协议的用例，增加 held-ACK 与 ACK 写出失败行为验证。
- `docs/source/File_Transfer/File_Transfer.rst`
- `docs/source/Network_APIs/TCP_Server_APIs.rst`
- `docs/source/Network_APIs/TCP_Client_APIs.rst`
- `docs/source/changes/transfer-fid-routes.md`
- `docs/plans/transfer-fid-and-resume-unify-plan.md`
- 本报告。

此前更改 3 的路由单测与加密转发最终状态断言保留。没有改分配权、kind/tfid、`/forward_resume`、`/forward_upload`、文件内容格式、Web 字段、CI、依赖或 coverage。

## 改前 / 改后真实程序证据

### 相同探针：上游 ACK 不得早于最后目标确认

一次性脚本直接启动实际 Server、源 Client 和两个目标 Client，传输 262,160 字节；仅在最后目标最终 ACK 发送边界阻塞，不 mock 数据泵。

| 探针观察 | 切换前双层确认版 | 当前单 ACK 版 |
|---|---|---|
| 最后目标已经写完文件但 ACK 被阻塞 | 是 | 是 |
| 已确认目标数 | 1 | 1 |
| 源端上传行 / metadata | 各 1 条 | 各 1 条 |
| 此时上游成功 ACK 数 | 1；违反最新要求 | 0 |
| 相同探针退出码 | 1，断言成功 ACK 过早 | 0 |

当前版释放目标 ACK 后，只向源端回传一次 `/FILE_RECEIVE_CLIENT_DATA`，源端返回 True，上传行、metadata 和 Server route/index 均清空；两目标文件与源数据完全相同。

### 零 ACK、部分成功与原 ID 恢复

独立实际程序分别传输普通文件及目录内文件；只在目标最终 ACK 发送边界注入失败。每场景内容 131,090 字节，无 Server 文件内容落盘。

| 场景 | upstream / child fids | 首次失败状态 | 后续恢复 | 最终状态 |
|---|---|---|---|---|
| 零 ACK，普通文件 | 7288614307 / 11583581603、15878548899 | False；成功 ACK 数 0；源上传/metadata 各 1；未完成索引 2 | 原 ID，依次 False → True；共 4 条目标腿 | 仅最后一次回传 1 个既有 ACK，源/Server 状态清空 |
| 部分 ACK，目录内文件 | 4883320775 / 9178288071、13473255367 | False；成功 ACK 数 0；源上传/metadata 各 1；未完成索引 1 | 原 ID，仅重放失败目标，返回 True；共 3 条目标腿 | 同样只有 1 个最终 ACK，源/Server 状态清空 |

两场景原目标映射与所有上下游 ID 保持不变；成功目标不重发。所有目标最终内容与源数据完全一致，SHA-256 为 `bac2bc23ee34db01070c104e661075be969be0aee6c3bb899c0db93963fcddb8`。脚本退出码 0。

另起独立 Python 进程加载首次失败时的快照：零 ACK 恢复 2 个未完成索引；部分 ACK 恢复 1 个，已完成目标不重新索引。两种情况源端均恢复 1 条五项上传行和 1 条 metadata；运行时只有发送锁/重放集合，没有结果同步对象。退出码 0。这验证状态读回，不宣称跨 Server 进程重启的自动续传。

## 测试与检查

### 定向回归

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/unit/network_api/test_fid_allocator.py test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_resume.py -q
```

结果：**52 passed，68.98s，退出码 0**。

关键行为：

- 原 file/folder × 零 ACK/部分 ACK 场景保留所有内容、状态、ID 和重复请求断言，并增加未完成时上游成功 ACK 数为 0、最终只有 1 个既有 ACK 的断言。
- 删除已废弃第二结果协议的专属断言；原丢失结果用例替换为上游最终 ACK 写出失败，覆盖返回 False 和抛异常两种形式，仍检查源端状态、完成路由和新 Client 读回。
- 新增实际目标 ACK 被阻塞时源端仍等待、上传进度完整但未清理的场景；释放后确认回传 marker 与既有目标 ACK 相同，最终状态清空。
- 旧完成状态读回、非法快照保护、端点作用域、普通续传、公钥和加密相关用例没有删除或弱化。

### 完整相关套件

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

结果：**233 passed，243.25s，退出码 0**。包含普通双向文件/目录、加密、公钥、原生/扩展转发、稳定 ID 续传、异步及 Web 消费者。与定向命令有用例交集，不相加为唯一用例总数。

### 静态检查

对当前修改的生产/测试 Python 文件运行 `python -m py_compile`，退出码 0。

```bash
ruff check --select F,E9 PyFlow/network_api/connect_tcp.py test/integration/network_api/test_tcp_transfer_resume.py
```

退出码 1，仅既有 `response` 未使用（F841），前两份原子报告已记录该存量问题。本次未修改无关控制台分支，不宣称全量 lint 通过。

## 明确未覆盖与风险

- 原子更改 4–6 未实施；现有 kind/tfid、client 转发恢复记录、恢复/上传命令和当前分配权保留。
- 上游 ACK 成功写入 TCP 不等于源端应用层收据。丢失 ACK 或源端等待超时可导致保守保留状态；本次没有增加自动清除此状态的协议。ACK 写出明确失败时 Server 保留完成路由。
- 原有最终 ACK 等待时限不扩大；慢目标或长时间手动暂停可能超过源端等待。目标确认读取超时保留，未声称所有阻塞写都有统一总时限。
- 此前发现的 Server 普通发送错误符号 fallthrough、Server 接收端 ACK 写出失败仍清理接收行的存量缺口，本次没有扩展修复。Client 发送错误符号分支因转发失败回传所必需而修正。
- 不解决固定 client_port、端点迁移、源文件外部删除/修改、混合新旧节点、旧断点迁移、中继内容落盘或暂停持久化。
- 没有全仓测试、完整 lint、文档构建、跨平台/负载验证或完整加密覆盖审计。本次无 UI 改动。

## 规则沉淀与人工闸门

“到达端未确认不得向源端回成功 ACK”“失败保留源记录”“恢复复用原上下游 ID”“ACK 写出失败不丢完成路由”已落实为真实 socket 行为回归及同步协议说明。

完整相关套件与独立烟测均已完成；一次性脚本及本次自建临时目录已清理。请人类审查本次单 ACK 版本。未获下一项明确批准，不进入更改 4，不自行提交。
