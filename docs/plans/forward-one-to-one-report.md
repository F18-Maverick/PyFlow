# 原生 TCP 文件转发 — 分阶段实施报告

**状态：A1–A3 已审计放行；A4 已完成计划内的 Linux 总验收，待人工最终确认。** [A4 总体报告与实测证据](#a4总体报告与最终验收) · [更改计划](forward-one-to-one-plan.md) · [需求卡](forward-one-to-one-requirements.md)

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
- A2 后续审查结论：已放行，已授权 A3。A4 总验收未开始。

## A3：同一路径的独立交付、暂停与恢复

**意图：** 用真实控制连接、数据连接、文件和最终 ACK 固化区分两种模式的回归。只改集成测试、使用说明、变更记录和计划/报告；没有改生产代码、配置、依赖或 CI。

| 文件 | 要防止的回归 |
|---|---|
| `test/integration/network_api/test_tcp_transfer_control.py` | `test_forward_one_to_one_pause_isolation` 从原生控制台同时发 x、y 给 B、C。真实首块写出后暂停 x→B，放开测试闸门，等发送线程确实进入原生暂停等待。C 自行暂停再继续；必须在 B 仍停着时得到另外三对的源端成功结果，再让 B 沿原 socket 完成。 |
| `test/integration/network_api/test_tcp_transfer_resume.py` | `test_forward_one_to_one_session_recovery` 扣住 B 的 ACK，在 C 只写入前缀或已写完但尚未 ACK 时真实断开 C 的控制连接。B 成功收尾后，源快照和服务端 route/index 只能剩 C；C 原端点重连时检查 replay 的 FID、child、原嵌套路径、实际偏移及发送字节数。原共享路由的全目标 ACK 用例改从**缺省控制台命令**进入，同时保留此前的断言。 |
| 使用说明及变更记录 | `docs/source/File_Transfer/File_Transfer.rst`、`docs/source/changes/forward-one-to-one.md` 说明同源路径可有两行、如何区分，以及为何进度到 EOF 不等于交付成功。 |

### V3 局部验证

```bash
P=.venv/bin/python; I=test/integration/network_api
"$P" -m pytest -q "$I/test_tcp_transfer_control.py::test_forward_one_to_one_pause_isolation" "$I/test_tcp_transfer_resume.py::test_forward_one_to_one_session_recovery" "$I/test_tcp_transfer_resume.py::test_concurrent_forward_upload_handshakes_match_out_of_order_replies" "$I/test_tcp_transfer_resume.py::test_forward_route_is_removed_only_after_all_real_targets_complete"
```

退出 **0：7 passed in 44.63s**，无跳过。7 项分别为暂停隔离的 3 种配置、单目标重连的 2 个中断点、已有的乱序握手和默认共享路由对照：

- **P3/P4：** 明文线程（配置别名）、加密线程、加密 asyncio（关闭跟踪、通配绑定）均观察到 x→B 在真实暂停等待中，三个兄弟任务已由源端收到最终 ACK 并单独清理；B 继续后所有文件逐字节相符。关闭跟踪不生成上传行，控制仍可用。
- **P6/P7：** 同一份 **32,785 B** 文件发往 B、C，源端实际有两个不同的 `(server_peer, fid)`。C 断线和 B 的真实 ACK 只留下 C 的五字段上传记录和原路由。两种情况下都从同一端点重连，只向 C 的原 child 重新发起请求，恢复沿用同一 upstream FID 和原嵌套路径。部分文件仅补剩余字节；文件已全但 C 尚未发 ACK 的分支只发头、不发正文，扣住重放 ACK 时路由和源记录都还在。核对接收内容、成功写入的头/正文长度、重复派发次数和源端最终结果。
- **P1/P6 对照：** 默认控制台请求只建立一条上行和一条双目标 route。第二个目标的 ACK 没发出时，两个文件虽然均已写完，源端仍未成功；放行 ACK 后才收尾。已有乱序端口应答测试仍通过。

### 本轮独立场景（非 pytest 入口）

Linux x86-64，CPython 3.10.20，隔离临时目录。本轮实际重新执行，两条命令最终均退出 **0**：

```bash
.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case split
.venv/bin/python /tmp/pyflow-forward-one-to-one-smoke.py --case session
```

- **S-split（P3/P4）：** 明文、asyncio Server、配置别名；同一个 x 的 upstream FID/数据端口分别是 `8131132158:59997`（B）及 `12426099454:34509`（C）。x→B 在 1,024 B 确认暂停，另外三对完成，B 在原 socket 继续；连同另一条目录转发及普通批量发送，实测发送名额峰值 3/3，转发子 ACK 6、成功发送 8，最终活跃 owner 和额外端口均为 0。
- **S-session（P6/P7）：** 明文、线程 Server，原控制端点重连，等名额的首次发送未跨会话。原 upstream `8404201258`、child `7397563762` 在普通发送占用唯一名额时等待，重复恢复请求未多开流；源五字段快照独立读回，释放名额后从接收盘实际的 8,192 B 续传。空文件在目标已 ACK、最终 ACK 写失败之后补确认，未增加下游。关闭跟踪时拒绝目标/健康目标 FID 为 `7823031870` / `12117999166`；显式关闭后 owner、路由、数据 socket、占用名额均为 0。
- 初次运行两条命令均退出 1：临时 smoke 入口少一个右括号，Python 在解析阶段报 `SyntaxError`，未建立连接。仅修正临时脚本后重跑，没有以先前阶段结果代替本轮验证。

### 范围与下一闸门

临时脚本已删除。A3 只验本机 Linux 的上述路径；未跑 Windows、macOS、自由线程 Python、手动端口矩阵、Web/消息/落盘扩展或全项目回归。加密配置参与功能验证，不替代抓包审计；A3 未修改加密或资源计账契约。后续审查结论：A3 已放行，已授权 A4。

## A4：总体报告与最终验收

### 验收前交付：基线、边界和逐路径清单

验收对象为已放行的 `521361e`（A1：Server 统一额度）、`3e4fa9d`（A2：Client 调度/配置）和 `d219be8`（A3：控制与恢复集成证据）。A4 本身只汇总报告，不调整生产代码、测试、超时、依赖、coverage 或 CI 策略。

原生文件/目录命令（含既有 Web 后端单目标入口）→ 结构化条目和目标 → 缺省每条目一次上行，或在 `is_forward_one_to_one=True` 时按唯一目标取得 Client 文件名额 → 原控制 socket/FID/端口和原数据通道 → 同一 Server 严格计费各下游副本 → 原路由的真实目标 ACK → 源端确认/按所属任务清理。只有发送端公开构造器和 Client 配置增加了末尾可选参数；Server 配置单位、wire、五字段进度和其他传输族保持原有接口。

| 需求路径 | 此前局部证据 / 本轮机械验收点（不是本轮结果） |
|---|---|
| P1 缺省、False、单目标 | A2/A3 已见缺省共享 ACK；baseline 直接对照缺省和 False 的上/下行数、子 ACK 和源 ACK。 |
| P2 输入、目标、目的路径 | A2 已验结构化拆分/落点；baseline 走真实 Web 后端、重复/自身/不可达及嵌套目录；split 复验一对一落点。 |
| P3 有界并行 | A2/A3 已见独立 FID 和名额；split、session 检查同时在途、等名额无行/端口，以及普通批量共享名额。 |
| P4 用户控制 | A1–A3 已见资源等待与用户意图独立；baseline/shared 与 split/independent 对照实际暂停等待、兄弟交付和原 socket 继续。 |
| P5 Server 额度 | A1 已见跨路由、含恢复计费；pressure 检查元信息/正文、在写份额、失败与停服的峰值、等待、归零。 |
| P6 错误与会话更替 | A2/A3 已验单项断线和乱序；V4 与 session 检查拒绝不串 FID、原会话取消和失去 owner 后的收尾。 |
| P7 交付与恢复 | A2/A3 已验原路径、原 upstream/child、零正文、切换开关；session 和 V4 对照实际磁盘 offset、五字段快照读回和真实最终确认。 |
| P8 环境与非目标消费者 | 历史部分覆盖；V4 合并运行普通直传、Web、消息、落盘扩展、线程/asyncio、加密和端口用例；四场景检查内容、临时资源、退出状态。 |

本轮应执行[计划 §5](forward-one-to-one-plan.md#5-可执行验收与证据)的完整 V4（不改选择集）和 `--case baseline|split|pressure|session` 四个独立入口。保留失败/跳过、实测平台及路径边界。全实例队列限额不是 RSS，暂停不等于取消；不保证公平、源文件变更一致性、跨命令目的路径互斥或变更 NAT/端点的恢复。不将 Linux 结果当作其他 OS 通过；合并、提交、推送均需另行人工决定。

### 本轮执行记录

环境：Linux x86-64（内核 `7.0.0-43-generic`）、CPython 3.10.20、pytest 9.1.1。工作树起始干净。为避免测试改写使用者的 `.Flow`、密钥或进度文件，从 `git archive d219be8` 解出私有临时源码目录，复制当前 CPython 3.10 的本地 `_crypto_api` 和 CMake 共享库，使用同一 `.venv` 解释器；V4 的 HOME、TMPDIR 均隔离。运行前确认 `connect_tcp` 来自该目录、原生加密库确实可加载。未重建或升级依赖。

在临时源码根目录执行（`P` 为原工作树 `.venv/bin/python` 的绝对路径，`RUN` 为私有临时目录）。`-ra` 和 JUnit 只用于取证；选择集与 V4 一致，不加重试或失败豁免。

```bash
U=test/unit/network_api; I=test/integration/network_api
"$P" -m pytest -q -ra --show-capture=no --tb=short --junitxml="$RUN/v4.xml" \
  test/unit/test_flow_setup.py "$U" test/unit/transfer_web/test_web_transfers.py \
  test/unit/transfer_web/test_file_location.py "$I/test_tcp_file_transfer.py" \
  "$I/test_tcp_transfer_control.py" "$I/test_tcp_transfer_resume.py" "$I/test_crypto_tcp.py" \
  "$I/test_tcp_async_server.py" "$I/test_tcp_broadcast_alloc.py" \
  test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py
```

**V4 退出 0：`847 passed, 1 skipped in 847.25s (0:14:07)`**。JUnit 848 项，0 失败、0 错误。唯一跳过：`test_receiver_pause_start_waits_for_real_rekey_without_blocking_key_reader[plain-alias]`，明文配置不适用 re-key；同一用例的 `encrypted`、`encrypted-async-untracked-wildcard` 两组均实际通过，不是缺少加密库。这是计划中的受影响范围合并回归，不是整个仓库的所有测试。

| V4 范围 | 通过 / 跳过 |
|---|---:|
| `test_flow_setup`、`unit/network_api` | 85 + 457 / 0 |
| Web `test_web_transfers`、`test_file_location` | 11 + 10 / 0 |
| `test_tcp_file_transfer`、`test_tcp_transfer_control`、`test_tcp_transfer_resume` | 37 + 95 + 81 / 1 |
| `test_crypto_tcp`、`test_tcp_async_server`、`test_tcp_broadcast_alloc` | 22 + 9 + 18 / 0 |
| `test_forward_extension`、`test_forward_msg_attribution` | 15 + 7 / 0 |

**独立 TCP 场景**：分别执行 `"$P" /tmp/pyflow-forward-one-to-one-smoke.py --case baseline`、`--case split`、`--case pressure`、`--case session`，最终各退出 **0**。不调用 pytest 入口，不代答握手、端口或 ACK；同步闸门只安排真实 I/O 时序，断线通过关闭实际 socket 触发。

| 路径 | 本轮结论与可定位证据 |
|---|---|
| P1 覆盖 | `baseline`：缺省和显式 False 分别只产生一个 upstream FID `6396275316`、`6592669000`；每组 1 上行 / 2 下行。B/C 文件均已完整但扣住 C 的 ACK 时，完成集合只有 B，源未成功；放开后各组均为 2 个真实子 ACK、1 个最终 ACK。 |
| P2 覆盖 | 自身唯一目标未分 FID；混合自身、重复、未连接、Server、同 IP 不同端口和无效文件后只向 B、C 交付。真实回环 HTTP multipart `POST /api/send_file`（False）、`POST /api/send_folder`（True）及 `GET /api/transfers` 均返回 200；扣住 ACK 时 Web 读到对应 FID，收尾后核对嵌套目的路径、4,011 / 9,023 B 内容。HTTP 接单不作为交付成功；未测试浏览器交互。V4 另验目录冲突首项保留及不同相对目录。 |
| P3 覆盖 | `split`：x→B、x→C 的 upstream FID/数据端口分别为 `8577173170:52143`、`12872140466:34987`。普通批量、目录和另一条转发共用 3 个名额，使用峰值 **3/3**。V4 的第四对在等待时没有 FID、进度行或数据端口。 |
| P4 覆盖 | `baseline` 中 B 暂停后共享 route 的 6 个角色均确认，源确实进入暂停等待，C 可以继续该任务；`split` 中 x→B 在 1,024 B 确认暂停时另三对均有最终 ACK，B 在原 socket 继续。`pressure` 中显式 start 不能开满额闸门，释放额度未取消用户暂停。 |
| P5 覆盖 | 见下表。另执行 `"$P" -B /tmp/pyflow-a4-replay-budget.py`（退出 0）：首次共享路由先由 E 确认、D 断线；切换发送端开关后 D 从落盘 **6 B**、原 FID `7388771744` / child `5088227418` 恢复。它与新的单目标及双目标路由**同时**有 3 条上行、4 条下行，在同一预算上等待，各 pump 占用 **0 / 8 / 0 B**，峰值 8/8 B；最终 5 份接收内容相符、3 个源端最终 ACK、余额 0，E 未重新收件。 |
| P6 覆盖 | V4 对乱序端口应答按实际 socket + FID 匹配。`session` 中等名额的旧命令未借新控制连接；跟踪关闭时拒绝 FID `4466522086`，健康 FID `8761489382` 正常完成。显式关闭时未发起项未建持久行、未开新数据连接。 |
| P7 覆盖 | 同一文件两份在途，V4 观察 B 的成功不清掉 C 的五字段行，部分文件与 EOF 未确认均按原 child/路径只重放 C。`session` 独立读回快照：原 upstream `5829132394`、child `8229536116` 恢复偏移 8,192 B，不用源已发送的 24,576 B；普通发送占用名额时回复重复请求不多占，完成后余额归零。空文件 FID `14419066986` 的目标已 ACK、源漏最终 ACK 时，仅补最终确认，不多开下游。 |
| P8 本机范围覆盖，跨平台未验 | V4 及四场景含线程/asyncio、明文/加密、跟踪开/关、系统/手动端口、别名及通配绑定。实际普通直传收到 1 个 ACK；消息由原发送者身份到达；落盘扩展真实上传一次、Server 从磁盘向两个目标推送各获 ACK，三者均未增原生 pump。原生转发时 Server 接收目录无文件。检查了内容、端口、owner、预算和线程；扩展通用线程池在 `stop()` 后的例外见下。 |

**S-pressure 明细（字节）**：每行 2 个不同源、1 单目标和 1 双目标路由，5 个控制连接、5 个数据连接；实测 2 个 reader 均曾等额度。计费含队列及 writer 已取出但未返回的份额。峰值由预留时累计峰值和独立采样核对，所有组最终余额、等待者、活跃 relay、额外端口均为 0。

| 场景 | 限额 / 实测峰值 | 累计预留 / 归还 | 源最终 ACK（两源） | 真实子 ACK |
|---|---:|---:|---|---:|
| 用户暂停后排空 | 1 / 1 | 856 / 856 | 是 / 是 | 3 |
| 加密、asyncio、下游 writer 实际失败 | 512 / 512 | 615023 / 615023 | 否 / 是 | 2 |
| 手动端口、等额度的源断线 | 256 / 256 | 307220 / 307220 | 是 / 否 | 1 |
| asyncio、满额停服 | 128 / 128 | 145 / 145 | 否 / 否 | 0 |

**加密与日志边界抽查（P6–P8）：** `"$P" -B /tmp/pyflow-a4-wire.py` 退出 0。手动端口（沿用 A2 隔离实例的不同 `port_add_step`）、加密、两个并发单目标任务；扣住 C 的最终 ACK 并真实断开 C 的控制 socket，原端点重连，再以 EOF **36,867 B** 重放。首发两条 `/forward_item` 和一条恢复请求在控制通道实际写出 3 个完整帧。接收解码边界的首发/重放原始字节流中，三条上行分别解析出 **6、6、3** 个完整帧，对应下行也是 **6、6、3**；未观察到测试正文标记。原 FID/child、内容、ACK 和手动端口归还均核对。普通日志开启、debug 关闭时，在 stdout/stderr 及实际生成的 14 份收发消息/事件日志与进度快照中未检出测试正文标记或两种 PEM 私钥头。没有打印密钥、正文或密文。此证据不等于审计了所有日志字段、debug 模式、握手引导流量、外部代理或 Web 鉴权。

### 中间失败、存量风险及合并前闸门

- `session` 首次退出 1：临时子脚本误把启动器的两行放在目标地址初始化处，解析报 `SyntaxError`，尚未运行协议。恢复初始化语句后原断言通过；未改仓库测试或生产代码。
- `baseline` 首次退出 1：真实 Web、普通直传、消息、扩展 ACK 和文件均已核对，但将“所有新增进程线程”都当作原生传输线程，停服后断言失败。复测保留传输和最终线程清理断言，并记录扩展任务的真实 `Future`、线程归属和栈：`stop()` 后 **0 个原生传输线程，1 个空闲 Server 通用线程池 worker**，对应扩展任务已成功返回。临时实例的 executor 显式 `shutdown(wait=True)` 后才达到整个 fixture 零新增线程，**不归功于 `stop()`**。
- 溯源：另从改前 `ddb71c9` 解出 `PyFlow`，真实 Server 调用 `submit_task(threading.current_thread).result()` 后 `stop()`；同样得到 `finished_future=True post_stop_pool_thread_alive=True stack=concurrent.futures.thread._worker`。不是本次转发的在途 owner 泄漏；反复创建/销毁扩展实例的线程池生命周期仍是存量风险，未越权修改。
- 范围复查（`ddb71c9..d219be8`）：变动 17 个文件；生产仅 `connect_tcp.py` 与 `flow_setup.py`，无 CI、依赖、coverage、Web UI、消息或落盘扩展实现变动。比较两个构造器 AST，旧位置参数及缺省值均保留；只有 Client 追加 `is_forward_one_to_one=False`。6 个已改测试文件的既有 `test_*` 名称无删除、新增 8 个，增量中无新增 skip/xfail/ignore 或容错退出标记；旧压力断言改为真实扣住 writer、严格不越界，不是删背压测试。
- 规则沉淀：P1–P8 路径清单、V1–V4 精确节点和真实 TCP 回归已入库；双模式 FID/ACK、跨路由严格预算、旧会话取消和按真实 offset 恢复可机械重验。文件传输指南、Client/Server API、实例配置和变更记录已在 A1–A3 随实现更新。`max_mem_buff` 的严格总额保证须升级 Server；不是 `max_thread_num`、带宽或进程内存保证。

**最终结论：** P1–P7 及 P8 的上述 Linux 组合有运行证据；保留唯一的明文 re-key 不适用项、扩展通用线程池退出行为，以及 A2 报告的同机手动端口冲突边界。未运行原生 Windows、macOS、自由线程解释器、全部配置笛卡尔积或全仓测试；模拟的 Windows 端口异常不算 Windows 验证。没有把这些边界视为已获人工豁免。

A4 工作树交付仅为本报告与计划状态。临时验收脚本、源码副本、测试日志/XML、测试密钥与传输文件已清理。**未创建提交、合并或推送；合并前最后一项为人类接受上述结果与边界。**

### Windows Actions 后续：接收路径断言

Windows Actions 报告 `2 failed, 1022 passed, 3 skipped`：`test_forward_one_to_one_session_recovery` 的 `partial` 与 `before-final-ack` 均已核对落盘内容，失败在接收进度行第 4 个字段的字符串比较。命令中的 `original/nested path` 保留 `/`，而 `str(c_output)` 在 Windows 输出 `\`。测试现按本机路径规则比较完整路径，其余四字段仍精确匹配；未更改持久化格式、传输逻辑、重连断言或 CI。

修正后执行 `.venv/bin/python -m pytest -q test/integration/network_api/test_tcp_transfer_resume.py::test_forward_one_to_one_session_recovery`，Linux 上 `2 passed in 12.48s`。另以 `ntpath` 验证混合分隔符匹配、不同目录不匹配。原生 Windows 修正结果待 Actions 重跑。
