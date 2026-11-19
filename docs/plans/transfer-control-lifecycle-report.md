# 传输控制生命周期实施报告

## C6：总体报告与总验收

**当前状态**：用户已批准 C6；本轮首次 V6 合并回归实际为 **692 passed、1 skipped，1028.13s**，退出 0。独立真实场景按下文记录。以下 C1–C5 章节是各阶段交付时的历史记录，不代表其旧“未实现/待验证”文字仍为当前状态；撤回的旧 C3a/C3b 不作为当前验收依据。本节只统计本轮实际输出，不借 C5 的 994 passed 冒充 C6 结果。

### Standards

**硬违规 0、阻塞 0、Fowler 判断项 0；correct，置信度 0.96。** 只读审查确认逐路径内容/ACK/owner 与测试数分开、五项部分覆盖清楚，四文档单原子范围与候选一致，未修改生产/测试/CI/依赖/公共接口；当前 C6 入口与历史状态、人工接受/合并/推送闸门明确。未编辑、运行 git 或执行检查。

### Spec

**0 个阻塞，correct，置信度 0.94。** 只读审查确认 C6 保持四文档范围，本轮结果与历史结果分开；P05/P11/P13/P17/P20 部分覆盖已披露，与独立运行证据及抽查消费者契约相符。tracking-off 实际组合、owner 样本、实例重建/手工 self 行/Flask test client 和人工闸门未冒称额外覆盖。未运行 git、测试或其他检查。

### 范围与总体数据流

- 基线 `b0e2783008`；C6 仅修改需求卡、计划、当前总体报告和变更记录。本轮未修改生产代码、测试或 CI，不增加 wire、公开签名、依赖、五项进度行或 relay 快照字段。
- 普通恢复仍由实际 Receiver 在旧文件关闭后请求磁盘 offset，同 fid/原路径建新数据腿；运行/暂停/握手/finally 占用不能被恢复抢占。资源由实际控制 socket 的 registry 所有，旧清理不按 peer 扫新连接。
- 转发由 Source 仲裁全任务用户 pause/start；Server writers 排空在途/已收字节，reader 高低水位仅由本地缓冲变化解除，不把自动背压变成用户 start。
- 恢复请求经过实际会话/route/child 校验，仅登记每 Target 数值 offset；等原 route 全部旧 owner（含 finally），再各自独立数据流/进度/失败。单 Target ACK 只完成该 Target，累计真实 ACK 后才允许整任务成功。
- 全目标完成后的显式 EOF 仅复用原 completed 证据，无正文、无 Target 重开；原最终 ACK 写成功后 Server 按 route 身份清理，Source 实际收到 ACK 后清所属上传行。原生自目标拒绝位于 Client 统一入口；mixed 合法目标仍走同一真实传输链。

### 当前完成的独立运行证据

下面参数缩写只用于本节：**M**=明文 thread/localhost；**E**=加密 thread；**A**=加密 async/wildcard。A 中 S1/S2/S4/S5 的恢复组合在真实保留行产生后关闭 tracking；无恢复的 S3 及普通边界补充仍开启 tracking，S6 replay 是手工构造的旧规范 self 行，均不能冒称真实 tracking-off 恢复证据。临时程序不调用 pytest 测试函数，不替换文件/网络/加密主路径；Event 仅固定交叉或明确 ACK 故障。worker 计数含本程序记录到的控制/加密/数据线程，0 存活来自实际 owning 收尾和 join。

| 场景/组合 | fid / 控制身份 | 实际证据 | worker / 最终存活 |
|---|---|---|---|
| S1 双向普通恢复 M | upload=802、download=804 | 双向实际 offset=65536，原嵌套路径、完整内容/hash、真实 Source ACK=True、所属进度清空 | 22 / 0 |
| S1 双向普通恢复 E | 同上稳定 fid | 同上，两条加密业务腿 | 35 / 0 |
| S1 双向普通恢复 A | 同上稳定 fid | 同上，保留行 tracking off 恢复 | 37 / 0 |
| 普通握手/finally/空文件 M | 1802/1806；EOF=4112/0 | 同 fid 握手预留及 socket 已释放的 finally 仍拒绝第二恢复；另一 fid 实际 ACK；显式再做才清原行 | 36 / 0 |
| 普通握手/finally/空文件 E | 同上稳定 fid | 两次明确业务 ACK False 后一次 True；真实内容/hash，不凭 size 或文件存在认定完成 | 49 / 0 |
| 普通握手/finally/空文件 A | 同上稳定 fid | async/wildcard，tracking 仍开启；该程序 JSON 的 untracked 仅参数回显，不算关闭 tracking | 50 / 0 |
| S2 旧 worker/新会话 M | 4493891096→新普通 4494091096 | 原 ACK False 留行；旧 worker 仍用旧 control 并返回 False；新连接实际消息/新文件 ACK True，旧行同对象 | 26 / 0 |
| S2 旧 worker/新会话 E | 10125171026→10125371026 | 同上，按实际业务 upstream socket 注入 ACK False | 65 / 0 |
| S2 旧 worker/新会话 A | 10265569570→10265769570 | 同上，tracking off 后保留旧 forward 行，旧 worker 收尾 | 65 / 0 |
| S3 全局暂停 M | 8531060282 | Source/Server upstream/每 Target 操作后状态收敛；两 Target 排空 1024 字节；同 socket start | 80 / 0 |
| S3 全局暂停 E | 9463323652 | 同上，reader 重入压力门无新增读取，排空不撤用户 pause | 119 / 0 |
| S3 全局暂停 A | 10419783064 | 再覆盖 Server child；队列 peak=2090≤64+2048，buffer 最终 0，完整内容及真实 ACK | 129 / 0 |
| S4 不同 offset/晚到目标 M | 8567571254；8192/24576 | A 正文门阻塞时 B 晚到、独立 socket/正文/Target ACK；此前无整任务 ACK，A 后完成 | 49 / 0 |
| S4 不同 offset/晚到目标 E | 11740498306；8192/24576 | A Target ACK 门阻塞时 B 完成；累计 B、A 真实 ACK 后整任务才 ACK | 88 / 0 |
| S4 不同 offset/晚到目标 A | 12248869122；8192/24576 | 原 live/paused/finally 只 pending；最后旧 owner 退出后建独立流；实际保留行 tracking off | 88 / 0 |
| S4 离线/新会话/单流失败 M | 4429802526；初始 16384/8192→A 新 24576 | Source 离线 pending 保留；A 断线丢旧请求，原端点新会话重发；A 再断腿不动 B，另 fid 真 ACK，显式 A 再恢复才整体 ACK | 64 / 0 |
| S4 离线/新会话/单流失败 E | 10150322048；8192/8192→A 新 16384 | 同上，真实加密与新实际 control socket；未自动重试 A | 129 / 0 |
| S4 离线/新会话/单流失败 A | 12168513906；8192/8192→A 新 16384 | 同上，原始保留行 tracking off，兄弟 owner 原对象保留，最终内容/hash/索引归零 | 128 / 0 |
| S5 终态再确认 M | 4799102086 | 两 Target 真实 ACK 后首次最终 ACK False；双端实际实例重建/原端点回连；再次异常保留、显式成功，零正文 | 59 / 0 |
| S5 终态再确认 E | 11454431374 | 同上，加密实际数据 channel；9 类错误/并发/坏头请求拒绝 | 124 / 0 |
| S5 终态再确认 A | 10253682156 | 同上，快照原 schema、保留行 tracking off；query 无行、运行时删除拒绝、关闭后删除不被再次 close 重建 | 126 / 0 |
| S6 自目标 M | Source=127.0.0.1:38647 | 6 次 self/replay False，5 次合法 Source ACK=True；另目标 127.0.0.2:38647 同端口不同 IP 真实收件 | 68 / 0 |
| S6 自目标 E | Source=127.0.0.1:44199 | console/Web file/folder、嵌套空文件，合法内容和 ACK；旧 self 上传行保留 | 80 / 0 |
| S6 自目标 A | Source=127.0.0.1:42569 | 同上，wildcard、tracking off；拒绝前无 fid/新行/请求/数据腿 | 80 / 0 |
| 原 route 握手期 M | 7504141324 | Target 实际控制会话发 child/offset=0 请求，只 pending、不拆原握手；原任务完成后无重复重放 | 26 / 0 |
| 原 route 握手期 E | 9342026838 | 同上，控制 reader 在握手 Event 中仍接收实际消息 | 65 / 0 |
| 原 route 握手期 A | 12610350206 | 同上，资源/final owner 全结束、真实 Target 内容与整任务 ACK | 66 / 0 |
| 原 route EOF 等待 M | 7844326422 | A 最终 ACK 明确失败，B ACK 被 Event 挂起时 A 请求 offset=65802，只 pending；旧 owner 结束后 A 零正文恢复 | 34 / 0 |
| 原 route EOF 等待 E | 10035117130 | 同上，原尝试 False→同 fid 恢复 True；完成 B 不重传 | 73 / 0 |
| 原 route EOF 等待 A | 11256552678 | 同上，旧 owner 保持到放行；pending 期间控制消息仍送达 | 73 / 0 |

S5 三组实际 child fid 分别为 `8383925712/12678893008`、`19702615484/23997582780`、`17607792216/21902759512`；全部 Target 已离线仍可确认。非空文件 65802 字节，SHA-256 `e0d362d4ab08c66b550e9ba59b456d2dd652e987d0f5921a94ad4acdb342bfa2`，两次确认仅三段原头；原目标 inode/mtime/内容不变。实际拒绝 unknown fid、错误成员、多目标、错误 Source、有效确认重复、finally 重复、非 EOF、offset 过大及截断头；失败保留同一原 route/行，最终 route/index/recovery/快照清空。

S6 文件 33031 字节，SHA-256 `52c984d43140724ae6a08aee355df7bd6a5f92e1d389eb299e731c21d1b46038`；自拒绝日志可见，每组仅 5 条合法请求，手工构造的旧 self 恢复 fid=619 行对象保留。Web 使用 Flask **test client** multipart `/api/send_file`/`/api/send_folder` 驱动真实后端与 TCP，不是监听 HTTP 端口或浏览器/UI 验收；HTTP 200 仍是异步受理，不是完成或新增 UI 拒绝提示。握手/EOF 窗口内容 SHA-256 `60fc47cfdeb525fd8cbe7a8226c3f7472c645a086e9dbfad195d9c1af4d5c736`，EOF 等待场景保持稳定 child 与原 offset，原尝试 False、后续 True；不把协议零 offset 请求说成已落盘非零恢复。

S1 Advanced 另跑明文 alias 与加密 async/wildcard/tracking-off 两组：同一接收根的 `bundle/a/same.bin`、`bundle/b/same.bin` 内容/hash 均正确；fid=902 两次真实断腿的实际 offset 为 65536、131072，paused busy 返回 False 并保留原 socket，另一 fid=904 独立完成真实 ACK。五字段快照关闭并重建 Server **实例**后从 131072 恢复到最终 ACK；worker 分别 29/59，最终存活均 0。不是进程重启。S2 Advanced 同两组在新会话另完成原生 forward 新 fid `10457927960`、`19838860184`，Target 完整 hash、Source 返回 True，worker 32/71、最终 0；不是把新普通文件误称成新 forward。

S3 首两组在 socket 收尾后才采端点，JSON `owners=[]`，不作为已记录端点声明；socket 身份保持、内容、暂停和 ACK 断言确实通过，第三组记录实际双向端点。S4 三组真正重建各 Target 原 child、保留 `original/nested/relay.bin`，原 Target 请求通过 Receiver 的 `_request_resumes()` 发送；原 Server finally 挂起时没有 active，A 阻塞时 B 先确认不清共享 Source 行。非最后恢复流的返回 False 表示整任务未完成，不是 Target 内容失败。

S4 故障补充不是预置恢复行：原两 Target 首次真实落盘后关数据腿；Source 实际离线，Receiver 原入口发送数值请求。A 实际控制连接关闭会删除只绑定旧 socket 的 pending，B 不丢；Source 原端点回连先服务 B，A 原端点新会话自动从磁盘重发。A 第一次恢复新增一块后再次关自身数据腿，Source 返回 False；共享控制仅留下 B owner，B 与无关普通 fid=1702 的内容/真实 ACK 不受影响。A 无自动重发；B 已完成仍保留 route/Source 共享行，A 显式以新实际 offset 重做后才有一个上游成功 ACK。三组 Source 结果均为原任务 False、A 失败 False、B 整体未完成 False、最后 A True；末尾 route/index/recoveries/进度清理，并由 owning cleanup 收尾所有 worker。

实际 owner 记录样本（同一行左右为该 socket 的 local/peer，均为 `127.0.0.1`；只列输出中取得的样本，不补猜已关闭 socket 端点）：S1 M control `34107↔37493`，上传旧 data `58711↔37559`、下载旧 data `44867↔45505`；S2 M 旧 control `43143↔51703`、新 `33079↔51703`；S3 A Source data `40375↔45897`、两 child data `37856↔40149`/`50458↔43689`；S4 M 原 Source data `48709↔42993`，独立 A/B data `60813↔38531`/`56751↔37819`，Target control 为 `34655`/`39629`。S4 故障 A 的 Source control 对象 `134456782269856→134456782271008`、Target control `134456772529120→134456772811104`，新请求按实际新 socket 被接纳，B receive owner 全程同对象；不是只比较 peer 端点猜 owner。

### 已记录的验收程序修正

- 两条加密 S1 的首轮失败来自临时跨方向 gate 未恢复，旧闭包影响第二方向；恢复原透传方法后只重跑失败参数通过，不修改生产发送。
- 初次加密 S5 的 ACK 观测仅按标记计数，误把密钥传输 ACK 当业务 ACK，实际显式确认先成功；诊断输出 `expected=[False], observed=[True], ack_attempts=3`。改为同时匹配原 route originator 与实际业务数据 socket 后，仅重跑受影响的加密参数通过，不修改生产加密或 ACK 协议。
- 原 EOF 窗口首轮已取得 False→True，但临时程序硬钉两个并发 Target ACK 回调的先后顺序；改为每 Target 的真实尝试次数和最后 A 恢复 ACK，保留全部内容、pending、owning 及结果断言，仅重验失败场景。握手已通过参数未重跑碰数。
- S2 初次三组 ACK 观察只按消息内容筛选，存在误命中公钥传输的风险；不计入有效 S2。修为同时限定业务 fid、实际 control 和 upstream data socket 后重验三组通过。Advanced 首轮 gate 把同一已关闭 socket 的尾部发送误当第二条腿而阻塞；改为仅不同 socket 固定事件后只重验失败两组通过。
- S3/S4 临时程序在批次运行期间被编辑导致四组 IndentationError，修复后只重验失败四组。S4 透传闭包晚绑定，明文一次表面 exit 0 也不计有效，加密两组等待正文门失败；固定 Server 原方法绑定并按业务 fid/socket 记录 ACK 后仅重验无效/失败三组通过。未修改产品 I/O、加密或协议。
- S4 故障补充的两次明文首轮失败来自临时程序硬钉 `file.write` 必须恰好 8192；诊断实际两文件为 24576/8192，TCP 接收允许合并写块。改为首个真实正写入后固定 Event、从已关闭文件/保留行读取各自实际 offset，不手工截断或改内容，再运行失败明文及此前未运行的两组加密均通过。未把正文发送块边界当接收写边界。
- 普通边界补充首轮临时观察器读取不存在的握手 `fid` 字段，尚未发业务就 KeyError；移除字段假设后又发现直接调用 `file_transfer_mode` 不经过外层 `_sending_transfer`，因此 finally Event 不到。改为观察真实 `resume_trans` worker 的外层 finally，只重验失败明文及未运行加密组合通过：业务 ACK 序列 False、False、True，busy 阶段无第二 worker。这里不是新增产品修复。

### P01–P20 本轮对照

“覆盖”只表示所列路径和实际参数有运行时/机械证据，不表示所有平台或所有排列已验收；“部分”列出本轮未独立证明的窗口。V6 集成路径是真实 socket 消费者，unit 的预置 registry/进度不是独立端到端场景。以下引用的测试均包含在本轮唯一一次 V6 中，未为填表重跑。

| 路径 | 结论 | 本轮证据与限制 |
|---|---|---|
| P01 普通上传 | 覆盖 | S1 双向中的上传；关闭文件后实际 offset、同 fid、原路径/hash/真实 ACK，M/E/A |
| P02 普通下载 | 覆盖 | S1 双向中的下载，M/E/A；不把上传证据代替下载 |
| P03 文件夹/同名不同目录 | 覆盖 | S1 Advanced 同接收根 `bundle/a`/`bundle/b`；S6 原生 folder 枚举、嵌套空文件及 Web 文件夹适配 |
| P04 普通 live/paused/握手/finally | 覆盖 | S1 Advanced paused/live busy、其他 fid；普通边界补充握手及 socket 已释放 finally 保留同 owner、`resume_trans=False`，后续真 ACK；独立程序后两窗口为上传方向 |
| P05 换腿再断线/重启 | 部分 | S1 Advanced 同 fid 两次断线、实际 65536→131072、Server 实例重建/原端点回连；未运行独立进程重启 |
| P06 实际会话/迟到清理 | 覆盖 | S2 旧 worker 固定旧 control/new 可用；V6 `test_relay_offline_source_replays_stable_route_or_stops_cleanly` 在新 owner 活跃时再次释放旧资源，原对象仍在且有最终内容/ACK |
| P07 旧 worker/connect | 覆盖 | S2 挂起 worker 与 connect 交叉，旧 socket False、旧行同对象、新消息/普通新 fid/原生新 forward 真 ACK；不是靠等待 30s 判定 |
| P08 各参与者 pause/start | 覆盖 | S3 + V6 五 issuer 参数（Source、Server upstream/alias、两个 Target）；已确认状态收敛、另一端 start、完整内容 |
| P09 未确认反向/交叉/重复 | 覆盖 | V6 `test_relay_cross_target_start_and_unconfirmed_reverse_intent_reach_source` 用 Event 固定 Source pause 通知，未确认时两 Target 反向 start，最后全状态 False/内容 ACK；普通双向亦有对应交叉契约 |
| P10 在途/排队排空 | 覆盖 | S3 两 Target 在 pause 时各写入 1024 字节，Source 下一块等待、同 socket start，真实 ACK；V6 双向普通排空 |
| P11 本地压力与 start | 部分 | S3/V6 reader 重入时读数不变、用户 start 不开压力门、drain 不撤用户 pause、peak≤high+max；未独立负断言自动压力绝不发送控制帧，不声明进程总内存上限 |
| P12 原 route 各 owner 窗口 | 覆盖 | S4 live/paused/finally、六组握手/EOF 等待补充：只 pending、不关旧 owner、结束后才恢复；控制消息和另一 fid 仍实际完成 |
| P13 错路由/去重/旧会话 | 部分 | V6 `test_resume_rejects_forged_or_stale_requesters_before_registration`、`test_duplicate_registration_keeps_only_the_latest_pending_offset` 为 unit 准入/去重；S4 故障为真实旧 socket pending 丢弃/新会话重发；未把所有非法 wire 请求独立透过控制 reader 重放 |
| P14 不同 offset/独立并行 | 覆盖 | S4 8192/24576、晚到 B；A 正文门/Target ACK 门各一种组合时 B 真实完成，最后累计 Target ACK 才整任务 ACK；V6 多 owner 共享 pause 与迟到旧释放 |
| P15 离线/单流失败 | 覆盖 | S4 故障三组：离线 Source pending，Target 旧请求丢弃、新 socket 重发，A 新实际 offset 重做；B owner/内容/ACK 和无关 fid=1702 保留，无自动重试；V6 Source/stop 两分支 |
| P16 零 offset/空文件/EOF | 覆盖 | 普通边界补充真实空文件 offset=0/EOF=4112，S4 Target EOF、S5 终态 EOF；V6 `test_forward_resume_with_zero_offset_reuses_existing_route` 通过真实原 wire/ACK 验证预置空文件 route 无新分配，明确它不是实际断腿产生的行 |
| P17 全目标完成补最终确认 | 部分 | S5 三组 Target 已真实 ACK、首上游 False、再次异常、显式成功、原记录重载、目标离线、坏头/并发/finally、零正文/无 Target 新命令；V6 C4 契约补充最后独立恢复后的 ACK 失败；未独立对 C4 组合 stop/中途 rekey/无关活跃 fid 全部交叉 |
| P18 自目标/混合/同机端点 | 覆盖 | S6 三组 self/replay False 且无新 fid/行/腿，mixed 真内容/ACK；M 另运行同端口不同 IP；Web 只验后端适配，非 UI |
| P19 tracking/query/delete/stop | 覆盖 | S1/S4 真实保留行后关闭 tracking；S5 query detached、运行删除拒绝、关闭删除后再次 close 不重建，原五字段行及 upload/download/relays schema；V6 删除/快照契约，无凭空新进度 |
| P20 安全 I/O/模式矩阵 | 部分 | M/E/A 各有真实内容、ACK、安全 channel/owner；V6 两个加密 rekey 参数实际 key reader 继续接收，帧有 FRAME_MAGIC、无明文 pause/start、无 raw 绕过；未覆盖 relay/终态中途 rekey、完整笛卡尔积及其他平台 |

### 本轮命令与机械结果

运行平台：Linux x86_64，`.venv/bin/python` **3.10.20**。合并回归仅执行下面一次，未重新执行 C5 项目全套、未重跑已知 37 条类型诊断。

```bash
set -o pipefail
.venv/bin/python -m pytest test/unit/network_api test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_file_transfer.py test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_crypto_tcp.py test/integration/network_api/test_tcp_async_server.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py -q --tb=short 2>&1 | tee local://c6-v6-pytest.log
```

实际：**692 passed、1 skipped，1028.13s，exit 0**。明文 rekey 参数在测试中显式 skip（`re-key requires the encrypted fixture variants`），本轮未新增 skip；不把 skip 或已知类型诊断计通过。

独立程序下列参数组均实际 exit 0；表中 `M/E/A` 分别展开为 `--alias`、`--encrypted`、`--encrypted --async-server --wildcard --untracked`，均使用前缀 `PYTHONPATH="$PWD:/tmp" .venv/bin/python`。命令是本轮历史执行记录，不作为删除临时程序后的长期入口；可持续复验入口是 V6 内已有契约测试。

| 程序 | 实际参数组 |
|---|---|
| `/tmp/pyflow_c6_direct.py` | `--case direct-leg` M/E/A；`--case relay-leg` M/E/A |
| 同上 Advanced | `--case direct-leg --gate ack` M/A；`--case relay-leg --gate ack` M/A（此处 ack 是 Advanced 选择，不是 ACK 注入） |
| `/tmp/pyflow_c6_relay.py` | `--case global-pause` M/E/A；`--case parallel-recovery --gate body` M/A；`--case parallel-recovery --gate ack` E |
| `/tmp/pyflow-transfer-lifecycle-smoke.py` | `--case parallel-recovery --window handshake` M/E/A；`--case parallel-recovery --window eof` M/E/A |
| 同上终态/自拒绝 | `--case eof-confirm` M/E/A；`--case self-target` M/E/A |
| `/tmp/pyflow_c6_faults.py` | `--case parallel-recovery` M/E/A |
| `/tmp/pyflow_c6_edges.py` | `--case direct-leg` M/E/A；该 A 的 tracking 参数未应用，不能据 JSON 参数回显声称 tracking off |

文档机械检查另实际运行一次：**4 份文档、11 个相对链接、P01–P20 恰好 20 行且顺序一致，15 项覆盖/5 项部分，错误 0，exit 0**。这只证明文档路径/链接和矩阵完整，不增加功能或全局属性通过声明。

本节结果来自实际独立程序输出与 V6 日志；八个临时程序/辅助模块均已删除，不保留第二套永久测试框架。没有新增或删除测试、放松断言、coverage、CI 或依赖；新采集过程只修临时观察器，不更改已验证的产品路径。

### 原子变更与影响

| 阶段 | 原子意图/当前状态 | 主要更改路径与公共影响 |
|---|---|---|
| C1 | 实际控制会话取消及旧 owner 收尾，已实现 | `connect_tcp.py` registry、会话定向取消、worker 固定 control；同 fid/进度格式不变 |
| C2 | Source 仲裁任务级控制、排空与本地背压，已实现 | 同模块控制 registry/relay readers/writers；pause/start 原签名/wire，转发语义改为任务级 |
| 旧 C3a/C3b | 已撤回，不作为现行方案 | `?`/None 查询、强制拆腿及单活跃 FIFO 是历史，不作为本轮契约或通过依据 |
| 本轮 C3 | 数值 offset + 原 owner barrier + 每 Target 独立恢复，已实现 | 同模块 parser/Receiver/route/调度；移除查询，不加 wire/schema 字段，各流正文/ACK 可并行 |
| C4 | 原 route/completed 上显式 EOF 补最终确认，已实现 | 同模块准入/header/最终 ACK/owning 清理；无新 Target 正文、终态表或 Source 行字段 |
| C5 | Client 原生统一入口拒绝自身完整控制端点，已实现 | `_forward_one` 及对应使用文档/契约；console/folder/Web 后端复用，消息/独立扩展不改 |
| C6 | 本次单原子验收与报告收尾 | 仅本报告、需求卡、计划及 `docs/source/changes/transfer-fid-routes.md`；未改生产/测试策略/CI/依赖/公开签名 |

### 明确边界与人工闸门

- native Windows、其他 Python、IPv6/NAT/端点迁移、独立进程重启、全部参数排列均未运行；实例重建不等于进程重启，Flask test client 不等于浏览器验收。
- 同一控制会话/同 fid 新旧数据腿迟到 pause/start 的严格代次隔离（问题二）继续暂缓；本轮会话定向清理不解决它。不引入 leg_id、换新 fid、自动重试或超时抢占。
- Server ACK 写成功但 Source 未收到的不确定性继续存在；不新增 ACK-of-ACK、终态证明或自动补偿。EOF 只在已完成集合上补确认，不证明历史文件名/大小/hash。
- 全局属性的覆盖依据是上述具体 socket 路径、帧检查和 owner/内容/ACK，不把“692 passed”单独当作全局安全证明；P05/P11/P13/P17/P20 的部分覆盖不隐藏。
- 已知 37 条类型诊断仍保留，未重复运行、未修或计通过。未改 CI、coverage、依赖、wire/schema，未合并、推送或变更保护策略。
- 规则沉淀复用已存在的消费者契约和本节路径矩阵：实际 socket ownership、finally barrier、每 Target 独立流/实际 offset、累计真实 ACK 和 self 统一入口。C6 没有以新增源码字符串断言替代运行证据。
- C6 可作为当前分支的验收文档提交；**不声明人工总验收已通过**。人工仍须接受本节部分覆盖与平台/协议边界后决定合并/远端；未接受项保持明确未覆盖，不借历史通过数覆盖。


## C5：原生文件与目录转发自拒绝

**意图与授权**：用户确认“同 IP 且同端口”属于自身，并明确要求开始 C5。混合列表只剔除自身，同 IP 不同端口及不同 IP 同端口仍合法。本次单原子变更不推进 C6，不合并或推送；C1–C4 的历史输出不作为本轮证据。

### 更改路径与数据流

- `PyFlow/network_api/connect_tcp.py::_forward_one`：复用 `_socket_local_endpoint` 和 `_is_control_endpoint`，使用准入 worker 捕获的实际控制 socket。配置主机仅在确实解析为该实际 IP 时匹配，wildcard 仅别名实际端口；不会把未绑定/端口回退后未使用的配置 IP 拼成自身。仅命中配置候选才做解析，实际端点及普通不同目标无需解析。过滤在新 fid、上传行和握手之前；纯自身记录拒绝并返回 False。有合法目标时，进度 peer、wire 和发送路径共用过滤后的规范化列表。
- `test/integration/network_api/test_tcp_transfer_resume.py`：六参数（file/folder × 127.0.0.1/localhost/0.0.0.0）真实 console 回归，证明无分配/新记录/请求/回环文件、mixed 的 Source ACK=True、保留行自恢复失败且原对象仍在；另加两参数，实际无 bind 和 EADDRINUSE 回退后，其他 IP/同端口目标仍真实收到，实际自身仍拒绝。
- File_Transfer、TCP_Client_APIs、变更记录和既有需求/计划/本报告同步。Web 适配和 Server 未改；沿用原安全发送、port/header/body/ACK、owning 清理，不新增协议、接口、schema、CI、依赖或抽象。

数据流：console/Web file/folder/保留行恢复 → 原 driver 或恢复 worker → `_forward_one` 规范化与自目标过滤 → 无合法目标则拒绝并停止；否则原流式转发 → 全部合法 Target 真实 ACK → 既有成功清理。目录逐文件检查，恢复公开 bool 仍是 worker 预留，拒绝不等于自动删除原恢复记录。

### 已执行检查与错误处理

| 实际命令/范围 | 结果 |
|---|---|
| 新测试 `[127.0.0.1-file]`，实现前 | 1 failed，4.87 秒，退出 1；实际转发自身并返回 True，期望 False，精确复现缺失的拒绝 |
| `test_forward_self_target_is_rejected_but_other_ports_still_receive` | **6 passed，18.85 秒，退出 0** |
| 审查 P2 未绑定/端口冲突回退的真实复现，修复前 | 2 failed，1.89 秒；合法 127.0.0.2:same-port 被配置主机误判自身，均返回 False |
| 上述两参数 + 原六参数，最终实现 | **8 passed，29.70 秒，退出 0** |
| V5 四份集成文件 `-k 'forward or relay or self_target'` | **66 passed、83 deselected，299.37 秒，退出 0** |
| resume 集成 `-k 'forward or eof_confirmation'` | **32 passed、42 deselected，147.14 秒，退出 0** |
| `uv tool run ty check PyFlow/network_api/connect_tcp.py --output-format concise` | 退出 1，37 条诊断，均在本次生产修改范围外；不声称通过 |
| 六份相关文档的相对文件链接 | 9 个链接、0 缺失，仅机械检查 |
| 最终完整 `.venv/bin/python -m pytest -q --tb=short`，仅一次 | **994 passed、1 skipped，1209.22 秒，退出 0**；运行版本包含最后的配置别名收窄与八项新增回归 |

```bash
.venv/bin/python -m pytest test/integration/network_api/test_tcp_file_transfer.py test/integration/test_forward_extension.py test/integration/test_forward_msg_attribution.py test/integration/network_api/test_tcp_transfer_control.py -k 'forward or relay or self_target' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k 'forward or eof_confirmation' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py::test_forward_different_ip_same_port_survives_a_different_configured_host test/integration/network_api/test_tcp_transfer_resume.py::test_forward_self_target_is_rejected_but_other_ports_still_receive -q --tb=short
```

V5/resume 批次与三组独立程序先于配置别名的最后收窄；该收窄由上述 8 项最终回归及最后完整 pytest 验证，不冒充之后重跑过独立程序。完整 pytest 仅运行一次，994 passed、1 skipped，无失败；不以 C4 历史结果替代。最终类型检查仍有 37 条改动范围外诊断，不能宣称所有检查全绿。完整套件通过不替代 C6 的全路径总验收，C6 未自动推进。

### S6：独立真实运行证据

一次性 `/tmp/pyflow-transfer-lifecycle-smoke.py --case self-target` 不调用 pytest 测试函数，不 mock 文件、网络或加密。仅透传记录日志、原发送结果与 socket owner。每组验证 console 文件、含非空/空文件的嵌套目录、真实 Flask `/api/send_file` 和 `/api/send_folder` 到原 TCP 路径，以及保留行恢复；Web HTTP 200/ok 仍表示异步受理，**不是成功送达或 UI 拒绝状态**，本次不改变 HTTP/UI 契约。

| 实际参数 | Source 控制端点 | 合法目标端点 | workers / 最终存活 |
|---|---|---|---|
| 明文 thread、localhost、Server wildcard 双 IP 监听 | 127.0.0.1:40233 | 127.0.0.1:44151；127.0.0.2:40233 | 68 / 0 |
| 加密 thread | 127.0.0.1:43811 | 127.0.0.1:38663 | 80 / 0 |
| 加密 async、wildcard、tracking off | 127.0.0.1:35589 | 127.0.0.1:41345 | 81 / 0 |

三组以 `&&` 执行，批命令退出 0，42.18 秒。每组 5 个自身文件条目结果均 False，且不分配新 fid、无上传行/Server route/握手/数据连接或回环文件；显式恢复 fid=591 结果 False，原五项上传行对象及快照仍在。mixed 和 Web 合法投递各组共 5 个真实 Source ACK=True；仅发送 5 条合法 `/forward_item`，其中不再带自身，跟踪开启时新上传 peer 只指合法目标。未删除手动保留的旧行，完成后 Server route/index 与合法 Target 接收进度均清空。

非空文件 65806 字节，目标及 Source 原文件 byte-for-byte 一致，SHA-256 `d844b5d8544c48d85fb6b05a2bbf1a9538c88deb1b96b913024edce44acf0296`；目录中的零长文件也取得真实 ACK。每组五条上游数据 socket 都记录实际控制 owner；加密组 channel.secure=True，明文组无加密 channel。Source 实际控制 socket 全程保持。观察到拒绝日志 `forward: refusing own control endpoint ('127.0.0.1', 40233), skipped`，别名与 wildcard 亦有对应拒绝。

```bash
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case self-target --alias --same-port-target
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case self-target --encrypted
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case self-target --encrypted --async-server --wildcard --untracked
```

首次双 IP 程序为 127.0.0.2 Target 仍配置 127.0.0.1 Server 接口，既有 Client 数据 listener 使用其配置 host，故无法在 .2 接收，目标内容断言失败。程序改为 Server wildcard 监听、该 Target 通过 .2 接口连接并监听，生产收发未改；最终上述双 IP/同端口真实内容与 ACK 通过，不把地址不通的首次结果冒充自拒绝缺陷。

一次性程序已删除，真实传输文件、上传暂存及隔离日志/进度/密钥目录均由 TemporaryDirectory 清理；未遗留永久验收脚本。

### 路径对照与边界

| C5 路径 | 实际证据 / 未覆盖 |
|---|---|
| 自身 same-IP/port、mixed、合法 same-IP/different-port | 六参数 console 回归与三组独立真实程序，合法目标内容/真实 ACK |
| 不同 IP/同端口 | 明文真实双接口程序，127.0.0.2:40233 完整收到且 Source ACK=True；不是只按端口拒绝 |
| console file/folder/嵌套/空文件 | 六参数回归及每组独立程序的非空/空条目，逐条真实结果 |
| Web 文件/目录 | 真实 Flask multipart 请求沿原 adapter 到 TCP，self False、合法 True；未更改或验证浏览器拒绝提示 |
| 恢复旧行、tracking off | self 恢复 worker 退出、旧行/快照保留、无新请求；async 程序在 tracking off 下完成合法投递 |
| 别名/wildcard、实际 socket | localhost、0.0.0.0、动态端口已验证；最终两参数额外覆盖无 bind 和真实 EADDRINUSE 取消 bind 后仍允许不同 IP/同端口；EACCES、重连中的地址变化未单独验证 |
| C1–C4、消息与磁盘扩展 | 既有受影响回归结果单列，不将 C5 扩为 Server 鉴权或磁盘扩展自拒绝；旧会话及中途 rekey 不扩大声明 |

只保证原生 Client 文件/目录入口，不阻止手工构造 wire 绕过 Client，不是新增 Server 鉴权策略。NAT/身份迁移、native Windows、其他 Python 版本及全部参数笛卡尔积未覆盖。问题二严格代次隔离继续暂缓，C6 未实施。规则沉淀为上述真实消费者回归；Python LSP capabilities 仍仅 Ruff，不支持 references/definition，未虚称符号引用分析完成。

### Standards

发现 0 项。未发现削弱既有测试、重复实现、安全 I/O 旁路、非 owning 清理或未经批准扩围；Fowler 独立启发式 0 项。生产仅统一入口，未改 Server、Web UI 或 Source 文件发送算法。

### Spec

发现 1 个 P2，已真实复现并修复：`client_host=127.0.0.2` 但未绑定或配置端口冲突后，实际 Source 为 `127.0.0.1:q`，另一 Client `127.0.0.2:q` 是合法目标；最初无条件配置别名会误拒。修正为配置候选仅在解析为实际 IP 时才认作自身（wildcard 除外）；两条真实路径修复前失败，最终八参数通过。无生产越界；无未修复的实现发现。审查未运行检查，运行结果均由父代理记录。


## C4：复用既有 route 的显式 EOF 补最终确认

**提交意图**：全部 accepted Target 已真实 ACK、上游最终 ACK 明确 False/异常后，由 Source 显式补最终确认，不重新交付文件。用户 `/skill:implement` 已授权本单一原子变更、验证、审查及提交当前分支；不推进 C5/C6，不合并或推送。此前任务迁移、独立终态表及 C4a/C4b 拆分均撤回。

### 更改路径、解决的问题与数据流

- `PyFlow/network_api/connect_tcp.py`：`_forward_item_handler` 在在线目标过滤前识别既有完整 completed route，校验当前实际 Source socket、originator 和单个原目标；`_forward_relay` 复核 route 对象并复用 receive reservation，原始/独立恢复 owner 或 finally 仍在时拒绝，终态仅建上游数据腿；`_forward_pump` 完整读取既有头、严格要求 offset=size，零正文后按完整完成集合发原最终 ACK，写成功才调用 `_drop_fid_route`。
- `test/unit/network_api/test_fid_routes.py`：旧 schema 完整完成快照重载、无反向索引/字节进度行；实际控制分派拒绝错误 Source、旧 socket、错误成员、多目标及部分完成伪终态。
- `test/integration/network_api/test_tcp_transfer_resume.py`：扩展原上游 ACK 失败场景到 file/folder/empty、False/异常、目标离线、再次失败与显式成功；加入非法头/非 EOF/finally busy；扩展 C3 独立恢复最后一流 ACK 失败后的补确认，保留兄弟内容、独立 offset、真实 ACK 与 owning 清理断言。
- 四份使用/变更文档、需求卡、计划、本报告同步；`GLOSSARY.md` 为用户规划阶段已澄清的领域术语。Source `file_transfer_mode/_send_file`、公开签名、wire、五项行、scoped key、relay schema、依赖、CI、coverage 均不变。

数据流：`Source.resume_trans(原 Target, fid, total)` 预留 worker → 原 `/forward_item ... fid=... resume=...` → 实际会话/成员/完整完成集合/owner 准入 → 原 port/stamp/Source→Server 数据连接 → 完整 name-length/name/size 且 offset=size → 原最终 ACK → Server 按 route 身份删除，Source 实际消费 ACK 后清共享上传行。任一失败只收尾本次资源并保留原依据；bool=True 不是完成证据。

### 本轮实际检查（不借 C1–C3 历史通过数）

| 实际命令/范围 | 结果 |
|---|---|
| `test_fid_routes.py` + 上游 ACK 失败及非法头/finally 两个集成函数 | 52 passed，32.23 秒，退出 0 |
| `test_forward_recovery_waits_for_old_finally_and_runs_targets_independently` 四参数 | 4 passed，23.51 秒，退出 0 |
| `uv tool run ty check PyFlow/network_api/connect_tcp.py --output-format concise` | 退出 1，最终 37 条诊断，全部位于本次生产修改范围外；不声明类型检查通过 |
| 完整 `.venv/bin/python -m pytest -q --tb=short`，仅一次 | 984 passed、1 failed、1 skipped，1369.13 秒，退出 1；唯一失败为旧自动背压消息契约 |
| 新 scoped-owner 真实复现 + 获批背压迁移（修复前） | 1 failed、1 passed，11.55 秒；前者实际 `/forward_error ... 'relay source is already active'`、Source False，证实审查 P2 |
| 最后窄组合：fid_routes、上游 ACK/非法头/finally、独立恢复、scoped owner、背压 | **58 passed，72.94 秒，退出 0**；包含最后生产修正与迁移测试 |

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_ack_write_failure_retains_source_and_completed_route test/integration/network_api/test_tcp_transfer_resume.py::test_eof_confirmation_ignores_old_recovery_owner_for_another_target test/integration/network_api/test_tcp_transfer_resume.py::test_eof_confirmation_rejects_bad_headers_and_waits_for_finally test/integration/network_api/test_tcp_transfer_resume.py::test_forward_recovery_waits_for_old_finally_and_runs_targets_independently test/integration/network_api/test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader -q --tb=short
```

完整 pytest 的唯一失败 `test_tcp_file_transfer.py::test_forward_flow_control_pauses_uploader` 仍要求自动 `/pause_trans`、`/start_trans`，与已批准 C2 的本地 reader 高低水位门槛相反。用户已明确选择 `migrate_contract`，扩大计划仅迁移该函数：Event 固定慢 writer，证明 reader 真正等待、峰值不超过 high_water+单次扇出、排空解除、真实 Source ACK=True 及完整内容；不改生产背压。修正后按批准仅运行上述 58 项窄组合，**没有重跑完整套件，不能写最终全套全绿**。V4 启动作业在宿主重置后无可取回结果，不计通过数；完整 pytest 也不等于 C6 路径总验收。

最初 EOF 专项的五个拒绝案例因 Server 夹具未标 `running=True` 而不分派；非法头案例误用 `_forward_read_sign(error_sign)`，该 helper 对错误标记本就返回 None。修正夹具并实读精确 `/FER\n` 后，6 passed；随后上述 52 passed。未删/skip 用例、弱化内容/ACK/所有权断言或修改阈值。类型检查的新增 `None.decode` 诊断通过局部 ACK 变量显式收窄消除，不压制诊断或修改配置。

### S5：独立真实程序核心六组与边界补充

一次性 `/tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm` 不调用 pytest 函数、不 mock 文件/socket/加密主路径。仅在最终 ACK 发送点注入 False/异常，其他钩子透传观测；无关普通 fid 在自己的最终 ACK Event 内保持活跃。每组先有两个真实 Target ACK，再失败一次补确认，最后显式成功；重载组使用原进度快照和同一 Source 实际控制端点，Target 不必回连。

| 实际参数 | upstream fid | 原 child fid（A / B） | workers / 最终存活 |
|---|---:|---|---|
| 明文 thread、localhost、folder、一个 Target 离线 | 7921691084 | 8074621898 / 12369589194 | 37 / 0 |
| 加密 thread、ACK 异常、Server/Source 快照重载、全部 Target 离线 | 11969814140 | 20188156416 / 24483123712 | 91 / 0 |
| 加密 async、wildcard、tracking off 既有行、空文件 | 8971573408 | 19441350896 / 23736318192 | 77 / 0 |
| 加密 thread、补确认头中断 data socket | 11110766478 | 18854031844 / 23148999140 | 74 / 0 |
| 加密 async、补确认头中断 control socket、原端点重载 | 8664377964 | 19983341822 / 24278309118 | 94 / 0 |
| 加密 async、补确认头中 stop、原端点重载 | 11276679026 | 19617231540 / 23912198836 | 96 / 0 |
| 加密 thread、一个有效确认等头时另一个同 fid 有效请求被拒，再中断/显式完成 | 9056271540 | 19920162170 / 24215129466 | 76 / 0 |
| 加密 thread、原端点重载后 ECDH 公钥发布延迟、前置确认 ACK False 后继续显式完成 | 10989437228 | 20366252022 / 24661219318 | 95 / 0 |

六组各退出 0；前三组批命令 34.42 秒，后三组 39.23 秒。上游 ACK 尝试均为 False→False→True，两个显式 Source 结果 False→True。两次确认逐字节仅发送原三段文件头，正文为 0；Target 命令/数据建连仍只有最初两份，原 child fid 不变，目标 inode/mtime/完整内容不变。非空内容为 131335 字节，SHA-256 `8dd07ed5180fd145d6805a4c2e20aa622b00a18770417c0c5372cc31c6f7fff9`；空文件 SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。两个无关普通传输均在补确认后才放行，实际 ACK=True、内容一致。

每组记录两条不同数据 socket 与同一实际控制 owner；加密组真实 channel.secure=True，明文组无加密 channel。失败快照只有原 `upload/download/relays` 类别和 `originator/targets/completed` 字段；最终 route/index/recovery/所属行及快照清空。未抓包，不扩大为全部加密连接安全审计。首次明文程序在已完成传输后因仅观测 `_crypto_attach_transfer_out`（明文不会调用）失败；改在通用 socket 登记边界透传观测后通过，未修改生产实现。

另两组边界程序分别验证同 fid 有效确认竞争（表第七行）与密钥窗口（第八行，`PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-c4-ready-window.py`，17.35 秒，退出 0）。密钥窗口用 Event 延迟真实 ECDH 公钥发布；此时 RSA 控制通道已安全发送请求，实际 request_secure=[True]，上游 stamp 为加密，Source owner 仍在、未收到 ACK；放行密钥后真实数据通道完成，额外注入 ACK False 仍保留原行，再走显式 False→True。该窗口不要求错误地等待 `_wait_control_secure` 调用次数。该程序曾在旧实例停机顺序、重复加载返回计数及错误的 RSA/ECDH 阶段假设处失败；改为显式关闭待重载实例、核验真实行及实际加密通道后通过，没有改生产握手、放行明文或压制异常。密钥未就绪窗口为 thread 组，未扩大为 async/rekey 全矩阵。

两个一次性程序均已删除；所有传输文件、隔离进度与密钥目录由 TemporaryDirectory 清理。上述独立程序先于最后的 scoped-owner 范围修正；最终修正以 58 项窄组合中的真实新旧 route/finally 回归为证据，不冒充之后重跑过全部独立程序。

```bash
export PYTHONPATH="$PWD"
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --alias --kind folder --offline one
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --encrypted --ack-failure exception --reload
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --encrypted --async-server --wildcard --untracked --empty
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --encrypted --fault-path data
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --encrypted --async-server --fault-path control
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case eof-confirm --encrypted --async-server --fault-path stop
```

### C4 路径逐项对照及风险

| 路径 | 实际覆盖 / 明确限制 |
|---|---|
| C4-P01 原始全部 ACK 后上游失败 | 专项 file/folder/empty × False/异常及六组双 Target 真实内容/ACK/保留证据 |
| C4-P02 最后一条 C3 恢复流失败 | 四参数独立恢复，late-target 两参数注入最后 ACK False，再显式 EOF；已完成兄弟不重放 |
| C4-P03 零正文/无 Target 重开 | 专项三种条目及六组 header/内容/hash/Target 命令/实际数据连接观测 |
| C4-P04 再失败、显式重做 | False 与异常两种专项、六组 False→False→True；每次失败保留原对象/快照 |
| C4-P05 一个/全部 Target 离线 | 明文一目标离线，其余组全部离线；部分完成不能借 EOF 成功另有拒绝案例 |
| C4-P06 旧规范快照重载 | unit 完整完成 route、Source 行重载；加密 thread 同实际端点重建 Server/Source 后显式成功 |
| C4-P07 非法请求/头/非 EOF | 实际控制五种身份/成员拒绝，offset 小/大于 size、截断头；中断程序只发送两字节头，失败保留 |
| C4-P08 独占和 finally | 同任务 finalizing owner 与有效确认竞争均拒绝；新增同值 fid、不同 Source/Target 的真实新旧 route/finally 交叉，新任务在旧 finally 未放行时补确认成功、旧 record 身份保持；未专门强制同 key 对象被替换后的 C4 清理交叉 |
| C4-P09 中断/stop/安全入口 | 三组真实 data/control/stop 中断后清本次资源并显式完成；另有 RSA 控制已安全/ECDH 尚未发布的 thread 窗口，加密 stamp 与 ACK 失败保留；不声明 async/rekey 完整矩阵 |
| C4-P10 tracking off/别名/wildcard/无关 fid | 两组无关普通 fid 保持活跃并最终真实 ACK；tracking off 只复用既有行，别名及 wildcard 分组运行，不声称全部组合 |

高风险审查关注实际会话、已完成集合、finally barrier 和对象身份清理。未运行 native Windows、其他 Python 版本、完整参数笛卡尔积、NAT/端点迁移或断电耐久性。失败且未再操作的原 route 可长期保留，本次不加 TTL；ACK 本地写成功但 Source 未收到的歧义不解决；本次 EOF 头不验证历史 name/size/hash，源文件须保持可用且未变。问题二严格代次隔离继续暂缓，C5/C6 未实施。

文档机械检查：上述八份需求/计划/报告/术语及使用文档，9 个相对文件链接，缺失目标 0；仅检查链接，不代替功能验收。

规则沉淀为真实消费者契约：EOF 不等于交付完成、完整 Target 完成集合与严格 EOF 缺一不可、busy/finally 不抢占、ACK 失败保留且显式成功 owning 清理。工具限制：Python LSP 仍仅 Ruff，definition 请求返回 Unknown request，未声称已完成符号引用分析。

### Standards

发现 1 个 P2，已复现并修复：旧 route 在真实 ACK 成功后已删除，但独立恢复的 `(旧 Target, fid)` 接收 owner 尚在 finally；另一 Source/Target 此时合法复用同值 fid，其 C4 被全表 fid 扫描误拒。`confirmation_busy` 现只将当前 route Target 集合内的 tuple key 计为冲突，整型任务 key 仍排他。回归通过真实两条 route 和 Event 固定旧 finally，修复前 Source False、修复后 True；最终 58 项组合通过。未发现删减/跳过测试、安全 I/O 旁路、非 owning 清理、未经批准扩围等硬规则违规；Fowler 独立启发式 0 项。

### Spec

实现缺陷 0 项、生产越界 0 项。审查时提示的多 Target、全部离线、两端快照重载、再次失败、有效确认竞争、无关 fid 及密钥窗口证据，已由上表和逐路径记录分别补齐或注明实际运行范围；未把全部参数笛卡尔积、未知 fid 的专用 S5 场景、native Windows 或所有 rekey 交叉冒充已验证。用户批准的单函数测试迁移不恢复旧自动控制协议。

本次保留一个 C4 原子提交，不拆 C4a/C4b。生产与测试之外同步了较长的需求/计划/报告及先前术语澄清，整体审查规模超过最初 300–500 行预估；不将文档拆名冒充多个已审的小提交。提交仅包括本代理与子代理的选定行，分支提交不等于人工合并批准，不推送。


## 本轮 C3 替代方案：已实现，局部运行证据如下

**当前状态：用户批准的数值请求、原 owner 收尾、独立并行恢复已实现并取得局部运行证据。** C1/C2 保留。最终四份 unit 文件 228 passed；转发集成整批 52 passed、3 个旧内部断言失败，迁移后专项 3 passed；新全局控制专项 3 passed，独立程序 3 组真实传输通过。上述是分次运行，不伪称最终单次整批全绿，不声明全部矩阵或 C6 总验收通过。旧 C3a/C3b 查询/强制 teardown/串行轮次仅为撤回历史；C4–C6 未实现。

### 本轮契约与更改路径

- `PyFlow/network_api/connect_tcp.py`：仅数值 `/resume_trans`，已删除 `?`/`None` 查询及其消费者。Receiver 旧接收 owner 和文件关闭后读取实际落盘 offset；Server 校验实际 Target socket、child/origin/target/route，原值登记，不查询兄弟目标、不用发送计数代替。
- Server 等同一原 route 全部旧 listener、握手、上/下游、reader/writer 和 finalizing owner 自然收尾，包含 finally；不因请求强制 teardown 或自动 start。等待不持全局锁、不阻塞控制 reader/event loop，不影响其他 route/fid。
- 此后按每 Target 独立 Source→Server→Target 流、实际 offset、运行时进度、资源和失败处理并行恢复；稳定 upstream/child fid、原完整路径、五项进度行及 relay schema 不变。同实际 socket+fid 仅端口握手短串行，绝不锁正文或最终 ACK；晚来 Target 不等已有大文件正文/ACK。
- C1 实际会话 owner 和 C2 Source 全局 pause/start 仲裁及本地背压保留；共享控制状态有多个 live owners，最后一个退出才释放。中间 Target 的真实 ACK 仅更新自身 completed，不伪造整个 route ACK；累计全部 accepted Target 真实 ACK 才能整任务成功，Source 整任务上传行不得被单流提前删除。
- Source 离线保留 pending。Target 断线丢弃旧实际 socket 的 pending，不借给替换会话；重连等旧接收 owner 收尾后读新实际 offset 重发。单 Target 失败不取消兄弟、不自动重试。
- 本轮运行的回归路径：`test/unit/network_api/test_resume_matching.py`、`test_fid_routes.py`、`test_transfer_control.py`、`test_transfer_handshakes.py`，以及 `test/integration/network_api/test_tcp_transfer_resume.py`、`test_tcp_transfer_control.py`；具体选择器与分次结果如下。
- 文档范围仅七份既有文件：`docs/plans/transfer-control-lifecycle-requirements.md`、`transfer-control-lifecycle-plan.md`、本报告；`docs/source/File_Transfer/File_Transfer.rst`、`docs/source/Network_APIs/TCP_Client_APIs.rst`、`TCP_Server_APIs.rst`、`docs/source/changes/transfer-fid-routes.md`。未创建文档。
- 最终 owner 边界：发送恢复指令返回 False/异常时，若 Source 已经消费指令并使 relay 绑定 receive record，调度 worker 仍等该 owner 的 finally，不提前释放占用；迟到握手不能采用已被释放的 reservation。Target 重连的新实际 socket 可以保存新数值 offset 等旧 Target owner 退出，同一会话的 active 重复请求仍拒绝；退出后只消费已收到的 pending，不生成重试。
- 显式 Source 发起的恢复也登记独立 active Target owner，失败后可再次合法发起；恢复 Target 握手使用准入 reservation 的实际控制 socket，不按 peer 借替换连接。

### 本轮路径证据与未覆盖边界

| 路径 | 必须取得的证据 | 本轮状态 |
|---|---|---|
| Receiver 数值 offset、零/空文件/Target EOF | 实际 offset、原路径、内容及真实 Target ACK | 数值解析、零 offset、不同落盘 offset、file/folder 的 Target ACK-loss EOF 零正文恢复已验证；全 Target 完成后的上游 EOF 再确认 C4 未实现 |
| 原 route live/paused/finally | 不拆活跃腿，原 owner 完整收尾前不建恢复流 | 四组独立正文 + 三组全局控制事件已验证 live/paused/finally；其他握手/EOF 窗口不扩大声明 |
| 不同 offset、部分完成及晚到 Target | A 正文或 ACK 被阻塞时 B 可建流并取得真实 ACK | 四参数真实集成与三组独立程序覆盖，A 再失败不取消 B |
| 多 owner 全局 pause/start/背压 | 所有 Source 流受统一仲裁，单 owner 退出不清共享状态 | 三参数全局控制通过；本轮未单独扩大到全部流量压力矩阵 |
| Source 离线、Target 会话更新及单流失败 | pending、旧 owner、新数值请求、兄弟继续，无自动重试 | Source 离线/原端点回连、stop、旧会话交叉、A 再失败/B 完成已通过；新 Target 会话 pending 另有单测，未扩大到多进程重启完整矩阵 |
| ACK 聚合及资源身份 | 单 Target 不伪造整任务成功，旧 finally 不删新 owner | 四参数正文、三参数全局控制与独立程序均保留真实 ACK/资源断言 |
| 明文/加密、thread/async、tracking off/持久化 | 真实 I/O、稳定 fid/path/schema；既有行恢复 | 三组独立程序、三组全局控制以及 route 持久化单测；不宣称所有参数笛卡尔积 |

实现后运行的命令与结果见下节。以上结论来自本轮记录，不借旧 C3a/C3b 或 C1/C2 历史通过数。

### 本轮 S4 独立程序证据

一次性程序从真实保留进度行和已落盘的部分文件发起恢复，不调用 pytest 测试函数。Source、Server、两个 Target 均为真实 TCP 实例；只用事件固定 A 的正文或 ACK 门槛，文件、socket、加密和实际 ACK 均走生产实现。B 在 A 尚未放行时才提交请求，先完成文件与真实 Target ACK。该程序验证独立恢复流；原活跃 route/finally barrier 由本轮集成契约验证，不以预置的保留行冒充断线运行证据。

| 实际参数 | A 被阻塞的位置 | upstream fid | 稳定 child fid（A / B） | workers / 最终存活 |
|---|---|---:|---|---|
| 明文 thread、localhost 别名 | 正文首次读取前 | 5942851624 | 4697782172 / 8992749468 | 33 / 0 |
| 加密 thread | Target 最终 ACK 写入前 | 12672816124 | 19156548134 / 23451515430 | 72 / 0 |
| 加密 async、wildcard、tracking off 既有保留行 | 正文首次读取前 | 10342202172 | 20511536392 / 24806503688 | 72 / 0 |

三组实际 offset 都是 A=8192、B=98304；目标沿用各自 `original/nested/payload.bin`。每份完整文件为 262307 字节，SHA-256 均为 `8fb29a369e87c3654ab4e692caabad6a31ac611dda80b64ee1e04819e022812b`。B 实际 ACK 后 A owner/socket 仍活跃、Source 原任务行对象仍保留；Source 两条恢复流使用不同 socket，运行时进度不覆写任务行。最终 Target ACK 次序 B→A，Source 结果 False→True：中间流不伪造整任务成功，全部真实 ACK 后清理 route/index/recovery/进度。实际控制 socket 未替换，所有 worker 收尾。

```bash
export PYTHONPATH="$PWD:$PWD/test/integration"
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate body --alias
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate ack --encrypted
.venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case parallel-recovery --gate body --encrypted --async-server --wildcard --untracked
```

上述三条以 `&&` 顺序执行，整体退出 0，31.69 秒。临时实例、文件和密钥目录由 TemporaryDirectory 清理；一次性程序已删除。不抓包，不扩称所有连接的安全属性已审计。局部 pytest 结果另列，不借历史通过数。此程序先于最后的异常 owning 清理/新 Target 会话 pending 修正运行；最终修正由后续窄回归验证，不把程序结果冒充该错误交叉的运行证据。

### 本轮局部回归记录

- 最终 owner / 手动零 offset / 独立正文 / 全局控制的组合命令实际结果：**45 passed、2 failed，78.37 秒，退出 1**。`test_fid_routes.py` 的请求发送失败后 owner 保留、同/新 Target 会话 pending，以及独立正文的四参数组合、零 offset 路径通过；失败均在新全局控制场景。
- 明文场景在关闭旧 Source data socket 后才等待其 start 通知，关闭线程可先释放旧控制 owner、清掉发送队列。修正为真实 start 通知确认后再 shutdown，再放正文门；不弱化通知断言。
- async/untracked 场景在接收 worker 已选定 `progress=None` 后才塞进字典的行，不是该 worker 持有的旧行；异常路径会按未跟踪传输删除部分文件，Server 注入的 offset 与 Receiver 行不一致。修正场景初始化为接收注册时就保留真实五字段行，关闭后同时核对实际部分内容和行 offset=1024；继续验证 tracking off 恢复既有行，不把无保留依据的任务冒充可恢复。
- 上述场景修正后，仅运行该函数的三个参数：**3 passed，30.20 秒，退出 0**。保留真实 pause condition wait、不同 socket、各自正文、B 先 ACK、最后 owner 释放、最终内容和资源清理断言；未删/skip 用例或降低阈值。组合失败记录保留，不伪称组合整批通过。
- 文档机械检查：七份现有文档、六个相对文件链接、零缺失目标；该检查不替代运行验证。
- 最终四份 unit 文件命令：**228 passed，0.78 秒，退出 0**。随后的转发/relay/并行选择器整批：**52 passed、3 failed、100 deselected，337.40 秒，退出 1**。失败全部是 `test_disconnected_relay_target_cannot_queue_behind_live_source` 的三参数场景：错误要求原始活跃 Source 的 `_resume_sends` 为空，但该表也持有原始发送 owner。
- 移除该实现细节断言，改为核验原 Source、Server upstream 和存活 Target 的实际 pause 状态仍为 True；保留错误/断线请求拒绝、无 Server recovery、原 socket/owner 不变、原 worker 仍运行、start 后存活 Target 完整内容及最终资源清理。只运行修正场景：**3 passed，19.54 秒，退出 0**。未再次运行整批，故不写“最终整批全绿”。
- 最早启动的六文件完整回归在运行时重置后无法取回 job/输出，不作为任何通过数来源。一次专项通过后的输出展示曾误用 Python `await sh(...)`，宿主报 TypeError；随后同步保留输出如上，不把宿主错误冒充产品失败或虚构统计。

```bash
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_recovery_waits_for_old_finally_and_runs_targets_independently test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_with_zero_offset_reuses_existing_route test/integration/network_api/test_tcp_transfer_control.py::test_parallel_recoveries_wait_for_old_owner_and_share_source_pause -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py::test_parallel_recoveries_wait_for_old_owner_and_share_source_pause -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py test/unit/network_api/test_transfer_control.py test/unit/network_api/test_transfer_handshakes.py -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py test/integration/network_api/test_tcp_transfer_control.py -k 'forward or relay or parallel_recoveries' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py::test_disconnected_relay_target_cannot_queue_behind_live_source -q --tb=short
```

### 当前风险与未实现边界

- 高风险路径是原 route 最后一个 owner 的 finally barrier、多 Target 同 upstream fid 的端口结果关联、独立进度与 owning 清理、共享全局控制最后释放及累计真实 ACK。必须以事件固定正文/ACK 阻塞交叉，不能只看最终文件存在。
- 原 route 暂停、旧 ACK 等待或 finally 未结束会延迟首次恢复；这是批准的自然收尾边界，不用超时抢占/强制拆腿掩盖。该 barrier 不得再次挡住后续独立恢复流或晚到 Target。
- 同控制会话、同 fid 换腿时迟到 pause/start 的严格代次隔离仍暂缓；不新增 leg_id、换 fid、wire 字段、schema、重试、CI 或依赖。
- ACK 本地写成功与对端应用已收到之间的不确定窗口不以新结果通道或 ACK-of-ACK 解决。C4 全 Target 完成后的上游 EOF 再确认、C5 自目标拒绝、C6 总体验收未实现。
- 本轮运行平台为 Linux、Python 3.10；native Windows、其他 Python 版本、多进程重启的完整矩阵及转发中途 rekey 未覆盖。具体明文/加密/thread/async 参数见本轮输出，历史平台结果不充数。

---

以下 C1/C2 及旧 C3a/C3b 保留其交付时状态与真实历史输出；旧 C3 的现行授权及验收状态以上节为准。

## C1：实际控制会话取消与旧 owner 收尾

状态：**C1 实现及局部验收完成；用户已要求开始 C2，C1 获阶段推进**。下述记录是 C1 的局部证据；当前 C2 状态见后续章节，未创建 git 提交、合并或推送。依据：[需求卡](transfer-control-lifecycle-requirements.md)、[计划 C1](transfer-control-lifecycle-plan.md#8-原子拆分固定次序逐项人工审查)。

### 意图、更改路径与解决的问题

- `PyFlow/network_api/connect_tcp.py`：删除 Server/Client 两份 `_drop_stale_transfers`。重连不再按 peer 扫描进度表并关闭扫描时的当前 socket，避免迟到清扫误伤新会话。
- `_retire_transfer_session` 统一取消实际控制 socket 的握手、listener/data socket、控制状态及未绑定接收请求；资源登记保留至 worker 真正收尾，`wait_socket` 等待所属资源释放。关闭 socket 在 registry 锁外执行。
- `_sending_transfer`、`_start_resume_send` 保存准入时控制 socket；`_wait_transfer_session` 仅等待捕获的原 send state/receive record。旧 finally 继续按对象身份释放，不删除替换后的 owner。
- Server 在会话退役时保存旧 owner 快照，后台排空后删除快照；重连 worker 固定新准入 socket，旧 worker 不再按 peer 取得后来替换的连接。`stop` 在 client 锁外取消和关闭连接。
- Client 先退役原 socket，再启动新控制 reader；后台恢复等待原 owner，不再在 reader 启动前等待。连续重连的恢复 worker 按原线程链排空。转发恢复显式传入捕获的 `control_socket` 到 `_forward_one`，避免旧任务借新 socket 发握手并形成等待环。
- `test/unit/network_api/test_transfer_handshakes.py`、`test_resume_matching.py`：保留实际会话取消、同 fid 占用和旧 owner 等待契约，增加旧清理后新 socket 仍可收发及迟到释放不删新 owner 的断言。
- `test/integration/network_api/test_tcp_transfer_resume.py`：将原 sweep 用例迁移为实际控制会话退役；相同 fid 的不同会话保持独立；暂停腿退役后用原 fid 真实恢复，比较内容并确认发送端成功清理。
- `test/integration/network_api/test_tcp_transfer_control.py`：事件固定“旧转发恢复 worker 已准入但尚未执行 → 新 connect → 放行旧 worker”，覆盖明文别名、加密 thread、加密 async/wildcard/tracking off。断言旧任务仅使用原 socket、失败保留原上传行、新会话可继续收消息。

数据流：实际控制会话退役 → registry 标记关闭并取消所属资源/握手 → 清除所属运行时控制状态 → 捕获旧 owner → 后台等原资源及 owner 收尾 → 经固定的新会话发恢复请求 → 原 fid/路径恢复 → 真实最终 ACK → owning 清理。

### 局部验证

V1 最终结果：**112 passed、97 deselected，240.29 秒，退出码 0**（Linux、Python 3.10.20）。筛选范围与计划相同；97 项为既定 `-k` 未选中，不是新增 skip，也不代表全套通过。

失败记录：首轮输出包含 2 个 fixture 错误，300 秒超时，不能记为通过。窄定位运行得到 17 passed、1 error：新增加密重连用例未等待新握手完成便结束，teardown 发现 `_crypto_exchange_thread`、`_crypto_wait_server_ready` 尚未退出。为新控制连接双方补上真实 `_wait_control_secure` 就绪前提，保留原零存活 worker 断言；随后执行下面完整 V1 选择并通过，未修改生产加密流程或延长 fixture join 来掩盖错误。

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_handshakes.py test/unit/network_api/test_TCP_client.py test/unit/network_api/test_TCP_server.py test/unit/network_api/test_resume_matching.py test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_transfer_resume.py -k 'disconnect or reconnect or session or owner or stale or cleanup or resume' -x -vv --tb=short
```

独立程序不调用 pytest 测试函数，使用真实控制/data socket、加密、文件 I/O 和最终 ACK；Event 固定交叉时序，方法包装仅观察真实执行或设置旧 worker 放行点。临时脚本 `/tmp/pyflow-transfer-lifecycle-smoke.py` 已在验证后删除，运行时临时文件与密钥目录也已清理。

```bash
# 每项分别运行 --alias、--encrypted、--encrypted --async-server --untracked
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case direct-leg <参数>
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case relay-leg <参数>
```

- S1 会话部分：3 组参数各完成上传、下载，共 6 次同 fid 恢复。fid 为 810/811；实际原文件 offset=0，原完整路径保持，旧 control/data socket 均已关闭，新控制端点保持，发送端实际最终 ACK=True，成功后所属进度行删除。加密 async 使用 wildcard 绑定，并在产生保留行后关闭 tracking 再断线/恢复。
- S1 完整内容 SHA-256：`f2a6736d846749945c57b953b657f9b483c8c4e842b590e00a0cdd0574c20c33`。明文、加密 thread、加密 async 的独立程序退出码均为 0；各自 35、74、73 个观察到的 worker 最终存活数为 0。
- S2 会话部分：3 组参数均证明旧转发恢复 fid=820 的 worker 固定原 socket、返回 False、原上传行保持且不建立伪 route；新控制连接继续完成一次真实转发，最终 ACK=True、内容一致。新转发按原规则分配自己的 fid，不把该新任务报告为 fid=820 的恢复成功。
- S2 内容 SHA-256：`5bc85cfbbe30eb61c8fcbbacdd78975ce49973c0fbf69771c03258ac793c7c03`。3 组程序退出码均为 0；各自 23、62、62 个 worker 最终存活数为 0。
- 独立程序首轮加密 S1 的观察器误将公钥传输（fid=None）的 ACK 计为业务恢复结果，断言失败、退出码 1；按业务 fid 过滤观察后，只重跑受影响的加密 S1 参数组并通过，未以此修改或绕过生产传输/加密路径。

### 范围、不变量与未覆盖项

- C1 对应 P01–P07、P16/P19/P20 的会话生命周期和回归保护。独立程序不穷举全部组合；V1 实际通过了 online resume 的各方向/加密/async/tracking off、文件/文件夹失败后恢复、实际落盘 offset、进度保存/重载、EOF/ACK、稳定转发 route/child、旧 socket 握手隔离及 localhost/wildcard 身份回归。不能把持久化重载用例说成已完成独立多进程重启矩阵，也不将零 offset 独立程序当作所有恢复路径的替代证据。
- 不改变稳定 upstream/child fid、pause/start 四 token、端口响应、`file_transfer_mode` 签名、进度/快照 schema、普通 busy 规则或真实 ACK 判成功规则。未改 CI、依赖、coverage 策略，未删用例、跳过路径或弱化断言。
- 问题二继续暂缓：同一控制 socket、同 fid 换腿时迟到 pause/start 的严格代次隔离未实现；未新增 leg_id、换新 fid、协议字段或自动重试。
- C1 交付时未实施 C2 全局暂停、C3 route 定向重建/offset 查询/逐 Target 顺序恢复、C4 EOF 补确认、C5 自转发拒绝、C6 总体验收，不能据 C1 结果宣称这些行为已完成；C2 的后续结果另列。
- 未运行 native Windows、其他 Python 版本或跨进程并发进度写入验收；本轮未做抓包，不将加密真实传输等同于全协议安全审计。
- C1 会话生命周期契约回归作为规则沉淀；变更记录见 `docs/source/changes/transfer-fid-routes.md`。本轮用户明确要求开始 C2，作为阶段推进授权。

## C2：Source 仲裁全局暂停、writer 排空与独立背压

状态：**C2 实现及局部验收完成，等待人工审查**。V2 本体通过，迁移的单个完成契约用例修正观察时序后通过；S3 独立真实多目标程序通过。C3–C6 未开始，不自动提交、合并或推送。

### 更改路径、问题与数据流

- `PyFlow/network_api/connect_tcp.py`：relay 控制状态注册时绑定运行时 route；实际会话/owner 校验后，Server 将本地或 Target 请求按 upstream fid 转交 Source，而不自行确认。Source 的既有串行 sender 决策入口决定最终状态；Server 再按各 child fid 投影通知，Target 不回送通知，避免回包循环。
- relay 请求不按当前确认值静默去重：另一 Target 的相反请求可能仍在途；保留 confirmed/last_requested，覆盖未确认 pause→start。握手完成前的请求与预置暂停先登记，Source 就绪后再发送；不增加协议字段或持久化状态。
- 删除 relay writer 的用户暂停等待；暂停只让 Source 下一块前等待，Server 已收正文继续排空。删除自动背压伪装成用户 pause/start 的发送，保留原 reader 高/低水位硬门槛与 TCP 流控；低水位不再意外解除 Source/Target 的用户暂停。
- `test/unit/network_api/test_transfer_control.py`：验证 Source 决定前 Server 不确认、相反请求有序裁决、child 映射、通知不回包、旧 owner/错误 socket 拒绝及握手前反向意图。
- `test/integration/network_api/test_tcp_transfer_control.py`：用真实 socket 固定在途块、writer 队列、迟到通知和压力时序；覆盖 Source/Server upstream/Server alias/两个 Target、异端 start、未确认反向请求、错误 peer/type/role、加密帧、目标退出及资源收尾。
- `test/integration/network_api/test_tcp_transfer_resume.py`：原部分完成用例不再用已废弃的独立 Target pause 卡 writer；改为用事件延迟第二 Target 的真实最终 ACK，仍验证完整内容、完成集合、route/index 保留，以及最后真实 ACK 后 Source 成功。未弱化成功条件。
- `docs/source/File_Transfer/File_Transfer.rst`、`Network_APIs/TCP_Server_APIs.rst`、`TCP_Client_APIs.rst`、`changes/transfer-fid-routes.md`：同步全 route 用户控制、重复登记语义、排空行为和背压边界。

数据流：参与者 API/控制帧 → 实际会话及当前 owner → Server upstream 请求队列 → Source 串行决定 → Server child 通知队列 → 各参与者确认；数据流只有 Source 等待用户暂停，Server writers 持续排空，reader 独立受本地压力门槛约束。

### 局部验证与真实运行证据

V2 本体实际结果：**225 passed、1 skipped**（Linux、Python 3.10.20）；skip 是既有明文 fixture 下不适用的 rekey 用例，两个加密 rekey 分支通过，未新增 skip。首次将下述单个迁移用例一并运行，总结果为 225 passed、1 skipped、1 failed，458.43 秒、退出码 1；失败是用例在第二 Target 到 ACK 时过早读第一个 Target 的文件。把完整内容断言移到第一个 Target 的真实 ACK 更新 completed 集合后，原内容/完成集合/最终 ACK 断言全部保留；仅重跑该用例，**1 passed，2.74 秒，退出码 0**。生产代码不因该测试时序问题修改。以下列出实际命令，不将分次验证声称为一次全绿组合运行：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_transfer_control.py test/integration/network_api/test_tcp_transfer_control.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_is_removed_only_after_all_real_targets_complete -x -vv --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_is_removed_only_after_all_real_targets_complete -q
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case global-pause <参数>
```

S3 不调用 pytest 测试函数，不替换文件/网络/加密主路径；事件只固定真实执行顺序，分别运行 `--alias`、`--encrypted`、`--encrypted --async-server --untracked`，退出码均为 0。每组有两个 Target，逐个由 Source、Server upstream、Server child、Target A/B 发 pause，再由另一 Target start；所有活跃逻辑确认最终一致、data socket 身份不变。临时脚本已删除，运行时临时文件与密钥目录已清理。

| 参数 | upstream fid | 两个 child fid | 观察到的 worker / 最终存活 |
|---|---:|---|---|
| 明文、localhost 别名 | 7352661538 | 7098315376 / 11393282672 | 80 / 0 |
| 加密、thread | 9451137674 | 18717822684 / 23012789980 | 119 / 0 |
| 加密、async、wildcard、tracking off | 11093087600 | 21104272900 / 25399240196 | 118 / 0 |

每组在暂停期间均向两个 Target 排空实际在途正文 1024 字节；Source 在下一块等待。压力 high-water=64、实际 peak=2094、单次 max_enqueue=2048，满足 peak≤high-water+max_enqueue；用户 start 后 reader 仍重入压力等待，读次数不变。排空解除压力后，用户暂停仍保持。最终两个文件完整一致、Source 实际最终 ACK=True、所属 route/index 清理。内容 SHA-256：`9a727e7d8b144a2f9ce213746ccc0fbcee65e58cd86c80b43607464789440ee6`。

### 风险、范围与规则沉淀

本阶段覆盖 P08–P11 及 P20 的对应控制矩阵；不改 fid、四 token、端口消息、公开方法签名、进度 schema、普通 busy 或真实 ACK 规则。自动压力不再投影为用户 paused；不宣称阈值约束全部进程 RAM，也不承诺撤回 socket 在途字节。问题二继续暂缓；C3 重建/顺序恢复、C4 EOF 补确认、C5 自转发和 C6 总体验收未实施。native Windows、其他 Python 版本、relay 中途 rekey 的独立场景未验证。测试保留普通控制/rekey、失败/收尾回归；未改 CI、依赖、coverage。全局确认、暂停排空、背压释放不清用户暂停成为永久契约检查。


## 旧 C3a / C3b（已撤回历史）：收尾后 offset 查询与 route 定向顺序恢复

用户授权推进 C3 后，因原阶段超过 500 行警戒线，选择拆成 C3a（查询协议及接收端收尾后回复）和 C3b（route 定向重建/顺序调度），授权本轮实现和局部验收。两个单元按意图审查，共用查询契约；未创建 git 提交，不自动进入 C4–C6。必要回归迁移使总差异仍大于 500 行，不将两个名称冒充两个已审查的小提交；最终规模及提交划分仍需人工审查。

状态：**旧 C3a/C3b 查询、强制 teardown 和串行活跃方案已撤回，不是当前契约或本轮完成结论**。下列仅保留旧方案当时的真实实现与输出。用户现已批准数值 offset、原 route 全旧 owner 含 finally 自然收尾、每 Target 独立并行流的新需求和计划；本轮状态见报告首节，尚待统一验证，不借以下历史结果声明通过。

### C3a：更改路径、解决的问题、数据流

- `PyFlow/network_api/connect_tcp.py`：`parse_resume_command` 查询分支返回 `(fid, None, origin, target)`，`format_resume_line` 将 `None` 格式化为 `?`；数值五 token 格式不变。Server 拒绝收到的查询作为 Receiver 恢复请求；Client 查询分派与数值发送分派分开，全部消费者区分 offset 值域。
- `TCP_Client_Base._query_resume_offset` 校验实际 Server socket、自身 target、保留 download fid/origin；后台等待旧接收 owner 与实际文件关闭，再经既有 secure/send 路径按磁盘大小回复数值 offset。未知/已移除的接收行不虚构零 offset，持久化模型不加字段。
- `test/unit/network_api/test_resume_matching.py` 保留数值、shell-quoted endpoint、边界和错误格式回归，并增加 `?` / `None` round-trip 与非法查询。真实查询/文件收尾证据来自下述 C3b 消费者场景，而不是源码/调用次数断言。

数据流：Server 定向查询 → Client 实际会话与保留身份校验 → 后台等待旧文件关闭 → 读原完整路径的实际大小 → 既有数值回复。控制 reader 不等待自己需要分派的握手/加密消息。

### C3b：更改路径、解决的问题、数据流

- `connect_tcp.py` 用 `_relay_recoveries[fid]` 保存一个运行时重建 owner：`route/old/queried/ready/active/running/wake`；替代旧 `_pending_originator_resumes`。实际控制 socket、原 Source、Target、child fid 和 route 先校验，再登记动作；重复请求不重复 teardown，active Target 拒绝重复。
- `_cancel_relay` 只取消捕获的该 route listener/data socket 与握手，不取消健康控制会话。旧 reader 停止入队，writers 退出/排空，最终关闭数据资源、清空捕获引用并置 `done` 后才释放 receive reservation，避免新轮越过旧 finally。队列不复用，不删除 Target 已落盘文件。
- `_run_relay_recovery` 查询在线未完成 Target；查询发送失败回滚 queried 标记，以便下一次合法触发重查。按合法 offset 回复的 FIFO 选择在线 Target；每轮仅一个上游和一个下游，保留原 upstream/child fid、route 完成集合和完整目标映射。
- 协议驱动的 Client 恢复经 `_resume_on_socket(..., drain_forward=True)` 固定实际准入 socket，并等待旧 Source sender 收尾；公开 `resume_trans` 的普通/忙对象拒绝语义保留。Server 也把实际 socket 传至直接恢复和 route 登记边界，不在后续后台工作里重新按 peer 借新控制连接。
- 中间轮只完成该 Target；其他 Target 未完成时沿用原 error/结束语义，不回整体成功 ACK，保留 Source 上传行和 route。Source 离线保留 ready；Target 离线不阻塞可服务兄弟目标；合法请求/重连重新触发，不增加定时或忙重试循环。
- `test/unit/network_api/test_fid_routes.py` 迁移旧 pending 内部断言，覆盖错误/旧实际 socket、origin/target/fid、单重建 owner、离线保留、并发回复仅一次真实 Source 请求，以及查询发送 False 后同 socket 合法重查。
- `test/integration/network_api/test_tcp_transfer_resume.py` 将已废弃的 busy-skip 恢复用例迁移为真实共享 relay 中断：A 实际写入 8192 字节后仅断数据 socket，B 实际写入 24576 字节，Source 活跃并确认暂停；A 的恢复触发旧 route teardown。A 真正 ACK 门槛内放行 B 的查询回复，证明 B 已 ready 但未启动；随后只按 A→B 重放，检查两份完整内容、稳定 child、`False/False/True` 原尝试/中间轮/最终轮结果及资源清理。
- 原 ACK-loss file/folder 用例保留异常/False 两种首次 ACK 故障、EOF 零正文、完成集合、原嵌套路径和真实终态 ACK。用事件固定查询回复顺序，不再假定最初请求者天然优先、也不要求对每个兄弟目标手动再次请求。refused-listener/reload 用例保留未完成索引和持久化证据，不将触发请求的 0 冒充已确认 disk offset。
- `test/integration/network_api/test_tcp_transfer_control.py` 同步离线 Source、完成兄弟、旧 owner 释放和新恢复的契约。`File_Transfer.rst`、`TCP_Server_APIs.rst`、`TCP_Client_APIs.rst` 和 `changes/transfer-fid-routes.md` 同步新行为和边界。

数据流：合法 Receiver 数值请求 → 单 route 重建登记 → 旧 data legs/队列/worker 完整收尾 → 各 Target 关闭文件后 offset 回复 → FIFO 单 Target 新数据腿 → 更新真实 completed → 下一轮或最终真实 ACK 后 owning 清理。独立 fid 不参与 teardown。

### 实际局部验证与失败处理

运行环境：Linux，Python 3.10.20。以下是分次运行，不声称一次完整 V3/V6 全绿，也不相加成唯一用例总数：

| 实际命令/范围 | 结果 |
|---|---|
| 原 V3 `fid_routes + resume + control -k 'forward or relay'` | 48 passed、4 failed、133 deselected，270.62 秒；退出 1 |
| `test/unit/network_api/test_fid_routes.py -q`（新查询失败用例添加前） | 36 passed，0.14 秒；退出 0 |
| `test_tcp_transfer_control.py -k 'forward or relay' -x -vv --tb=short` | 36 passed、54 deselected，233.88 秒；退出 0 |
| 两份 unit（resume_matching、fid_routes）加 ACK-loss、refused-listener、serial-recovery 三个函数 | 81 passed，28.22 秒；退出 0 |
| `test_tcp_transfer_resume.py -k 'forward or relay' -q --tb=short` | 13 passed、46 deselected，46.22 秒；退出 0 |
| `test_tcp_transfer_control.py -k 'relay_offline or reconnect_keeps' -q --tb=short` | 9 passed、81 deselected，59.86 秒；退出 0 |
| 新 `failed_offset_query...` 与真实 serial-recovery 两个函数 | 2 passed，6.72 秒；退出 0 |
| 最终 socket 固定边界：fid_routes/control 的 forged/stale/query-failure/online-resume/disconnected-Target/old-session 子集 | 15 passed、112 deselected，70.72 秒；退出 0 |

原 4 个失败：零 ACK file/folder 两个用例仍按旧手动调度契约计数，实际合法 B 回复可先于 A，不能由 calls=3 推断双 sender；一个 refused-listener 用例引用已删字段；新顺序恢复用例缺事件屏障，取 route 前文件已完成。迁移契约和真实事件顺序后通过，未删除路径、弱化内容/ACK 断言、修改 CI 或 coverage。另修复 review 发现的 receive reservation 早于 relay `done` 释放，以及失败查询残留标记；检查 Python 引用的 LSP 仅有 Ruff、`textDocument/references` 不可用，已报告工具限制，消费者由仓库调用点和运行验证覆盖。

精确重跑命令：

```bash
.venv/bin/python -m pytest test/unit/network_api/test_resume_matching.py test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_resume.py::test_forward_ack_loss_retains_state_and_resumes_original_ids test/integration/network_api/test_tcp_transfer_resume.py::test_forward_route_survives_a_refused_target_listener_and_reload test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_tears_down_live_route_and_replays_targets_serially -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_resume.py -k 'forward or relay' -q --tb=short
.venv/bin/python -m pytest test/integration/network_api/test_tcp_transfer_control.py -k 'relay_offline or reconnect_keeps' -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py::test_failed_offset_query_can_be_requested_again_on_the_same_session test/integration/network_api/test_tcp_transfer_resume.py::test_forward_resume_tears_down_live_route_and_replays_targets_serially -q --tb=short
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py test/integration/network_api/test_tcp_transfer_control.py -k 'forged or stale or failed_offset_query or online_resume or disconnected_relay_target or reconnect_keeps' -q --tb=short
```

### S4 独立真实程序：已取得的运行证据

不调用 pytest 测试函数，不 mock 文件/网络/加密主路径；仅用 Event 固定真实写入、ACK 与发送 owner 时序，并真实关闭故障数据 socket。Source 同时有另一个普通上传 fid 在正文门槛内保持活跃；恢复期间该 socket 仍打开，最后取得其真实 ACK 且内容一致。

| 场景 | upstream fid | 两个稳定 child fid | 恢复 offset | workers / 最终存活 |
|---|---:|---|---|---|
| 明文 live、localhost 别名 | 10865447346 | 6493365684 / 10788332980 | 8192 → 24576 | 48 / 0 |
| 加密 thread、Source paused、Target B 离线再原端点回连 | 15823586500 | 17644686304 / 21939653600 | 8192 → 24576 | 110 / 0 |
| 加密 async、wildcard、EOF 等待、保留行 tracking off | 15091650932 | 18378608972 / 22673576268 | 8192 → 150851（EOF 零正文） | 90 / 0 |

每组 Source 原尝试/中间轮/最终轮实际 ACK 结果为 `False/False/True`；两份文件 byte-for-byte 等于 150851 字节原内容，SHA-256 均为 `0a45ceaa5fac82dd386c144b9985cd3985b91a76aba8e5fbac335315c9459f14`。原 relay 捕获的 6 个 socket 登记全部关闭，原 Source owner 已退出；route/index/recovery 清理，Source 与未断线 Target 的实际控制 socket 保持。无关 fid 依次为 6570480050、11528619204、10796683636，最终 ACK=True。EOF 组实际以保留行完成恢复，不在 tracking off 时创建新行；B 从 total=150851 恢复，仍经原真实 header/ACK，而不是因大小相等跳过确认。

```bash
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode live --alias
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode paused --encrypted --offline-target
PYTHONPATH="$PWD" .venv/bin/python /tmp/pyflow-transfer-lifecycle-smoke.py --case serial-recovery --mode eof --encrypted --async-server --untracked
```

独立程序的首次 live 运行因测试注入只 shutdown 而未 close 接收 data socket，仍可读到内核已排队字节，固定 8192 的断言失败；改为真实 shutdown+close 后通过。首次主动关闭 B 控制 socket 的重连没有保持原本地端点；改为由 Server 发 FIN、Client 被动退出并释放旧 socket 后，原端点回连通过。它们是程序时序/断线注入修正，不修改生产内容或网络算法。

首次 EOF 独立程序等待 B 的 offset 回复超时：旧 B ACK 的本地 write 可能成功、但 Server teardown 已不再收它，B 因而不再保留 receive entry。这属于原 ACK 写成功/接收不确定窗口，不伪称生产代码已修复。最终 EOF 场景在放行旧 ACK 前真实关闭其接收 data socket，使旧 ACK **明确失败**后再查询，验证同 fid 零正文恢复；退出 0，10.88 秒。前述两组成功程序亦退出 0；批命令中的后续场景失败不否认已打印并完成的前一组结果。

一次性程序已删除；它的临时目录、传输文件和隔离密钥目录均由 TemporaryDirectory 清理。未抓包；加密结果是现有 framed channel 实际传输的内容/ACK 证据，不扩大成所有连接安全属性的全量审计结论。

### 旧方案当时的范围、风险与规则（历史，非现行契约）

- C3 对应 P03/P05/P12–P16/P19/P20 的本阶段路径；完成目标保留、离线 Source、文件夹原完整路径、数值零/EOF 回归由局部测试提供。多进程重启的独立完整矩阵、native Windows、其他 Python 版本、转发中途 rekey 不列为本次通过项。
- 未知/没有保留 receive entry 的 Target 不返回虚构 offset；可能包括尚未接收文件头或 tracking off 未产生行的任务。它们保留 Server 未完成 route，不承诺自动补齐缺失的接收恢复依据。查询成功发送但无合法回复不新增定时重试；Source 接受/端口/ACK 超时仍沿用现有失败路径，可再次合法操作或结束控制会话后恢复。
- ACK 本地写成功不等于对端应用已收到；不增加 ACK-of-ACK。旧目标 ACK 写成功但 Server teardown 未收到的歧义窗口不承诺自动收敛；EOF 场景必须区分“明确失败”与“写成功但未收到”。本轮不声称修复该协议不确定性。
- 问题二（同控制会话、同 fid 换腿交叉的旧 pause/start 严格代次隔离）继续暂缓。C4 全 Target 已完成后的 EOF 再确认、C5 自转发拒绝、C6 合并回归与总验收未实施；不把 Target 自身 EOF ACK-loss 恢复说成已完成 C4。
- 规则沉淀为真实 socket 契约：错误实际会话无 teardown、query 后实测落盘 offset、FIFO 一目标一轮、旧 finally 后再释放占用、中间轮无整体成功、query False 后同 socket 可重新触发。无新增依赖、CI 修改、skip 或 coverage 放宽；进度 JSON、原 fid、pause/start 签名/四 token 与端口格式保持。
