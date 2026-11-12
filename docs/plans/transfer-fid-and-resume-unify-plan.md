# 传输 FID 与恢复链路统一计划

状态：原子更改 1–4 及补充 O1–O4 的历史审查事实保留；原更改 6 的接收端分配
方向由 `transfer-sender-fid-revision-plan.md` 取代。用户明确授权 R1 后继续 R2；
R1/R2 已实施，最终受影响套件 **434 passed in 278.89s**，明文/加密独立程序通过，
文档构建成功。证据及已知警告见 `transfer-sender-fid-r2-report.md`。
当前待人工审查；未创建提交、未合并、未推送。

本文件替换先前“kind 位 + tfid 重放”的草案。后续实现不得沿用被替换的设计。

## 1. 需求卡

目标：普通文件与公钥仍由发送方分配新 FID；中继源端预先分配 upstream FID，Server 为每个目标分配 child FID；恢复复用既有上下游 FID 与逐跳路由。

现状：普通传输/公钥沿用发送方分配；R1 将中继 upstream FID 从 Server 分配改为源 Client 预分配，Server 仍独立分配各目标 child FID。详见当前修订方案。

非目标：不改文件字节流格式，不做中继内容落盘、暂停状态持久化、client_port 变化后的身份迁移；不改加密算法、鉴权体系、CI、依赖及覆盖率要求。关闭进度记录时保持不支持自动续传的既有约定。新旧节点不混跑，不承诺旧版断点自动迁移，不自动删除旧进度文件。

记录边界：复用 `file_upload_progress` 和 `file_recv_progress` 的五项值 `[bytes, total, speed, local_path, peer]`。不新增 client 恢复专用表，不复制已有路径和进度信息。接收记录的 peer 是原始发送者，发送记录的 peer 是逻辑接收者，不将二者混作 origin。

Server 将现有转发恢复记录收敛为一份 FID 路由关系：原始源端、上游身份、目标全集及下游身份、目标完成状态；不另建重复权威状态。已有 relays 持久化区域承载该关系。

恢复方向：`/resume_trans <fid> <offset> <origin> <target>`；origin 是自己则发送本地文件，否则 server 核对原始请求者与路由并转换上下游 fid。最终接收端以稳定 fid、来源、文件名和总大小匹配旧接收记录，继续写其完整路径，不用新 ID/同路径桥接。

完成语义：Server 等完整接受目标集合的既有最终数据 ACK 后，才在上传数据连接上向源端回传同一种 ACK；任一目标未确认不回成功 ACK。删除第二结果通道和额外等待状态。源端仅凭最终数据 ACK 清理任务，失败保留进度；稳定 ID 恢复及逐目标状态保留。

风险：中高。改变中继 upstream FID 分配权并要求 `/forward_item` 带 `fid=`、显式 `resume=`；保留逐目标身份、五项进度值和文件内容格式。

禁止事项：不删/跳过测试，不弱化断言，不改 CI/依赖/coverage，不引入第二套 fid 生成器，不把握手 request 标记作为持久文件 ID，不绕过既有发送/接收/加密入口，不自行提交远端或进入下一原子更改。

## 2. 路径全集与原子拆分

| # | 原子意图 | 覆盖路径 | 预计代码/测试与对应说明改动 | 局部验证 |
|---|---|---|---|---|
| 1 | 现有索引补齐接收端作用域 | 普通双向发送/接收、转发上传/目标端口、公钥双向端口、暂停、断线清理、持久化、Web | 250–400 行 | 不同接收端同号、跨方向同号、并发端口匹配、重启读回和 Web 输出 |
| 2 | Server FID 路由表接管现有转发恢复状态 | 中继登记、上下游查找、目标全集、relays 持久化 | 200–350 行 | 一对多路由与重启读回、协商失败不误算完成 |
| 3 | 逐目标完成与源端记录生命周期（含已批准提前的稳定 ID 恢复依赖） | 数据泵逐目标 ACK、聚合原始最终 ACK 回传、发送保留与清理、沿用旧上下游 fid、接收端旧记录衔接 | 删除第二结果通道；审查重点为目标 ACK→原路由→同种 ACK 回传→源端清理 | ACK 不提前回传、零 ACK、部分成功、失败目标恢复与最后目标完成、上游 ACK 发送失败保留、完成状态重启读回 |
| 4 | 统一恢复入口 | 普通上传/下载、转发、目录内单文件、离线补发、二次断线、重复请求、不同 offset；复用更改 3 已落地的稳定 ID 衔接 | 300–450 行 | 旧 fid/原路径/内容不变；删除 forward_resume、client 专用恢复表及 kind |
| 5 | 统一并发握手关联 | 普通文件、目录内文件、公钥、中继 | 250–400 行 | 乱序与旧连接迟到响应不串任务；删除 forward_upload 专用通道 |
| 6 | 原接收端分配方案（已被修订取代） | 普通双向、公钥双向、中继每跳、恢复复用 | 由 `transfer-sender-fid-revision-plan.md` 的发送方分配 R1/R2 取代 | 用户已授权 R2，实施和验证进行中 |


每项包含对应测试、运行时证据、文档和报告，不留测试文档尾单。出现多意图或超出可审规模时先修订拆分。准备项不被宣称为整项任务完成；所有线路切换迁移其全部调用方，不留兼容别名或未使用接口。

## 3. 原子更改 1：已批准实施契约

### 3.1 索引与入口

- 接收进度维持本端 `fid -> 五项值`；发送进度改为 `(receiver_tuple, fid) -> 五项值`。
- 入站运行状态仍以本地 fid 定位；出站 socket、活动标记与暂停开关使用 `(receiver_tuple, fid)`，两种键不可相互释放。
- Client 出站 receiver 始终为配置 server，包括转发上传；转发值中的逻辑目标不改成 server。
- Server 本地暂停/继续发送要求显式 receiver peer。Client 默认使用其 server。已有远程暂停线路格式不变。
- 端口缓存增加该响应的 receiver peer；从控制连接的现有对端身份取得，不能只按 fid 领取或删除。
- 普通发送、恢复发送、双向公钥推送、中继目标端口收集都使用相同的 peer 匹配约束。
- 接收路径不再删除同数值 fid 的出站端口通知。
- 分配器对恢复中的发送索引按 receiver 检查占用；不改变当前 kind 布局与分配权。

### 3.2 持久化与 Web

- `download` 仍以十进制 fid 字符串为键。
- `upload` 的键使用紧凑 JSON 编码的 `[receiver, fid]`；五项值及逻辑 peer 含义不变。
- 不接受旧的裸 upload fid 键作为兼容别名。加载、写回及关闭均保留旧文件并记录拒绝原因；旧断点自动迁移不在本次范围。
- Server/client Web 适配发送索引，API 继续输出数值 fid 和既有进度字段；同号两条任务均保留，不扩展 UI 功能。

### 3.3 变更文件

修改：`PyFlow/network_api/connect_tcp.py`；`PyFlow/transfer_web/web_backend/server_backend.py`；`PyFlow/transfer_web/web_front/client_backend.py`；`test/integration/network_api/test_tcp_file_transfer.py`；`test/integration/network_api/test_tcp_transfer_resume.py`；`test/integration/network_api/test_crypto_tcp.py`；`test/unit/transfer_web/test_web_transfers.py`；TCP 两端 API、文件传输说明及本计划。

新增：`test/unit/network_api/test_fid_allocator.py`（接收端作用域占用回归）；`docs/source/changes/transfer-receiver-scope.md`；`docs/plans/transfer-fid-and-resume-unify-atomic-1-report.md`。

不变：线路上的 fid 与文件字节格式、当前 kind 与 tfid、分配方、恢复命令；其后续切换均未授权。

### 3.4 验收与报告

- 先复现：同号上传进度覆盖、跨方向误关闭、端口误领取。
- 用真实 socket 驱动同号并发发送，分别观察路径与进度；清理一个 peer 后另一个仍能接收完全一致的 payload。
- 回归跨方向清理和同号持久化；每条值仍为五项，读回不得覆盖已存在的其他 receiver。
- Web API 输出两条同号任务，字段和排序正确。
- 运行现有文件、恢复、转发、公钥/加密相关套件；另做真实程序烟测，测试结果不能替代路径证据。
- 原子报告列明命令、结果、路径覆盖、未覆盖、公共契约影响和风险。完成更改 1 后停在人工审查闸门，不推进更改 2。

## 4. 原子更改 2：已批准实施契约

- Server 用 `_fid_routes[upstream_fid]` 保存唯一权威路由：`originator` 与完整 `targets {receiver: fid}`。不保存源路径、目标路径、速度、文件名或目录元数据副本。
- `_fid_route_index[(receiver, downstream_fid)]` 仅回指上游 fid；不复制记录。上游查找直接用本地 fid，下游查找必须带实际请求 peer。
- 登记在主键或下游索引冲突时不做部分覆盖；读回不得覆盖当前 live 路由。删除按路由对象所有权清理主表与索引。
- 复用 `_forward_relays_lock`。现有 `_forward_relays` 只保留在途条件变量、缓冲及流控状态，不是另一份持久化路由。
- Server 的 `relays` JSON 每个上游 fid 只写一行，内容为 `originator` 和 `targets: [[receiver, fid], ...]`；加载时重建索引并以权威路由计数。旧 server relay 结构拒绝读取并保留原文件，不添加兼容别名。
- Server 所有恢复分派、收发进度读回的路由占用判断和分配器占用检查迁移到新表。Client 专用转发恢复记录不在本次删除。
- 可达性初筛后交给中继的目标构成本次接受目标全集；后续端口、连接或开始标志协商失败不能从此集合消失。只有交付数等于接受目标全集大小时，才允许删除整组路由。
- 改动文件：`connect_tcp.py`、既有恢复集成测试与分配器单测、新增路由行为单测，以及文件传输说明、变更说明、计划和原子报告。
- 验收：一对多上下游查找、同数值下游 fid 的接收端隔离、读回重建与冲突不覆盖、真实协商失败后仍保留完整路由、全目标成功后路由清理；现有转发/恢复/加密回归不退化。
- 非目标：不发明新的完成通知、不修复源端最终 ACK 生命周期、不删除 kind/tfid、不改分配方或恢复/上传线路；更改 3–6 仍需分别批准。
- 更改 2 的证据见 `docs/plans/transfer-fid-and-resume-unify-atomic-2-report.md`；server 路由已切换，client 旧恢复记录及现行线路仍保留。

## 5. 原子更改 3：已批准契约与依赖调整
本节记录原子更改 3 在更改 4 切换前的已批准实现与验收，以下保留的旧线路/状态名称是历史边界；当前统一恢复契约以第 7 节为准。

改前真实复现：目标完整接收字节后丢失最终 ACK，零 ACK 和部分 ACK 两种情况下，源端 `_forward_one` 都返回 True，上传进度和 client 恢复记录均被删除；server 仍保留路由。这是更改 3 的改前问题证据，不是最终验收结论。

实施前 `_resume_forward` 会先 pop 旧进度和恢复记录，再调用 `_forward_one`；server 随后生成新上游 fid 和新目标 fid。原路由与新尝试没有关联。因此除了修复最终 ACK 的成功条件，还需保留已批准的稳定 ID 恢复关联，才能让后来成功的重放完成原路由。

人类已确认选择：将原子更改 4 中“恢复沿用旧上下游 fid、接收端按旧记录磁盘 offset 衔接”提前至 3，使逐目标完成能跨重放闭合。仍保留 `/forward_resume`、client 既有恢复记录、kind 和 tfid 命名；统一 resume 命令及旧表删除留在 4，握手合并和分配权切换留在 5/6。

最新人类决策：第二结果通道不再使用，Server 只回传到达端的既有最终 ACK。`_fid_routes[fid]` 保留 `originator`、完整 `targets` 和 `completed`，仅未完成目标保留反向索引。当前上游字节完整、原目标全集均确认后，才经既有数据连接与发送/加密入口回传同种 ACK；成功写出后按 owning route 清理。失败返回既有错误符号或关闭连接，源端保留可恢复状态。

删除新增结果命令、额外超时、result/event 字段和处理函数；不留兼容别名。源端上传函数仅按既有最终 ACK 判成功，错误符号必须返回 False，不能 fall through 成功清理。`_forward_one` 仅在成功后删除其 owning metadata 并持久化。恢复继续用可选 `relay_fid=` 复用原上下游 fid、不重发已完成目标，同 fid 串行及同目标重复合并保持。

接收端 ACK 发送失败时保留原完整路径与进度，即使磁盘字节已齐；稳定 fid、原始来源、文件名、总大小和磁盘 offset 衔接保留，允许 EOF 再确认。保留已有队列作用域、失败缓冲释放、读取超时及快照/读回修复。不增加结果收据/自动重试协议，不扩展普通发送的既有 ACK 等待时限，不自行创建 git 提交，不进入更改 4。

### 更改 3 验收与人工闸门

当前单 ACK 版本相关完整套件 233 passed，定向回归 52 passed；独立真实程序观察到最后目标 ACK 释放前上游成功 ACK 数为 0、释放后为 1，零/部分失败保留源状态并通过原 ID 恢复完成。ACK 写出失败和加密路径由相关回归覆盖，另起新进程验证完成集合及源上传/metadata 读回。新证据与风险见 `docs/plans/transfer-fid-and-resume-unify-atomic-3-report.md`；上一轮双层确认验证不替代本次验收。

更改 3 及补充 O1–O4 已完成审计，现获用户放行更改 4。更改 3 的历史报告保留其当时边界；更改 4 的具体迁移以下一节新增契约为准，握手合并及接收端分配权切换仍需更改 5/6 单独批准。

## 6. 更改 4 前的补充 O1–O4

人类要求先给方案、确认前不写代码：修复 Server 普通发送错误符号误判成功、Server 普通接收最终 ACK 明确写出失败仍清理记录，增加 Server/Client 本地只读的公开进度查询与控制台命令；另增补充项 O4：公开 `delete_config`，通过 `is_list_config=True` 返回可删除名字 list，默认 False 时按指定名字在停止后删除本地配置/状态文件，支持主动放弃持久化进度。

需求卡、查询/删除边界、O1→O2→O3→O4 拆分及验收证据见 [补充计划](ordinary-transfer-progress-inspection-plan.md)。人类已审计通过 O1–O3 并明确批准 O4；O4 不等于原 FID 大计划的原子更改 4。不增加 ACK 收据、自动重试、僵尸自动判定或逐记录删除命令，不自行进入原计划更改 4–6。

补充 O1–O4 已获人类审计通过；运行证据保留在各原子报告和 [补充总报告](ordinary-transfer-progress-inspection-report.md)。本次授权是原 FID 大计划的原子更改 4，不是补充 O4 的重复实现。

## 7. 原子更改 4：已批准实施契约与路径映射

- 唯一恢复线路为 `/resume_trans <fid> <offset> <origin_repr> <target_repr>`，两个地址均为 shell-quoted tuple。删除三 token 旧线路、`/forward_resume` 常量/解析/分支，不保留别名；更改 4 当时保留的 `/forward_upload` 由已放行更改 5 删除，`relay_fid=` 保留至更改 6。
- Server 接收请求时验证 target 等于实际控制请求端；origin 指向 Server 才走本地 `(receiver,fid)` 上传，否则必须准确匹配权威 route 的 originator 和请求端下游 fid，再转换为原上游 fid 转发。未知或伪造来源/目标不降级为另一条恢复路径。
- Client 只有在 origin 为本次控制连接自身身份时发送本地上传；路径取自既有上传五项行。目标不是 Server 时通过原稳定上游 fid 重放单一目标，Server 再核对 route。源端不再复制 targets/kind/path/destination/目录元数据；目标通过原接收行恢复嵌套完整路径，普通 `/file` EOF 重放不要求重建目录命令。
- 移除 Client `_forward_resume_records` 及专用读回/快照代码；Client `relays` 快照区域保持为空，非空旧区域明确拒绝并保留原文件，不静默迁移。Server 路由区域不变。O3 查询随之不再伪造 Client relay 行，O4 在途检查改用瞬态发送同步状态。
- 仅保留在途锁与请求标记，不保存路径或持久化元数据。相同 fid 的不同目标串行，同目标重复请求合并，结束后释放瞬态项；失败保留上传，成功清理。普通恢复和转发恢复共用入口与此约束。
- 删除 fid kind 常量/函数/参数；所有分配调用迁移为同一无 kind 分配器，低位固定零，日志格式为 `e<epoch>.<counter>`。不改变谁分配新传输，也不删除现行 tfid 命名。
- 离线源端请求保留于 Server，重连后经过同一格式化函数与安全发送入口；重复的同目标等待项合并，最新 offset 保留。发送未成功不得先删除待发请求，已完成/不存在路由不得补发陈旧请求。

修改范围：`connect_tcp.py`、分配器/解析/路由与既有真实恢复测试、补充查询/删除测试的受影响状态契约、TCP 两端与文件传输文档、当前变更说明、计划及原子更改 4 报告。历史报告不回写为当前格式；旧需求草案标明被本计划替代。逐项迁移现有消费者，不弱化 ACK/稳定 ID/原路径断言。

路径全集：普通双向文件/目录项 → 统一命令与本地上传行；转发一对一/多目标及部分完成 → scoped route 转换与单目标重放；源端离线/重连 → 待发队列；二次断线/EOF 再失败/成功 → 原 fid 与原文件；同目标重复/不同目标不同 offset → 瞬态合并与串行；Server/Client 读回与进度关闭 → 不新增恢复状态；O3 查询/O4 删除 → 新状态契约；加密/公钥/异步/转发扩展 → 无握手和发送旁路回退。

更改 4 验收要求：保留更改 3 与补充 O1/O2 真实失败证据的行为回归，增加五 token 格式/路由身份边界、无 kind 分派、上传行独立恢复、离线最新 offset 补发与二次断线证据；运行真实普通/转发/目录、并发/EOF 与加密烟测及相关完整套件，报告各路径结果。上述为当时验收契约；当前方向及授权见第 9 节。

## 8. 历史原子更改 5：已被 R2 取代的契约与实施结果

- 覆盖双向普通文件/目录内文件、双向公钥、每个中继目标 listener 和中继源上传。每次握手生成全新 `request=<uuidhex>`，重试不复用。`/file`、`/file_folder`、`/crypto_pub_key` 将标记置于末尾 FID 之前；`/forward_item` 将标记置于 kind 之后。
- request 不是 FID，不进入五项进度行、进度/route JSON 或恢复身份。它只绑定本次实际控制 socket 的一次端口请求；相同地址的新连接不能接收旧连接的结果。
- 发送控制命令前登记实际 control socket、request id 和 expected FID；中继上传尚未分配时仅由匹配请求接收 Server 分配的 upstream FID，不改变现有分配权。每个目标独立登记，可乱序完成。
- 唯一监听响应为 `/server_file_transfer_port <port> <fid> <stamp> <request_id>`。实际 socket 身份、request id、预期 FID 必须共同匹配；端口与加密 stamp 一起交付给唯一 waiter。错误 socket/FID、重复和迟到响应直接丢弃，不缓存、不 fallback、不留兼容 alias。
- 成功消费即移除登记；失败、超时、取消、控制连接断开和对象 teardown 均退役登记并唤醒 waiter。新任务不能接收旧结果；失败不改变既有进度/路由保留规则。
- 删除 `/forward_upload` 生产命令、FIFO 与共享 `_forward_error` 槽。源上传使用上述共同响应；失败只用 `/forward_error <request_id> <reason>` 定向到同一实际控制 socket 的请求，不能唤醒另一个并发上传。
- 不改 allocation authority、`tfid`/`relay_fid=`、稳定上下游 FID、五项进度行、Server route、暂停/恢复、文件字节、ACK 或加密 framing。公钥腿继续 plaintext，端口和 nonce 关联不改变密钥交换顺序。不得提前进入更改 6。
- 迁移所有生产调用方、真实握手/恢复/加密/async/forwarding 测试和活动文档，不保留旧 cache、命令、参数别名或旁路。历史报告不回写。翻译目录只移除过期 gettext 块；新英文源文待翻译，不全局再生成。
- 验收要求（非已运行证据）：真实 socket 并发普通/目录/公钥/中继请求；延迟第一响应并让后发请求先完成；取消/超时/断线后旧响应不能满足新请求；错误 socket/FID、重复响应不能串任务；端口与 nonce 必须成对。由整合负责人运行受影响普通/加密/async/forwarding/恢复套件与文档构建并另行记录结果。

更改 5 实施证据：受影响完整套件 413 passed；之后新增双向旧控制 socket 回调隔离用例，单独运行 2 passed，未计入前述 413。独立加密程序通过双向目录项、同源乱序中继、定向错误隔离、原嵌套路径与收尾清理。文档构建成功但保留两条警告；最终 Ruff 仍有一处历史 custom-handler F841。详细命令、路径结论和限制见原子更改 5 报告；当前待人类审查，不进入更改 6。

## 9. 发送方 FID 修订 R1/R2

原更改 6“接收端分配 FID”的契约由 `transfer-sender-fid-revision-plan.md` 取代，历史更改 1–5 报告保留其当时事实。当前 R1/R2 边界、全量路径与风险以修订方案为准。

R1：普通文件/目录项/公钥继续由各自发送方分配；中继源端在发送 `/forward_item` 前分配 upstream FID，并绑定现有上传行。Server 必须接收 `fid=<upstream_fid>` 并保留原值，只通过同一 `_FidAllocator` 为每个目标分配 child FID。新请求不带 `resume=`；恢复必须显式带 offset（包括 `resume=0`）并复用现有 route 与上下游 FID。

Server 在 route 登记前拒绝已被 upstream route、接收进度或同目标 outbound progress/socket/route/pause 占用的上游 FID，不覆盖既有所有者。R2 以实际控制 socket 和 FID 关联，并以 `/forward_error <fid> <reason>` 返回错误；每次协商只有一个端口或错误终结响应。已发送且无终结结果的超时/取消 FID 在同会话保持未决，不能重试；其他任务不受影响。自然断线清理该会话等待、未决标记及所属 listener/数据腿，重连沿用原 FID；数据或 ACK 失败不断开健康控制连接。

R1 定向回归 13 passed；R1 阶段的 427 passed / 1 项旧 `request_id` 调用失败按用户要求由 R2 移除旧参数解决。最终 R1/R2 完整受影响套件 434 passed；独立明文/加密运行、自然重连与 EOF 证据及历史失败修复记录见 `transfer-sender-fid-r2-report.md`。当前只交付人工审查，不自动提交、合并或推送。



