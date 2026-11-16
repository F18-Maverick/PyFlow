# 双向文件暂停、继续与按对象恢复 — 实施报告

状态：需求卡和七项拆分已确认。**C1 已通过人工审查；本轮获准完成的 C2a 已实现并通过定向验证，等待人工审查；C2b–C5 未开始。** 本报告随已完成的原子变更增补，不将未实现或未运行的路径标为完成。本轮未创建提交、合并或推送。

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

待 C2a 人工审查放行，以及下节公共协议/API 许可后，才能进入 C2b。

## 公共协议/API 变更许可点

以下仍是拟定契约，C1 未实施；应在 C2b 生产切换前明确许可：

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

需求卡和拆分的确认不在本报告中代替这一许可；未获许可前不修改公共协议/API。
