# 传输控制生命周期与转发恢复 — 需求卡

状态：**C5 实现、真实运行验证与双轴审查完成：最终完整 pytest 994 passed、1 skipped，八项专项及三组独立程序通过**。拒绝同 IP 且同端口的自身目标，合法目标仍传输；审查发现的配置 IP 与实际源 IP 不同时误拒已修复。C1–C4 保留，不改 fid、wire、公开签名或持久化模型；类型检查仍有 37 条改动范围外诊断，C6 不自动推进。

本轮命令、实际结果、失败修正及明确未覆盖项见[实施报告](transfer-control-lifecycle-report.md)。旧历史结果不作为 C4 通过证据。用户通过 `/skill:implement` 授权实现、检查、审查及提交当前分支；不合并或推送。

计划编写阶段的文档机械检查通过：3 份文档、6 个相对链接、21 个计划涉及的既有文件路径、14 个验收命令引用路径、20 条验收路径、6 个原子阶段及命令组、6 个拟定运行场景，错误数为 0。当时实施报告尚未创建；这份历史检查不代表功能验收通过，当前实现证据另见实施报告。

## 审计结论的使用边界

- 此前问题一“正常恢复流程导致内容损坏”的结论已撤回：人工构造旧 offset / 新进度的实验不能证明正常协议生命周期会产生该状态，不再作为本轮修复项。
- 任意端可请求暂停/开始、由数据发送端仲裁，是已确认特性，不是缺陷。
- 问题二暂缓：不解决旧 pause/start 与同一控制会话、同 fid 数据腿重建交叉时的严格代次隔离。控制发送失败沿用现有失败/关闭路径，允许人工重新操作或结束控制会话后恢复；不承诺该窗口自动无缝收敛。断线资源清理和已商议的恢复能力仍在范围内。
- 此前问题九（超长文件名）、十（控制队列增长）、十一（无 owner 的预置暂停）暂不处理。
- 用户已批准撤回旧 C3 的查询、强制重建及串行活跃契约，并批准本需求卡和对应更改计划；保留历史输出，不将其冒充本轮验收。

## 目标与非目标

### 已确认目标

1. 普通上传/下载及转发恢复保持原 upstream fid/child fid、完整接收路径及进度记录；不为问题二增加数据腿编号、握手字段或持久化身份迁移。
2. 会话关闭只处理该实际控制 socket 拥有的资源；不再让重连任务按 peer 扫描并关闭后来建立的新数据 socket。
3. 流式转发采用全 route 的用户暂停：任意 Source、Server、Target 可请求 pause/start，Source 作最终仲裁。一个 Target 暂停，所有 Target 停止获得新的源数据。Server 已接收的数据继续排空到 Target；保留数据 socket，不做 seek 或重握手。
4. 不采用“每个 Target 持有独立暂停锁、必须本人解除”的规则。任意端的 start 仍可经 Source 仲裁解除用户暂停。自动背压的内存硬门槛不被用户 start 清除。
5. 有效转发恢复请求由 Receiver 在其旧接收 owner/文件收尾后，带实际数值 offset 提交 Server。Server 按 child fid/实际会话定位原 route；原 route 的数据腿、listener、握手、reader/writer 或 finalizing owner 尚未全部结束时只保存请求，不主动中断。全部旧 owner（含 finally）收尾后，按每个 Target 自己的 offset 独立转交 Source、建流；不复用旧 relay 内存、不更换 fid、不查询其他 Target 的 offset。
6. C5 在 Client 原生文件/目录统一入口拒绝自身完整控制端点；同 IP 不同端口和不同 IP 同端口仍允许，混合列表只剔除自身。拒绝先于 fid/新进度/握手，console、Web、目录条目和保留行恢复共用；拒绝恢复不删旧行。配置主机必须解析为捕获 socket 的实际 IP 才作为别名，wildcard 仅别名实际端口；未绑定/端口回退后未使用的配置 IP 不能认作自身。
7. 保留真实最终 ACK 判成功规则：每个 Target 的真实 ACK 只更新其完成集合，累计所有 accepted Target 的真实 ACK 才能使整任务成功；中间 Target 成功不伪造整 route ACK。Source 的共享上传行保留到全部目标完成且收到最终 ACK。C4 已实现复用已有完整完成集合补最终确认，不将 offset==size 单独当作任务完成证据；运行证据与限制见实施报告。
8. 多个 Target 恢复各有独立 offset、Source→Server→Target 数据流、进度、资源及失败结果，正文并行。晚到 Target 不等待已有恢复流的大文件正文或 ACK；同一实际控制 socket+fid 仅端口握手短串行，不锁正文或最终 ACK。

### 非目标

- 问题一不再列为待修缺陷；问题二暂缓，不增加 leg_id、恢复换新 fid 或 pause/start 代次字段；问题三是已确认特性；问题九、十、十一继续暂不处理。
- 不增加内存回放缓存、磁盘中转缓存、目标断点范围过滤或新的传输结果通道。
- 不改 CI、依赖、coverage 策略；不删或弱化测试。
- 不解决 NAT、控制端点改变后身份迁移、多进程共享进度文件及同尺寸源文件内容改变。
- 不把 pause 返回 True 当作对端确认或立即停止落盘；不保证撤回已经交给 socket 的字节。
- 不用一个 Target 的 offset、运行时进度或失败替代其他 Target；并行恢复采用独立流，不增加共享正文的字节范围过滤机制。
- 不增加 Server→Target 的 offset 查询、`?`/`None` offset、恢复状态消息或能力协商；不把所有数据失败改成自动发起恢复/循环重试。
- 不因恢复请求主动关闭上游或其他 Target 的活跃数据腿，也不自动 start 用户暂停；等待只针对同一 upstream route，不等待整个 peer 或其他 fid。

## 修改思路与确认状态

### D2：暂缓旧控制命令跨腿隔离（本轮不修）

- fid 继续表示原文件/转发任务，恢复不重分配；原接收记录、完整路径、route 和 child 映射继续使用现有 fid。
- 不实现 leg_id 分配器、端口字段、控制帧字段、file_transfer_mode 新关键字或相关测试/helper 的协议迁移；不建立其他代次隔离机制替代它。
- pause/start 和端口响应保持现有四 token 格式；原 `/file`、`/file_folder`、`/forward_item ... resume=<offset>` 及持久化模型不变。此前换新 fid 所需的 old/new 映射、rebinds、快照 version 也不实施。
- 保留现有实际控制 socket 校验、pending/unresolved 握手隔离、同 fid 占用及旧 owner 收尾；这不能被表述为已经解决同一控制会话内迟到控制消息的问题。
- 暂缓只针对问题二，不取消问题四/五的重连安全修复，也不取消问题七和八的正常控制及恢复方案。

### D4：统一为实际控制会话所有权（C1 保留）

- 复用 `_TransferHandshakeRegistry.track_socket/cancel_socket`，让会话资源只有一个登记及取消入口。
- 已知的旧控制会话退役时，取消它自己的 listener/data socket/握手及控制状态；新会话不得借 peer 地址取得旧会话之外的资源。
- 旧 worker 固定使用创建/准入时的控制 socket，不在后台重新读取全局 `self.client_socket` 或按 peer 获取替换后的 socket。这也避免旧转发恢复 worker 误用新连接。
- 拆除 `_drop_stale_transfers` 按进度表找 socket 并关闭的重复职责。保留基于旧 owner 快照的必要收尾等待；等待不得持有全局锁或阻塞需要处理握手的控制接收线程。
- 旧 finally 按原 socket、entry、record 身份释放，不能删新 owner；失败进度继续按原规则保留。

### D8：数值 offset、原 route 旧 owner 收尾与每 Target 独立恢复（用户已批准）

1. `/resume_trans <fid> <offset> <origin> <target>` 仅保留原五 token 数值形式，offset 是非负整数；`0` 表示从头恢复。删除 `?` 查询、`offset=None`、查询线程/状态及查询专用消费者，不另增替代命令。
2. Receiver 等自己的旧接收 owner（含 finally）退出、原文件关闭后，读取实际文件大小提交请求。沿用显式/重连恢复入口，不新增失败自动请求循环。Server 原样保存数值 offset，不用发送计数或其他 Target 的 offset 替换。
3. Server 入队前校验实际控制 socket、origin、target、child fid 及未完成 route；将 child fid 映射为稳定 upstream fid 后转交 Source，保留数值 offset 与端点身份，不按 fid 全局猜 route。
4. 首次恢复建流必须等同一原 route 的上游、全部下游及 listener/握手/reader/writer/资源 owner（包括 finally）全部自然收尾；socket 已关闭不代表 owner 已退出。不主动 teardown、不自动 start 暂停；不等待健康控制连接断开，也不等待其他 fid/route。
5. 等待采用 route-scoped 运行时 pending，不阻塞控制 reader、异步 event loop 或持全局锁等待。旧 owner 完成后，所有合法可服务 Target 可独立进入恢复；不把随后活跃的独立恢复流重新当成整 route barrier。
6. 每个 Target 最多一个待处理请求和一个活跃恢复 owner，重复请求合并。各 Target 各有实际 offset、独立 Source→Server→Target 流、运行时进度、资源及失败结果；稳定 upstream/child fid、完整路径和持久化 schema 不变。Source 整任务上传行仍是恢复依据，不被某条独立流的瞬时进度覆盖或提前删除。
7. 同一实际控制 socket+fid 的端口握手短串行以关联既有四 token 回复；锁只覆盖登记/发送/取得端口结果，不覆盖正文、最终 ACK 或整任务。A 已开始正文或等待 ACK 时，B（含晚到 Target）可独立建流和传输，不等 A 完成；不同 offset 不共享正文过滤或旧队列。
8. Source 离线时保留 pending，合法回连/后续触发后处理。Target 断线即丢弃绑定旧实际 socket 的 pending 请求；不把旧请求借给新 socket，重连后由 Receiver 等旧 owner 收尾、读取新实际 offset 重新请求。离线/失败 Target 不阻塞可服务兄弟目标，活跃流失败仅收尾自身，不启动自动重试。
9. Source 仍仲裁全 route 用户 pause/start；多个恢复 sender 共享该任务控制状态，最后一个 owner 退出才释放，任一 Target 的 pause 使所有 Source 流停止新增正文。Server 已收字节继续排空，本地背压与用户 start 独立。
10. 每个真实 Target ACK 只记该 Target 完成；其他 Target 未完成时不发整 route 成功 ACK，Source 上传行及 Server route 保留。offset 为零、空文件和 EOF 均沿用数值恢复；offset==size 不等于成功。C4 全完成后的上游 EOF 再确认仍未实现。

代价与风险：原 route 尚有活跃传输、用户暂停或未结束的 ACK/finally 时，首次恢复继续等，不增加强制关闭、超时抢占或自动重试。旧 owner 全部收尾后的独立恢复流不互相等待正文/ACK。运行时 pending/active 不扩展持久化 schema，重启后仍由原接收记录重新请求；同 socket+fid 迟到控制消息的严格代次隔离仍暂缓。

### D9：C4 复用现有 fid 路由与共享上传行（已批准并实现）

- **目标**：所有已接受 Target 均已真实 ACK、Server 上游最终 ACK 返回 False/异常后，允许 Source 以原 upstream fid 和 EOF offset 显式再确认；不重发正文、不重开 Target，成功后清理所属记录，再次失败仍可显式操作。
- **当前记录职责**：Source 只有一条以 `(Server, upstream_fid)` 为键的共享任务上传行；Target 各有接收进度；原生流式转发的 Server 不创建逐 Target upload/download 字节进度行，持久化的是 `relays` 下的 fid route。A 成功时其接收行、反向 child 索引和数据资源可清理，但 B 尚未完成时不能删 Source 共享上传行。
- **完成依据**：复用 `_fid_routes[fid]` 的原 Source、完整目标映射及 `completed`；只在已接受目标非空且全部真实确认时允许终态分支。`offset == 本次文件头 size` 只判定零正文，不能生成缺失的目标完成事实。
- **准入**：校验当前实际 Source 控制 socket、原 originator/upstream fid、请求目标属于原映射及当前 route 对象；不依赖已完成目标的反向索引。只在同任务旧 owner（含 finally）已退出后准入，忙/重复沿既有失败或占用规则拒绝，不抢占、不阻塞控制 reader/event loop。
- **触发与路径**：仅 Source 显式 `resume_trans(原 Target, upstream_fid, 原上传记录 total)`；公开 bool 仍只是 worker 预留。重载原记录/原端点回连后仍显式发起；不增加重连自动补发或失败重试循环。Server 只开 Source→Server 数据腿，仍读取现有完整文件头并校验 EOF，不连接 Target、不要求其在线、不改写目标文件。
- **清理与重启**：Server 仅在现有最终 ACK 写入成功后按 route 对象身份删除；Source 仅在实际消费该 ACK 后清所属共享行。失败保留原 route 和上传行，旧规范快照可直接按既有格式重载；不增加新的迁移器或持久化类别。
- **影响范围**：生产仅调整 `connect_tcp.py` 的 Server 转发分派/建流/最终 ACK 分支，复用 Source 现有 EOF 发送及 owning 清理。对应测试、文档、路径全集和命令见[更改计划](transfer-control-lifecycle-plan.md)的 C4 章节。
- **非目标/禁止项**：不把任务信息迁到 Source，不改五字段上传/下载行、route schema、公开签名或 wire；不新增完成表/完成位/Server 逐目标字节进度，不再拆 `file_transfer_mode/_send_file`，不混 C5/C6、CI、依赖或测试策略修改。
- **风险与上限**：待补确认的旧 route 可能长期保留，本轮不增加 TTL 或自动清理；不承诺有界待确认任务数。上游本地 ACK 写成功但对端是否收到的歧义、源文件改变后的内容复核、NAT/端点迁移及断电耐久性仍不在保证内。风险等级高，必须真实 socket 与事件证明。

此前讨论的“取消所有 Server route、迁移到 Source”“新增 completed_relays 小终态表”“按 C4a/C4b 分拆压缩与确认”均已撤回；不进入当前实施清单。[术语表](../../GLOSSARY.md)只定义领域概念，不作为新增数据结构的授权。


## 路径全集与验收证据要求

| 路径 | 必须证明的行为 |
|---|---|
| 普通上传/下载，稳定 fid 换新数据腿 | 原 fid/路径保持，实际 offset、内容及真实 ACK 正确；保留原普通恢复语义，不验收问题二的跨腿旧控制隔离 |
| 普通/转发控制连接重连 | 只关闭旧会话资源；新腿不被旧清理或旧 finally 删除 |
| 暂停中断线、tracking off、listener/建连中退出 | 线程和 owner 结束；保留允许恢复的文件与进度；清理不依赖进度开关 |
| 旧恢复 worker 与新 connect 交叉 | worker 不在新 socket 上发送；接收线程启动不依赖误用新 socket 的旧任务 |
| Source、Server、任一 Target 发 pause/start | 有限输入排空后所有参与方的逻辑确认状态一致；由 Source 排序决定 |
| 两个 Target 相反请求、未确认 pause→start | 不吞掉反向意图、不回包循环；不是独立端点暂停锁 |
| 全局 pause，存在在途块/Server 排队块 | Source 下一块前停止；已有字节继续到达并落盘；同 socket start；最终内容和真实 ACK 正确 |
| 高水位与用户 start 交叉 | 不越过本地内存硬门槛，不误将自动背压释放当用户 start |
| 转发 resume 到达时原上游/兄弟仍在线、暂停、握手、EOF/ACK等待或 finalizing | 原活跃 socket/owner 不被中断；最后一个原 owner 含 finally 收尾后才建恢复流；控制线程仍可处理消息 |
| 多 Target 不同 offset、部分完成、晚到请求、A 正文/ACK 阻塞 | 各自实际 offset、独立流/进度/资源；B 在 A 正文/ACK 未完成时可建流并传输；仅 socket+fid 端口握手短串行；完成目标不重传 |
| Source/Target 断线及单 Target 失败 | Source 离线保留 pending；Target 断线丢旧 socket pending，新会话用新实际 offset 重发；单流失败不打断兄弟、不自动重试 |
| offset 为零/EOF、最终 ACK 再次失败 | 不误清恢复记录；确认失败可再次恢复；成功清理所属记录 |
| Client 自转发与本机不同 Client | 拒绝自身完整控制端点，允许同 host 不同端口；统一入口覆盖 console/Web/文件夹/恢复 |
| 加密、密钥交换窗口、线程式/async Server | 统一 send/receive 路径；无明文降级；不阻塞密钥接收线程；真实内容、ACK、资源收尾 |

验收须使用真实本机 socket 传输，记录稳定 fid、实际 owner/socket、文件内容和最终 ACK；交叉时序用事件固定，不能仅断言文件存在或测试没有抛错。问题二明确列为暂缓，不计入覆盖或通过声明。约定命令见更改计划，已执行结果仅以实施报告为准。

## 数据、安全和范围约束

- 所有 peer 身份由实际控制会话及现有端点规则确定，不按 fid 全局猜路由。
- 入队前校验实际会话和 route；控制请求走现有加密就绪及 send_message 入口，不靠 fid 全局猜路由，不把旧待处理请求借给新 Target socket。
- 恢复入队不关闭任何活跃数据腿、健康控制连接或其他 fid，不删除接收文件；仅自然结束的旧 owner 做原有定向收尾。
- 不把 socket 仍打开或 offset 等于 size 单独作为成功证据。
- pause/start、端口响应、公开 Python 签名及持久化模型不变；不新增 wire 字段、leg_id 或恢复换新 fid。恢复 parser/formatter 只接受数值 offset；调度改为等待原 route 旧 owner 全收尾、每 Target 独立并行恢复。
- 风险等级：高（多 owner 并发、全局控制共享、文件完整性和真实 ACK 聚合）。本轮实施及局部验证单列；不宣称 C6 总验收通过。按用户实施指令提交当前分支，不合并或推送。

## 下一阶段闸门

C4 的实现、验收与旧背压迁移已提交。用户已明确批准 C5 开始，实施及验证严格对照[计划](transfer-control-lifecycle-plan.md) 2.2 节，实际证据见[报告](transfer-control-lifecycle-report.md)。C5 仅提交当前分支，不合并或推送；C6 须另获批准。
