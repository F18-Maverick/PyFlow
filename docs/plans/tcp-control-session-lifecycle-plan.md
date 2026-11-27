# TCP 终态启动与控制会话端口归属 — 提交拆分计划

依据：[需求卡 A：终态与重试](tcp-terminal-start-retry-requirements.md)、[需求卡 B：会话端口归属](tcp-session-port-ownership-requirements.md)。**已确认需求并授权写计划；当前 diff 优先收口，不以此授权后续实现。** E1 的线程池与 [E2 验收边界](custom-executor-shutdown-report.md) 保留，不改写历史验收结论。

## 0. 先处理恢复的 diff，不补写后续代码

基线 `caf6228`。提交前，`connect_tcp.py` 的 `+25/-7` 在工作区而不在暂存区；暂存区只有 `test/integration/network_api/test_executor_lifecycle.py` 的 `+56`。现已按授权拆为以下两个本地产品提交；测试仍单独暂存，规划文档未混入。

| 原问题 | 这批真正改变了什么 | 不能以此宣称的结果 |
|---|---|---|
| #1 启动/关闭交错 | **没有改** Server 启动、Client 成功连入后的状态发布/reader 启动 | 不是对旧 listener、`running`、reader、协商的原子交接；Server stale `running` 未修。 |
| #2 关闭后继续连接 | **A0**：Client 外层重试从 `while True` 改为检查 `_stopped`；退出返回 `False`。恢复的测试在实际重试中关闭，再直接调用 `connect()`，监听端无新连接。 | 未覆盖检查后的在途尝试、内层本地端口回退、协商等待或配置生命周期；未增加 1 秒等待。 |
| #3 同会话重复释放 | **B0**：实例锁串行化正常的范围分配/释放；记录 `(control_socket, client_num)`，断线/会话更替按 socket 匹配，终态 `free_port()` 释放尚存登记，成功后清空；已停止实例不再通过 `alloc_port()` 分配。 | 是“一次归还”，**不是“最后一个具体端口归还后再释放”**。目前没有借出口计账，也不能同时表示两代登记。 |
| #4 旧实例误删复用编号的登记 | **B0**：A 断线已归还登记，B 复用同一编号，A 迟到的 `close()` 不再删 B 的文件行；测试还检查 B 自己关闭后记录/锁文件清理。 | 仅顺序 ID 复用，不是多个实例同时读写的互斥；仍可能重复通知覆盖单槽、移动本地范围时改写 `self.port`。 |

**已提交，待审**：A0 `0f38e38c8e` 只含 `connect()` 的重试循环和出口（`+2/-1`）；B0 `66929f891b` 只含登记与会话清理（`+23/-6`）。独立测试步骤：`test_terminal_client_close_stops_auto_reconnect` 对应 A0；`test_late_client_close_preserves_reassigned_port_range` 对应 B0。既有 Windows 10038 探针和断言不变。

**逐提交局部证据**：Linux、CPython 3.10.20，分别从两个提交导出独立源码副本，隔离 HOME、临时目录和 `.Flow`，仅叠加暂存的测试文件。A0：`test_close_before_connect_keeps_the_client_closed` 和 `test_terminal_client_close_stops_auto_reconnect`，`2 passed in 2.15s`。B0：下列 V0（含 A0 回归），`9 passed in 5.43s`。不据此宣称覆盖在途启动竞争、仍持端口时的延迟释放或原生 Windows。

```bash
# 各命令从相应源码根目录执行；P 指向项目 .venv 的 Python，可为绝对路径。
P=.venv/bin/python; U=test/unit/network_api; I=test/integration/network_api
# V0：基线产品可单独使用前三项和原有的 E2 节点；两条恢复的规格在测试步骤加入。
"$P" -m pytest -q "$U/test_TCP_client.py::test_close_before_connect_keeps_the_client_closed" \
  "$I/test_tcp_broadcast_alloc.py::test_palloc_returns_zero_without_manual_alloc" \
  "$I/test_tcp_broadcast_alloc.py::test_hand_free_port_removes_entry" "$I/test_executor_lifecycle.py"
```

**人审闸门**：只按这张表批准 A0/B0 为现状快照，不能据此放行完整 A/B。特别是 `hand_free_port()` 从按当前可变 `client_num` 删除改为仅消费本实例实际登记，调用方仍须按既有加锁约定使用底层方法。若不能接受 B0 在后续改动前会过早释放占用中的范围，就不把 B0 作为完整所有权修复发布。未获单独放行前不做后续原子、合并或推送。

## 1. 后续原子与数据流（拟审）

生产只改 `PyFlow/network_api/connect_tcp.py`；不删除文件，不改变构造器、公开返回值、例外边界（丢弃进度的对象仍拒绝启动）、线协议、磁盘格式、CI、依赖或覆盖率。可见行为变化以两张需求卡为限。三个主意图是 Server 启停、Client 连接、会话端口归属；最后一个分配器改动和各消费路径分别可验，预先拆开，避免一个原子跨所有传输路径。

| 产品原子 / 依赖 | 入口 → 关键分支 → 出口（同一产品文件内） | 产品 diff 预估 | 局部验证 / 固定规格 |
|---|---|---:|---|
| A1 Server 启动交接；基线收口后 | `start_TCP_Server` 登记候选 listener → bind/listen → 与 `stop` 互斥发布；停止方夺取候选/活动资源、在锁外 shutdown，线程/async accept 的迟到连接不再注册。已停对象跳过 bind，不等待普通启动调用返回。 | 40–100 行 | V1；A-S |
| A2 Client 连接交接与重试；A0 后 | `connect`/auto-reconnect → 为每次尝试登记候选 socket → 首次/固定端口回退/普通失败重试 → 仅在原对象仍开放时发布会话与已启动 reader；`close` 退休候选或当前会话，通知重试/协商，防止迟到 reader/resume 及状态写入。 | 70–160 行 | V2；A-C、A-R、A-F |
| B1 Client 范围模型；B0 后 | `/client_alloc_port_range` 携实际来源会话 → 第一条有效通知选择 OS/手动 → 取得不重叠的本地范围或保持待分配；`palloc/pfree` 记录具体端口的原登记 → 会话退休禁止新领用 → 最后一次归还触发注销。允许新旧范围共存，远端目标独立。 | 130–260 行 | V3；B-N、B-R、B-D |
| B2 普通上传与逐文件准入；A2/B1 后 | 单文件、`/multiple_file`、目录文件在提交处捕获会话 → 等文件名额/策略及范围 → 既有 `/file` 或 `/file_folder` 握手 → 等具体端口 → 既有数据发送/确认/归还。会话失效时按文件取消，绝不晚读新 `client_socket` 来首次提交。 | 70–160 行 | V4；B-U、B-Q |
| B3 会话内的接收、转发与重放；B1/B2 后 | 双向收件 listener（含复用此路径的协议传输）、转发首次/重放等候端口 → 绑定原会话的已有握手和 socket → 失效时退名额/端口，不跨会话开监听或删已接纳转发的进度。 | 70–170 行 | V5；B-I、B-F |
| B4 扩展借用的端口；B1 后 | 两端 `create_temporary_server/client` 区分实际借用、显式绑定及 OS 端口 → 创建失败回滚；控制会话断开不强关扩展 socket → 由扩展依约关闭后仅归还原借用。 | 35–85 行 | V6；B-X |

顺序：**只收口已有 diff → 停下审计划 → A1 → A2 → B1 → B2 → B3 → B4 → 总验收**。表中依赖是技术前提，不是绕过逐原子人审的并行授权。每个产品原子使用既有测试和隔离实测做局部验证；审查放行后，相应测试、文档各自提交，再开始下一原子。产品文件里不夹带测试、接口文档或变更记录。

### 实现约束

- 启动交接保护**资源和状态一起发布**，不是仅锁住 `running=True`。关闭取得终态后不再创建下一次尝试；已在途的系统调用可收尾。候选失败只关闭自己的 socket，不动后来者。持锁只做短状态交接，不跨 bind/connect、网络读写、用户回调、握手等待、线程 join 或 executor drain。保留 E1 池内/池外区别、监听回调和日志 flush 顺序。
- 范围通知必须传递实际来源会话；不能在阻塞返回后用 `self.client_socket` 猜归属。新会话的“尚无通知”、已选择 `NO_LIMIT`、手动待分配、手动可用是不同状态。旧登记停止发放但保留账目，具体借用归还到其原登记；没有借用不伪造归还。分配绕让只移动本地基址，兼容现有持久行与公开端口方法。本次不升级跨实例/进程锁文件协议。
- 等待使用会话取消信号与条件通知，检查条件与睡眠交接一致，处理伪唤醒；端口/名额归还、有效策略到达、断线/终态均能唤醒对应等待者。不持生命周期锁等文件锁或网络，控制 reader 不为等可用范围/文件名额而阻塞。借用已获准但 bind/开线程失败时也只有一个归还者；固定端口回退为 OS 端口时归还**申请的端口**，不是把 OS 端口记入旧范围。
- 扩展 `stop_event` 是关闭请求，不是端口已实际归还的证据。Client `create_temporary_client(bind_port=None)` 会 `palloc()`，当前 reader 收尾只 `close()`；B4 要配对归还且不改变扩展接口。Server 同名出站接口不传 `bind_port` 是 OS 选择，不凭方法同名推断已借端口。

## 2. 分原子的测试规格与验收命令

以下 V1–V6 是**计划，不是运行结果**。使用 V0 的变量。每个 V 均有已有回归入口；未在仓库出现的新时序先通过临时隔离探针按下表取证，在该产品原子审查后再以独立测试提交固化同一组断言。不能用整个文件的通过数代替右栏的证据。

| 编号 | 已有回归入口（`"$P" -m pytest -q` 后接） | 独立测试必须锁定的观察点 |
|---|---|---|
| V1 | `"$U/test_TCP_server.py::test_stop_before_start_keeps_the_server_stopped" "$I/test_tcp_async_server.py::test_stop_ends_the_event_loop"` | A-S：真实 socket + 同步闸门固定两种胜序，在旧启动调用仍可收尾时由新实例监听同地址；用真实连接、旧 fd、`running`、客户端表和退栈后的配置删除结果判定，不靠 sleep 猜中竞态。 |
| V2 | `"$U/test_TCP_client.py::test_close_before_connect_keeps_the_client_closed" "$I/test_listener_lifecycle.py" "$I/test_executor_lifecycle.py" "$I/test_tcp_transfer_resume.py::test_client_connects_when_its_configured_port_is_taken"` | A-C/R/F：连接前、TCP 已到而未发布、协商等待、两次尝试之间、固定端口回退前分别关闭；记录 peer 实际 accept 次数、当前 reader/登记。用可控等待边界核对 1 秒和提前取消，不用苛刻的毫秒断言。覆盖首次、自动重连、`is_wait_server=False` 与普通库外调用者。 |
| V3 | `"$I/test_tcp_broadcast_alloc.py" "$I/test_executor_lifecycle.py::test_late_client_close_preserves_reassigned_port_range"` | B-N/R/D：分别发送 `NO_LIMIT`、数值、重复、冲突、旧会话迟到通知；无通知不建数据连接。扣住旧范围的一个真实端口并断线；验证旧登记在、新范围不重叠、无范围时控制消息仍往返。释放前/后核对两份登记和锁文件，重复归还不改他人记录。移动本地范围后的重连 peer 仍是原 Server。 |
| V4 | `"$U/test_TCP_client.py" "$I/test_tcp_file_transfer.py::test_server_receives_file_from_client"` | B-U/Q：仅缺范围时不能发首握手；范围已领、具体端口全忙时握手先发生。分别卡在这两处及文件并发名额处后断线/重连，断言等待者退出、新 peer 无旧请求或数据连接、无新上传进度。单文件、批量 A 在传/B 等待、目录内文件，逐项核对字节、ACK、名额归还。 |
| V5 | `"$I/test_tcp_file_transfer.py::test_client_receives_file_from_server" "$I/test_tcp_transfer_resume.py::test_upload_resumes_after_a_reconnect" "$I/test_tcp_transfer_resume.py::test_forward_one_to_one_session_recovery" "$U/test_TCP_client.py::test_forward_pending_pairs_do_not_follow_a_replacement_socket"` | B-I/F：两个方向的接收者等本地端口时断开，确认无迟到监听；不增加接收方 20 秒定时器。未准入的一对一项取消，已准入的共享/独立转发在端口等待中断时保留原 FID 和已有行，按原 offset 与目标恢复；零正文也须真实最终 ACK。覆盖跟踪开/关、明文/加密，验证别名和通配绑定不改变 peer 匹配。 |
| V6 | `"$I/test_side_channel_records.py" "$I/test_listener_lifecycle.py"` | B-X：真实临时 listener 与出站连接，借用/显式/OS 三种来源；控制会话关闭后已建扩展连接仍由扩展处置。对实际借用，持有时登记在、释放后可复用；覆盖失败清理、正常关闭、迟到/重复归还。不能以调用了 `stop_event.set()` 或回调计数代替资源证据。 |

## 3. 测试与文档交付清单

下表路径以 `test/`、`docs/source/` 为根；没有列为“已有”的新文件仅在对应独立流程新增。API 文档记录调用者看到的阶段保证，既有 E2 报告保留历史失败，不以新计划反向改成通过。

| 原子 | 审查后的测试提交落点 | 审查后的文档提交落点 |
|---|---|---|
| A0 / B0 | 已有 `integration/network_api/test_executor_lifecycle.py` 的两条增量，分别对应重试/重复释放；保留原 E2 全部断言 | 本计划 §0 给出已验证范围；收口时分别记录 Client 外层重试边界、手动范围顺序释放边界，不能提前宣称 A1/B1 已完成（`Network_APIs/TCP_Client_APIs.rst`、`Port_Allocation/Port_Allocation.rst`）。 |
| A1 | `unit/network_api/test_TCP_server.py`、`integration/network_api/test_tcp_async_server.py` | `Network_APIs/TCP_Server_APIs.rst`、新增 `changes/tcp-control-session-lifecycle.md` 的 Server 段 |
| A2 | `unit/network_api/test_TCP_client.py`、`integration/network_api/test_executor_lifecycle.py`、`integration/network_api/test_tcp_transfer_resume.py` | `Network_APIs/TCP_Client_APIs.rst`、同一变更记录的重试与在途连接段 |
| B1 | `integration/network_api/test_tcp_broadcast_alloc.py`、`integration/network_api/test_executor_lifecycle.py` | `Port_Allocation/Port_Allocation.rst`、`Network_APIs/TCP_Client_APIs.rst`、同一变更记录的会话归属段 |
| B2 | `unit/network_api/test_TCP_client.py`、`integration/network_api/test_tcp_file_transfer.py` | `Network_APIs/TCP_Client_APIs.rst`、`File_Transfer/File_Transfer.rst`、同一变更记录的等待与取消段 |
| B3 | `integration/network_api/test_tcp_file_transfer.py`、`integration/network_api/test_tcp_transfer_resume.py` | 两份 TCP API、`File_Transfer/File_Transfer.rst`、同一变更记录的双端接收/恢复段 |
| B4 | `integration/network_api/test_side_channel_records.py` | 两份 TCP API 的临时连接章节、`Port_Allocation/Port_Allocation.rst`、同一变更记录的扩展借还段 |

最终报告拟落 `docs/plans/tcp-control-session-lifecycle-report.md`：按 A-S 至 B-X 列覆盖/部分/未覆盖、实际命令、退出码、无明文泄漏的连接/端口证据。Linux、Windows 实机结果分列；模拟错误或历史 E2 通过不能替代本次 Windows/macOS 验收。不自动创建提交以外的合并、推送或改 CI。本计划的下一道闸门是当前 diff 的独立提交审查，而不是开始写 A1。
