# 原生 TCP 文件转发 — 分阶段实施报告

**状态：A1 已审计放行。A2 已完成局部验证，待人工审查；本轮未提交、未开始 A3。** [更改计划](forward-one-to-one-plan.md) · [需求卡](forward-one-to-one-requirements.md)

## A1：Server 共享字节预算

让同一 Server 上所有原生转发（首次或恢复，单目标或多目标）共用一个严格的待下发字节上限。改变的是既有 `max_mem_buff` 的作用域，不是转发任务的身份、协议或用户暂停状态。

| 文件 | 本阶段改动 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 在 Server 实例上创建 `_ForwardBudget`；`_forward_pump` 改为先计费再发布、逐下游归还。实际控制会话退役关闭所属数据 socket 后唤醒等待额度的 reader。 |
| `test/unit/network_api/test_transfer_control.py` | 用已满队列触发发布失败：只退本次预留，不退其他发送持有的字节；空出的额度可再用。 |
| `test/integration/network_api/test_tcp_file_transfer.py`、`test/integration/network_api/test_tcp_transfer_control.py` | 将两处要求越过水位的压力断言改为实际扣住 writer、证明 reader 等待且总账不越界；增加跨源、跨路由测试，保留控制状态、真实内容、ACK 和清理断言。 |
| `docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`、`docs/source/changes/forward-one-to-one.md` | 说明按下游份数计费、适用的 Server 版本和非 RSS 边界。计划中更新了授权闸门。 |

**数据流（P5、P4 的资源分支）：** reader 从原上传连接读文件头和正文，不预占下一次读取的额度。对于每个下游，只在总账有空间时发布容得下的字节；一块或一次扇出的总量大于上限也可分段推进。writer 出队不退账，沿原 `send_message` 发送，返回或抛错才归还当前项。失败的 writer 先在预算锁下摘除自己的队列，再在锁外排空并归还剩余字节。没有下游的 EOF 补确认仍校验头，但不入队、不争额度。

队列成员检查、预留与发布受同一个 Condition 保护；每次被唤醒都重新核对 socket 和队列。预算锁不跨网络读写、用户控制或路由锁。用户 start 不能制造额度，退还额度也不发 start。任务仍须得到所属目标的真实确认；活跃 pump 清理与失败路由保留用于恢复是两件事。

## 局部验证与实际证据

环境：Linux x86-64，CPython 3.10.20，pytest 9.1.1，本机 IPv4 TCP。

**先红后绿。** 旧实现执行 `.venv/bin/python -m pytest -q test/integration/network_api/test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader`，退出 1：限制 **32,768 B**，一个真实 writer 尚未返回时已计费 **65,555 B**。实施后执行计划 V1：

```bash
P=.venv/bin/python; U=test/unit/network_api; I=test/integration/network_api
"$P" -m pytest -q "$U/test_transfer_control.py" "$I/test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader" "$I/test_tcp_file_transfer.py::test_forward_shared_budget_across_routes" "$I/test_tcp_transfer_control.py::test_relay_pressure_preserves_user_intent_and_source_start_cannot_open_reader"
```

退出 **0：145 passed in 64.08s**。32 KiB 用例保留了 300 KiB 正文；跨路由用例还将额度压到 1 B：双目标路由的 writer 持有这一字节时，另一源的单目标路由连头都不能发布。释放 writer、仍扣住第一源的正文时，另一源收到最终 ACK；第一条路由随后向两个真实目标完成交付。

**独立 S-pressure。** 执行 `.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case pressure`，退出 **0**。每组使用两个不同来源、两个文件、一个单目标和一个双目标路由；5 条控制连接，2 条上行、3 条下行数据连接。在真实 `send_message` 边界保持 writer 未完成，不替换握手、加密、文件、端口、数据或 ACK。表中峰值为运行时采样和预留总账峰值，两者相等；正文仅做逐字节比较，不写入日志。

| 路径（Server / 通道 / 端口） | 上限 / 峰值 B | 两条 reader 进入额度等待的次数 | 两源获最终 ACK | 真实目标 ACK 数 | 核对成功正文 B |
|---|---:|---:|---|---:|---:|
| 放开 writer（线程 / 明文 / 系统） | 1 / 1 | 342、653 | 是、是 | 3 | 805 |
| 目标断线、实际下游写失败（asyncio / 加密 / 系统） | 512 / 512 | 3、1199 | 否、是 | 2 | 614460 |
| 等额度的源控制会话断开（线程 / 明文 / 手动） | 256 / 256 | 1144、1 | 是、否 | 1 | 307203 |
| 两条路由受压时停服（asyncio / 明文 / 系统） | 128 / 128 | 1、1 | 否、否 | 0 | 0 |

- 放开组发送的计费数据为 **856 B = 805 B 正文 + 51 B 文件头**。满额时显式 start 后 reader 再次进入等待，读取数不变；排空后被用户暂停的源仍未发下一块，另一源已完成。其他三组没有自动 pause/start。
- 源断线组：占满 256 B 的另一 writer 尚未放行，等待中的源已失败并退出 pump。停服组两个源都没有假成功。四组结束时总账、各 pump 余额、活跃 relay、额外分配端口均为 **0**，临时 socket/握手及传输 owner 登记已清理。失败任务不要求删除恢复路由。
- 脚本首次退出 1，未进入计费路径：测试把目标控制连接绑到另一个回环 IP，而文件 listener 未监听该 IP，真实连接返回 `Connection refused`。仅将测试拓扑改为同一回环地址、不同端口后执行上述四组，未绕过连接或改生产监听逻辑。

**P5 恢复补证。** `.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case pressure --recovery-only`，退出 **0**。先真实断开下游数据 socket，97 B 文件仅落盘 11 B；随后从实际 offset 11、原 upstream FID `8171444276` 和 child `6337585112` 重放。另一来源持有全部 1 B 时，该恢复连文件头都在等额度。另一空文件的目标此前已真实 ACK、源漏收最终 ACK；其原 FID `12466411572` 在同一满额期间以 **0 下游、0 新计费**完成补确认。释放 writer 后两个剩余文件内容均一致，峰值 1 B，最后余额及活跃 relay 均为 0。一次性脚本不入库。

## 未覆盖与风险

- P5 和 P4 背压分支有上述证据；P8 只覆盖表中的配置及本机端口/临时资源。未运行 Windows、macOS、自由线程 Python 或所有配置的笛卡尔积，不把这些结果外推到其他平台。
- A1 不实现 Client 拆分、发送名额、排队项取消或一对一任务控制隔离；未执行 V2–V4 或全项目回归，未改 CI、依赖、配置格式、普通直传、消息、Web 或落盘扩展。
- 严格上限针对队列和 writer 手中的待交付字节，不覆盖 reader 当前块、加密临时对象或 OS 缓冲。没有带宽公平保证。部署依赖总额保护须更新 Server；只更新 Client 无效。A1 后续审查结论：已放行，已授权 A2。

## A2：发送端按文件条目与目标调度

**意图：** 新增可选 `TCP_Client_Base(..., is_forward_one_to_one=False)`。缺省仍是一个条目一份上行、全部目标共享；开启后每个条目与每个唯一目标独立上传、独立收最终 ACK。参数追加在 `is_auto_reconnect` 后。不增加父任务、不改 Server、线上命令或五字段进度格式。

| 文件 | 改动与审查点 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | Client 原生文件、目录入口共用结构化解析和遍历。开启时提交即捕获控制 socket，去重目标、排除自身，按目的目录/相对目录/文件名保留第一个落点；每个 driver 先取实例 `file_semaphore`，才启动一个 `_forward_one`。归还由启动失败的 driver 或已启动的 worker 负责，不提前分配等待项的 FID、记录或数据端口。 |
| 同上，Client 恢复与进度 | `_resume_on_socket` 仍交给原 owner 合并机制；旧 owner 排空后才在后台取同一份名额，使用旧 FID 和指定 offset 重放一次，不经过首次枚举。首次五字段行在握手发送前登记；登记和按 owner 删除与快照使用同一把进度锁。持锁时不等名额、网络或 ACK。 |
| `PyFlow/flow_setup.py`、`test/unit/test_flow_setup.py`、`test/unit/network_api/test_TCP_client.py` | 只补 `CLIENT_DEFAULTS`，测试缺省及显式开启的配置落盘、读回和构造器透传；实际 socket 替换发生在 driver 开始前或等名额时，均不得分 FID、建快照或发命令。没有修改用户 `setup.json`。 |
| `test/integration/network_api/test_tcp_file_transfer.py` | 两个 V2 节点：扣住真实目标 ACK 检验有界并行；用带空格的目录、重复/自身/拒绝目标、冲突落点和不同相对目录核对 wire、内容及最终行。拒绝目标保留原协议要求的进度，不误删兄弟任务。 |
| 文档 | `docs/source/Network_APIs/TCP_Client_APIs.rst`、`docs/source/Instance_Setup/Instance_Setup.rst`、`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/changes/forward-one-to-one.md`、本计划及报告。 |

### 局部验证

Linux x86-64，CPython 3.10.20，pytest 9.1.1。配置用例在改动前运行 `.venv/bin/python -m pytest -q test/unit/test_flow_setup.py::test_one_to_one_round_trips_only_in_client_config`，退出 1：旧的缺省配置读回无 `is_forward_one_to_one` 键。实施后执行：

```bash
P=.venv/bin/python; U=test/unit/network_api; I=test/integration/network_api
"$P" -m pytest -q test/unit/test_flow_setup.py "$U/test_TCP_client.py" "$I/test_tcp_file_transfer.py::test_forward_file_to_multiple_clients" "$I/test_tcp_file_transfer.py::test_forward_one_to_one_item_target_admission" "$I/test_tcp_file_transfer.py::test_forward_one_to_one_folder_destinations"
"$P" /tmp/pyflow-forward-one-to-one-smoke.py --case split
"$P" /tmp/pyflow-forward-one-to-one-smoke.py --case session
"$P" -B /tmp/pyflow-a2-history.py
```

最终四条命令均退出 **0**，V2 **101 passed in 9.10s**。三个临时验证脚本（含 session 子脚本）只用本机隔离目录与真实 TCP；同步闸门安排时序，不替代握手、文件、加密、端口应答或最终 ACK。运行结束后删除，不入库。

| 路径 | 实际观察 |
|---|---|
| P1–P3，首次准入 | V2 扣住三个目标 ACK 时仅 3 条上传记录、3 个 FID 和 3 次源端数据端口分配；释放一个 ACK 即准入第四对。四对最终 FID 不同、单目标路由独立，目标文件内容与预期相同。目标或落点重复不多分 FID；相同 basename 在不同子目录均交付。 |
| S-split，P3/P4 | 明文、asyncio Server，源配置 `localhost`。`x→B`、`x→C` 的 upstream FID 分别为 `6976803528`、`11271770824`，数据端口分别为 `42121`、`46501`。B 的 `x` 在 1,024 B 处确认暂停期间，`x→C`、`y→B`、`y→C` 都收到最终 ACK；C 自己暂停/继续也未解除 B 的暂停。B 沿原 socket 继续。另一次目录转发与普通 `/multiple_file` 共用同一名额，峰值 **3/3**。共 6 个转发子 ACK、8 个成功发送（含 2 个普通文件），最后 owner、额外端口均为 0。 |
| S-session，P3/P6/P7 | 明文、线程 Server、`localhost` + 通配绑定，1 个名额。真实控制断开并以原端点重连：未发起的文件无 FID、数据 socket 或五字段行，新会话未收到旧队列命令。在途任务接收端已落盘 **8,192 B**（源已发 **24,576 B**）；源快照经独立实例读回。原 upstream `8354677942`、child `6833030408` 及原嵌套路径保留。恢复等普通发送的真实 ACK 让位；期间控制 reader 处理了重复请求，接受结果依次 `True`、`False`。真正重放只取一次名额，按落盘位置交付余下正文，内容一致。 |
| S-session，零正文与失败 | 空文件的目标已 ACK、源最终 ACK 写失败；原 FID `16944612534` 补确认，不再开下游。跟踪关闭时不可达目标收到拒绝，下一健康目标成功；显式 `close()` 取消排队项，未产生新数据连接或持久行。收尾后活跃 owner、路由、数据 socket 和占用名额均为 0。 |
| 开关切换与启动失败 | 加密、线程 Server、手动端口。在两个目标中令 B 的真实 ACK 写失败、C 成功。原共享 route 从 False 切到 True 重放时仍为原两目标 route；原独立 route 从 True 切到 False 时也未合并。两种情况下只重新握手 B，使用原 child、原 upstream FID，offset **8,201 B**，C 没有重新收件；检查了上/下行加密通道和端口归还。另让第一个 worker 的系统启动调用抛错：失败项无 FID，另一个目标得到真实最终 ACK，名额无泄漏。 |

补验历史路由的脚本首次退出 1：同机隔离实例在加密握手阶段选到冲突的手动端口，文件 listener 的 `listen()` 返回 `EADDRINUSE`，未进入待验转发路径。给临时实例配置不同的既有 `port_add_step` 后通过；没有改端口分配器或用假端口绕过此错误。此结果不证明默认手动端口在同机没有冲突。

### 范围与下一闸门

- 同一请求可推导的目的路径冲突只在本次命令内跳过；不承诺远端文件系统别名或跨命令互斥。暂停任务仍占名额；`max_thread_num=1` 下等待不表示任务间共享暂停状态。
- 未运行 V3、V4 或全量回归，未运行 Windows、macOS、自由线程 Python；未单独启动 Web UI、消息转发或落盘扩展。未改这些模块、CI、依赖或 A1 的 Server 预算。本轮的加密观测是转发数据通道，不是全协议抓包审计。
- A2 交付待审。A3 的固化集成证据和 A4 总验收未开始。
