# 传输 FID 与恢复链路统一计划

状态：原子更改 1 已审查并由人类提交；原子更改 2 已实施并完成局部验收，等待人类审查。更改 3–6 未放行、未实施。

本文件替换先前“kind 位 + tfid 重放”的草案。后续实现不得沿用被替换的设计。

## 1. 需求卡

目标：所有新传输由本跳接收端通过统一入口分配 fid；删除 tfid 特殊类型与 fid kind 位；以原始发送者及 server 的逐跳路由映射统一恢复。

现状：代码已存在 `_FidAllocator`，普通文件和公钥推送当前仍由发送端分配，中继由 server 分配 1+N 个 ID。分配权切换属于原子更改 6，不是更改 1。

非目标：不改文件字节流格式，不做中继内容落盘、暂停状态持久化、client_port 变化后的身份迁移；不改加密算法、鉴权体系、CI、依赖及覆盖率要求。关闭进度记录时保持不支持自动续传的既有约定。新旧节点不混跑，不承诺旧版断点自动迁移，不自动删除旧进度文件。

记录边界：复用 `file_upload_progress` 和 `file_recv_progress` 的五项值 `[bytes, total, speed, local_path, peer]`。不新增 client 恢复专用表，不复制已有路径和进度信息。接收记录的 peer 是原始发送者，发送记录的 peer 是逻辑接收者，不将二者混作 origin。

Server 将现有转发恢复记录收敛为一份 FID 路由关系：原始源端、上游身份、目标全集及下游身份、目标完成状态；不另建重复权威状态。已有 relays 持久化区域承载该关系。

恢复方向：`/resume_trans <fid> <offset> <origin> <target>`；origin 是自己则发送本地文件，否则 server 核对原始请求者与路由并转换上下游 fid。最终接收端以稳定 fid、来源、文件名和总大小匹配旧接收记录，继续写其完整路径，不用新 ID/同路径桥接。

完成语义：本跳数据 ACK 不等于原目标全集全部完成。草案采用统一控制结果通知 `/transfer_result <fid> <pending|complete>`，未明确完成则保留源端发送进度。具体实现属于更改 3，尚未放行。

风险：中高。接收端独立分配要求身份包含接收端，线路与部分持久化索引将变化；五项进度值和文件内容格式不变。

禁止事项：不删/跳过测试，不弱化断言，不改 CI/依赖/coverage，不引入第二套 fid 生成器，不把握手 request 标记作为持久文件 ID，不绕过既有发送/接收/加密入口，不自行提交远端或进入下一原子更改。

## 2. 路径全集与原子拆分

| # | 原子意图 | 覆盖路径 | 预计代码/测试与对应说明改动 | 局部验证 |
|---|---|---|---|---|
| 1 | 现有索引补齐接收端作用域 | 普通双向发送/接收、转发上传/目标端口、公钥双向端口、暂停、断线清理、持久化、Web | 250–400 行 | 不同接收端同号、跨方向同号、并发端口匹配、重启读回和 Web 输出 |
| 2 | Server FID 路由表接管现有转发恢复状态 | 中继登记、上下游查找、目标全集、relays 持久化 | 200–350 行 | 一对多路由与重启读回、协商失败不误算完成 |
| 3 | 逐目标完成与源端记录生命周期 | 数据泵 ACK、结果通知、发送完成清理 | 200–350 行 | 零 ACK、部分成功、最后一个目标完成 |
| 4 | 统一恢复入口 | 普通上传/下载、转发、目录内单文件、离线补发、二次断线、重复请求、不同 offset | 300–450 行 | 旧 fid/原路径/内容不变；删除 forward_resume、client 专用恢复表、路径桥接及 kind |
| 5 | 统一并发握手关联 | 普通文件、目录内文件、公钥、中继 | 250–400 行 | 乱序与旧连接迟到响应不串任务；删除 forward_upload 专用通道 |
| 6 | 分配权切换至接收登记入口 | 普通双向、公钥双向、中继每跳、恢复复用 | 150–300 行 | 逐路径记录分配方；新传输接收端分配，恢复不再分配；无 tfid/发送端分配旁路 |

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
