# 普通传输失败保留、进度查询与按名清理：总体报告

状态：O1–O3 已获人类审计通过；O4 已按批准完成实现与局部验收，待人类审查及补充项总验收。未创建 git 提交、未合并、未推送。本报告仅涵盖补充 O1–O4，不代表原 FID 大计划的原子更改 4–6 已实施。

## 目标与总体数据流

四项共同完成以下行为：

1. Server 普通发送只有观察到原最终 ACK 才成功，错误符号不能误入成功清理。
2. Server 接收只有最终 ACK 明确写出成功才清理；失败时即使完整文件已落盘也保留恢复行。
3. Server/Client 提供一致的本地只读查询；内存进度、磁盘快照与运行时状态分开显示，不自动判定僵尸。
4. 用户可在停止实例后按白名单名字删除文件级状态；放弃进度后旧对象不能使旧任务复活，新对象保持正常运行能力。

传输流：现有文件/目录/恢复入口 → 既有安全收发 → 本地文件与原五项进度行 → 原最终 ACK → 成功清理 / 明确失败保留 → 原 fid、原路径 EOF 再确认。

观察流：本地 API 或 `/transfers` → 既有内存快照 + 可选磁盘只读快照 → 带身份/来源的独立 JSON-ready 结果。不连接、不加载、不恢复、不写回。

清理流：用户核对并备份 → 停止所有共享文件的写入者，等待任务收尾 → `delete_config` 白名单解析及生命周期检查 → unlink 指定文件 → 仅在删除/确认缺失进度时永久封闭旧对象的 persist/load/start。源文件与接收文件不删除。

## 原子项汇总

| 项 | 实施内容 | 当前人工闸门 | 证据 |
|---|---|---|---|
| O1 | Server 两处错误符号分支立即 False；保留现有失败持久化 | 已审计通过 | `ordinary-transfer-progress-inspection-o1-report.md` |
| O2 | Server 接收最终 ACK 的布尔结果决定 finished_ok；失败保留原接收行 | 已审计通过 | `ordinary-transfer-progress-inspection-o2-report.md` |
| O3 | 两端 `get_transfer_progress` 与本地 `/transfers`，内存/磁盘及上传作用域不混淆 | 已审计通过 | `ordinary-transfer-progress-inspection-o3-report.md` |
| O4 | 两端 `delete_config`、只读列表、按名删除、关闭/写回互斥及进度放弃闸门 | 实现和局部证据完成，待审查 | `ordinary-transfer-progress-inspection-o4-report.md` |

以上是原子实施切片，不是已经由本代理创建的 git commit。未跨越人工审查自动进入下一个原 FID 切片。

## 需求卡路径逐条对照

| 路径 | 结论 | 运行时或机械证据 |
|---|---|---|
| Server 普通发送开始符号错误 | 覆盖 O1 | 真实错误后 False，二进制发送尝试为 0；首次不造记录，恢复时不破坏原部分文件 |
| Server 发送全部字节后错误 | 覆盖 O1 | 文件/目录项、初次/4096 恢复均保留 scoped 上传及磁盘行；改前误返 True 的 4 例改后通过 |
| 两向正常文件/目录与原有恢复 | 覆盖相关回归 | 本轮完整套件 332 passed；独立程序新对象正常双向文件传输及新失败状态保存 |
| Server 最终 ACK False/异常 | 覆盖 O2 | 4 例改前 KeyError 丢行、改后保留；独立 16 组明文/加密、文件/目录、首次/恢复组合 |
| 文件完整后的重复 EOF 再确认 | 覆盖 O1/O2 | 失败再失败仍保留，成功才清理；原 fid、完整路径、内容不变 |
| 中途断线、超时、源文件不存在 | 保持既有边界 | 既有真实 socket 回归继续通过；本任务不扩展超时或源文件生命周期 |
| 加密收发及关键失败恢复 | 覆盖本任务运行场景 | O1/O2 独立程序真实握手/帧流验证；O4 明文/加密双向删除与新实例运行；非全局安全审计 |
| 公钥注册和 ACK 失败 | 覆盖 O2 及既有回归 | 在 connect 前注入两种 ACK 失败，实际命中且两端 framed，就绪前 TOFU 已保存，普通进度无公钥行 |
| 转发零/部分完成、稳定 ID 重放 | 保持并展示 O3 | 现有真实转发/恢复回归通过；O3 真实失败转发产生 Server route 与 Client metadata 并显示 |
| 两端全部内存进度 | 覆盖 O3 | 活动、暂停、中断、100% 保留行；同号不同 receiver 和收发同号不折叠 |
| 两端磁盘独有、落后快照 | 覆盖 O3 | 内存/磁盘分开返回，不加载磁盘；返回副本变更不影响原状态 |
| 缺失/空/损坏/不支持/读取失败/跳过读盘 | 覆盖 O3 | 状态区分为 missing/ok/error/not_requested；文件字节不变，内存仍可见 |
| 未连接、开关关闭、空记录和并发变化 | 覆盖 O3 | 离线查询不连接；实际保留记录仍展示，未知为 null；190 次并发观察不影响两条传输完成 |
| 本地两端 console | 覆盖 O3 | 实际 stdin/输出 JSON；日志关闭可见；额外参数和未闭合引号只打印用法，对端未收到查询命令 |
| 现有 Web `/api/transfers` | 刻意保持不变 | 既有 Web 回归通过，不扩散本地完整路径/恢复快照 |
| O4 列表优先与纯只读 | 覆盖 | 两端运行中可列举真实 5 类文件；名字参数不触发删除；回归检查返回 list、排序及文件/状态不变 |
| O4 按名删除进度与四类日志 | 覆盖 | 每次只消失指定文件；未选择的日志字节保持；32 次新进程读回对照默认保留/删除后/新对象任务 |
| O4 缺失与重复删除 | 覆盖 | 合法目标缺失返回 False；进度缺失也封闭旧对象，不能从内存重新持久化 |
| O4 非法类型、名称、路径、符号链接/目录、歧义 | 覆盖边界回归 | 明确报错，外部目标/链接目标不变；支持当前属性的合法 basename 重定向 |
| O4 权限等删除失败 | 覆盖 | 回归确认失败不禁用默认保存；独立真实 chmod 0500 导致 PermissionError，原字节不变 |
| O4 运行中、意外掉线、关闭中、活动传输/中继 | 覆盖 | 真实运行中/Client 意外掉线均拒删；关闭阻塞和 active fid/socket/relay/日志缓冲由行为回归拒删 |
| O4 发布/删除竞争、迟到收尾、重复关闭、旧对象启动 | 覆盖 | 进度锁内先完成 publication 再删；late finish/load/persist、stop/close 不复活；旧对象启动前报错 |
| O4 损坏文件按名清理、内容边界 | 覆盖 | 无须解析损坏进度即可删除；源文件/接收文件及未选择状态保持 |
| O4 默认关闭保留、新实例继续工作 | 覆盖 | 关闭后真实快照各恢复 1 行；删除后为 0；新对象真实传输成功，新失败任务又可恢复 1 行 |

“覆盖”是上述范围和证据，不表示跨进程所有权、全局僵尸判定或 ACK 应用收据已解决。

## 最新整体回归与证据

O4 本轮定向命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_delete_config.py test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/unit/network_api/test_TCP_server.py -q
```

**101 passed，2.24s**。

本轮完整相关套件：

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/integration/network_api/test_message_event_store.py test/integration/network_api/test_send_message_store.py test/unit/transfer_web/test_web_transfers.py -q
```

**332 passed，253.72s**。包括新增 O4 与前序相关回归；定向/历史命令有交集，不相加为唯一总数。独立 O4 程序在 50.71s 内完成 8 组双端真实路径、32 次新进程读回、4 组新实例传输及真实权限错误。各项 smoke 的原始观察与限制作于原子报告，历史通过数不替代本轮运行。

编译通过；新增 O4 测试 F/E9 检查通过；生产/测试联合 F/E9 仅既有 console `response` F841，不宣称全量 lint 通过。

## 架构、接口与副作用

- 公共 API 新增两端同名 `get_transfer_progress`、`delete_config`；新增的 `/transfers` 仅本地 console。
- 进度仍是原五项值，Server route 与 Client metadata 保持既有格式；不新增权威恢复表或持久化“完成/僵尸”字段。
- 删除通过本地 allowlist，旧对象的终结状态仅内存存在；未选择删除时默认保存保持。
- 不改协议标志、字节流、ACK 时限、加密入口、Web 合同、CI/覆盖率策略或依赖版本。
- 非本次迁移：kind/tfid、`/forward_resume`、`/forward_upload`、原 client 恢复表与 ID 分配权。原计划切换仍需分别批准。

## 未覆盖与需要接受的风险

1. ACK 写入 TCP 不等于对端应用确认；没有新增收据、自动重试或自动清除不确定状态的协议。
2. 用户判断“疑似僵尸”后删除的是整份进度，包含其中正常中断任务；不可按 fid 部分删除，也不联动删除对端。
3. 同一路径可能由多对象/进程共享。删除前必须停止所有写入者；本实现不提供隔离、锁定外部进程或阻止其重新创建文件。
4. 内存状态在进度删除后仍可被观察，但旧对象已终结，不得连接或启动；应新建实例。日志没有永久禁写标志，迟到新消息可形成新日志。
5. API 不替调用者主动停止/join 全部 worker；运行中或关闭/传输未静止时拒绝。临时网络 teardown 机制未在本次重构。
6. 查询是观察快照，不是内存/磁盘/暂停开关的原子事务，也不能证明远端存活或完成。
7. 固定 client_port、端点迁移、源文件外部改变、混合新旧版本、旧断点迁移及中继内容落盘均不在范围。
8. 未做全仓测试、文档构建、全量 lint、跨平台或负载验收；报告不声称全局加密覆盖审计。

## 合并前闸门

- 规则沉淀：O1/O2 真实 ACK 行为回归、O3 只读/作用域回归、O4 白名单与进度防复活回归。
- 文档收尾：文件传输说明、两端 Network API、变更记录、计划和各原子报告已同步。
- O4 与本总体报告待人类审查；明确接受上述风险并完成最终闸门前不请求合并，不自行提交远端。
