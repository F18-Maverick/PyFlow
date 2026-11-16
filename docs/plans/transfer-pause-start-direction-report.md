# 双向文件暂停、继续与按对象恢复 — 实施报告

状态：需求卡和七项拆分已确认。**C1–C2b 已通过人工放行；C3 已实现并完成局部验证，待人工审查；C4–C5 未开始。** 本报告随已完成的原子变更增补，不将未实现或未运行的路径标为完成。本轮未创建提交、合并或推送。

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
- **C3 待人工审查，未创建提交**。C4a/C4b 与 C5 未开始。

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
