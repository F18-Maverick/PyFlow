# 双向文件暂停、继续与按对象恢复 — 需求卡与更改计划

状态：**需求卡和拆分已确认；C1 已实现并通过定向验证，待人工审查**。本文件替代本会话此前尚未落盘的 C1–C5 提案；不修改历史计划或报告的结论。本轮授权范围为 C1；C2a–C5 尚未开始，公共协议/API 变更仍须在 C2b 切换前单独取得许可。每个原子变更完成后另附验证报告并等待审查。不自行提交、合并或推送。C1 证据见[实施报告](transfer-pause-start-direction-report.md)。

## 1. 需求卡

### 目标

- 普通上传、下载和转发的每条数据 leg，均允许数据发送端或接收端发起 pause/start。
- 用方向位区分同一控制 peer 下同 fid、同类型的反向传输；用数据发送端的串行决定解决双端控制命令交叉后的状态失配。
- 暂停保留文件数据连接；继续沿同一流发送，不附 offset、不重新握手。接收端继续读取并写入在途数据。
- `/resume_trans` 可在控制连接仍在线时使用：只恢复进度表中没有存活文件连接的对象，跳过仍连接的对象，包括暂停连接；不受其他文件传输影响。
- 断线丢弃旧连接的暂停、请求和确认状态，保留已有进度和转发恢复路由；重建的 leg 不继承旧暂停。
- pause/start 统一经过状态接口和现有安全发送路径；复用 resume 的端点识别和加密就绪逻辑。

### 非目标与固定边界

- 不兼容旧 pause/start 线格式或旧 API 调用形态，不引入能力协商或兼容分支。
- 不增加 pause/start offset、文件回退、丢弃在途数据、活跃连接强制 teardown、自动重试或新的接收超时。
- 不改变进度 JSON 的 upload/download 五字段、现有键格式及 relay 路由结构；方向、类型和控制状态只在运行时登记。
- 不改变 `/resume_trans <fid> <offset> <origin> <target>` 线格式、稳定 fid 和落盘 offset 来源。
- 不解决 NAT 身份、client 重连端口变化、跨进程共享进度文件；不重构整个 TCP、加密或持久化模块。
- 不改 CI、依赖、覆盖率策略；不扩展 Web UI 或进度查询返回 schema。

### 受影响范围

- 实现：`PyFlow/network_api/connect_tcp.py`，包括协议解析、server/client 控制入口、文件 leg 登记与清理、普通收发循环、relay 路由/pump、恢复入口及进度查询的内部状态投影。
- 现有测试：`test/unit/network_api/test_fid_routes.py`、`test_transfer_progress_query.py`、`test_transfer_handshakes.py`、`test_resume_matching.py`；`test/integration/network_api/test_tcp_file_transfer.py`、`test_tcp_transfer_resume.py`、`test_crypto_tcp.py`、`test_tcp_async_server.py`。
- 新增必要契约测试：`test/unit/network_api/test_transfer_control.py`、`test/integration/network_api/test_tcp_transfer_control.py`。只覆盖下文不确定的协议/并发边界，已有路径复用既有测试。
- 文档：`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`、`TCP_Client_APIs.rst`、`docs/source/changes/transfer-fid-routes.md`。
- 任务载体：本计划；实施报告落入 `docs/plans/transfer-pause-start-direction-report.md`，实施前不创建完成报告。

### 安全与数据边界

- wire 不携带 peer；对端身份取自实际控制连接。运行时 key 使用该连接对应的规范命名空间；不能凭相同端口把不同地址当作同一 peer。
- 所有控制帧经状态接口、控制通道加密就绪检查和现有 `send_message`；禁止业务调用裸 `send/sendall`，禁止用 raw/plaintext 绕过加密过滤。
- 外部自行构造命令也必须实现相同的定位、状态与排序规则并走安全发送入口；直接调用通用 `send_message` 不会自动获得状态机语义。项目自身不保留这种旁路。
- 不新增文件内容、密钥或明文 payload 日志；不放宽 resume 的 origin/target/控制 peer 路由校验。
- `direction` 是定位字段，不是信任声明；还要验证该会话是否拥有对应 leg、类型及角色。

### 禁止事项与风险

- 禁止删/跳测试、弱化断言、降低 coverage、改 CI、伪造成功 ACK、用文件存在作为传输完成条件。
- 禁止复制一套 peer 别名表、重复状态工具或保留旧协议 shim；所有内部调用点与状态读者一起迁移。
- 禁止把“加了锁”当作双端一致性证明，或把“不持久化 pause”当作旧内存状态已清理的证明。
- 风险等级：**高**，重点审查公共协议、交叉控制、同 fid 隔离、relay 背压和恢复占用竞争。

## 2. 四项问题的解决规则

### 2.1 问题 1：方向位与唯一定位

新线格式严格四 token：

```text
/pause_trans <fid> <trans_type> <direction>
/start_trans <fid> <trans_type> <direction>
```

- `fid >= 0`；`trans_type` 仅允许 `0`（普通）或 `1`（转发）；`direction` 仅允许 `0`（本命令由数据发送端发出）或 `1`（由数据接收端发出）。
- direction 相对当前数据 leg，不相对 TCP client/server；收命令时本地角色为 `1 - direction`。
- 本地控制 key：`(control_peer, fid, trans_type, local_direction)`。状态还绑定实际 control socket 和 leg owner，不能让旧会话清理或命令影响新会话。
- 例：S 向 C 发送 `/pause_trans 42 0 1`，指 C→S 的上传；S 发送 `/pause_trans 42 0 0`，指 S→C 的下载。即使双向 fid 都是 42，也不互相暂停。
- typed key 只改变控制命名空间，不迁移进度表。既有进度键仍不允许同一 upload `(peer,fid)` 被两个文件占用；type 位不授予覆盖进度行的能力。
- 转发上下游分别登记类型与角色；server 在 source→server leg 上是接收端，在 server→target leg 上是发送端。

### 2.2 问题 2：数据发送端串行决定、接收端请求与确认

采用上一轮流程图中的方案，**替代更早“发送端 start 要等接收端回发 start”的方案**。不同时保留两套启动握手。

| 入口/消息 | 行为 |
|---|---|
| 发送端本地 pause/start | 进入该 leg 唯一串行入口，决定状态；发生变化后发 direction=0 状态通知 |
| 接收端本地 pause/start | 按 confirmed/last_requested 双条件去重，登记目标意图并发 direction=1 请求；不把希望的状态当作发送端已采用的状态 |
| 发送端收到 direction=1 | 与本地操作按进入串行入口的顺序处理，执行状态迁移，回 direction=0 的实际状态 |
| 接收端收到 direction=0 | 按该控制连接的消息顺序更新已确认状态，保留最后请求意图；不回发相同命令形成循环 |
| 发送端收到与当前状态相同的请求 | 不重复迁移，但仍回实际状态，以完成必要确认 |
| 已确认状态相同的通知 | 不重复迁移、不回包；必须继续处理本地尚未提交/尚待仲裁的相反意图 |

- 每个 leg 只有一个状态决定者；S 的本地操作和 R 的请求以 S 的处理顺序为准，不以两端不可比较的调用时间为准。
- 状态迁移与 direction=0 帧的发送顺序一致；不可先决定 P→R，却把 R→P 的通知顺序发出。复用既有 socket 写入排序，不新建每块数据的控制队列。
- 本地确知重复的操作在统一入口中静默忽略；发送端以当前决定状态判断，接收端按下述 confirmed/last_requested 双条件判断。不能仅凭旧的已确认状态相同就丢掉新的反向操作。
- 接收端记录 `confirmed_state` 和 `last_requested_state`，初始均为运行；收到 direction=0 只按序更新 confirmed，不把通知当作可精确计数的逐请求 ACK。调用 pause/start 时，仅当目标同时等于 confirmed 和 last_requested 才能忽略，否则按 FIFO 登记并发送，更新 last_requested。这样 confirmed 仍为运行时的 pause→start 不会丢掉 start；S 主动 start 后 R 也能再次请求 pause。尚未确认时重复的同目标请求允许作为幂等请求提交，避免为去重引入不可验证的 ACK 计数。接口不提供逐请求完成 future，也不新增 request id/generation；状态机契约覆盖主动通知与请求响应交错，证明有限输入排空后的收敛。
- 收敛证据：任意有限本地操作结束、控制消息全部处理后，S 的决定状态与 R 的已确认状态相同。网络延迟期间允许观察值暂时不同，不承诺“pause 返回后对端已停止落盘”。
- 发送端下一块前检查暂停；已交给 socket 的数据继续交付。接收端保持 recv/落盘/进度更新，start 不 seek/truncate，不新建数据连接。
- 空文件、最后一块及最终 ACK 不能因重复控制卡住。控制状态确认不是文件传输成功 ACK。
- 加密未就绪时，控制操作只登记/等待安全发送，不降级明文；控制接收线程不得阻塞等待它自己需要处理的密钥交换消息。
- 发送失败沿用会话错误/关闭流程，不能把单次写入返回当作对端确认，也不添加自动重试。

### 2.3 问题 3：逐对象 resume 与断线重置

恢复资格按具体对象判断，不检查整个实例是否还有别的文件连接：

1. 通过现有 progress、origin/target 和 relay 路由找到对象；不存在或已完成的不恢复。
2. 仍有存活文件连接，或正在握手/登记恢复占用：静默跳过。暂停中的 socket 也是存活连接，不走 resume。
3. 无存活连接、旧 owner 已完成退出并释放占用：允许从实际落盘大小恢复，即使控制连接从未断开。
4. 检查和恢复占用登记在同一并发边界完成，避免两个请求同时建出新 leg。旧线程尚在清理或新恢复进行中时，不创建第二个 worker。
5. 不关闭现有连接来满足资格；被跳过的活跃请求不排队等它日后结束，也不伪报“已恢复”。自动重连与手动调用共用同一判断。
6. 转发中 target 的旧下游已断开，但同 upstream source leg/relay 仍在运行时，不启动该 source fid 的第二次重放；静默跳过该对象。其他独立 fid 的传输不阻挡恢复。没有活跃 leg、只是 source 离线的现有 pending-originator 请求仍按既有逻辑保留。
7. 断线/自然结束清除该 owner 的暂停键、目标意图和待确认状态并唤醒等待者；进度和中断路由按既有规则保留。新 leg 默认运行，除非对新 owner 再次显式 pause。

先前会话已验证的基线，不再当成未修缺陷：普通上传/下载实际断线能关闭暂停 socket、结束线程并删除暂停键；关闭 progress tracking 时同样成立；单目标转发的 target 断线可退出 writer、移除 live relay 并保留恢复路由。C4 的工作是把新状态接入这些路径并补恢复资格，不重做已正常的清扫。

### 2.4 问题 4：端点复用、安全门槛和暂停键

- 复用 `_socket_local_endpoint`、`_socket_peer_endpoint`、`_is_control_local_endpoint` 与 client resume 的“配置端点/当前控制连接实际端点”识别，抽取共享判定，pause/start/resume 共用。归一化后映射到原有 peer 命名空间，不改变 persisted upload key。
- `0.0.0.0:port` 作为本实例监听配置可与该连接的具体本地端点对应；不是任意地址通配规则。相同端口的无关地址仍拒绝。
- 控制发送复用 `_wait_control_secure` 的就绪条件，复用后日志改为适用于控制操作；不扩大 `_PLAINTEXT_LINE_PREFIXES`。
- `_transfer_pause` 只保存真实暂停标志：pause 写 True，start 移除对应 typed key，并唤醒发送等待者；不留下 False 暂停项。完整状态记录与暂停标志分开，运行状态记录存在不代表 fid 被占用。
- `_register_fid_route` 区分真实活动 owner、同类型控制记录和无效残留；不再把 False 键或无关类型/方向的控制记录当作碰撞。合法预置暂停必须能被对应 leg 接管，不能阻挡它自己的建立；真正进度、socket、route 冲突仍拒绝。
- relay 的 writer 暂停与上游背压分别维护。reader 的内存高水位等待是独立硬门槛，只有缓冲下降能解除；任何 start 都不能清掉它。server 作为上游 R 汇总本地用户暂停和自动背压意图，再经统一接口请求 source。source 本地操作仍按 source 的串行顺序决定控制状态；即使确认 start，relay 也可因硬门槛继续等待，数据最终由 socket 背压限制。状态确认不承诺下游具备持续消费能力。

## 3. 公共接口、运行时记录与数据流

### 3.1 拟定接口变更（仍待单独许可）

```python
# server：peer 必填
pause_trans(fid, peer, *, trans_type, direction)
start_trans(fid, peer, *, trans_type, direction)

# client：省略 peer 表示当前 server
pause_trans(fid, peer=None, *, trans_type, direction)
start_trans(fid, peer=None, *, trans_type, direction)
```

- API 的 direction 是调用方在该文件 leg 中的本地角色；必填，不使用猜测或默认值。
- 统一 API 自己完成校验、状态登记与协议发送；调用者不能先改状态再调用，以免首次操作被误判为重复。
- 删除旧的纯发送 `pause_trans_remote`/`start_trans_remote` 入口，迁移所有调用、文档/API 列表；不保留转发别名。外部调用方须切换到上述 API。
- bool 表示活跃 leg 的操作是否被登记；静默忽略返回 False，不代表文件成功/对端已确认。保留现有发送端预置暂停：未建立 leg 时记录精确 typed key、返回 False；匹配 leg 建立后接管，并在对端可定位后公布状态。start 清除预置项。
- fid/type/direction 非法的本地参数抛 ValueError；wire 解析失败、错误 peer/role 或找不到 live owner 静默忽略，不创建未知 wire 请求的暂停项。
- `resume_trans(peer_addr, fid, offset)` 保留签名；仍在线/正建连/无恢复资格时返回 False，不再把活跃 leg 当作已接受的恢复。
- 内部 send/recv/pump 从入口显式传递 trans_type、控制 peer 与 local role；控制登记不依赖 progress bar 开关。key 和控制记录在 leg 建立时绑定，数据循环不逐块分配 key、复制状态表或重新路由。
- `get_transfer_progress` 返回 schema 不变；其 upload `paused` 字段按实际活动 leg 的新控制记录投影，不能继续用旧 pair key 直接查 typed 表。

### 3.2 数据流

- 普通：API → peer 归一化 → typed leg 定位 → S 串行决定或 R 请求 → 安全控制发送 → 对端同一入口 → 状态确认；数据循环只读取自己的暂停开关。
- 转发下游：target direction=1 请求 → server 用 `(target, child_fid)` 定位 writer → server 作为 S 决定 → direction=0 确认；不影响其他 writer 的控制状态。
- 转发上游：server 作为 R 因背压请求 pause/start，使用 source upstream fid、type=1、direction=1 → source 作为 S 决定 → direction=0 确认 → server 更新上游已确认状态。
- source 本地 pause/start：仅决定 source→server leg；relay 已排队数据可继续流向 target。target pause 初始只控制自身 writer；其积压达到整体高水位后可能间接停住 source 和其他目标的新数据。
- 转发优先以 target child fid 定位；现有 upstream fid 别名仅在该 peer 的现有映射唯一匹配同一 writer 时接受。禁止只用全局 fid 猜 route，禁止 type=0 回退控制 relay。
- resume：请求 → progress/route/peer 校验 → 对象级连接与占用门槛 → 保留 fid 的原有握手 → 从落盘 offset 重放 → 最终文件 ACK → 原有完成清理。

## 4. 路径全集与验收映射

以下 P01–P18 是本次增量必须覆盖的路径全集。复用既有测试时保留内容/事件断言；新增控制并发用例用屏障/事件控制顺序，不靠 sleep 碰运气。

| 路径 | 必须覆盖的入口/分支 | 验收证据 | 提交 |
|---|---|---|---|
| P01 | 四 token、非法/缺多 token、负 fid、越界 type/direction、旧两/三 token | 严格解析结果；非法输入不改变任何状态或发包 | C1、C2b |
| P02 | 同 peer/type/fid 双向普通收发；相同 fid 不同 peer/type | 暂停仅命中指定 leg；其他 leg 持续传输并内容一致 | C2a、C2b |
| P03 | 普通上传，S/R 各自 pause/start | 发送停止产生新块；在途落盘；同 socket 继续；最终内容/ACK | C2b |
| P04 | 普通下载，S/R 各自 pause/start | 同 P03，方向反转 | C2b |
| P05 | 重复操作、快速 pause→start、双方交叉、本地与远端竞争 | 确定性屏障枚举顺序；消息清空后 S/R 状态一致，无控制回包循环 | C2a、C2b |
| P06 | 空文件、尾块/EOF/最终 ACK、文件夹逐文件 leg | 不因控制状态死锁、重握手或重复确认文件成功；内容/ACK 正确 | C2b |
| P07 | 发送端预置 pause、解除预置、未知 wire 请求 | 对应 leg 接管；解除后不留 False 项；未知请求不占键 | C2a、C2b |
| P08 | 多目标 relay：目标 child fid、合法 upstream 别名、错误 peer/type/role | 指定 writer 暂停；其他 writer 独立；错误消息无效果 | C3 |
| P09 | relay 上游 source 本地操作、server 背压操作交叉 | source 不因控制交叉失配；reader 不绕过高水位；缓冲不持续增长 | C3 |
| P10 | target start 与背压未解除；其他目标变慢或断开 | 有限缓冲；不虚报文件成功；未完成 route 保留 | C3、C4b |
| P11 | 控制加密关闭/开启、握手切换窗口、密钥重新交换 | 控制帧走现有加密入口、不退回明文；最终状态与文件内容正确 | C2b、C3 |
| P12 | 同一连接配置别名/具体 IP、wildcard bind、已关闭控制 socket | 归一到同 key；无关地址不匹配；关闭时不泄漏旧 owner | C1、C2b、C4b |
| P13 | tracking off；线程式/async server 控制分派 | 运行时暂停及关闭仍有效；分派线程不阻塞密钥交换；不新增进度文件状态 | C2b、C3、C4b |
| P14 | 普通 progress 有行无连接，控制连接在线，手动恢复 | 只恢复指定对象；其他活跃传输不变；按落盘 offset 内容一致 | C4a |
| P15 | 普通对象仍运行/暂停/建连；重复恢复与清理竞争 | 静默跳过、不建第二个 socket/worker、不排队恢复活跃对象 | C4a |
| P16 | 转发无 live leg、source 在线/离线、零偏移/EOF、活跃共享 source leg | 无连接对象恢复；离线 pending 保留；活跃共享 leg 跳过；fid/route 稳定 | C4b |
| P17 | 普通双向、relay source/target 断线、旧会话迟到清理 | 旧线程退出、控制状态丢弃；重建不继承 pause；新 owner 不被清理 | C4a、C4b |
| P18 | 进度查询、持久化 reload、config 删除/stop 清理与新状态相交 | 查询契约不变；JSON 无控制字段；无残留线程、键或错误路由拒绝 | C2a、C2b、C3、C4b、C5 |

relay 高水位证据按实际入队粒度计算：记录高水位、单次 enqueue 的扇出字节和峰值，允许既有阈值检测的有限单次超调；不把现有 `buffered > max_mem_buff` 的机制写成“绝不超出 max_mem_buff”。不扩大队列或改为无界缓冲。

## 5. 原子拆分（C1–C5 五阶段，七个原子变更）

原 C2 拆为 C2a/C2b，原 C4 拆为 C4a/C4b，避免把状态机与全调用切换、普通与转发恢复各混成一个大提交。估计包含必要测试和局部说明；超过 500 行需回到本计划重拆，不能边写边临时拆分。

| 提交 | 单一意图、文件与边界 | 预计行数 | 局部验证/证据 |
|---|---|---:|---|
| C1 | 完成严格控制解析、typed identity、共用端点识别基础；`connect_tcp.py`、`test_transfer_control.py`、既有身份测试。只加入完整基础能力，尚不激活新协议；旧 parser 在 C2b 一次移除。复用抽取不改变既有 resume 行为。 | 150–250 | P01/P12 的解析、身份隔离测试；既有 resume 身份回归 |
| C2a | 完成 sender-authoritative 状态迁移、请求/通知排序、重复规则、预置暂停和暂停键生命周期；同上实现/单测。基础状态能力全部实现，不先改公开入口或留下空处理器。 | 200–350 | P02/P05/P07/P18 的确定性契约；遍历交叉顺序后最终状态一致 |
| C2b | 端到端切换普通收发控制和公共 API：server/client handler、leg 登记、sender loop、receiver 逻辑状态、安全发送、进度查询投影；删除旧 parser/remote API，迁移所有调用及 pump 命令格式。relay 同时绑定共享控制入口并保留完整的现有 writer/背压行为；C3 再完成复杂路由和硬门槛验收，不以空 handler 或功能开关跳过 relay。不留新解析器配旧发送点。 | 300–500 | P01–P07/P11–P13/P18；真实上传/下载同 socket，全部既有 pause 调用同步迁移后的相关回归 |
| C3 | 完整转发控制路径：精确角色/fid 路由、writer 状态、上游背压和生命周期接管；消除暂停键误占用路由；新增转发并发回归，迁移既有 route/progress 测试。C2b 已切换的协议与普通行为不另做第二套实现。 | 250–450 | P08–P13/P18；多目标真实流、背压交叉、峰值和内容/ACK；False 残键与真实 owner 碰撞对照 |
| C4a | 普通恢复资格和原子占用：进度有行无连接才恢复；活跃/暂停/建连跳过；双向及 tracking off 的新控制状态随旧 leg 清理。保留现有关闭机制。 | 180–300 | P14/P15/P17；在线控制链路手动恢复、另一活跃文件不中断、并发只建一条新 leg |
| C4b | 转发恢复与会话清理：共享 source/live relay 门槛，pending-originator 复用，新 owner 与旧状态隔离；source/target 断线及 stop/config 清理；不主动 teardown 活跃 relay。 | 180–350 | P10/P12/P13/P16–P18；真实 source/target 断线、source 离线后重连、稳定 fid 与未完成 route |
| C5 | 文档和报告收尾：RST、`transfer-fid-routes.md`、本计划状态与逐项证据、实施报告；移除旧 API 文档引用，更新运行时继续与对象级 resume 的边界。 | 100–200 | 路径表逐项有结论；相关集成回归一次；文档锚点/接口说明机械核对 |

依赖顺序：`C1 → C2a → C2b → C3 → C4a → C4b → C5`。每项先完成局部运行证据和报告，等待人类放行再进入下一项。C1/C2a 是内部完整基础变更，不对外声称新功能可用；C2b 后所有协议生产者和消费者同版本一致，C3 完成复杂转发验收后才视为转发功能交付。

每项实施时查询公开符号引用再改签名；当前工作区语言服务只有 Ruff，不支持 references，故已通过调用搜索枚举已知消费者。实施时若有支持导航的服务则必须使用；不能声称 Ruff 做过引用分析。

## 6. 验收命令与报告要求

以下为各阶段的定向验收命令。C1 已运行新控制基础契约、既有 resume matching、伪造端点拒绝及真实连接身份回归，结果为 **81 passed in 17.77s**；实际命令和覆盖边界见实施报告。其余命令尚未运行，不作为已验证证据。每个原子变更结束运行对应最窄项一次，C5 再进行一次相关合并回归，不反复运行整个仓库。

```bash
# C1/C2a：实现后新增必要边界契约
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py -q

# C2b/C3：实现后新增真实文件控制场景
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py -q

# C4a/C4b：复用既有恢复覆盖并补对象级门槛
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -q

# C5：相关合并回归，包含控制入口、转发与加密
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_crypto_tcp.py test/integration/network_api/test_tcp_async_server.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py -q
```

运行证据至少包括：

- P03/P04 在暂停前后记录同一数据 socket owner、无新 `/file`/port 握手、实际文件内容和最终 ACK。
- P05 固定交叉顺序并排空消息后的 S/R 状态；不只断言“未抛异常”或发送了指定字符串。
- P08–P10 记录各 target 的内容进度、writer 状态和 relay 峰值；受整体背压影响与误控其他 writer 分开说明。
- P11 在真实加密连接上观测安全通道/帧和控制效果，证明未使用 raw/plaintext 旁路；不得 mock 掉加密或文件 socket。
- P14–P17 记录恢复前后 owner、socket/worker 数、实际落盘 offset、恢复内容；覆盖在线控制连接和真实断线，不能只靠手动调用 sweep。
- P18 核对 query schema、persisted JSON 无暂停字段、旧 owner 清理不影响新 owner；规则沉淀由上述方向隔离、交叉状态和恢复资格契约承担。
- 单项报告列提交意图、文件/关键点、路径映射、已运行命令与输出、未覆盖项、风险。总报告逐条回答 P01–P18。

## 7. 影响与确认闸门

- **公共协议/接口有变化**：四 token；direction 的角色定义；pause/start 必填 type/direction；移除旧 remote API；控制确认语义改为发送端权威；resume 活跃对象返回 False。
- **持久化数据模型不变**；运行时控制记录改变。查询返回 schema 不变。测试增加必要边界契约及迁移受影响断言，不降低验证强度。
- **CI/依赖/覆盖率策略不变**。不宣称本计划覆盖未运行的多平台环境、NAT、多进程共享或任意网络中断组合。
- 新功能依赖存活且可投递的控制连接；仅有数据 socket 存活但控制连接不可用时，不承诺远程 pause/start。自然关闭路径按既有机制结束旧 leg。
- 需求卡、P01–P18 路径全集及七项原子拆分已确认，本轮仅授权并完成 C1。C1 审查通过后再授权 C2a；C2b 前仍需明确许可四 token 协议、pause/start 必填 type/direction、删除 remote API、返回值与预置暂停契约，以及 resume 活跃对象返回 False。C1 未切换生产 pause/start 解析器、协议或公共签名，未创建提交。
