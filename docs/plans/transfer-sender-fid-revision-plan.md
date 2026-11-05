# 修订方案：发送方分配 FID，保留逐腿 ID，撤销 UUID request

状态：用户已明确授权 R1 后继续 R2。R1/R2 已完成实现；最终受影响完整套件 **434 passed in 278.89s**，明文/加密独立真实程序通过，文档构建成功。历史 R1 的 427 passed / 1 项旧 `request_id` 调用失败按用户要求由 R2 删除旧参数解决，不给测试补旧参数。证据、失败修复记录及既有 lint/文档警告见 `transfer-sender-fid-r2-report.md`。当前等待人工审查；未创建提交、未合并、未推送。

## 1. 已确认方向与本次收敛

1. 普通双向文件、目录项和公钥已经由发送方分配新 FID。普通 resume 复用原上传行的旧 FID，不应重新分配。
2. 修订前中继由 Server 分配上游 fid 及每个目标的 tfid；R1 已将源 Client 到 Server 的上游腿改为源 Client 预先分配。Server 发往目标的下游腿仍由 Server 分配。
3. tfid 只是下游腿角色名。当前已经调用普通无 kind `_fid_alloc.alloc(receiver=target)`，不需要第二个分配器，也不需要删除逐腿映射。
4. 发送方预分配 FID 能解决不同新任务的端口/错误关联，但不能单独区分稳定 FID 的多次尝试。不同尝试若使用同一实际控制 socket 和同一 FID，迟到的旧响应仍无法仅凭 FID 识别。
5. 已有控制连接真正断线时，新连接自然是新会话，不需要额外主动换一次。文件数据子连接失败或关闭，不代表长期控制连接也已断开；两种情况必须分开。
6. 端口申请已发出但终结响应未知的超时、取消或发送结果不确定，按 `(actual_control_socket, fid)` 留下未决标记：当前会话内不再为该 FID 发起新端口申请，迟到响应直接丢弃，不恢复旧等待。其他 FID、命令和健康控制连接不受该标记主动中断；待控制连接自然断开并重新建立后，才允许恢复原 FID。
7. 端口响应已明确消费后的文件数据/最终 ACK 失败，不默认断控制连接，也不自动归入“端口未决”。原数据腿结束、原协商收尾后仍按既有串行/合并约束处理恢复，不能在旧腿仍拥有资源时再启动同 FID 写入。

**本次不再提出“所有失败统一换控制会话”。** 保守代价收窄为：没有终结响应的该 FID 等待自然重连，可能长期保留未完成进度；不自动重连、不改号、不用改名 attempt token 绕过此规则。完整去 UUID 与未决标记必须在同一个原子更改中落地，不能留下仅删 request 的不安全中间版本。

## 2. 需求卡

### 目标

- 所有新数据腿由该腿的发送方通过唯一 `_FidAllocator` 分配 FID。
- 新中继上传在发控制命令前，源端已将 upstream FID 与文件绑定；不再等 Server 分配 upstream FID。
- 保留每个目标独立的 downstream FID（现称 tfid）及 Server 逐腿 route/completed 状态。
- 普通与中继恢复复用原 FID、原完整接收路径、磁盘 offset 和既有最终数据 ACK。
- 删除 `request=<uuidhex>`、相关参数/生成器和 UUID-keyed 等待登记，改用实际控制 socket 与 FID 关联；同时隔离未决 FID，不以文件失败主动断开健康控制连接作为前提。

### 非目标

- 不恢复旧递减计数器、专用 `_forward_alloc_fid`、FIFO、共享 `_forward_error` 槽或无主端口缓存。
- 不迁移到接收端分配；原主计划更改 6 的该方向取消，不将其视为必要重构。
- 不把每次 resume 变成新 FID，不添加改名后的 attempt token，不新增第二张路径/进度权威表。
- 不改变五项进度值、现有 JSON key 结构、文件字节协议、加密算法、鉴权策略、CI、依赖和 coverage。
- 不解决 client_port 变化后的身份迁移，不新增自动重试协议，不自动迁移或删除旧进度文件。

### 安全与数据边界

- FID 仍不是鉴权凭据。Server 继续核对实际请求端、原始 origin、目标全集和 route 所有权。
- port 与 stamp 一次交付，不拆分消费；公钥腿仍 plaintext，普通加密腿仍走现有 framing/发送入口。
- 外部源端提供 FID 后，Server 不得覆盖其他源的 route、接收行、活动腿或暂停状态。保留现有裸 upstream key 结构意味着数值占用冲突必须明确失败，不是无条件支持不同源同号并存；不得偷偷换号。
- 本方案选择冲突拒绝以避免 JSON/索引迁移。若要求不同源同号都可同时成功，需要另行批准 source-scoped 接收/route key 及持久化契约；不夹带在本方案中。

### 风险与禁止事项

风险中高：改变中继上游分配权、控制线路和未决握手的重试条件。未决 FID 可能要等待自然重连；会话内未决标记的内存随未决 FID 数量增长，不宣称 O(1)，断开后释放。禁止删除/跳过行为测试、弱化内容/路径/ACK 断言、恢复 FIFO、仅靠概率宣称无冲突、无声接受旧响应、新旧节点混跑、直接 reset/clean 工作区、自动提交或推送。

## 3. 线路与接口契约

### 3.1 普通文件、目录项、公钥

保留命令原字段及末尾 FID，移除 request 标记：

```text
/file <现有字段> <fid>
/file_folder <现有文件项字段> <fid>
/crypto_pub_key <fid>
/server_file_transfer_port <port> <fid> <stamp>
```

无文件内容的目录创建命令仍不申请数据端口。

### 3.2 中继源请求

```text
/forward_item <kind> fid=<upstream_fid> [resume=<offset>] <现有文件与目标字段>
/forward_error <fid> <reason>
```

- 新任务：源端 `_fid_alloc.alloc(receiver=server_peer)`；在发请求前将 FID 与源路径、总大小和既有五项上传行绑定。上传 key 仍为 `(server_peer, fid)`，五项值中的 peer 继续沿用逻辑接收者含义，不借此改进度结构。Server 只能登记这个 upstream FID，不能另行分配。
- 新任务不带 `resume=`；恢复明确带 `resume=<offset>`，包括 `resume=0`。恢复要求旧 route 存在且属于源端，不因 offset 为零误建新 route。
- Server 创建每个目标的 child FID 时继续调用 `_fid_alloc.alloc(receiver=target)`，保留 `targets` 和 `completed` 的逐目标身份。
- `relay_fid=` 线路被必要的 `fid=` 加显式恢复语义替代，所有调用方迁移，不留旧线路别名。Python 内部参数统一表达 upstream FID，名称调整不得扩展为新类型体系。
- 目标 listener 与源上传使用同一四 token 端口响应；不恢复 `/forward_upload`。
- `forward_target_command`、`forward_item_to` 移除仅为 UUID 添加的 keyword-only `request_id`，其余已有字段保持。

### 3.3 统一恢复

```text
/resume_trans <fid> <offset> <origin_repr> <target_repr>
```

格式不变。接收方使用旧接收记录和磁盘 offset；Server 按实际 requester、origin、target 和 route 转换 downstream/upstream FID；源端从既有上传行取路径。普通或转发恢复均不重新分配已存在的腿 ID。原路径、相同 fid、部分完成和 EOF 再确认约束全部保留。

## 4. 去 UUID 后的等待与会话不变量

- 瞬态等待 key 改成 `(actual_control_socket, fid)`，登记早于发送；每个 key 最多一个有效协商。复用现有同目标合并、同 upstream FID 的不同目标串行，不全局串行所有文件。
- 每次生产协商只允许一个终结控制结果：端口响应或协商错误。端口返回后，数据传输失败走既有数据连接错误/ACK，不再补发同次协商的第二个控制结果。该单终结约束与去 UUID 同批验收。
- 正常端口或错误响应只交付当前 key，端口与 stamp 成对消费。错误 socket、无人等待、已经有结果和未决 key 的响应均丢弃，不缓存为后续任务结果。接收循环捕获所属 socket 对象，不在旧回调中读取新的 `self.client_socket`。
- 无歧义地在发送前退出：删除登记即可。命令可能已经发出、但本方尚未确定终结结果就超时或取消：该 key 标为未决；不能简单删除后在同一会话重登同 FID。等待返回与超时/取消必须在同一同步边界决定归属，避免把已到达的结果误分类。
- 未决 key 不再触发新 `/file` 或 `/forward_item`，也不被迟到端口/错误自动解除；只保留必要的瞬态 FID/状态，不复制文件路径、不持久化。旧会话退出清理标记；新 socket 可以根据原进度/route 恢复。没有自动重连时，继续沿用用户手动重连的既有行为。
- 多目标收集中每个 key 单独定性：已经明确取得结果的目标不因另一个目标超时被一律标为未决；源上传若已经收到明确协商错误，也不等同于它自身的端口响应未知。部分目标状态与完整 route/completed 保留。
- 已消费端口结果后的数据/ACK 失败：关闭该数据腿、保留进度/route，不主动断健康控制连接；原腿未释放前，不能启动第二条同 FID 写入。暂停/继续仍作用于原数据连接，不当作失败。
- 控制连接自然断开：取消其 waiters、清理未决标记及其所属 pending listener/数据腿，保留未完成进度/route，复用已有退出/重连生命周期。当前 pending accept listener 尚未进入 accept 后的数据 socket 登记（connect_tcp.py:5488–5520、10954–10986），需补齐运行时资源归属，不新增持久化状态。
- 去 UUID 的关联以协议生产方单终结响应为前提；不承诺区分错误实现对已结束协商重复发送、且恰落在下一次同 socket/同 FID 等待中的旧响应。若还要求此保证，必须保留额外尝试身份或进一步禁止会话内复用该 FID，不能靠分配器证明。

## 5. 路径映射与验收要求

| 路径 | 必须修改或保持 | 验收证据 |
|---|---|---|
| 普通双向文件、目录项 | 发送端分配保持；去 request；socket+fid 等待 | 同连接延迟第一响应，第二文件先完成，精确字节与嵌套路径正确 |
| 双向公钥及并发 key exchange | 原发送端分配、明文 key 腿和安全切换顺序保持 | 多客户端真实 key exchange 完成，正常帧消息可往返 |
| 首次一对一/多目标中继 | 源先分配 upstream，Server 只分配 child | 源发命令前已有绑定；响应回同 FID；不同目标不同任务的文件不能互换 |
| 无效目标/协商失败 | 错误带 fid；不改其他请求状态 | 一项失败、另一项成功；失败行/已登记 route 保留 |
| 普通/中继 resume | 复用旧上下游 FID，不新建 route | 零 ACK、部分 ACK、EOF 再确认；原路径/内容正确，完整集合 ACK 前源不成功 |
| 同目标重复/不同 offset | 继续合并/串行，不恢复第二张元数据表 | 相同目标只一次有效发送；不同目标各用其 offset |
| 未决端口超时/取消/发送结果不确定 | 当前 socket 的该 FID 不再发新协商，迟到回复丢弃；其他 FID 不被主动中断 | 先超时后注入旧端口/错误，不唤醒新任务且不重发该 FID；另一 FID 完成、控制命令往返正常 |
| 数据腿失败/EOF ACK 未完成 | 已消费端口不等于端口未决，不默认关闭健康控制连接；前一腿结束后再恢复 | 数据 socket 释放、进度保留、控制连接仍收发；恢复保持原 ID/路径 |
| 控制连接自然断线 | 清理旧 waiters/标记/listener，沿用既有退出与重连，不额外主动换一次 | 同地址新 socket 可以恢复原 FID；旧回调不能满足新等待；自动重连开/关符合原配置 |
| 源离线、目标重连 | 保留 pending 最新 offset 与 route | 新会话补发后原 FID 完成；发送失败不提前删除 pending |
| 占用冲突 | 拒绝，不覆盖、不重分配 | 两源同数值 upstream、与已有接收/下游活动身份冲突均返回明确错误，旧任务状态不变 |
| 暂停/继续、O3 查询/O4 删除、读回 | 不新增持久化状态，不把暂停当失败 | 控制/查询/删除在途保护、重启读回回归通过 |
| async、forwarding API、Web 消费者 | 全部迁移，不留 request 调用旁路 | 相应既有集成/契约回归通过 |

## 6. 原子拆分与文件范围

用户已明确授权按 **R1 → R2** 继续，R2 正在实施。每项附对应测试、文档、局部报告和真实运行证据；合并旧 A 的分配权/恢复接入；旧 B/C 的未决规则与去 UUID 必须合在 R2，避免无隔离的中间版本。测试、文档和报告不另拆为后续任务。

| 原子项 | 单一意图与估计 | 关键改动 | 局部验收 |
|---|---|---|---|
| R1：统一每跳发送方分配 | 预计 200–350 行；保留当前 request 关联直至 R2，不新增临时别名 | 源端先分配并绑定 upstream FID；Server 接收必填 `fid=`，只分配 downstream FID；用显式 `resume=` 区分旧 route 恢复；合并冲突检查、失败保留和稳定恢复调用方 | 两个不同目标并发乱序不互换文件；新上游 ID 不被 Server 改写；`resume=0` 不建新 route；部分 ACK/EOF 恢复仍用旧上下游 ID；占用冲突不覆盖 |
| R2：用 FID 完成握手闭环并去 UUID | 预计 350–500 行；握手关联与未决隔离不可拆开 | 所有普通/目录/公钥/中继改为 socket+FID；删除 request 全链参数；端口与错误直接携带 FID；未决 key 阻止同会话重试，正常数据失败不断控制连接；归并单终结响应、旧会话资源清理和全部消费者迁移 | 双向并发/乱序、nonce 配对；超时后该 FID 不重发/不吃迟到回复；其他任务/命令正常；自然重连后原 FID 恢复；数据失败与 ACK 丢失保留原进度且控制连接存活 |

估计为待实施 diff 规模，不是已有结果。若单项实际超过 500 行或混入第二意图，先回到计划修订；不为凑两项跳过审查，也不把必需的测试/文档推迟到下一项。

### R1 文件范围

- `PyFlow/network_api/connect_tcp.py`：`_forward_one`、`_forward_item_handler`、`_forward_relay`、route/进度占用检查及恢复调用；保留现行 UUID 线路直至 R2。R1 中 `/forward_item` 保留 kind 后的 request 标记，同时新增必填 `fid=`；完成后不再存在“Server 为新上传分配 upstream ID”的分支。
- `test/unit/network_api/test_fid_allocator.py`、`test_fid_routes.py`；`test/integration/network_api/test_tcp_transfer_resume.py`，以及受影响的转发/加密既有行为测试。
- 同步中继分配权/恢复接口说明、主计划相应状态和 R1 原子报告；主计划原更改 6 的接收端分配方向改为被本修订取代，不回写历史报告。

### R2 文件范围

- `PyFlow/network_api/connect_tcp.py`：registry/解析构造/分派、普通双向/目录项/公钥/中继的全部生产者与消费者、control socket 生命周期、待接收资源所有权。保留正确的 socket 捕获、port/stamp 配对和数据 ACK 行为。
- `test/unit/network_api/test_transfer_handshakes.py`、必要的 `test_fid_routes.py`/`test_resume_matching.py`；`test/integration/helpers.py`；`test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_file_transfer.py`、`test_crypto_tcp.py`、`test_tcp_async_server.py`；`test/integration/test_forward_msg_attribution.py`。
- 同步普通/公钥/中继全部新线路及未决 FID 等待自然重连的说明、R2 原子报告；受影响翻译条目局部清理，不全局再生成。

### 不再单独拆分或追加的事项

- tfid 当前已经是普通分配器产物，只保留角色名称/映射，不做第二轮分配器重写或全仓机械改名。
- 不恢复 `/forward_resume`、`/forward_upload`、共享端口 cache/FIFO/错误槽；不增加第三张路径/恢复表，不改 progress/route JSON。
- 普通发送分配权、五 token resume、逐目标完成与原最终 ACK、O3/O4、Web 输出契约均保留，只迁移确实受影响的消费者。

生产代码预计仅需 `PyFlow/network_api/connect_tcp.py`；不修改 Web 数据适配器或进度 JSON。测试具体范围：

- `test/unit/network_api/test_fid_allocator.py`、`test_fid_routes.py`、`test_resume_matching.py`、`test_transfer_handshakes.py`。
- `test/integration/helpers.py`。
- `test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_file_transfer.py`、`test_crypto_tcp.py`、`test_tcp_async_server.py`。
- `test/integration/test_forward_msg_attribution.py`，以及真实消费者检索发现的其他受影响调用；实施前对公开 builder/dispatch 运行可用 LSP references，若服务不支持则记录并作全仓定位。
- 文档：主计划、TCP 两端 API、File_Transfer、活动 transfer 变更说明、对应新原子报告；翻译仅同步受影响旧条目，不全局再生成。此前报告保留其当时事实，由修订计划标明替代关系，不伪造为新方案证据。

不通过直接 git reset 回到旧版本实现 R1/R2。当前 UUID 更改尚未提交；按修改意图收敛，保留正确的实际 socket 捕获、资源退役、端口/stamp 配对和行为回归。确认拆分不等于授权创建 git 提交、合并或推送。

## 7. 验证命令与已运行证据

### 方案前真实运行证据（不替代 R1/R2 验收）

本轮在内存加载 HEAD `5002502` 的无 request 历史模块，临时目录隔离日志/进度，启动真实 Server 和 Client。两文件预先由源分配不同 FID，压住第一 port 响应，第二文件先完成，再释放第一响应。实际结果：

- payload 60,018 / 60,035 字节，两文件完整内容各自正确；两个发送函数均成功。
- 回应为四 token，无 UUID request；观察到端口 38079 / 46425。
- 同数值 upstream FID 的第二 origin 被现行 route 登记拒绝，旧 route 不被覆盖。
- 所有跟踪 worker 退出，未写入仓库代码；没有保留烟测脚本。

前轮证据继续有效，不重跑以掩盖事实：`7b84660` 同一 FID 第二握手领取迟到旧端口，两次握手期间分配器调用次数为 0；`5002502` 共享 FIFO 可使不同目标互收对方文件。这两项说明不能只靠分配器或恢复旧队列宣称安全。

当时尚未实施 R1/R2；前述预实施探索不是“源预分配中继、当前冲突拒绝、去 UUID 新 waiter、未决 FID 隔离及自然重连恢复”已完成的证明。

### 已按约定执行的整合验证

各原子项运行对应行为回归和真实 socket 烟测，最后执行：

```text
.venv/bin/python -m pytest test/unit/network_api \
  test/integration/network_api test/integration/test_forward_extension.py \
  test/integration/test_forward_msg_attribution.py \
  test/unit/transfer_web/test_web_transfers.py -q
.venv/bin/python -m sphinx -b html docs/source /tmp/pyflow-sender-fid-html
```

最终受影响套件 434 passed；独立真实程序已覆盖明文/加密、不同目标并发、多目标部分 ACK/EOF、未决隔离、健康控制连接与自然重连原 FID；全部跟踪 worker 退出。Sphinx 构建成功，保留两条既有警告；Ruff 仅剩历史 custom-handler F841。路径结论、命令、修复记录和未覆盖边界详见 `transfer-sender-fid-r2-report.md`。

补充真实运行证据：在当前工作区版本中以既有错误符号替代文件最终 ACK，发送函数返回 False，数据 socket 已退出活动登记，上传行保留；原控制 socket 和运行状态保持，随后通过同一控制连接完成一次真实命令/响应。全部跟踪 worker 退出。该证据验证两类 socket 的生命周期不同，不是无 request 同 FID 重试已实现的证据。

## 8. R1 证据与 R2 执行状态

R1 覆盖源端预分配 upstream FID、显式 `resume=`（含零 offset）、Server 逐目标分配 child FID、占用冲突拒绝，以及对应测试与活动文档。定向回归 13 passed；合并受影响套件 427 passed / 1 项旧 `request_id` 调用失败。用户明确要求不单独修复该旧调用，因为 R2 删除此参数，并明确授权继续 R2。

R2 已实施并通过本报告关联的验证；去 UUID 与未决 FID 隔离同批完成。不因数据腿或 ACK 失败主动断开健康控制连接，未决 FID 等待自然会话断开后恢复，既有自动重连配置不变。R1/R2 当前交付人工审查，不将验证通过等同于人类审计通过，不创建提交、合并或推送。
