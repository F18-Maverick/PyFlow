# 普通传输失败保留、进度查询与按名清理：需求卡和增量计划

状态：**O1–O3 已获人类审计通过；补充 O4 delete_config 已按批准完成实现、定向回归与独立真实运行验证，待人类审查及补充项总验收。本轮定向 101 passed、完整相关套件 332 passed。不进入原 FID 大计划的原子更改 4–6，未创建 git 提交、未合并、未推送。**

本任务插入 `transfer-fid-and-resume-unify-plan.md` 的更改 3 与更改 4 之间。更改 3 的历史验收只证明其原有范围，不作为本任务修复或新增查询功能的验收证据。

## 1. 需求卡

### 目标

1. 修复 Server 普通发送收到错误符号后仍可能返回成功、删除上传记录的问题。
2. 修复 Server 普通接收最终 ACK 明确写出失败后仍删除接收记录的问题。
3. Server 和 Client 提供相同的公开、本地只读查询接口和控制台命令，查看当前实例全部已有文件进度及相关恢复记录，由用户自行判断是否属于疑似僵尸记录。
4. 增加 O4：两端提供 `delete_config(config_name=None, *, is_list_config=False)`；列表模式返回可删除文件名 list，删除模式在实例停止后按白名单名字删除指定配置/状态文件，允许用户主动放弃整份持久化文件进度。

### 非目标

- 暂不解决“ACK 写入 TCP 成功，但对方应用没有收到”的不确定窗口；不增加收据、去重/重传协议、额外 ACK 或等待时限。
- 不自动判断僵尸、不自动清理，不增加逐记录删除命令；仅 O4 允许显式按白名单删除整份配置/状态文件，不删除传输内容，不触发恢复、重发或连接操作。
- 不推进原子更改 4–6；现行 kind/tfid、client 转发恢复记录、恢复命令及分配权保持。
- 不改五项进度值、持久化 JSON 结构、文件字节协议、暂停持久化、端点迁移或加密机制。
- 不增加远程查询协议、HTTP 端点或 Web 页面；现有 `/api/transfers` 的字段、权限与界面保持不变。
- 不扫描其他进程、其他实例或整个 `.Flow` 目录；不新增全局注册表、历史完成表、时间戳或状态副本。

### 范围与安全边界

- 生产改动集中于 `PyFlow/network_api/connect_tcp.py`：Server 的普通发送/接收收尾、两个类的本地查询和控制台分支，以及 O4 的配置列举/按名删除与关闭后进度防复活边界。
- 文件传输仍经过现有 `send_message`、`receive_message` 与加密入口；复用原进度、socket、暂停和路由状态，不新增发送旁路。
- 查询可能包含完整本地路径、端点及目标关系，只返回给本进程调用者/本地控制台，不向对端发送，不注册到网络命令处理器，不写入消息/事件记录。
- 返回结果是独立的 JSON-ready 观察快照，不暴露 socket、锁、Event 或可修改的内部记录引用。它不是跨连接事务快照。
- O4 的删除只面向实例显式管理的配置/状态白名单，不接受任意路径，不触及密钥、协议表、用户数据库或传输内容；默认关闭不删除，用户明确选择删除才放弃恢复。共享路径的其他写入者必须先停止，不能声称实例属性代表独占文件。

### 风险等级与禁止事项

风险：中；O4 的显式删除属于不可逆的数据清理路径，须重点审查名称映射与关闭后写回。普通传输收尾影响失败保留与重连；接收端作用域、旧磁盘快照和运行时状态不能混淆。

禁止：删/跳过测试、弱化断言、改 CI/依赖/coverage、绕过统一收发入口、把字节 100% 当作完成、把无活动 socket 当作僵尸证明、静默忽略不可读进度文件、查询时加载/恢复/覆写状态、任意路径删除、运行中清理恢复文件、自行提交或进入下一原子更改。

## 2. 现状证据与修复决定

### 2.1 Server 普通发送

实施前，`TCP_Server_Base.file_transfer_mode` 的“等待开始符号”和“等待最终确认”两个循环中，错误符号分支调用 `close_socket()` 后只是 `break`；后者可继续执行 `file_sent = True`、成功日志和成功清理。O1 已将两处分支改为立即 False。Client 对应两个分支此前已经返回 False，本任务不重复改写已修逻辑。

决定：Server 两处错误符号分支立即返回 `False`。只有真实收到既有最终确认符号才能设置 `file_sent = True`。保留现有 `finally` 的 owning-entry 清理：已经登记且具备 peer 的失败上传保留，尚未登记的开始阶段失败不凭空创建记录，源文件不存在的原有处理保持。

数据流：普通文件/目录内文件发送入口 → 同一 `file_transfer_mode` → 开始符号 / 字节发送 / 最终确认 → 错误立即失败，成功 ACK 才成功收尾。

### 2.2 Server 普通接收

实施前，`TCP_Server_Base.file_transfer_mode_recv` 内部接收函数在文件写完后就设置 `finished_ok = True`，随后忽略最终 ACK 发送的布尔结果；发送异常也被局部捕获，导致 `finally` 删除接收进度。O2 已删除提前赋值，复用 Client 既有 `finished_ok = bool(self.send_message(...))` 模式。

决定：复用 Client 的现有模式，Server 只有最终 ACK 明确写出成功才将 `finished_ok` 置真。`False` 或异常时，利用既有 `_file_progress_finish(..., keep=True)` 保留并持久化可恢复接收行，即使字节数已等于总大小。完整文件不删除；记录继续指向原完整路径。

保持通知、事件和公钥 TOFU 注册先于 ACK 的顺序；公钥推送仍不创建普通文件进度。数据已接收的通知不升级为“对端已收到 ACK”的证据。

数据流：Client 文件/目录项上传 → Server 写入原目标文件 → 接收通知及必要的公钥注册 → 尝试原最终 ACK → 成功清理 / 明确失败保留 → 后续既有稳定 fid 恢复可在 EOF 重新确认。

### 2.3 不确定窗口的处理边界

最终 ACK 写出成功不等于对端应用确认收到。这一窗口暂不解决，可能出现一端已清理、另一端保留的状态。新增查询使用户能核对两端现有记录，但不声称消除不确定性或补齐分布式完成协议。

## 3. 新增公开查询契约

两个类分别提供同名方法，签名一致：

```python
get_transfer_progress(self, *, include_persisted=True) -> dict
```

- 查询本实例；不要求连接已经建立，也不因查询启动网络连接。
- 默认同时展示内存记录和本实例 `file_trans_progress_file` 的磁盘快照；`include_persisted=False` 不读取磁盘。
- 内存与磁盘分开显示，不合并、不覆盖、不将磁盘记录恢复进内存。两者可能包含同一任务的不同时刻值，不能相加为任务数。
- 复用 `_transfer_progress_snapshot` 的现有序列化来源和 `_read_transfer_progress` 的既有磁盘格式规则；新增展示字段不落盘。不调用 `_load_transfer_progress`、`_persist_transfer_progress` 或 `_request_resumes`。

### 3.1 返回结构

```text
{
  progress_enabled: bool,
  speed_enabled: bool,
  memory: {
    uploading: [row, ...],
    downloading: [row, ...],
    relays: [record, ...]
  },
  persisted: {
    path: str,
    status: "ok" | "missing" | "error" | "not_requested",
    snapshot: {uploading: [...], downloading: [...], relays: [...]} | null,
    error: str | null
  }
}
```

内存和磁盘中的进度 row 采用相同字段：

| 字段 | 语义 |
|---|---|
| `fid` | 原始整数传输 ID，不截断、不重新分配 |
| `receiver` | 上传 key 中的实际下一跳接收端；下载为 null |
| `peer` | 现有五项进度值中的逻辑对端，不与 receiver 混用 |
| `name` / `path` | 文件名和完整的已有本地路径 |
| `transferred` / `total` / `speed` | 原进度值；speed 未知或关闭时为 null |
| `active` | 该进度 key 是否位于本实例活动 fid 集合 |
| `socket_registered` | 该 key 是否仍在本实例活动传输 socket 表中；不是远端存活证明 |
| `paused` | 上传的本地暂停开关；下载不推断远端暂停状态，填 null |

磁盘不持久化运行时活动/socket/暂停状态，因此磁盘 row 的 `active`、`socket_registered`、`paused` 均为 null，不伪装成 False。内存字段仅描述查询时观察到的本地状态，不增加完成 ACK 状态字段。

### 3.2 “全部已有记录”的边界

- 内存上传和下载全部列出，包括活动、暂停、中断、恢复读回及字节 100% 仍保留的记录，不只展示 active 项。
- 上传按完整 `(receiver, fid)` 展示；不同 receiver 的同号记录不折叠。下载和上传也不按裸 fid 合并。
- Server 的 `relays` 列出权威 `_fid_routes`：上游 fid、originator、完整目标与各下游 fid、completed；不把反向索引重复算作记录，不编造 Server 未保存的文件路径或逐文件字节进度。
- Client 的 `relays` 列出现有 `_forward_resume_records` 中的恢复元数据；不把它伪装成 Server 的完成集合，不新建替代表。
- 进度开关关闭时仍返回所有实际已有记录，并明确 `progress_enabled=False`；本来未记录的传输无法补查。公钥推送不生成虚假进度。
- 已成功删除的记录不是历史任务，不能查询回来。磁盘文件不存在与存在但为空分开表达；损坏、权限错误或不支持的旧格式显示 `error` 和文件路径，内存结果仍可查看，原文件不被改写。
- 不新增最近更新时间、僵尸评分或“已确认完成”推断。`active=False`、socket 未登记、speed 为零/null、100% 字节均不足以独立证明是僵尸。

### 3.3 并发与开销

复用现有锁和既有快照顺序；只在需要的锁内复制对应状态，释放后格式化和输出，不在路由/暂停锁内读磁盘或打印。沿用 `_transfer_progress_lock` → 路由/恢复记录锁的既有快照顺序，不引入反向嵌套。暂停状态单独观察。

返回的是可能随传输变化的观察快照，不承诺内存、磁盘、暂停开关在同一时刻原子一致。不遍历 socket 做探测、不读取文件内容或计算文件 hash，不添加周期扫描/后台线程。

## 4. 本地控制台命令

```text
/transfers
```

- Server 的 `console_input` 与 Client 的 `interactive_mode` 均在本地处理，调用同一个公开查询契约。
- 输出完整、缩进的 JSON，包含内存与磁盘两部分，不截断记录；使用直接命令输出，不依赖 debug 或普通日志开关。
- 不带过滤器、不增加删除命令。附带不支持的参数时打印用法，不回退成网络聊天消息。
- Server 本地 `/help` 增加说明。Client 的既有 `/help` 仍发给 Server，本次不另建 Client 帮助系统；在 Client API 文档写明本地 `/transfers` 用法。
- 命令依赖既有控制台循环在运行；实例未连接/控制台未启动时通过公开 Python 方法查询，不新增独立离线 CLI 或远程管理通道。

数据流：本地 API/控制台 → 既有内存快照 + 可选只读磁盘快照 → 带作用域和来源的 JSON → 用户核对。没有状态回写或网络出口。

## 5. 路径全集与验收映射

以下为本任务必须覆盖的路径；文件/目录项、普通/恢复、明文/加密按同一收发核心执行的组合验证，不把单个样例当作全集证明。

| 路径 | 必须修改点或保持项 | 验收证据 |
|---|---|---|
| Server→Client：开始符号阶段错误 | O1：立即 False；不发送后续文件元数据或生成虚假进度 | 真实 socket 返回错误符号；发送函数结果为 False；无成功事件 |
| Server→Client：文件发完后错误 | O1：立即 False；已有可恢复上传不删 | 文件/目录项、首次/恢复分别执行；结果 False，完整 scoped key 保留并可读回 |
| Client→Server、Server→Client 正常文件与目录项 | 保持两端成功 ACK 与清理语义；Client 已修分支不重复实现 | 两向真实传输，内容相同；正确 owning 记录清理，同号其他接收端不受影响 |
| Client→Server：最终 ACK 返回 False 或抛异常 | O2：以发送结果决定 finished_ok | 两种失败均保留完整目标文件、原 fid/peer/path/字节数和持久化接收行 |
| 已完整接收记录的恢复与再次失败 | O2：沿用现有 EOF 确认，不改内容和路径 | 原 fid、原完整路径恢复；EOF 不追加内容；再失败仍保留，成功后清理 |
| 数据未齐、断线、超时与源文件不存在 | 保持既有失败/保留边界，不扩展超时或重试 | 既有相关回归及局部真实失败场景；不提前报成功 |
| 上述双向普通文件及关键 ACK 失败路径的加密模式 | 继续经过既有安全收发入口 | 真实加密连接传输/失败保留/恢复；握手成功，不新增明文发送旁路 |
| `/crypto_pub_key` 接收与 ACK 失败 | O2：TOFU/ready 不因 ACK 失败跳过，仍不记普通进度 | 既有公钥 ACK 丢失回归及真实握手；普通进度中无公钥行 |
| 转发零/部分完成、原 ID 恢复 | 收尾改动不回退更改 3；O3 展示而不改变路由 | 现有逐目标 ACK、完成集合及稳定 ID 回归；查询后状态未变 |
| Server/Client 内存查询 | O3：上传/下载/各自 relays，含活动、暂停、中断、100% 保留记录 | 同号不同 receiver、跨方向同号均分别列出；活动标志正确，未知不编造 |
| Server/Client 磁盘查询与尚未加载记录 | O3：默认读取但不恢复；内存和磁盘分离 | 含磁盘独有行和落后快照；查询前后内存状态及磁盘文件字节不变 |
| 磁盘缺失/空文件快照/损坏/不支持格式/读失败/跳过读取 | O3：missing、ok 空快照、error、not_requested | 各状态显式可区分；内存可见；不覆盖/删除旧文件 |
| 未连接、进度开关关闭、速度关闭、空记录、并发完成/恢复 | O3：不因查询启动连接；不虚构未记录任务或强一致性 | 实例 API 可直接查询；速度未知为 null；反复查询不改变传输及记录生命周期 |
| 两端本地控制台 | O3：本地拦截 `/transfers`；参数错误也不发网 | 实际控制台输入、观察完整 JSON；普通日志关闭时仍可见；对端不收到该命令 |
| 现有 Web `/api/transfers` | 本轮不替换、不改字段或权限，不经 Web 暴露完整诊断数据 | 既有 Web 回归保持，接口继续原有轻量进度输出 |

## 6. 原子拆分与文件列表

按下表顺序执行，每项完成局部验证和报告后停在人工审查闸门；不自行创建 git 提交，也不把一次批准视作可越过逐项审查。

| 项 | 单一意图 | 修改/新增文件 | 预计代码、测试及接口说明改动 | 局部验证 |
|---|---|---|---|---|
| O1 | Server 普通发送只按最终 ACK 成功 | 修改 `PyFlow/network_api/connect_tcp.py`、`test/integration/network_api/test_tcp_transfer_resume.py`、`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/changes/transfer-fid-routes.md`；新增 `docs/plans/ordinary-transfer-progress-inspection-o1-report.md` | 40–120 行，另附报告 | 开始/最终阶段错误、正常成功、文件/目录项、恢复及加密的真实发送行为 |
| O2 | Server 普通接收 ACK 明确失败后保留 | 修改上述生产/恢复测试/传输说明/变更说明；新增 `docs/plans/ordinary-transfer-progress-inspection-o2-report.md` | 80–180 行，另附报告 | False/异常、原路径保留、进程读回、EOF 再确认、加密和公钥 ready |
| O3 | 两端公开本地完整只读观察入口 | 修改 `connect_tcp.py`、`test/unit/network_api/test_TCP_client.py`、`test/integration/network_api/test_tcp_broadcast_alloc.py`、`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Server_APIs.rst`、`docs/source/Network_APIs/TCP_Client_APIs.rst`、`docs/source/changes/transfer-fid-routes.md`；新增 `test/unit/network_api/test_transfer_progress_query.py` 和 `docs/plans/ordinary-transfer-progress-inspection-o3-report.md` | 300–450 行，另附报告 | scoped identity 不合并、内存/磁盘分离、只读错误边界和两端实际命令输出；本轮新增回归已运行 |
| O4 | 两端按名列举/删除本地配置与关闭后放弃进度 | 修改 `connect_tcp.py`、两端 Network API 文档、文件传输说明和变更说明；新增 `test/unit/network_api/test_delete_config.py`、O4 原子报告与补充计划总报告；既有恢复集成测试复用且不改写 | API/生命周期为单一 cutover；回归覆盖调用边界和持久化竞争 | list 模式只读、按名白名单与错误边界、停止后清理、重复关闭/迟到写回不复活、新进程不恢复旧任务；详见第 9 节 |

四个原子项共同更新本计划的状态；批准后产生本任务总报告 `docs/plans/ordinary-transfer-progress-inspection-report.md`。O4 是本补充计划的第四项，不等同于原 FID 大计划的原子更改 4。如实际规模超过约 500 行或出现新执行路径，先修订拆分，不边实现边扩张。

既有 `test_tcp_file_transfer.py`、`test_crypto_tcp.py`、Web 与转发测试先复用回归，不为相同路径新增重复测试；仅当必要的 consumer-visible 契约确实缺失时补充相应行为用例。永久测试关注误判成功、丢失可恢复记录、作用域混淆、只读边界与错误状态，不增加源码/文案/别名转调或格式快照测试。

## 7. 验收命令与运行时证据

以下是**批准后执行的命令和场景，不是本轮已执行结果**。

定向与完整相关回归：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py -q
.venv/bin/python -m pytest test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_TCP_client.py test/integration/network_api/test_tcp_broadcast_alloc.py -q
.venv/bin/python -m pytest test/unit/network_api/test_delete_config.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_message_event_store.py test/integration/network_api/test_send_message_store.py -q
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_crypto_tcp.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_async_server.py test/unit/transfer_web/test_web_transfers.py -q
```

- O1/O2 批准后先保留能击中旧行为的契约回归，修复后同路径通过；历史报告的成功数量不挪用为新证据。
- 独立临时 smoke 驱动真实 Server/Client 与文件传输：只在指定的最终符号发送边界注入 False/异常，不 mock 整条传输。记录函数结果、保留的 scoped key、文件路径/大小/内容 hash、磁盘快照、EOF 恢复后的最终清理；加密场景同样真实运行。
- O2 用新实例/新进程读回失败保留的记录，证明不是只存在于当前内存；不替换成手工构造的一份“期望快照”。
- O3 的 smoke 通过两个真实控制台输入 `/transfers`，观察完整输出、磁盘读取状态及对端没有收到查询命令；另直接调用离线实例 API。用查询前后文件 hash 和状态比对证明只读。
- 汇总观察值和实际执行命令到各原子报告；不以“测试通过”替代逐路径运行时证据。Smoke 临时文件在证据记录后删除。
- 对实际改动 Python 文件执行编译和静态检查。既有 F841 等存量问题单独列出，不顺手修复，不将既有 lint 失败改报为通过。

## 8. 接口、兼容性及人工闸门

- 新公共契约为两端 `get_transfer_progress`、`delete_config` 和本地 `/transfers`；需人类批准后实施。既有公开方法签名不变；普通发送返回值纠正为其既定成功语义，O4 明确放弃进度后的旧对象不得重新启动/连接，必须新建实例。
- 不触及 CI、依赖、覆盖率策略、五项持久化模型或远程协议。既有 Web 端点与 UI 刻意不改，避免把完整路径/历史快照扩散到其现有可访问范围。
- 规则沉淀采用行为回归：错误符号不能成功清理、ACK 明确写出失败不能丢接收记录、查询不能恢复/写回或合并不同作用域身份、列表模式不能删除、显式清理后重复关闭/迟到写回不能复活进度文件。
- 明确未覆盖：应用层 ACK 收据、自动去重/重传、逐 fid 人工删除、全局僵尸判定、跨实例/进程独占与共享文件隔离、变更端点迁移、历史完成任务以及未被进度开关记录过的任务。
- 人类已审计通过 O3 并明确“开始O4 delete_config更改”，现仅执行补充 O4；实现、局部验证与原子/总体报告完成后交付人工审查。原 FID 大计划的更改 4–6 仍单独等待批准，不自行提交、合并或推送。

## 9. O4 补充：按名删除本地配置/状态文件

人类已确认此公共契约并明确开始 O4：Server 与 Client 均增加 `delete_config`；按传入的配置文件名删除；`is_list_config=True` 时只返回可删除文件名组成的 list，默认 False。用户可在关闭实例后主动删除持久化进度快照，放弃其中疑似僵尸任务的恢复记录。不增加自动清理或远程删除命令。

### 9.1 方法与返回值

```python
delete_config(self, config_name=None, *, is_list_config=False) -> bool | list[str]
```

- `is_list_config=True` 优先进入纯查询模式，即使同时传入名字也不删除；返回本实例白名单中当前存在、可作为删除目标的文件名，排序后以 `list[str]` 返回，没有则返回 `[]`。这里表示 API 允许删除，不保证调用者的文件系统权限；实际删除失败须报告。
- 默认 `is_list_config=False`，每次明确传入一个文件名；成功删除返回 True，白名单内文件本来不存在返回 False。没有名字、空名字、未知名字或带路径的名字抛 ValueError；权限等实际文件系统错误向调用者抛出，不吞掉并声称成功。传入非字符串名字抛 TypeError。不提供默认删除全部、通配符、目录递归或批量删除。
- 列表模式可在运行中调用，且不创建目录、文件、连接或关闭实例。删除模式只允许实例未启动或已显式 `stop()` / `close()` 完成；运行中、仅连接意外断开或仍有活动传输/中继时抛 RuntimeError，不隐式关闭或取消任务。

### 9.2 可删除范围与按名解析

范围采用 TCP 实例直接管理的可丢弃持久化状态白名单，不扫描 `.Flow` 中所有文件，也不把所有 `.json` 都当作配置。

| 实例路径属性 | 默认可传入的文件名 | 删除含义 |
|---|---|---|
| Server 的 `file_trans_progress_file` | `server_file_trans_progress.json` | 放弃本文件内全部上传、下载与 server relay 路由恢复记录 |
| Client 的 `file_trans_progress_file` | `client_file_trans_progress.json` | 放弃本文件内全部上传、下载与 client 转发恢复元数据 |
| 两端 `messages_log_file` | `messages_log.json` | 删除已落盘的接收消息记录 |
| 两端 `events_log_file` | `events_log.json` | 删除已落盘的接收事件记录 |
| 两端 `sent_messages_log_file` | `sent_messages_log.json` | 删除已落盘的发送消息记录 |
| 两端 `sent_events_log_file` | `sent_events_log.json` | 删除已落盘的发送事件记录 |

名称取实例当前路径属性的 basename，实际删除使用该属性已配置的完整路径；支持既有实例文件路径重定向，不根据用户输入拼任意路径。Server 默认不列出/删除 Client 的进度文件，反之亦然。若两种属性重定向为相同 basename、却指向不同文件，则拒绝歧义，不猜测或一次删除两个文件。符号链接或目录不作为普通删除目标，删除时明确拒绝，不跟随链接删除其目标。

不纳入：协议必需的 `decode_command_table.json`、端口分配/锁文件、私钥/公钥/TOFU 注册表、启动器 `setup.json`、Web 配置/用户数据库/登录凭据、其他实例的文件以及传输的源文件或接收文件。扩大白名单须另行明确批准；本 API 不是任意文件删除工具。

当前默认路径由多实例共享，四种消息/事件日志还可能由 Server 与 Client 共用；对象归属不等于磁盘文件独占。删除前必须停止所有使用同一路径的实例/进程，否则其他写入者仍可能重建它。本次不建立跨进程文件所有权或隔离系统。

### 9.3 关闭、删除和防止进度复活

推荐顺序：O3 查询两端内存与磁盘记录 → 用户判断并备份仍需恢复的信息 → 停止涉及的实例并确认无活动传输/中继 → `delete_config` 删除指定文件 → 新建实例启动。列表/删除均为本地方法，不增加 `/delete_config`、HTTP 端点或在关闭时无条件清理的默认行为。

现有 `stop()` / `close()` 会调用 `_persist_transfer_progress()`；仅在关闭前 unlink，或者关闭后 unlink 但允许重复关闭/迟到 finally 继续写回，均不能达到清理目的。因此 O4 包含必要的生命周期约束：

1. 没有请求删除时，关闭仍按原规则保存所有可恢复记录；不改默认持久化语义。
2. 在 `_transfer_progress_lock` 内串行检查静止条件并删除进度文件；必须与正在执行的快照写回互斥，不能留下随后覆盖回来的临时快照。
3. 成功删除、或明确删除的进度文件已不存在时，当前对象进入“此进度快照已主动放弃”的终结状态；其后 `_persist_transfer_progress` / `_load_transfer_progress` 不再写回或恢复这份已放弃快照。此状态仅存于当前对象，不写 tombstone 文件、不修改 JSON 格式，也不重置全局 FID 分配器。
4. 同一对象重复 `stop()` / `close()` 和迟到收尾不得重建进度文件；主动放弃进度后，重新启动/连接该对象必须明确拒绝，使用新实例恢复正常工作，避免旧内存任务在新会话复活。不把原对象内存记录静默挪到新实例。
5. 删除是文件级放弃，不逐 fid 识别僵尸，也不承诺只删僵尸。完整/部分接收文件和源文件始终保留；对端的进度快照不会被联动删除，需分别检查和操作。
6. 消息/事件日志删除只清理指定落盘文件；不会改变消息协议、删除其他日志或建立永久禁写策略。关闭刷盘后执行；重复关闭没有新记录时不应生成被删日志，若迟到产生新消息记录则须先完成相关 worker 收尾再操作。

### 9.4 O4 路径与验收

| 路径 | 必须观察的证据 |
|---|---|
| Server / Client 列表；默认参数；同时传入名字和 True | 返回真实 list，仅含本实例白名单中存在的合法普通文件；列表查询前后文件字节、内存及运行状态不变，不能误删 |
| 关闭后分别按名删除进度和四类日志 | 指定文件消失、未指定文件字节不变；结果 True；新实例不再读回被删进度 |
| 合法文件缺失与重复删除 | 返回 False；不新建文件；对进度仍落实明确放弃后的防复活边界 |
| 无名字、非法类型、未知名字、绝对路径、`..`、通配符、目录/符号链接、重定向 basename 冲突 | 明确报错，范围外文件与链接目标不变 |
| 无删除权限等真实 I/O 错误 | 异常可见，不报成功，不留下已经删除的假象；不更改其他目标 |
| 运行中、意外掉线未关闭、关闭仍有活动传输/中继 | 拒绝删除，进度/路由不丢失；列表模式仍只读可用 |
| 与写回串行、重复关闭、迟到持久化/读取、同对象重启 | 删除返回后文件不复活；已放弃对象不能重启；新对象仍可正常保存新的任务 |
| 用户判断后放弃进度；磁盘格式损坏或不支持 | 不要求能够解析快照才允许按白名单删除；源文件/接收文件和其他配置保持不变 |
| 默认关闭未调用删除 | 可恢复进度照常写回并可由新实例读回，不弱化既有恢复 |

O4 是独立原子项，接在 O3 后，不是原 FID 大计划中的“原子更改 4”。生产变更集中于 `connect_tcp.py`；新增 `test/unit/network_api/test_delete_config.py`，同步两端 API 文档、文件传输说明、变更说明与报告。既有恢复集成测试不重复加同路径装配，保留并完整回归；真实关闭/删除/新进程验证由独立烟测覆盖。生命周期与删除必须同次 cutover，不分离出会复活进度的 unlink-only 接口。

批准后的定向验收命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_delete_config.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_message_event_store.py test/integration/network_api/test_send_message_store.py -q
```

独立运行时 smoke 在临时目录用真实实例产生文件进度及日志，停止并完成 worker 收尾后列举和删除指定文件；观察重复关闭/写回不复活、新进程不恢复旧任务、其他配置与传输内容 hash 不变。用默认未删除的实例作保持原持久化行为的对照。报告只记录实际执行的结果，不把计划中的验收当成已通过。

### 9.5 已实现用法（默认文件名）

```python
# 列表模式只读；运行中也可以查看。
server_names = server.delete_config(is_list_config=True)
client_names = client.delete_config(is_list_config=True)

# 由用户决定放弃整份进度；先关闭并确认活动传输/中继已退出。
server.stop()
server.delete_config("server_file_trans_progress.json")

client.close()
client.delete_config("client_file_trans_progress.json")
```

示例使用默认文件名；重定向路径时使用列表返回的实际 basename。未选择的日志不会被删除。新一次运行创建新的 Server/Client 实例，不复用已放弃进度的对象。

## 10. O1 已批准实施记录

- 授权：人类明确“审计完毕，开始01的更改”。本次只修复 Server 普通发送的开始等待与最终确认等待两处错误符号退出；不改接收端 ACK 生命周期，不实施查询或删除 API。
- 改前真实回归：文件/目录项、offset=0/4096 四种组合均收到真实错误符号，却返回 True 并打印成功日志；原子报告记录命令与改后证据。
- 实现：两处 `break` 改为 `return False`；既有 `finally` 的 owning-entry、失败持久化、活动 socket/暂停清理不变，公共方法签名、线路与最终 ACK 时限不变。
- O1 验收报告：`docs/plans/ordinary-transfer-progress-inspection-o1-report.md`。当时新增回归 4 passed、完整相关套件 237 passed，独立真实程序验证明文/加密、文件/目录项、首次/恢复及 EOF 再确认。后续人类已审计通过 O1 并批准开始 O2；该历史数量不作为 O2 本轮证据。

## 11. O2 已批准实施记录

- 授权：人类明确“审计通过，开始02”。仅修复 Server 接收最终 ACK 明确失败后丢失接收行的问题，不更改协议、持久化结构、ACK 时限或 O1 发送端语义。
- 决定：复用 Client 接收端现有模式，`finished_ok` 仅依据最终 ACK 的实际发送布尔结果设置；保持本地通知、事件更新和公钥注册在 ACK 前的顺序。
- 本轮验收：新增回归 4 passed、完整相关套件 241 passed；独立真实程序验证 16 组明文/加密、文件/目录、首次/恢复、False/异常及重复 EOF 确认，并另起进程读回真实失败快照 32 次。进度关闭与连接前注入的公钥 ACK 失败均已运行。编译通过，F/E9 仅既有 F841；证据见 `docs/plans/ordinary-transfer-progress-inspection-o2-report.md`。一次性脚本已清理，O2 待人工审查，不进入 O3。

## 12. O3 实施与验收记录

- 授权：人类明确“审计通过，开始更改03”。仅增加 Server/Client 的同名本地 `get_transfer_progress` 与 `/transfers`；不增加远程协议、HTTP 端点或删除功能。
- 复用两端 `_transfer_progress_snapshot` 和 `_read_transfer_progress`。返回的 relay 列表由既有快照生成，每条用整数 `fid` 标识快照主键；Server 保持 originator/targets/completed，Client 保持既有恢复元数据，不统一成虚假字段。
- 运行时活动/socket 状态在进度锁内复制，暂停状态单独读取；磁盘读取不持有路由/暂停锁。只读格式化共用一个实现，不更改持久化来源或 Web 输出契约。
- O3 当轮 49 个局部回归、独立真实 API/控制台烟测与完整相关套件 264 passed 已完成，证据见 `docs/plans/ordinary-transfer-progress-inspection-o3-report.md`；随后人类已审计通过并授权开始 O4。该历史数量不替代 O4 本轮验收。

## 13. O4 批准实施记录

- 授权：人类明确“审计完毕，开始O4 delete_config更改”。O1–O3 已通过人工审查，本项实施范围以第 9 节为准，不实施原 FID 大计划的原子更改 4。
- 生命周期边界：区分未启动、启动过但尚未关闭、关闭正在执行和关闭完成；只允许静止实例删除。进度的删除与持久化/加载共用既有进度锁，明确放弃后旧对象不再保存/加载，启动入口在网络操作前报错。
- O4 本轮结果：定向回归 101 passed，完整相关套件 332 passed；独立真实程序在明文/加密、上传/下载、默认保留/主动删除 8 组双端场景中验证关闭、按名清理、32 次新进程读回及新实例正常运行，实际权限失败可见且原文件不变。编译与新增测试 F/E9 通过；联合检查仅既有 F841。证据见 O4 原子报告，补充项路径全集与风险见总报告。O4 和补充项总验收待人类放行；临时烟测脚本已删除。

