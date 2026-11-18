# 传输控制生命周期实施报告

## 本轮 C3 替代方案：已实现，局部运行证据如下

**当前状态：用户批准的数值请求、原 owner 收尾、独立并行恢复已实现并取得局部运行证据。** C1/C2 保留。最终四份 unit 文件 228 passed；转发集成整批 52 passed、3 个旧内部断言失败，迁移后专项 3 passed；新全局控制专项 3 passed，独立程序 3 组真实传输通过。上述是分次运行，不伪称最终单次整批全绿，不声明全部矩阵或 C6 总验收通过。旧 C3a/C3b 查询/强制 teardown/串行轮次仅为撤回历史；C4–C6 未实现。

### 本轮契约与更改路径

- `PyFlow/network_api/connect_tcp.py`：仅数值 `/resume_trans`，已删除 `?`/`None` 查询及其消费者。Receiver 旧接收 owner 和文件关闭后读取实际落盘 offset；Server 校验实际 Target socket、child/origin/target/route，原值登记，不查询兄弟目标、不用发送计数代替。
- Server 等同一原 route 全部旧 listener、握手、上/下游、reader/writer 和 finalizing owner 自然收尾，包含 finally；不因请求强制 teardown 或自动 start。等待不持全局锁、不阻塞控制 reader/event loop，不影响其他 route/fid。
- 此后按每 Target 独立 Source→Server→Target 流、实际 offset、运行时进度、资源和失败处理并行恢复；稳定 upstream/child fid、原完整路径、五项进度行及 relay schema 不变。同实际 socket+fid 仅端口握手短串行，绝不锁正文或最终 ACK；晚来 Target 不等已有大文件正文/ACK。
- C1 实际会话 owner 和 C2 Source 全局 pause/start 仲裁及本地背压保留；共享控制状态有多个 live owners，最后一个退出才释放。中间 Target 的真实 ACK 仅更新自身 completed，不伪造整个 route ACK；累计全部 accepted Target 真实 ACK 才能整任务成功，Source 整任务上传行不得被单流提前删除。
- Source 离线保留 pending。Target 断线丢弃旧实际 socket 的 pending，不借给替换会话；重连等旧接收 owner 收尾后读新实际 offset 重发。单 Target 失败不取消兄弟、不自动重试。
- 本轮运行的回归路径：`test/unit/network_api/test_resume_matching.py`、`test_fid_routes.py`、`test_transfer_control.py`、`test_transfer_handshakes.py`，以及 `test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_transfer_control.py`；具体选择器与分次结果如下。
- 文档范围仅七份既有文件：`docs/plans/transfer-control-lifecycle-requirements.md`、`transfer-control-lifecycle-plan.md`、本报告；`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Client_APIs.rst`、`TCP_Server_APIs.rst`、`docs/source/changes/transfer-fid-routes.md`。未创建文档。
- 最终 owner 边界：发送恢复指令返回 False/异常时，若 Source 已经消费指令并使 relay 绑定 receive record，调度 worker 仍等该 owner 的 finally，不提前释放占用；迟到握手不能采用已被释放的 reservation。Target 重连的新实际 socket 可以保存新数值 offset 等旧 Target owner 退出，同一会话的 active 重复请求仍拒绝；退出后只消费已收到的 pending，不生成重试。
- 显式 Source 发起的恢复也登记独立 active Target owner，失败后可再次合法发起；恢复 Target 握手使用准入 reservation 的实际控制 socket，不按 peer 借替换连接。

### 本轮路径证据与未覆盖边界

| 路径 | 必须取得的证据 | 本轮状态 |
|---|---|---|
| Receiver 数值 offset、零/空文件/Target EOF | 实际 offset、原路径、内容及真实 Target ACK | 数值解析、零 offset、不同落盘 offset、file/folder 的 Target ACK-loss EOF 零正文恢复已验证；全 Target 完成后的上游 EOF 再确认 C4 未实现 |
| 原 route live/paused/finally | 不拆活跃腿，原 owner 完整收尾前不建恢复流 | 四组独立正文 + 三组全局控制事件已验证 live/paused/finally；其他握手/EOF 窗口不扩大声明 |
| 不同 offset、部分完成及晚到 Target | A 正文或 ACK 被阻塞时 B 可建流并取得真实 ACK | 四参数真实集成与三组独立程序覆盖，A 再失败不取消 B |
| 多 owner 全局 pause/start/背压 | 所有 Source 流受统一仲裁，单 owner 退出不清共享状态 | 三参数全局控制通过；本轮未单独扩大到全部流量压力矩阵 |
| Source 离线、Target 会话更新及单流失败 | pending、旧 owner、新数值请求、兄弟继续，无自动重试 | Source 离线/原端点回连、stop、旧会话交叉、A 再失败/B 完成已通过；新 Target 会话 pending 另有单测，未扩大到多进程重启完整矩阵 |
| ACK 聚合及资源身份 | 单 Target 不伪造整任务成功，旧 finally 不删新 owner | 四参数正文、三参数全局控制与独立程序均保留真实 ACK/资源断言 |
| 明文/加密、thread/async、tracking off/持久化 | 真实 I/O、稳定 fid/path/schema；既有行恢复 | 三组独立程序、三组全局控制以及 route 持久化单测；不宣称所有参数笛卡尔积 |

实现后运行的命令与结果见下节。以上结论来自本轮记录，不借旧 C3a/C3b 或 C1/C2 历史通过数。

### 本轮 S4 独立程序证据

一次性程序从真实保留进度行和已落盘的部分文件发起恢复，不调用 pytest 测试函数。Source、Server、两个 Target 均为真实 TCP 实例；只用事件固定 A 的正文或 ACK 门槛，文件、socket、加密和实际 ACK 均走生产实现。B 在 A 尚未放行时才提交请求，先完成文件与真实 Target ACK。该程序验证独立恢复流；原活跃 route/finally barrier 由本轮集成契约验证，不以预置的保留行冒充断线运行证据。

| 实际参数 | A 被阻塞的位置 | upstream fid | 稳定 child fid（A / B） | workers / 最终存活 |
|---|---|---:|---|---|
| 明文 thread、localhost 别名 | 正文首次读取前 | 5942851624 | 4697782172 / 8992749468 | 33 / 0 |
| 加密 thread | Target 最终 ACK 写入前 | 12672816124 | 19156548134 / 23451515430 | 72 / 0 |
| 加密 async、wildcard、tracking off 既有保留行 | 正文首次读取前 | 10342202172 | 20511536392 / 24806503688 | 72 / 0 |

三组实际 offset 都是 A=8192、B=98304；目标沿用各自 `original/nested/payload.bin`。每份完整文件为 262307 字节，SHA-256 均为 `8fb29a369e87c3654ab4e692caabad6a31ac611dda80b64ee1e04819e022812b`。B 实际 ACK 后 A owner/socket 仍活跃、Source 原任务行对象仍保留；Source 两条恢复流使用不同 socket，运行时进度不覆写任务行。最终 Target ACK 次序 B→A，Source 结果 False→True：中间流不伪造整任务成功，全部真实 ACK 后清理 route/index/recovery/进度。实际控制 socket 未替换，所有 worker 收尾。

```bash
export PYTHONPATH="$PWD:$PWD/test/integration"
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate body --alias
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate ack --encrypted
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate body --encrypted --async-server --wildcard --untracked
```

上述三条以 `&&` 顺序执行，整体退出 0，31.69 秒。临时实例、文件和密钥目录由 TemporaryDirectory 清理；一次性程序已删除。不抓包，不扩称所有连接的安全属性已审计。局部 pytest 结果另列，不借历史通过数。此程序先于最后的异常 owning 清理/新 Target 会话 pending 修正运行；最终修正由后续窄回归验证，不把程序结果冒充该错误交叉的运行证据。

### 本轮局部回归记录

- 最终 owner / 手动零 offset / 独立正文 / 全局控制的组合命令实际结果：**45 passed、2 failed，78.37 秒，退出 1**。`test_fid_routes.py` 的请求发送失败后 owner 保留、同/新 Target 会话 pending，以及独立正文的四参数组合、零 offset 路径通过；失败均在新全局控制场景。
- 明文场景在关闭旧 Source data socket 后才等待其 start 通知，关闭线程可先释放旧控制 owner、清掉发送队列。修正为真实 start 通知确认后再 shutdown，再放正文门；不弱化通知断言。
- async/untracked 场景在接收 worker 已选定 `progress=None` 后才塞进字典的行，不是该 worker 持有的旧行；异常路径会按未跟踪传输删除部分文件，Server 注入的 offset 与 Receiver 行不一致。修正场景初始化为接收注册时就保留真实五字段行，关闭后同时核对实际部分内容和行 offset=1024；继续验证 tracking off 恢复既有行，不把无保留依据的任务冒充可恢复。
- 上述场景修正后，仅运行该函数的三个参数：**3 passed，30.20 秒，退出 0**。保留真实 pause condition wait、不同 socket、各自正文、B 先 ACK、最后 owner 释放、最终内容和资源清理断言；未删/skip 用例或降低阈值。组合失败记录保留，不伪称组合整批通过。
- 文档机械检查：七份现有文档、六个相对文件链接、零缺失目标；该检查不替代运行验证。
- 最终四份 unit 文件命令：**228 passed，0.78 秒，退出 0**。随后的转发/relay/并行选择器整批：**52 passed、3 failed、100 deselected，337.40 秒，退出 1**。失败全部是 `test_disconnected_relay_target_cannot_queue_behind_live_source` 的三参数场景：错误要求原始活跃 Source 的 `_resume_sends` 为空，但该表也持有原始发送 owner。
- 移除该实现细节断言，改为核验原 Source、Server upstream 和存活 Target 的实际 pause 状态仍为 True；保留错误/断线请求拒绝、无 Server recovery、原 socket/owner 不变、原 worker 仍运行、start 后存活 Target 完整内容及最终资源清理。只运行修正场景：**3 passed，19.54 秒，退出 0**。未再次运行整批，故不写“最终整批全绿”。
- 最早启动的六文件完整回归在运行时重置后无法取回 job/输出，不作为任何通过数来源。一次专项通过后的输出展示曾误用 Python `await sh(...)`，宿主报 TypeError；随后同步保留输出如上，不把宿主错误冒充产品失败或虚构统计。

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_recovery_waits_for_old_finally_and_runs_targets_independently test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_with_zero_offset_reuses_existing_route test/integration/network_api/test_tcp_transfer_control.py::test_parallel_recoveries_wait_for_old_owner_and_share_source_pause -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py::test_parallel_recoveries_wait_for_old_owner_and_share_source_pause -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py test/unit/network_api/test_transfer_control.py test/unit/network_api/test_transfer_handshakes.py -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_transfer_control.py -k 'forward or relay or parallel_recoveries' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py::test_disconnected_relay_target_cannot_queue_behind_live_source -q --tb=short
```

### 当前风险与未实现边界

- 高风险路径是原 route 最后一个 owner 的 finally barrier、多 Target 同 upstream fid 的端口结果关联、独立进度与 owning 清理、共享全局控制最后释放及累计真实 ACK。必须以事件固定正文/ACK 阻塞交叉，不能只看最终文件存在。
- 原 route 暂停、旧 ACK 等待或 finally 未结束会延迟首次恢复；这是批准的自然收尾边界，不用超时抢占/强制拆腿掩盖。该 barrier 不得再次挡住后续独立恢复流或晚到 Target。
- 同控制会话、同 fid 换腿时迟到 pause/start 的严格代次隔离仍暂缓；不新增 leg_id、换 fid、wire 字段、schema、重试、CI 或依赖。
- ACK 本地写成功与对端应用已收到之间的不确定窗口不以新结果通道或 ACK-of-ACK 解决。C4 全 Target 完成后的上游 EOF 再确认、C5 自目标拒绝、C6 总体验收未实现。
- 本轮运行平台为 Linux、Python 3.10；native Windows、其他 Python 版本、多进程重启的完整矩阵及转发中途 rekey 未覆盖。具体明文/加密/thread/async 参数见本轮输出，历史平台结果不充数。

---

以下 C1/C2 及旧 C3a/C3b 保留其交付时状态与真实历史输出；旧 C3 的现行授权及验收状态以上节为准。

## C1：实际控制会话取消与旧 owner 收尾

状态：**C1 实现及局部验收完成；用户已要求开始 C2，C1 获阶段推进**。下述记录是 C1 的局部证据；当前 C2 状态见后续章节，未创建 git 提交、合并或推送。依据：[需求卡](transfer-control-lifecycle-requirements.md)、[计划 C1](transfer-control-lifecycle-plan.md#8-原子拆分固定次序逐项人工审查)。

### 意图、更改路径与解决的问题

- `PyFlow/network_api/connect_tcp.py`：删除 Server/Client 两份 `_drop_stale_transfers`。重连不再按 peer 扫描进度表并关闭扫描时的当前 socket，避免迟到清扫误伤新会话。
- `_retire_transfer_session` 统一取消实际控制 socket 的握手、listener/data socket、控制状态及未绑定接收请求；资源登记保留至 worker 真正收尾，`wait_socket` 等待所属资源释放。关闭 socket 在 registry 锁外执行。
- `_sending_transfer`、`_start_resume_send` 保存准入时控制 socket；`_wait_transfer_session` 仅等待捕获的原 send state/receive record。旧 finally 继续按对象身份释放，不删除替换后的 owner。
- Server 在会话退役时保存旧 owner 快照，后台排空后删除快照；重连 worker 固定新准入 socket，旧 worker 不再按 peer 取得后来替换的连接。`stop` 在 client 锁外取消和关闭连接。
- Client 先退役原 socket，再启动新控制 reader；后台恢复等待原 owner，不再在 reader 启动前等待。连续重连的恢复 worker 按原线程链排空。转发恢复显式传入捕获的 `control_socket` 到 `_forward_one`，避免旧任务借新 socket 发握手并形成等待环。
- `test/unit/network_api/test_transfer_handshakes.py`、`test_resume_matching.py`：保留实际会话取消、同 fid 占用和旧 owner 等待契约，增加旧清理后新 socket 仍可收发及迟到释放不删新 owner 的断言。
- `test/integration/network_api/test_tcp_transfer_resume.py`：将原 sweep 用例迁移为实际控制会话退役；相同 fid 的不同会话保持独立；暂停腿退役后用原 fid 真实恢复，比较内容并确认发送端成功清理。
- `test/integration/network_api/test_tcp_transfer_control.py`：事件固定“旧转发恢复 worker 已准入但尚未执行 → 新 connect → 放行旧 worker”，覆盖明文别名、加密 thread、加密 async/wildcard/tracking off。断言旧任务仅使用原 socket、失败保留原上传行、新会话可继续收消息。

数据流：实际控制会话退役 → registry 标记关闭并取消所属资源/握手 → 清除所属运行时控制状态 → 捕获旧 owner → 后台等原资源及 owner 收尾 → 经固定的新会话发恢复请求 → 原 fid/路径恢复 → 真实最终 ACK → owning 清理。

### 局部验证

V1 最终结果：**112 passed、97 deselected，240.29 秒，退出码 0**（Linux、Python 3.10.20）。筛选范围与计划相同；97 项为既定 `-k` 未选中，不是新增 skip，也不代表全套通过。

失败记录：首轮输出包含 2 个 fixture 错误，300 秒超时，不能记为通过。窄定位运行得到 17 passed、1 error：新增加密重连用例未等待新握手完成便结束，teardown 发现 `_crypto_exchange_thread`、`_crypto_wait_server_ready` 尚未退出。为新控制连接双方补上真实 `_wait_control_secure` 就绪前提，保留原零存活 worker 断言；随后执行下面完整 V1 选择并通过，未修改生产加密流程或延长 fixture join 来掩盖错误。

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_handshakes.py test/unit/network_api/test_TCP_client.py test/unit/network_api/test_TCP_server.py test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_transfer_resume.py -k 'disconnect or reconnect or session or owner or stale or cleanup or resume' -x -vv --tb=short
```

独立程序不调用 pytest 测试函数，使用真实控制/data socket、加密、文件 I/O 和最终 ACK；Event 固定交叉时序，方法包装仅观察真实执行或设置旧 worker 放行点。临时脚本 `/tmp/pyflow-transfer-lifecycle-smoke.py` 已在验证后删除，运行时临时文件与密钥目录也已清理。

```bash
# 每项分别运行 --alias、--encrypted、--encrypted --async-server --untracked
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case direct-leg <参数>
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case relay-leg <参数>
```

- S1 会话部分：3 组参数各完成上传、下载，共 6 次同 fid 恢复。fid 为 810/811；实际原文件 offset=0，原完整路径保持，旧 control/data socket 均已关闭，新控制端点保持，发送端实际最终 ACK=True，成功后所属进度行删除。加密 async 使用 wildcard 绑定，并在产生保留行后关闭 tracking 再断线/恢复。
- S1 完整内容 SHA-256：`f2a6736d846749945c57b953b657f9b483c8c4e842b590e00a0cdd0574c20c33`。明文、加密 thread、加密 async 的独立程序退出码均为 0；各自 35、74、73 个观察到的 worker 最终存活数为 0。
- S2 会话部分：3 组参数均证明旧转发恢复 fid=820 的 worker 固定原 socket、返回 False、原上传行保持且不建立伪 route；新控制连接继续完成一次真实转发，最终 ACK=True、内容一致。新转发按原规则分配自己的 fid，不把该新任务报告为 fid=820 的恢复成功。
- S2 内容 SHA-256：`5bc85cfbbe30eb61c8fcbbacdd78975ce49973c0fbf69771c03258ac793c7c03`。3 组程序退出码均为 0；各自 23、62、62 个 worker 最终存活数为 0。
- 独立程序首轮加密 S1 的观察器误将公钥传输（fid=None）的 ACK 计为业务恢复结果，断言失败、退出码 1；按业务 fid 过滤观察后，只重跑受影响的加密 S1 参数组并通过，未以此修改或绕过生产传输/加密路径。

### 范围、不变量与未覆盖项

- C1 对应 P01–P07、P16/P19/P20 的会话生命周期和回归保护。独立程序不穷举全部组合；V1 实际通过了 online resume 的各方向/加密/async/tracking off、文件/文件夹失败后恢复、实际落盘 offset、进度保存/重载、EOF/ACK、稳定转发 route/child、旧 socket 握手隔离及 localhost/wildcard 身份回归。不能把持久化重载用例说成已完成独立多进程重启矩阵，也不将零 offset 独立程序当作所有恢复路径的替代证据。
- 不改变稳定 upstream/child fid、pause/start 四 token、端口响应、`file_transfer_mode` 签名、进度/快照 schema、普通 busy 规则或真实 ACK 判成功规则。未改 CI、依赖、coverage 策略，未删用例、跳过路径或弱化断言。
- 问题二继续暂缓：同一控制 socket、同 fid 换腿时迟到 pause/start 的严格代次隔离未实现；未新增 leg_id、换新 fid、协议字段或自动重试。
- C1 交付时未实施 C2 全局暂停、C3 route 定向重建/offset 查询/逐 Target 顺序恢复、C4 EOF 补确认、C5 自转发拒绝、C6 总体验收，不能据 C1 结果宣称这些行为已完成；C2 的后续结果另列。
- 未运行 native Windows、其他 Python 版本或跨进程并发进度写入验收；本轮未做抓包，不将加密真实传输等同于全协议安全审计。
- C1 会话生命周期契约回归作为规则沉淀；变更记录见 `docs/source/changes/transfer-fid-routes.md`。本轮用户明确要求开始 C2，作为阶段推进授权。

## C2：Source 仲裁全局暂停、writer 排空与独立背压

状态：**C2 实现及局部验收完成，等待人工审查**。V2 本体通过，迁移的单个完成契约用例修正观察时序后通过；S3 独立真实多目标程序通过。C3–C6 未开始，不自动提交、合并或推送。

### 更改路径、问题与数据流

- `PyFlow/network_api/connect_tcp.py`：relay 控制状态注册时绑定运行时 route；实际会话/owner 校验后，Server 将本地或 Target 请求按 upstream fid 转交 Source，而不自行确认。Source 的既有串行 sender 决策入口决定最终状态；Server 再按各 child fid 投影通知，Target 不回送通知，避免回包循环。
- relay 请求不按当前确认值静默去重：另一 Target 的相反请求可能仍在途；保留 confirmed/last_requested，覆盖未确认 pause→start。握手完成前的请求与预置暂停先登记，Source 就绪后再发送；不增加协议字段或持久化状态。
- 删除 relay writer 的用户暂停等待；暂停只让 Source 下一块前等待，Server 已收正文继续排空。删除自动背压伪装成用户 pause/start 的发送，保留原 reader 高/低水位硬门槛与 TCP 流控；低水位不再意外解除 Source/Target 的用户暂停。
- `test/unit/network_api/test_transfer_control.py`：验证 Source 决定前 Server 不确认、相反请求有序裁决、child 映射、通知不回包、旧 owner/错误 socket 拒绝及握手前反向意图。
- `test/integration/network_api/test_tcp_transfer_control.py`：用真实 socket 固定在途块、writer 队列、迟到通知和压力时序；覆盖 Source/Server upstream/Server alias/两个 Target、异端 start、未确认反向请求、错误 peer/type/role、加密帧、目标退出及资源收尾。
- `test/integration/network_api/test_tcp_transfer_resume.py`：原部分完成用例不再用已废弃的独立 Target pause 卡 writer；改为用事件延迟第二 Target 的真实最终 ACK，仍验证完整内容、完成集合、route/index 保留，以及最后真实 ACK 后 Source 成功。未弱化成功条件。
- `docs/source/File_Transfer/File_Transfer.rst`、`Network_APIs/TCP_Server_APIs.rst`、`TCP_Client_APIs.rst`、`changes/transfer-fid-routes.md`：同步全 route 用户控制、重复登记语义、排空行为和背压边界。

数据流：参与者 API/控制帧 → 实际会话及当前 owner → Server upstream 请求队列 → Source 串行决定 → Server child 通知队列 → 各参与者确认；数据流只有 Source 等待用户暂停，Server writers 持续排空，reader 独立受本地压力门槛约束。

### 局部验证与真实运行证据

V2 本体实际结果：**225 passed、1 skipped**（Linux、Python 3.10.20）；skip 是既有明文 fixture 下不适用的 rekey 用例，两个加密 rekey 分支通过，未新增 skip。首次将下述单个迁移用例一并运行，总结果为 225 passed、1 skipped、1 failed，458.43 秒、退出码 1；失败是用例在第二 Target 到 ACK 时过早读第一个 Target 的文件。把完整内容断言移到第一个 Target 的真实 ACK 更新 completed 集合后，原内容/完成集合/最终 ACK 断言全部保留；仅重跑该用例，**1 passed，2.74 秒，退出码 0**。生产代码不因该测试时序问题修改。以下列出实际命令，不将分次验证声称为一次全绿组合运行：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_is_removed_only_after_all_real_targets_complete -x -vv --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_is_removed_only_after_all_real_targets_complete -q
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case global-pause <参数>
```

S3 不调用 pytest 测试函数，不替换文件/网络/加密主路径；事件只固定真实执行顺序，分别运行 `--alias`、`--encrypted`、`--encrypted --async-server --untracked`，退出码均为 0。每组有两个 Target，逐个由 Source、Server upstream、Server child、Target A/B 发 pause，再由另一 Target start；所有活跃逻辑确认最终一致、data socket 身份不变。临时脚本已删除，运行时临时文件与密钥目录已清理。

| 参数 | upstream fid | 两个 child fid | 观察到的 worker / 最终存活 |
|---|---:|---|---|
| 明文、localhost 别名 | 7352661538 | 7098315376 / 11393282672 | 80 / 0 |
| 加密、thread | 9451137674 | 18717822684 / 23012789980 | 119 / 0 |
| 加密、async、wildcard、tracking off | 11093087600 | 21104272900 / 25399240196 | 118 / 0 |

每组在暂停期间均向两个 Target 排空实际在途正文 1024 字节；Source 在下一块等待。压力 high-water=64、实际 peak=2094、单次 max_enqueue=2048，满足 peak≤high-water+max_enqueue；用户 start 后 reader 仍重入压力等待，读次数不变。排空解除压力后，用户暂停仍保持。最终两个文件完整一致、Source 实际最终 ACK=True、所属 route/index 清理。内容 SHA-256：`9a727e7d8b144a2f9ce213746ccc0fbcee65e58cd86c80b43607464789440ee6`。

### 风险、范围与规则沉淀

本阶段覆盖 P08–P11 及 P20 的对应控制矩阵；不改 fid、四 token、端口消息、公开方法签名、进度 schema、普通 busy 或真实 ACK 规则。自动压力不再投影为用户 paused；不宣称阈值约束全部进程 RAM，也不承诺撤回 socket 在途字节。问题二继续暂缓；C3 重建/顺序恢复、C4 EOF 补确认、C5 自转发和 C6 总体验收未实施。native Windows、其他 Python 版本、relay 中途 rekey 的独立场景未验证。测试保留普通控制/rekey、失败/收尾回归；未改 CI、依赖、coverage。全局确认、暂停排空、背压释放不清用户暂停成为永久契约检查。


## 旧 C3a / C3b（已撤回历史）：收尾后 offset 查询与 route 定向顺序恢复

用户授权推进 C3 后，因原阶段超过 500 行警戒线，选择拆成 C3a（查询协议及接收端收尾后回复）和 C3b（route 定向重建/顺序调度），授权本轮实现和局部验收。两个单元按意图审查，共用查询契约；未创建 git 提交，不自动进入 C4–C6。必要回归迁移使总差异仍大于 500 行，不将两个名称冒充两个已审查的小提交；最终规模及提交划分仍需人工审查。

状态：**旧 C3a/C3b 查询、强制 teardown 和串行活跃方案已撤回，不是当前契约或本轮完成结论**。下列仅保留旧方案当时的真实实现与输出。用户现已批准数值 offset、原 route 全旧 owner 含 finally 自然收尾、每 Target 独立并行流的新需求和计划；本轮状态见报告首节，尚待统一验证，不借以下历史结果声明通过。

### C3a：更改路径、解决的问题、数据流

- `PyFlow/network_api/connect_tcp.py`：`parse_resume_command` 查询分支返回 `(fid, None, origin, target)`，`format_resume_line` 将 `None` 格式化为 `?`；数值五 token 格式不变。Server 拒绝收到的查询作为 Receiver 恢复请求；Client 查询分派与数值发送分派分开，全部消费者区分 offset 值域。
- `TCP_Client_Base._query_resume_offset` 校验实际 Server socket、自身 target、保留 download fid/origin；后台等待旧接收 owner 与实际文件关闭，再经既有 secure/send 路径按磁盘大小回复数值 offset。未知/已移除的接收行不虚构零 offset，持久化模型不加字段。
- `test/unit/network_api/test_resume_matching.py` 保留数值、shell-quoted endpoint、边界和错误格式回归，并增加 `?` / `None` round-trip 与非法查询。真实查询/文件收尾证据来自下述 C3b 消费者场景，而不是源码/调用次数断言。

数据流：Server 定向查询 → Client 实际会话与保留身份校验 → 后台等待旧文件关闭 → 读原完整路径的实际大小 → 既有数值回复。控制 reader 不等待自己需要分派的握手/加密消息。

### C3b：更改路径、解决的问题、数据流

- `connect_tcp.py` 用 `_relay_recoveries[fid]` 保存一个运行时重建 owner：`route/old/queried/ready/active/running/wake`；替代旧 `_pending_originator_resumes`。实际控制 socket、原 Source、Target、child fid 和 route 先校验，再登记动作；重复请求不重复 teardown，active Target 拒绝重复。
- `_cancel_relay` 只取消捕获的该 route listener/data socket 与握手，不取消健康控制会话。旧 reader 停止入队，writers 退出/排空，最终关闭数据资源、清空捕获引用并置 `done` 后才释放 receive reservation，避免新轮越过旧 finally。队列不复用，不删除 Target 已落盘文件。
- `_run_relay_recovery` 查询在线未完成 Target；查询发送失败回滚 queried 标记，以便下一次合法触发重查。按合法 offset 回复的 FIFO 选择在线 Target；每轮仅一个上游和一个下游，保留原 upstream/child fid、route 完成集合和完整目标映射。
- 协议驱动的 Client 恢复经 `_resume_on_socket(..., drain_forward=True)` 固定实际准入 socket，并等待旧 Source sender 收尾；公开 `resume_trans` 的普通/忙对象拒绝语义保留。Server 也把实际 socket 传至直接恢复和 route 登记边界，不在后续后台工作里重新按 peer 借新控制连接。
- 中间轮只完成该 Target；其他 Target 未完成时沿用原 error/结束语义，不回整体成功 ACK，保留 Source 上传行和 route。Source 离线保留 ready；Target 离线不阻塞可服务兄弟目标；合法请求/重连重新触发，不增加定时或忙重试循环。
- `test/unit/network_api/test_fid_routes.py` 迁移旧 pending 内部断言，覆盖错误/旧实际 socket、origin/target/fid、单重建 owner、离线保留、并发回复仅一次真实 Source 请求，以及查询发送 False 后同 socket 合法重查。
- `test/integration/network_api/test_tcp_transfer_resume.py` 将已废弃的 busy-skip 恢复用例迁移为真实共享 relay 中断：A 实际写入 8192 字节后仅断数据 socket，B 实际写入 24576 字节，Source 活跃并确认暂停；A 的恢复触发旧 route teardown。A 真正 ACK 门槛内放行 B 的查询回复，证明 B 已 ready 但未启动；随后只按 A→B 重放，检查两份完整内容、稳定 child、`False/False/True` 原尝试/中间轮/最终轮结果及资源清理。
- 原 ACK-loss file/folder 用例保留异常/False 两种首次 ACK 故障、EOF 零正文、完成集合、原嵌套路径和真实终态 ACK。用事件固定查询回复顺序，不再假定最初请求者天然优先、也不要求对每个兄弟目标手动再次请求。refused-listener/reload 用例保留未完成索引和持久化证据，不将触发请求的 0 冒充已确认 disk offset。
- `test/integration/network_api/test_tcp_transfer_control.py` 同步离线 Source、完成兄弟、旧 owner 释放和新恢复的契约。`File_Transfer.rst`、`TCP_Server_APIs.rst`、`TCP_Client_APIs.rst` 和 `changes/transfer-fid-routes.md` 同步新行为和边界。

数据流：合法 Receiver 数值请求 → 单 route 重建登记 → 旧 data legs/队列/worker 完整收尾 → 各 Target 关闭文件后 offset 回复 → FIFO 单 Target 新数据腿 → 更新真实 completed → 下一轮或最终真实 ACK 后 owning 清理。独立 fid 不参与 teardown。

### 实际局部验证与失败处理

运行环境：Linux，Python 3.10.20。以下是分次运行，不声称一次完整 V3/V6 全绿，也不相加成唯一用例总数：

| 实际命令/范围 | 结果 |
|---|---|
| 原 V3 `fid_routes + resume + control -k 'forward or relay'` | 48 passed、4 failed、133 deselected，270.62 秒；退出 1 |
| `test/unit/network_api/test_fid_routes.py -q`（新查询失败用例添加前） | 36 passed，0.14 秒；退出 0 |
| `test_tcp_transfer_control.py -k 'forward or relay' -x -vv --tb=short` | 36 passed、54 deselected，233.88 秒；退出 0 |
| 两份 unit（resume_matching、fid_routes）加 ACK-loss、refused-listener、serial-recovery 三个函数 | 81 passed，28.22 秒；退出 0 |
| `test_tcp_transfer_resume.py -k 'forward or relay' -q --tb=short` | 13 passed、46 deselected，46.22 秒；退出 0 |
| `test_tcp_transfer_control.py -k 'relay_offline or reconnect_keeps' -q --tb=short` | 9 passed、81 deselected，59.86 秒；退出 0 |
| 新 `failed_offset_query...` 与真实 serial-recovery 两个函数 | 2 passed，6.72 秒；退出 0 |
| 最终 socket 固定边界：fid_routes/control 的 forged/stale/query-failure/online-resume/disconnected-Target/old-session 子集 | 15 passed、112 deselected，70.72 秒；退出 0 |

原 4 个失败：零 ACK file/folder 两个用例仍按旧手动调度契约计数，实际合法 B 回复可先于 A，不能由 calls=3 推断双 sender；一个 refused-listener 用例引用已删字段；新顺序恢复用例缺事件屏障，取 route 前文件已完成。迁移契约和真实事件顺序后通过，未删除路径、弱化内容/ACK 断言、修改 CI 或 coverage。另修复 review 发现的 receive reservation 早于 relay `done` 释放，以及失败查询残留标记；检查 Python 引用的 LSP 仅有 Ruff、`textDocument/references` 不可用，已报告工具限制，消费者由仓库调用点和运行验证覆盖。

精确重跑命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_ack_loss_retains_state_and_resumes_original_ids test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_survives_a_refused_target_listener_and_reload test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_tears_down_live_route_and_replays_targets_serially -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k 'forward or relay' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py -k 'relay_offline or reconnect_keeps' -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py::test_failed_offset_query_can_be_requested_again_on_the_same_session test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_tears_down_live_route_and_replays_targets_serially -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_control.py -k 'forged or stale or failed_offset_query or online_resume or disconnected_relay_target or reconnect_keeps' -q --tb=short
```

### S4 独立真实程序：已取得的运行证据

不调用 pytest 测试函数，不 mock 文件/网络/加密主路径；仅用 Event 固定真实写入、ACK 与发送 owner 时序，并真实关闭故障数据 socket。Source 同时有另一个普通上传 fid 在正文门槛内保持活跃；恢复期间该 socket 仍打开，最后取得其真实 ACK 且内容一致。

| 场景 | upstream fid | 两个稳定 child fid | 恢复 offset | workers / 最终存活 |
|---|---:|---|---|---|
| 明文 live、localhost 别名 | 10865447346 | 6493365684 / 10788332980 | 8192 → 24576 | 48 / 0 |
| 加密 thread、Source paused、Target B 离线再原端点回连 | 15823586500 | 17644686304 / 21939653600 | 8192 → 24576 | 110 / 0 |
| 加密 async、wildcard、EOF 等待、保留行 tracking off | 15091650932 | 18378608972 / 22673576268 | 8192 → 150851（EOF 零正文） | 90 / 0 |

每组 Source 原尝试/中间轮/最终轮实际 ACK 结果为 `False/False/True`；两份文件 byte-for-byte 等于 150851 字节原内容，SHA-256 均为 `0a45ceaa5fac82dd386c144b9985cd3985b91a76aba8e5fbac335315c9459f14`。原 relay 捕获的 6 个 socket 登记全部关闭，原 Source owner 已退出；route/index/recovery 清理，Source 与未断线 Target 的实际控制 socket 保持。无关 fid 依次为 6570480050、11528619204、10796683636，最终 ACK=True。EOF 组实际以保留行完成恢复，不在 tracking off 时创建新行；B 从 total=150851 恢复，仍经原真实 header/ACK，而不是因大小相等跳过确认。

```bash
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode live --alias
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode paused --encrypted --offline-target
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode eof --encrypted --async-server --untracked
```

独立程序的首次 live 运行因测试注入只 shutdown 而未 close 接收 data socket，仍可读到内核已排队字节，固定 8192 的断言失败；改为真实 shutdown+close 后通过。首次主动关闭 B 控制 socket 的重连没有保持原本地端点；改为由 Server 发 FIN、Client 被动退出并释放旧 socket 后，原端点回连通过。它们是程序时序/断线注入修正，不修改生产内容或网络算法。

首次 EOF 独立程序等待 B 的 offset 回复超时：旧 B ACK 的本地 write 可能成功、但 Server teardown 已不再收它，B 因而不再保留 receive entry。这属于原 ACK 写成功/接收不确定窗口，不伪称生产代码已修复。最终 EOF 场景在放行旧 ACK 前真实关闭其接收 data socket，使旧 ACK **明确失败**后再查询，验证同 fid 零正文恢复；退出 0，10.88 秒。前述两组成功程序亦退出 0；批命令中的后续场景失败不否认已打印并完成的前一组结果。

一次性程序已删除；它的临时目录、传输文件和隔离密钥目录均由 TemporaryDirectory 清理。未抓包；加密结果是现有 framed channel 实际传输的内容/ACK 证据，不扩大成所有连接安全属性的全量审计结论。

### 旧方案当时的范围、风险与规则（历史，非现行契约）

- C3 对应 P03/P05/P12–P16/P19/P20 的本阶段路径；完成目标保留、离线 Source、文件夹原完整路径、数值零/EOF 回归由局部测试提供。多进程重启的独立完整矩阵、native Windows、其他 Python 版本、转发中途 rekey 不列为本次通过项。
- 未知/没有保留 receive entry 的 Target 不返回虚构 offset；可能包括尚未接收文件头或 tracking off 未产生行的任务。它们保留 Server 未完成 route，不承诺自动补齐缺失的接收恢复依据。查询成功发送但无合法回复不新增定时重试；Source 接受/端口/ACK 超时仍沿用现有失败路径，可再次合法操作或结束控制会话后恢复。
- ACK 本地写成功不等于对端应用已收到；不增加 ACK-of-ACK。旧目标 ACK 写成功但 Server teardown 未收到的歧义窗口不承诺自动收敛；EOF 场景必须区分“明确失败”与“写成功但未收到”。本轮不声称修复该协议不确定性。
- 问题二（同控制会话、同 fid 换腿交叉的旧 pause/start 严格代次隔离）继续暂缓。C4 全 Target 已完成后的 EOF 再确认、C5 自转发拒绝、C6 合并回归与总验收未实施；不把 Target 自身 EOF ACK-loss 恢复说成已完成 C4。
- 规则沉淀为真实 socket 契约：错误实际会话无 teardown、query 后实测落盘 offset、FIFO 一目标一轮、旧 finally 后再释放占用、中间轮无整体成功、query False 后同 socket 可重新触发。无新增依赖、CI 修改、skip 或 coverage 放宽；进度 JSON、原 fid、pause/start 签名/四 token 与端口格式保持。
