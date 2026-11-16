# 双向文件暂停、继续与按对象恢复 — 实施报告

状态：需求卡和七项拆分已确认。**C1–C4b 已提交并通过人工放行；C5 文档与相关合并验收已完成，待人工最终审查。** 各阶段记录保留实际失败及定向重跑，不将未运行或未覆盖的场景标为完成。本轮未创建提交、合并或推送。

## C1：严格解析、typed identity 与共享端点识别

### 提交意图与改动内容

建立后续控制状态机可复用的私有协议和身份基础，等价抽取既有 resume 的端点判定，不激活新 pause/start 协议。

| 文件 | 关键改动 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 新增 `_validate_transfer_control_identity`、`_parse_transfer_control_command` 和 `_transfer_control_key`；新增 `_is_control_endpoint`，供 Server 本地端点判定及 Client `resume_trans` 的 direct 判定复用 |
| `test/unit/network_api/test_transfer_control.py` | 新增解析/本地参数边界、peer/fid/type/双向 key 隔离，以及真实 socket 的别名、wildcard、同端口异地址拒绝和关闭回退契约 |
| `docs/source/changes/transfer-fid-routes.md` | 追加 C1 私有基础和暂未切换生产入口的变更说明 |
| `docs/plans/transfer-pause-start-direction-plan.md` | 记录已确认拆分、C1 授权与验证状态、公共协议/API 的后续许可点 |
| 本报告 | 记录实际命令、路径覆盖边界和未覆盖项 |

### 为什么这样改与数据流

- P01：新 parser 严格接收四 token 的 pause/start，输出 `(paused, fid, trans_type, direction)`；非法命令返回 None。`fid` 非负，type/direction 只能取 0 或 1；本地 identity 校验拒绝 bool、浮点和字符串强制转换，抛出 ValueError。
- P02 的身份基础：经过现有 peer 归一化的地址 → `_transfer_control_key` → `(peer, fid, trans_type, local_direction)`。收到 wire 消息的调用方使用 `1 - direction` 作为本地角色；本阶段不登记实际 leg 或状态，不迁移 progress key。
- P12：Server 的 origin 判定 / Client 的 resume target 判定 → 从当前 socket 获取 concrete endpoint → `_is_control_endpoint` 精确比较 configured 或 concrete endpoint → 继续原有 direct/relay 恢复逻辑。没有按端口匹配，也没有独立别名表；Client 仍以配置地址作为 upload 行 key。
- `_pause_command_fid`、生产 handler、公开 pause/start/remote API 和现有控制消息保持原样；C2b 负责一次性移除旧 parser 并迁移生产者、消费者及调用点。

### 局部验证

执行一次：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py::test_resume_uses_scoped_route_not_id_lowbit_and_rejects_forged_endpoints test/integration/network_api/test_tcp_transfer_resume.py::test_resume_identity_uses_the_connected_endpoint test/integration/network_api/test_tcp_transfer_resume.py::test_closed_control_socket_falls_back_to_the_configured_endpoint -q
```

实际结果：**81 passed in 17.77s**（命令墙钟 18.43s）。

运行时证据：

- 新基础契约通过真实 IPv4 listener/client 连接获取 accepted socket 本地端点和 client peer 端点；配置 `localhost` 或 `0.0.0.0` 均与 concrete 地址匹配；同端口 `127.0.0.2` 被拒绝。关闭 socket 后仅配置地址仍可作为回退身份。
- 复用既有 `test_resume_identity_uses_the_connected_endpoint` 四个场景：`localhost` / wildcard bind × configured / connected 旧行。每个场景在真实控制与文件 socket 上同时验证 Client→Server 和 Server→Client 从实际半文件 offset 续传；断言最终文件字节内容等于完整 payload，且原 fid 的双端进度行在完成后移除。
- 既有闭合控制 socket 回归确认 Server 广告端点回退配置身份，其他端点被拒绝；既有 route 回归确认错误 peer/origin/target/fid 不改变 route 或生成恢复请求。
- key 契约对相同 fid 的不同 peer/type/local role 分别读写，包含同端口异地址与不同 fid，确认只有选中项改变。它验证身份基础，尚不是生产暂停效果证据。

### 明确未覆盖

| 路径 | C1 结论 |
|---|---|
| P01 | 新 parser 和本地 identity 边界已覆盖；生产 handler 的严格切换留给 C2b |
| P02 | typed key 身份隔离已覆盖；活跃双向数据 leg 暂停隔离留给 C2a/C2b |
| P12 | 共享端点判定与既有真实双向 resume 身份回归已覆盖；pause/start 接入及旧 owner 清理留给 C2b/C4b |
| P03–P11、P13–P18 | 未在 C1 实现或验收；按原计划的状态机、普通/转发接入及对象级恢复阶段处理 |

C1 没有改加密入口、安全就绪、数据循环、路由占用规则、恢复资格或持久化模型；因此本轮结果不作为这些属性的新验收证据。没有运行广泛套件、formatter、lint 或多平台验证。新增测试是基础契约，未删除或弱化既有测试。

### 风险与待确认

- 语言服务 references 请求返回 `-32601 Unknown request: textDocument/references`，不具备导航引用能力。已按已定位调用点做私有函数抽取；本阶段不修改公共签名，不能将该服务请求计作引用分析。
- 新 parser 与旧 parser 暂时同时存在是获批 C1→C2b 拆分边界；新 parser 仅供后续基础接入及契约验证，未对外宣称新控制行为已可用。C2b 必须移除旧 parser，不留兼容分支。
- C1 后续审查结论：用户已审计放行，并明确授权 C2a。C1 证据保留如上；C2a 实施及其独立待审事项见下节。

## C2a：发送端权威状态机与暂停键生命周期

### 提交意图与改动内容

为后续普通/转发数据 leg 共用一个私有控制登记表，完整实现串行决定、请求/通知语义及 owner 生命周期。C1 已由用户审查放行，本轮授权仅为 C2a。

| 文件 | 关键改动 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | 新增 `_TransferControlRegistry`，Server/Client 均初始化该登记表，共用实例现有的暂停表和 Condition；尚未注册生产 leg 或迁移公开入口 |
| `test/unit/network_api/test_transfer_control.py` | 补充可控 FIFO 双端调度、独立期望状态、阻塞写入排序/唤醒、预置暂停、身份与 owner 隔离、失败清理、真实持久化内容契约 |
| 计划、变更记录、本报告 | 记录 C1 放行、C2a 实施范围、命令结果及生产切换的后续许可点 |

### 数据流与不变量

- 登记：规范 typed key + 实际 control socket + leg owner → `register` → 独立 leg 锁及运行状态。冲突 owner/socket 拒绝覆盖；同 owner 重复登记复用记录。只有发送角色接管该 key 的预置暂停；`announce` 在对端可定位后公布一次。
- 发送端本地操作或收到接收端请求 → 同一 leg 锁 → 决定实际状态 → 更新暂停键 → 在锁内提交 direction=0 通知。对同状态远端请求仍发送确认；本地同状态操作忽略。
- 接收端本地操作 → 仅当目标同时等于 `confirmed_state` 和 `last_requested_state` 时忽略，否则更新最后意图并按序提交 direction=1 请求。接收 direction=0 只更新确认值，不覆盖最后意图、不回包。
- start 移除暂停键并唤醒等待者；完整运行记录与暂停标志分开。接收角色从不通过确认值阻止 recv。未知 wire key、错误会话均返回 False，不创建预置项。
- `release` 仅释放该记录的 owner，清理暂停、确认/请求意图及初始通知标记并唤醒等待者；`discard_socket` 仅清理匹配实际 socket 的记录。旧记录再次清理不影响替换 owner。发送 False/抛异常会释放该记录；异常继续上抛，不重试、不伪造确认。
- 锁序为 leg 锁 → pause Condition；定位阶段先释放 Condition 再等待 leg 锁。消息发送回调在 leg 锁内调用，因此同一 leg 状态决定与发送顺序一致；不同 leg 使用独立锁。回调契约要求安全就绪门槛 + 现有 `send_message`，C2b 才接入生产安全发送与接收线程调度，不能在接收线程阻塞其自身的密钥交换。

### 局部验证与运行证据

首次定向命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/unit/network_api/test_transfer_progress_query.py -q
```

结果：**154 passed in 0.99s**（墙钟 1.32s），出现一项 `PytestRemovedIn10Warning`：参数化传入 `itertools.product` 迭代器。随后将参数化数据改为 tuple，仅重跑受影响用例：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py::test_every_fifo_delivery_interleaving_converges_after_finite_input -q
```

结果：**64 passed in 0.62s**，无告警。

机械证据：

- 枚举双方 pause/start 的全部 64 个三操作组合；每个组合递归枚举本地提交与双向 FIFO 消息的全部可达交错。每条终态均检查 S 决定值 = R 确认值 = 独立记录的最后一次 S 串行处理意图，并限制通知数量以捕获回包循环。另测重复请求必须确认及 R 快速 pause→start 不能丢 start。
- 线程事件将第一个发送回调固定阻塞，另一个操作在尝试获得同一 leg 锁前发出事件。确认后续 start/释放不能抢先修改状态；解除阻塞后验证通知顺序、等待者确实收到 Condition 通知，以及线程全部结束。使用 Future 传播线程断言，不用 sleep 或仅检查线程存活。
- 预置暂停接管/单次公布、公布前 start 取消、未建连 start 删除 key、错误 peer/socket/type/反向 role 隔离、重复旧 owner 清理及 False/异常写失败均有状态断言。
- Server/Client 各写一次真实进度 JSON，完整内容等于控制操作前的 snapshot，确认新增运行时状态没有进入持久化文件；既有进度查询契约一并通过。

另运行一次标准输入临时脚本（未落盘），四组 `socket.socketpair()` 通过真实 `TCP_Server_Base` / `TCP_Client_Base.send_message` 传递新控制帧，再将 socket 读取结果交给新 parser 和 registry：

| 发送端 | type | S→R 帧 | R→S 帧 | 排空后 S/R paused | 暂停键 | 同一 socket |
|---|---:|---:|---:|---|---:|---|
| Server | 0 | 5 | 2 | False / False | 0 | 是 |
| Server | 1 | 5 | 2 | False / False | 0 | 是 |
| Client | 0 | 5 | 2 | False / False | 0 | 是 |
| Client | 1 | 5 | 2 | False / False | 0 | 是 |

每组实际经过预置暂停公布、S start、R 快速 pause→start、S 本地操作与远端请求交叉，以及同状态请求确认。最后检查消息已消费、双端状态一致、控制状态不在 progress snapshot，旧会话释放后新 owner 默认运行。脚本退出码为 0。这是明文真实控制写入证据，未经过生产 pause/start handler，也未传输文件 payload，不作为 P03/P04/P11 的验收。

### 路径覆盖、明确未覆盖与风险

| 路径 | C2a 结论 |
|---|---|
| P02 | 活跃状态记录按 peer/type/role 隔离；生产双向数据流隔离留给 C2b |
| P05 | 确定性交错、重复及阻塞发送排序已覆盖；生产 handler/真实文件并发仍需 C2b |
| P07 | 预置暂停接管/解除与未知 wire 不占键已覆盖；真实握手后公布时机在 C2b 接入 |
| P18 | registry 释放/唤醒与 JSON 不含控制状态已覆盖；路由冲突、query 新状态投影、stop/config 的生产清理仍属后续既定阶段 |

本轮未改公开 API、生产 parser/协议、data loop、relay pump、加密门槛、恢复资格或 CI/依赖。新登记表已初始化，但生产 leg 和关闭路径尚未调用它，故不宣称暂停新行为已对外可用。C2b 必须将生产调用、发送调度与生命周期统一接入；不能保留旧控制状态或旁路。未运行 broad suite、formatter、lint 或加密/多平台验证。语言服务能力仍仅 Ruff，无 references 支持。

C2a 后续审查结论：用户已审计放行，并明确授权开始 C2b，包括下节列明的公共协议/API 切换。

## C2b：普通收发端到端接入与协议/API 切换

### 提交意图、接口与数据流

将已审查的控制状态机接入普通收发和 relay 的全部生产控制入口。本轮“开始 C2b”的授权包含计划第 3.1 节的公共协议/API 变更；旧 remote API、旧 fid parser 和独立 writer_pause 状态均移除。

- API → fid/type/本地角色校验 → 配置/当前控制端点归一化 → 精确 typed key（relay 使用该 peer 对应的 child fid）→ registry 串行登记 → per-leg FIFO worker → 等待对端 leg 可定位与既有安全就绪门槛 → 正常 `send_message`。接收线程只分派，不等待密钥交换。
- S 决定与通知保持 FIFO；R 请求与确认值分开。数据 socket 建立前 S 绑定 owner，对端 R 在 startup sign 前登记；S 观察 startup 后释放初始通知门槛。普通/转发、上传/下载与 tracking off 共用入口。
- sender loop 每个新数据块前读取预绑定状态；R 持续 recv/落盘。在途块不丢弃；start 无 seek/truncate/重建。空文件、EOF 恢复与最终 ACK 不等待不存在的下一块。
- 加密开启时，控制发送先等待 framed readiness，再在写入入口拒绝无 framed channel 的控制帧；帧创建阶段检查 secure/frames_allowed，防止重新交换窗口回退明文。未改 plaintext allowlist，未新增 raw send 旁路。
- relay writer 直接等待自己的控制记录，上游高/低水位请求携带 type=1、direction=1；reader 的高水位硬门槛独立保留。活动 owner 冲突仍拒绝，纯预置暂停不作为路由占用。复杂多目标路由/背压的完整验收仍属 C3。
- 数据 owner/实际控制 socket 关闭、stale sweep 或发送失败释放记录和等待者。进度查询从活动 typed sender 记录投影到旧 upload key；持久化字段不变。

### 变更文件与局部止血

实现位于 `PyFlow/network_api/connect_tcp.py`。新增真实控制契约位于 `test/integration/network_api/test_tcp_transfer_control.py`；迁移 `test_transfer_control.py`、`test_transfer_progress_query.py`、`test_fid_routes.py`、`test_tcp_file_transfer.py` 与 `test_tcp_transfer_resume.py` 的调用/状态契约。三份 File_Transfer/TCP_Server_APIs/TCP_Client_APIs RST、变更记录、计划及本报告同步更新。

旧测试中直接构造 pair 暂停键/把活跃 progress 当控制 leg 的样板改为真实 typed owner；断言保留内容、最终 ACK 和清理。首次新场景发现 tracking off 的 EOF 接收恢复已成功但保留旧进度行：让接收 leg 持有既有 resume_entry 供现有完成清理，不为未跟踪的新传输创建进度行。原失败断言未放宽。

### 实际命令、结果与运行证据

首轮：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/unit/network_api/test_transfer_progress_query.py test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_control.py -q
```

**223 passed、2 failed、1 skipped in 180.92s**。失败仅为 encrypted/async/tracking off 的 EOF 上传和下载：文件内容及最终 ACK 成功，但旧 receive progress 行未删除。plain fixture 的 re-key 用例被跳过，encrypted 两组实际运行。

修复旧 receive 行所有权后仅重跑失败项：

```bash
.venv/bin/python -m pytest 'test/integration/network_api/test_tcp_transfer_control.py::test_empty_and_eof_progress_complete_with_real_ack[encrypted-async-untracked-wildcard-eof-upload]' 'test/integration/network_api/test_tcp_transfer_control.py::test_empty_and_eof_progress_complete_with_real_ack[encrypted-async-untracked-wildcard-eof-download]' -q
```

**2 passed in 9.19s**。不将首轮失败抹去或标成全量复跑成功。

已观察运行证据：

- 真实文件 socket：普通上传/下载 × S/R 本地控制，确认后同一 socket 继续，最终 sender 返回真实 ACK 成功且文件字节一致，owner/进度/暂停键清理。
- 同 fid 双向传输分别绑定角色；对一边 start 不影响另一边。非法/旧/缺多 token、错误 type、未知 fid 输入通过真实控制通道送达后不改变活动状态。client 配置别名与实际 peer 地址归一到同 key，含 wildcard bind。
- receiver 快速 pause→start 在首个请求实际发送前登记相反意图；排空通知后 S/R 运行状态一致。另一个真实在途块在 pause 确认后才交给发送调用，接收端仍写入该块。
- Plain、encrypted、encrypted+async+tracking off 三种实际配置执行控制；两组 encrypted 配置触发真实强制重新交换并持有 key publication。R pause→start 仍返回、读线程继续接受真实对端 key，安全门槛打开后 FIFO 请求/通知收敛。观测帧均含 FRAME_MAGIC 且无控制明文片段，未使用 raw 控制写入。
- 临时标准输入脚本通过正式文件夹入口上传/下载各两项，每条在数据循环观察点执行 S pause、R start；四份文件内容一致，四条 leg 的 owner/socket 均保持，最终进度行、控制 owner 和暂停键为零。脚本退出码 0，未落盘。

其后为 owner 隔离补强发送等待条件：仅当前 registry 仍持有同一 state 时等待暂停键，防止旧等待者观察同 key 新 owner 的暂停。执行控制基础及 plain 同 fid 双向实际传输用例：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py 'test/integration/network_api/test_tcp_transfer_control.py::test_same_fid_opposite_directions_and_invalid_wire_are_isolated[plain-alias]' -q
```

**134 passed in 4.12s**。

既有受影响路径一次相关回归：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_crypto_tcp.py test/integration/network_api/test_tcp_async_server.py test/unit/network_api/test_transfer_handshakes.py test/unit/network_api/test_wire_lines.py -q
```

**159 passed in 358.02s**。包含普通文件/文件夹、relay 背压与多目标 ACK、原有断线恢复、实际控制端点识别、加密重新交换、async server、socket/FID handshake 和 wire buffer；不替代 C3/C4 新契约的路径验收。

### 路径对照与明确未覆盖

| 路径 | C2b 结论与证据边界 |
|---|---|
| P01 | 生产分派已严格使用四 token parser；真实控制连接的旧/非法/type/fid 输入及基础参数边界通过，不创建未知 wire 状态 |
| P02 | 真实同 peer/type/fid 反向普通传输独立暂停；纯状态契约覆盖不同 peer/type。复杂 relay 身份全集仍在 C3 |
| P03/P04 | 真实上传/下载，S/R 均能 pause/start，同 data socket 完成内容及最终 ACK；接收在途块继续落盘 |
| P05 | C2a 全部有限三操作 FIFO 交错契约通过；生产连接补充 R 未确认 pause→start 和 S/R 交叉，消息排空后状态一致 |
| P06 | 空文件、EOF 恢复、最终 ACK 及逐文件夹 leg 的真实内容/owner 清理证据通过 |
| P07 | 预置发送暂停在对端登记后公布；start 删除键；未知 wire 不产生预置状态，基础和实际连接均有证据 |
| P11 | 明文及实际加密、重新交换窗口通过；控制写入走安全门槛和 send_message，帧无控制明文片段。加密 relay 的复杂交叉验收仍属 C3 |
| P12 | 配置别名、concrete endpoint、wildcard 以及既有关闭 socket 身份回归通过；旧 source/target owner 全部恢复竞争仍属 C4b |
| P13 | 新控制场景覆盖 tracking off 和 encrypted async 分派，重新交换时读线程继续处理 key；原有线程式/async 回归通过 |
| P18 | 查询 schema 及活动 typed pause 投影、JSON 无控制字段、完成/关闭释放均通过；stop/config 与转发恢复竞争的完整验收在 C4b/C5 |
| P08–P10、P14–P17 | 仅复用既有相关回归，不标为新增契约已全验收；分别按 C3 和 C4a/C4b 路径清单继续 |

没有改 CI、依赖、coverage 策略或 progress JSON schema；没有删除/跳过既有测试或放宽失败断言。本轮唯一 skip 是纯明文 fixture 不适用重新交换场景，两组加密场景均实际运行。未运行 repo-wide suite、formatter、lint、文档构建或多平台检查。

### 风险与审查注意

- **变更规模超出计划预估的 300–500 行**：单是新集成契约文件为 387 行，此外还有生产接入、旧入口删除、既有调用迁移和接口文档。此次保持一次协议生产者/消费者同步切换，未擅自拆成混用新旧协议的中间阶段。该规模偏差必须在 C2b 人工审查中明确接受，或按仓库流程退回计划重拆；不以测试通过代替放行。
- 状态登记成功仅表示已进入控制 FIFO，不代表对端确认。控制发送失败沿既有错误/关闭机制终止会话，不重试、不降级；不承诺仅有 data socket、控制连接不可用时仍可远程控制。
- 生产控制 FIFO 只排控制消息，不排每块文件数据。活动记录与暂停键分离；路由冲突仍检查真实 owner，复杂 relay 路由/背压、共享 source 门槛及恢复资格没有在本轮提前宣称交付。
- 语言服务仍只有 Ruff、无 referencesProvider；公开符号引用请求返回 `-32601`，随后按调用枚举迁移。不能将该请求计为成功引用分析。
- C2b 后续结论：用户已放行 C3，本阶段的协议/API 切换及规模偏差随该放行进入下一阶段。C3 实施及其独立待审事项见下节。

## C3：转发精确控制、用户意图与共享背压

### 提交意图与改动内容

完成 P08–P10 的转发控制：每条 downstream leg 独立定位并沿同一 socket 继续，shared upstream 的用户暂停和自动背压互不覆盖；reader 通过独立高水位门槛保持有限缓冲。本轮不改 resume 资格。

| 文件 | 关键改动 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | registry 为 receiver 分开记录 user_paused/backpressure 并汇总请求；转发索引仅存 canonical child；唯一活跃同 peer alias 查找；writer 在 connect 前绑定控制并在失败时释放；END 不等待数据暂停；reader/header enqueue 硬门槛与有限缓冲观测；真实 setup socket 占用保护 |
| `test/unit/network_api/test_transfer_control.py` | 精确 child 优先、同 peer 唯一活跃 alias、错误类型/角色/peer、陈旧映射与 owner、用户/压力 OR 和通知意图隔离 |
| `test/unit/network_api/test_fid_routes.py` | 预置/False/无数据 owner 的控制记录不预占路由；真实 setup socket 在 active index 尚未登记时拒绝覆盖 |
| `test/integration/network_api/test_tcp_transfer_control.py` | 两目标真实 relay 控制、错误命令隔离、未暂停 target 的真实 ACK、用户/压力交叉、source start 不开读门、关闭 target 及峰值上界证据 |
| File_Transfer RST、计划、变更记录、本报告 | 同步 upstream OR 语义、child 优先、缓冲边界及实际验收记录 |

### 数据流与不变量

- public 或 wire 控制 → peer/type/local role → exact child key 优先 → 仅在无 exact child 时接受同 peer 唯一活跃 writer 的 upstream alias → 原有 per-leg 串行决定/FIFO → 对应 target。索引不再同时写 child/upstream 别名，避免两个 route 的数字交叉时覆盖。
- server 用户对 upstream receiver 的 pause/start 更新 user_paused；buffer high/low 更新 backpressure，均提交 `user_paused OR backpressure`。收到 source 的权威通知只改 confirmed，不清理任一意图。背压操作携带 upstream owner，旧 pump 无法改新记录。
- source 本地 start 仍是发送端权威决定，允许暂时确认 running；relay reader 的独立 backpressured 门槛不会因此打开。只有 buffered 降到 low water 才解除该门槛。header enqueue 同样经过门槛，峰值必须小于等于 high_water + max_enqueue，后者为实际最大单次 fan-out 入队字节数。
- writer 先取 item，END 直接进入最终 ACK；仅真实数据发送前等待暂停。数据 connect 前登记 writer，让 receiver startup 之后立刻发来的请求可定位；connect/安全握手/startup 失败立即释放绑定。
- 无真实数据 owner 的记录、False 残留及精确预置暂停不独立阻挡路由；真实 socket/setup owner、active socket/progress/route 冲突仍原子拒绝，不授予覆盖进度行的能力。

### 局部验证

主定向验证：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_control.py -k 'not receiver_pause_start_waits_for_real_rekey_without_blocking_key_reader' -q
```

**217 passed、3 deselected in 274.44s**（墙钟 276.27s）。三项 deselected 为本轮未重复运行的 C2b 普通连接重新交换用例，不是新增 skip；两组加密配置下的新转发用例实际运行。

运行时证据：

- 两个真实 target 连接和实际 relay 文件流；target child 控制与 server upstream alias 均只改变指定 writer。错误 peer/type/role 控制通过真实连接送达并用后续消息屏障确认已处理，其他 writer/upstream 状态保持。
- 一个 writer 暂停期间，另一个 target 收到完整内容并返回真实最终 ACK；指定 writer 沿原 data socket 继续后，全部目标完成，source 获得成功 ACK，route/index/control/pause 均清理。
- tiny high water、writer 事件屏障及真实 source pause/start 交叉；buffer pressure 触发后，source 本地 start 虽可确认运行，reader 被唤醒后仍重新等待高水位门槛，实际读取计数不增加。峰值大于阈值但不超过 `high_water + max_enqueue`。
- 用户 pause 在自动 pressure 释放后仍保留；用户 start 在 pressure 存在时仍请求 pause。源端权威通知只更新确认状态，不清除 user/pressure 意图，最终解除用户 pause 后实际流排空。
- 一个 target 在背压期间关闭：剩余 target 的文件内容完整，source 获得失败结果，不伪造全目标成功；仅真实完成 target 写入 completed，未完成 route 保留，control owner 与暂停键释放。
- plain、encrypted、encrypted+async+tracking off 配置全部执行上述场景。加密配置观察真实控制通道帧：含 FRAME_MAGIC、无 pause/start 明文片段，未 raw 发送；未替换加密或实际传输。

既有受影响的转发与恢复回归：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py -k forward -q
```

**20 passed、73 deselected in 65.45s**（墙钟 66.20s）。覆盖既有多目标文件/文件夹、背压、目标不可达、ACK 丢失/写失败、最后目标 ACK、路由 reload、零偏移恢复和并发转发握手；未将这些旧路径回归计作 C4 新恢复资格契约的验收。

### 路径对照与明确未覆盖

| 路径 | C3 结论与证据边界 |
|---|---|
| P08 | 真实 target child 与 server upstream alias 精确命中，错误 peer/type/role 无效果，另一 target 独立完成；单位契约覆盖数字交叉时 child 优先、唯一活跃 alias 与陈旧映射 |
| P09 | source 本地操作、server 用户意图及自动背压交叉后可继续；reader 不受 source start 绕过，实际读取计数与有界峰值通过 |
| P10 | 另一 target 被事件屏障放慢或关闭时，解除控制不解除 reader 高水位门槛；正常流内容/真实 ACK 成功，关闭分支仅记录真实完成目标、上游失败且保留未完成 route。无 live leg 后的恢复资格仍属 C4b |
| P11 | 新 relay 场景在真实加密连接中执行，帧无控制明文且未 raw 写入；C2b 已有重新交换窗口证据，本轮未重复执行那三项普通连接用例，不声称新增 relay 专用 re-key 交错全集 |
| P13 | plain、encrypted、encrypted async/tracking off 三组配置运行全部新增多目标场景；未跟踪场景仍由运行时记录控制 |
| P18 | 完成/target 关闭均释放控制 owner 和暂停键；active socket/setup owner 冲突拒绝，而 False/预置/无数据 owner 记录不单独阻挡路由。config 删除、stop 竞争及 reload 与新恢复资格的总验收仍在 C4b/C5 |
| P14–P17 | 本轮不实现新恢复资格或跨会话恢复竞争；仅记录既有相关回归结果，按 C4a/C4b 继续 |

### 风险与审查注意

- 下游控制保持独立，但慢 target 的积压达到共享阈值后会间接阻止 source 和其他 target 获得新数据，这是共享上游的既定背压语义。缓冲上界包含一次 enqueue 的 fan-out 超调，不承诺绝不超过 max_mem_buff。
- 自动请求与用户意图汇总不改变发送端权威规则：source 本地 start 可暂时确认运行，reader 仍自行守住高水位；后续请求按 source 处理顺序决定状态。返回 True 仍只表示控制操作登记，非逐请求 ACK。
- 本轮未改公共签名、wire 格式、持久化 schema、CI、依赖或覆盖率策略；未删除或弱化既有测试。新测试使用真实 socket/加密、事件屏障和实际内容/ACK，未运行 formatter、lint、文档构建、repo-wide suite 或多平台验证。
- C3 保留小范围生产修改并补充两目标契约（集成新增 241 行）；文档/报告与测试也是本次审查的一部分。完成范围仅为上表证据，不宣称所有断线/网络时序组合已枚举。
- C3 后续结论：用户已授权开始 C4a，C3 随该放行进入下一阶段。C4a 实施与独立待审事项见下节。

## C4a：普通对象级恢复资格与原子占用

### 提交意图与数据流

让普通恢复仅接纳已中断、无活动 owner 的具体文件对象；活跃、暂停、建连、重复恢复及仍在最终清理的对象静默返回 False，不关闭或排队等待。其他 fid 和相反方向不阻挡该对象。公共签名和 resume wire 不变。

- Server/Client 普通 `resume_trans` → 既有 peer/进度行匹配 → progress lock 内检查 scoped active socket/fid、发送 worker、typed sender owner 和同 socket/FID 的 pending/unresolved 握手 → 原子登记一个恢复 worker → 原有安全握手和 file_transfer_mode → 真实内容/最终 ACK → owner 释放。
- 接收端 `_request_resumes` → 同一个 per-fid 资格边界 → 请求占用 → 收到 `/file` 时同会话 listener 接管 → accept 后数据 owner 接管 → 最终进度清理后释放。重复请求/监听不能创建第二条 leg；发送失败释放请求，未回应请求按既有 accept 超时期限允许重新申请，会话结束清理无 owner 请求。
- 数据 socket 关闭不会立即抹掉 registry 所有权；旧 worker 的最终清理仍阻挡恢复。重连 sweep 关闭旧 socket 并等待其 owner/外层发送记录结束，然后沿现有自动恢复流程继续；手动活跃请求不进入等待。
- tracking off 不创建新进度行；恢复已有上传/下载行时，该 leg 持有旧行完成清理。暂停/断线清除旧控制状态，新恢复 leg 默认 running，fid 和接收完整路径保持。

### 变更范围与局部止血

实现集中在 `PyFlow/network_api/connect_tcp.py`。新增普通接收请求/监听占用与 progress Condition、握手 occupied 判定；普通发送恢复使用原子占用，转发 target 合并/共享 source 资格留给 C4b。现有 socket shutdown、加密和文件 ACK 机制继续使用。

测试修改位于 `test_resume_matching.py`、`test_transfer_handshakes.py`、`test_tcp_transfer_control.py`、`test_tcp_transfer_resume.py`。旧清扫断言从“close 即释放”迁移为“worker 最终释放”，保留不同 receiver/方向隔离及最终清理断言。补充控制在线、双向恢复、tracking off、建连前/清理中拒绝，以及旧 socket 已移除但外层发送 owner 仍在的重连等待契约。三份 File_Transfer/TCP API RST、变更记录、计划和本报告同步更新。

### 局部验证与运行证据

新资格、握手及实际连接场景：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_transfer_handshakes.py test/integration/network_api/test_tcp_transfer_control.py -k 'resume or control_disconnect_releases' -q
```

**42 passed、66 deselected in 75.32s**（墙钟 76.40s）。实际上传/下载各覆盖 plain alias、encrypted、encrypted async/tracking off wildcard：

- 中断真实 data socket，control socket 始终在线；落盘 partial 内容先核对。旧 sender/receiver 完成退出后，两个并发恢复调用只有一个 True；活动、暂停、pending handshake、接收 listener 及旧 finally 屏障期间均拒绝第二次恢复。
- sender bookkeeping 与 receiver 行值被刻意设成不同于实际磁盘长度；接收请求使用磁盘 offset，恢复 worker 实际参数保留原 fid/offset，接收完整文件等于 payload，sender 得到真实最终 ACK。
- 另一条相反方向传输在整个恢复期间保持原 socket 和暂停状态；随后可独立完成。新 owner 不继承旧 pause，结束时进度、控制记录、socket 和恢复占用清理。
- 真实 control shutdown 唤醒双向暂停 leg；旧线程退出，所有新控制意图/暂停键及对象占用释放，包含 tracking off。未用人工 sweep 替代断线证据。

受影响既有普通恢复/配置/查询契约：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/unit/network_api/test_transfer_handshakes.py test/unit/network_api/test_delete_config.py test/unit/network_api/test_transfer_progress_query.py -k 'not forward and not unified_resume_serializes' -q
```

首轮 **139 passed、2 failed、14 deselected in 151.66s**。两项失败均为旧清扫契约要求 socket 关闭立即删除 active 索引，与本次保留 owner 到最终清理的契约冲突。迁移断言位置/增加关闭后 owner 仍保留的断言，未弱化隔离、内容、ACK 或最终释放：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py::test_equal_server_upload_fids_are_isolated_by_receiver test/integration/network_api/test_tcp_transfer_resume.py::test_equal_inbound_and_outbound_fids_cleanup_only_their_own_sockets -q
```

**2 passed in 4.92s**。不将定向重跑记作全量复跑成功。

自动重连等待补强后，执行实际 upload/download 自动重连及恢复单位契约：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_resume.py::test_upload_resumes_after_a_reconnect test/integration/network_api/test_tcp_transfer_resume.py::test_download_resumes_after_connect_without_a_manual_call -q
```

**34 passed in 12.05s**。新增事件屏障契约确认：即使 data socket 已释放，外层旧发送记录退出之前，重连清扫仍等待；记录退出的 Condition 通知使等待结束。

接收 claim 调整至安全/listener 初始化后、首次公告前，避免初始化失败残留占用。仅重跑受影响的 plain 双向实际恢复场景：

```bash
.venv/bin/python -m pytest 'test/integration/network_api/test_tcp_transfer_control.py::test_online_resume_reserves_one_owner_and_uses_receiver_disk_offset[plain-alias-upload]' 'test/integration/network_api/test_tcp_transfer_control.py::test_online_resume_reserves_one_owner_and_uses_receiver_disk_offset[plain-alias-download]' -q
```

**2 passed in 13.33s**。

### 路径对照与明确未覆盖

| 路径 | C4a 结论与证据边界 |
|---|---|
| P14 | 控制连接在线、进度有行但无数据 owner 时，普通上传/下载可以恢复；receiver 实际落盘 offset、原 fid、最终内容/ACK 均有真实证据，另一活跃反向文件保留原 socket |
| P15 | 运行、暂停、握手、重复恢复、接收 request/listener 和旧 owner 最终清理期间拒绝恢复；屏障并发只接纳一个 worker，不排队、不关闭活跃对象 |
| P17 | 普通双向真实 control shutdown 清理新控制记录、暂停键、数据 socket 和 worker；数据中断后新 owner 默认 running；最终清理中恢复被拒绝。转发 source/target 与跨会话迟到清理的完整验收在 C4b |
| P12/P13 | 新普通恢复场景含配置别名、encrypted async/tracking off wildcard；保留 control socket 身份，不新增未跟踪进度行。转发会话身份仍按 C4b 验收 |
| P18 | 受影响 query/config 契约通过；接收占用纳入配置删除门槛，恢复完成和会话关闭清理占用，持久化 schema 不变。新增接收占用与 stop/config 删除的专门交叉验收仍在 C4b/C5 |
| P10/P16 | 本轮未切换 relay 共享 source/live leg/pending-originator 资格；既有 relay target 合并行为保留，由 C4b 统一处理 |

### 风险与审查注意

- `True` 仅表示普通恢复 worker 已原子预留，不表示恢复已成功；最终文件 ACK 才是完成证据。完整但未确认的 EOF 行仍可恢复，以完成确认。
- 重连流程等待已结束会话的 owner 最终清理，不通过先删索引抢占资格；等待使用 Condition，无新增自动重试。手动请求遇到占用直接 False。
- 未应答 receive 请求保留无数据 owner 的短期占用，按 RESUME_ACCEPT_TIMEOUT 期限在后续请求时允许替换；实际 listener/data owner 不按时钟强制替换。失败发送/会话关闭立即清理请求；这不是逐请求完成 future。
- 实际范围比原 180–300 行预估更大，原因是发送资格之外必须同时覆盖接收请求、监听交接和自动重连清理。审查应连同新增契约与报告评估该规模偏差；未擅自增加 C4b 的转发资格或拆出混用新旧资格的中间阶段。
- 本轮未改公共签名、wire、progress JSON schema、CI、依赖或覆盖率策略；没有删除测试或弱化内容/ACK/隔离断言。未执行 repo-wide suite、formatter、lint、文档构建或多平台检查。语言服务仅 Ruff，references 请求返回 -32601，不能记作成功的引用分析。
- C4a 后续结论：用户已放行 C4b，C4a 随该放行进入下一阶段。C4b 实施与独立待审事项见下节。

## C4b：转发共享 source 恢复门槛与会话清理

### 提交意图与数据流

一个 upstream fid 同时只允许一个 source/relay owner。即使请求 target 已断开，运行、暂停、建连或清理中的共享 source/relay 仍使本次请求返回 False，不排队、不主动 teardown。只有无活动对象且 source 离线时保留现有 pending；单次活跃请求被跳过不保证稍后自动补传，需重新发起。

- source 普通/转发 `resume_trans` 共用 `_transfer_busy` 和 `_start_resume_send`，在 progress lock 内检查两种 typed sender、active socket/fid、发送占用和 pending/unresolved 握手，再登记唯一 worker。删除旧的重复合并、不同 target 排队路径，不留无 control socket 的兼容分支。
- target 请求和转发 listener 复用 C4a `_claim_receive`，沿请求→监听→数据 owner 保持占用，最终按 owner 清理；tracking off 同样生效。
- Server 验证 origin/target/child route → progress/route 锁内检查共享 upstream 占用 → 忙碌则不写 pending；无活动且 source 离线则保留最新 offset。在线 flush 在同一边界预留 upstream 请求，释放锁后通过既有安全门槛和 `send_message` 投递。失败写释放预留、保留 pending。
- `/forward_item` 校验原 route/未完成 target → 原子接管同会话 upstream 请求记录、登记 canonical child keys → 原有文件/ACK 路径 → 最终清理释放预留。旧记录的迟到释放不能删除替换 owner。未新增状态表、持久化字段、重试协议或文件成功标记。

### 变更范围

生产改动集中于 `connect_tcp.py` 的恢复资格、pending 投递、relay 登记和 target 接收占用。测试位于 `test_fid_routes.py`、`test_delete_config.py`、`test_tcp_transfer_resume.py`、`test_tcp_transfer_control.py`。旧的“活动 source 后排队”契约迁移为“拒绝、owner 退出后显式重试”，保留不同 offset、稳定 fid、目标路径、内容及最终 ACK 断言。三份 RST、变更记录、计划和本报告同步更新。现有断线 shutdown、加密、路由持久化与配置删除契约保留。

### 局部验证与实际结果

首轮门槛、生命周期及真实连接检查：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/unit/network_api/test_delete_config.py test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_control.py -k 'not pause_start_preserves and not receiver_unconfirmed and not same_fid and not empty_and_eof and not receiver_keeps and not receiver_pause_start_waits and not relay_child_and_upstream and not relay_pressure and not online_resume and not control_disconnect_releases' -q
```

**137 passed、6 failed、63 deselected in 87.46s**。六项失败均停在新增 source/stop 用例的 1 KiB 断点：按 writer 等待调用次数定位无法固定预期 payload 进度。改为首个真实 payload 块实际发送后设置事件屏障，保留真实 send/crypto 和 receiver 进度断言；未改生产代码或放宽落盘/ACK 断言。

仅重跑六个受影响场景：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py::test_relay_offline_source_replays_stable_route_or_stops_cleanly -q
```

**6 passed in 47.99s**。不将定向重跑记作全量复跑。

受影响既有转发、零偏移/EOF、恢复及握手回归：

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_file_transfer.py test/unit/network_api/test_transfer_handshakes.py -k 'forward or stale_sweep or handshake or port_replies or pre_send_cancellation or sent_cancellation or terminal_reply or old_record or wait_many or disconnect_cancels or reconnect_socket or port_announcement or old_or_malformed' -q
```

**39 passed、69 deselected in 58.59s**。

### 路径对照与运行证据

| 路径 | C4b 结论与证据边界 |
|---|---|
| P10 | 实际关闭 target 控制/data socket，共享 source 仍活跃时两端拒绝恢复、不增加 pending/worker；另一 target 保留原 socket 并完成内容/ACK，上游失败且未完成 route 保留 |
| P12/P13 | plain alias、encrypted、encrypted async/tracking off wildcard 均运行新增场景；旧 control/receive 记录迟到清理不影响新 owner；未改变端点命名空间或加密入口 |
| P16 | source 断线且旧 relay 全部退出后，离线请求从 offset 0 更新为实际 1 KiB；原 client 端点重连后单次重放复用 upstream/child fid、同 route/completed，已完成 target 不重发。零偏移/空文件/EOF 由既有真实回归覆盖；单位契约覆盖不同 fid 独立及并发 flush 只发送一次 |
| P17 | source/target 真实断线释放旧 worker、socket 和暂停状态；新 owner 默认 running。保留旧记录执行迟到清理后，新 owner 仍有效，重复恢复 False；最终内容等于 payload、source 得到真实成功 ACK、状态清空 |
| P18 | 同一实际断点执行 stop，transfer/control worker 退出、占用释放、route 快照保留；随后可删除 progress 文件，重复 stop 不重新生成文件，内存 route 按原契约保留。单位契约覆盖失败写/关闭请求释放和配置删除门槛；合并总验收仍在 C5 |

### 风险与明确未覆盖

- 活跃请求不排队：target 重连时若只发起一次请求而 source 仍活跃，需要稍后重新请求。未新增 timer、自动重试或 wire 拒绝确认，不能宣称最终自动补传。
- 离线已有多个 target pending 时，同 upstream 一次只预留一个；剩余项等待后续合格 flush/请求/重连触发，不在活动对象后建立 worker 队列。
- 控制写入不是逐请求恢复确认；未回应请求按现有接收预留期限处理，实际 relay owner 不按时钟强制替换。
- 新场景验证 source 断线重连和 target 断线拒绝；未另加 target 同端点重连补传场景，不宣称网络时序全集已枚举。配置/持久化与加密的相关合并验收留在 C5。
- 本轮未改公共签名、wire、持久化 schema、CI、依赖或 coverage 策略；未删除测试或弱化内容/ACK/稳定身份断言。未运行 formatter、lint、文档构建、repo-wide suite 或多平台验证；Ruff 不支持 references，不能记作成功引用分析。
- C4b 后续结论：用户已授权开始 C5，C4b 随该放行进入收尾阶段；C5 总表和合并回归见下节。

## C5：总体报告与合并验收

### 原子提交汇总

| 阶段 | 已观察提交 | 意图与审查状态 |
|---|---|---|
| C1 | `b02def0` | 严格 parser、typed identity、共享端点基础；已放行 |
| C2a | `01de684` | 发送端权威状态、FIFO、意图/确认分离、预置暂停；已放行 |
| C2b | `fd189d1` | 普通收发与 relay 生产接入、breaking API/wire 切换；已放行 |
| C3 | `86223f1` | child 定位、writer 隔离、用户/压力 OR、高水位门槛；已放行 |
| C4a | `11ba6ab` | 普通对象级恢复资格和原子占用；已放行 |
| C4b | `25aac75` | 共享 source 恢复资格、pending 接管和会话清理；已放行 |
| C5 | 本轮未提交 | 接口文档/逐路径总表完成；合并回归 640 passed、1 skipped；待人工最终验收 |

### 总体数据流与架构影响

pause/start：公开 API 校验 fid/type/本地角色并归一 control peer → 精确 typed leg 与实际 socket/owner → S 决定状态或 R 登记意图 → per-leg FIFO 等待对端登记和安全通道 → 现有 send_message → 对端按实际会话验证、反转角色并处理 → S 通知确认。发送循环在新块之前读暂停状态；接收与在途落盘持续进行，start 不重建/seek/truncate。relay 每个 writer 独立控制，upstream 用户暂停与背压取 OR；source start 不解除 reader 独立高水位门槛。

resume：保留的进度/route 与 origin/target 校验 → 对象级资格和原子占用 → 原有 listener/port/安全握手 → 从 receiver 磁盘 offset、原 fid/路径继续 → 实际文件 ACK → 完成/保留中断状态并释放 owner。活跃/暂停/建连/清理对象返回 False，不关闭、不排队。离线 source 的 pending 保留最新 target offset，投递与 relay 复用同一 upstream 占用；未完成 route 仍由真实 ACK 决定。

公共变化：四 token pause/start、不兼容旧 remote API、必填 type/direction、异步登记不等于确认；resume 签名及五 token wire 保持，忙碌改为 False。持久化五字段、upload key、relay targets/completed 与 query schema 保持；typed 控制、FIFO、意图及恢复占用仅存内存。未改变 CI、依赖或覆盖率策略。

### P01–P18 逐路径对照

以下“覆盖”指已定义路径有运行/机械证据，非对任意网络时序或环境的穷举。合并回归结果在下节单列；各阶段的失败与修复记录保留在前文。

| 路径 | 结论 | 主要证据与边界 |
|---|---|---|
| P01 | 覆盖 | 单元 parser/本地参数拒绝；same_fid 实际连接输入旧/非法/type/fid 命令后经消息屏障检查状态不变 |
| P02 | 覆盖 | same_fid 双向普通实际内容/ACK、独立 socket；不同 peer 的真实同 fid 上传隔离、typed type/role key 契约；不授予进度行覆盖能力 |
| P03 | 覆盖 | pause_start 上传参数组：S/R 控制、同 socket 完成；receiver_keeps 验证已确认暂停后在途块仍落盘 |
| P04 | 覆盖 | 同 P03 的下载参数组，方向反转，真实最终 ACK 与内容 |
| P05 | 覆盖 | 全部三操作 FIFO 交错的确定性枚举；unconfirmed_reverse 与阻塞发送屏障验证本地/远端交叉、反向意图和排空收敛 |
| P06 | 覆盖 | empty_and_eof 真实最终 ACK；既有文件夹回归；C2b 临时脚本四个实际文件夹 leg 的 S pause/R start、内容和清理证据 |
| P07 | 覆盖 | 精确预置暂停接管/单次公布/解除；实际注册后通知；未知 wire 不建立控制状态；start 不留 False key |
| P08 | 覆盖 | 两 target 实际 child/合法 alias 控制、错误 peer/type/role 屏障；另一 writer 内容/ACK 完成；exact child 数字交叉和陈旧 alias 单元契约 |
| P09 | 覆盖 | relay_pressure 固定 source/用户/背压交叉，reader 被唤醒仍回到压力门槛、读取计数不增；peak ≤ high_water + max_enqueue |
| P10 | 覆盖 | 慢/断 target 的有界缓冲、其他目标完成、上游失败及未完成 route 保留；disconnected_target 验证活跃共享 source 不重放 |
| P11 | 覆盖，有限交错 | plain/encrypted 配置实际控制；真实强制 re-key 持有 key publication，reader 继续处理 key、控制帧无明文；relay 实际 encrypted 帧。未另跑 relay 专用 re-key 组合全集 |
| P12 | 覆盖 | localhost/concrete/wildcard 的普通与转发场景，真实 socket 关闭回退与同端口异地址拒绝；source 原端点重连和旧 control 迟到清理 |
| P13 | 覆盖 | 三组实际 channel：plain、encrypted、encrypted async/tracking off；运行控制/恢复/关闭不依赖新增进度行，receiver 未阻塞密钥交换 |
| P14 | 覆盖 | online_resume 上传/下载：control 在线、真实 partial offset、唯一新 owner、独立反向 leg 原 socket 保持、内容/ACK |
| P15 | 覆盖 | 活跃/暂停/协商/接收监听/旧 finally 屏障拒绝；并发只有一次 True/worker，busy 不关闭或排队；EOF 行仍可补最终确认 |
| P16 | 覆盖 | shared source 活跃拒绝、离线 pending 最新 offset、重连稳定 fid/route/completed；零偏移、空文件与 EOF 真实恢复，完成 sibling 不重放 |
| P17 | 覆盖定义分支 | 普通双向及 relay source/target 实际断线均退出旧 worker/清控制；source 重连新 owner 默认运行，旧记录迟到释放无效。未另跑 target 同端点重连补传组合 |
| P18 | 覆盖定义分支 | query schema/typed paused 投影、实际 JSON 无控制字段、route reload、预置不挡路由/真 owner 冲突、真实 stop 后清理及配置删除/重复 stop 契约 |

### C5 当前文档与机械核对

修正 Client API 仍描述 target 排队/重复合并的段落；历史 changelog 标明其已被 C4a/C4b 替代。三份 RST 明确 receiver 的未确认重复请求可能再次登记并返回 True、控制登记不等于 ACK、同 socket start、对象级恢复、无 wildcard/resume-all、data-only failure 不新增自动重试，以及活跃共享 source 被跳过后需重新请求。

临时标准输入脚本通过 inspect.signature 核对六个公开方法：四个 pause/start 都要求 keyword-only type/direction；Server peer 必填，Client peer=None；两个 resume 均为 `(self, peer_addr, fid, offset)`；旧 remote 符号不存在。三份 RST 共检查 23 个显式锚点，transfer 引用全部解析，未保留旧 remote 或当前 coalescing 说明。结果 PASS，未落盘脚本。

文档完整构建：

```bash
.venv/bin/python -m sphinx -b html -q -w /tmp/pyflow-c5-docs-ihOz0C/warnings.log docs/source /tmp/pyflow-c5-docs-ihOz0C/html
```

退出码 **0**，墙钟 **14.54s**；两项警告为 `html_static_path` 的 `_static` 不存在，以及 `command_control_extension_tcp` 模块 docstring 第 23 行 definition list 缩进。完成最终措辞后对同一输出目录增量构建，退出码 **0**、墙钟 **2.30s**，仅 `_static` 警告；不把增量构建未重读的 docstring 警告记为已修复。未改 Sphinx 配置或无关 docstring。

通过真实浏览器打开构建的 File_Transfer 页面和控制章节锚点，观察并截取当前签名、typed 角色、wire 与返回值说明；Client API 页面文本确认包含共享 upstream 的拒绝规则和未确认请求说明，不再包含重复合并旧规则。浏览器页已关闭。构建输出位于 `/tmp/pyflow-c5-docs-ihOz0C/html`，没有写入仓库。

### C5 相关合并回归

执行一次计划中的相关合并回归，不含 repo-wide suite：

```bash
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_crypto_tcp.py test/integration/network_api/test_tcp_async_server.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py -q
```

**640 passed、1 skipped in 761.28s（12 分 41 秒）**，墙钟 **761.77s**。该命令执行一次，无失败、无 deselected；本轮未因结果通过而重复或扩大套件。唯一 skip 是 plain fixture 不适用的重新交换场景（`test_receiver_pause_start_waits_for_real_rekey_without_blocking_key_reader`），两组加密配置实际运行。原始输出见本会话 `artifact://70`。

C5 本轮只修改三份接口 RST、变更记录、计划和本报告，未修改生产实现或测试。总体数据流、六个实现提交、P01–P18 路径结论、实际文档构建及浏览器证据已汇总；上述合并回归是本次最终代码状态的相关验证，不替代逐路径运行证据或人工审查。已完成全部七项计划阶段的交付，最终人工验收、提交/合并和远端操作尚未批准。

C5 验证后，用户单独授权将 `.github/workflows/ci.yml` 的 Test job 超时从 10 分钟调至 30 分钟。测试命令、OS/Python 矩阵和覆盖率策略不变；未改其他 job。仅核对工作流配置，不为时限调整重复运行测试，也未触发远端 Actions。

### 明确限制与最终人工闸门

- 活跃恢复不排队、无逐请求 wire 拒绝 ACK；target 单次重连请求被跳过后需再次发起。离线已有多 target pending 同 upstream 一次只投递一个，余项等待后续合格 flush/请求/重连触发；未增加最终自动补传保证。
- 单元状态契约加实际路径证据不等于任意调度穷举。未覆盖 relay 专用 re-key 全组合、target 同端点重连补传、NAT/端口改变、多进程进度共享、多平台和所有网络中断组合；已按各阶段报告边界披露，最终需人工接受。
- 最终 ACK 超时仍保守保留进度；控制可用且可投递是远程 pause/start 前提。缓冲允许一次 enqueue fan-out 的有限超调，不承诺绝不超过阈值。
- 规则沉淀为 typed 双向隔离、FIFO 收敛、对象原子恢复与 owner 清理契约；未删除测试或降 coverage。C5 验证后仅按用户单独授权延长 Test job 时限，未改测试命令与矩阵。阶段 C2b/C4a 规模偏差已随后续放行记录保留；C5 以文档与合并验证收尾。
- 本轮不创建提交、合并或推送。最终报告须由人类审查放行；合并/远端提交另需明确授权。

## 公共协议/API 变更许可记录

用户审查放行 C2a 并授权开始 C2b；下列协议/API 按已确认计划执行切换。恢复资格的新语义按 C4a/C4b 后续实施。

```python
# Server
pause_trans(fid, peer, *, trans_type, direction)
start_trans(fid, peer, *, trans_type, direction)

# Client
pause_trans(fid, peer=None, *, trans_type, direction)
start_trans(fid, peer=None, *, trans_type, direction)
```

- 四 token 的 `/pause_trans <fid> <trans_type> <direction>`、`/start_trans <fid> <trans_type> <direction>` 替代旧 wire 格式；不保留兼容路径。direction 表示命令发起方在当前数据 leg 中的角色（0 发送，1 接收）。
- 删除 `pause_trans_remote` / `start_trans_remote`，项目内调用与文档统一迁移到上述入口。
- bool 表示活跃 leg 的操作是否登记，不表示已确认或文件成功。发送端未建连时允许精确 key 的预置 pause，返回 False；start 解除预置；非法本地 fid/type/direction 抛 ValueError。
- `resume_trans(peer_addr, fid, offset)` 保留签名，但活跃、暂停或正建连对象静默跳过并返回 False；具体资格门槛在 C4a/C4b 实现。

本轮 C2b 授权包含上述 pause/start 协议/API 的切换；C3–C5 保持原定分阶段审查闸门。
