# 传输控制生命周期实施报告

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

