# 终态 ACK 交付修复报告

状态：实现、回归与本地运行验收完成，待人工审查。未创建 git 提交、未合并、未推送，未触发远端 CI。

后续修正：本报告记录的是先前粘包修复阶段，当时完整 sign 历史及未终止尾部尚无容量上限。需求方随后完成本地方案审查并要求有界化；原 `_take_wire_lines` 和历史列表已由消费后删除的有界队列替代，实施与当前验收见 [transfer-sign-buffer-bounds-report.md](transfer-sign-buffer-bounds-report.md)。下文测试数字为该历史阶段证据，不冒称本次新增限制的验证结果。

## 1. 恢复点与提交意图

恢复原会话 `01a1aa1d-bfac-75d9-9634-ed1bae67d734` 中的最后一项实际需求：取消 ACK 发送时机重构，继续增量修复 ACK 交付问题。原会话的较早 recap 仍写“等待确认/另行讨论”，但后续用户消息与已执行工具记录表明，代码修复、最初的粘包回归和部分验证已经完成；中断在边界单测与报告收尾。

单一提交意图：两端发送腿按完整协议行识别 ACK，不再把一次 socket read 当成一条 sign；中继回传同一个已确认 ACK 时保留换行终止。没有新增 ACK-of-ACK、结果通道、重试或延迟关闭。

本报告是未提交工作区的审查材料，不表示人工批准或合并前闸门已通过。原会话按轻量增量实施；本次没有补写虚构的审批记录。

## 2. 改动文件与关键决策

| 文件 | 变更 |
|---|---|
| `PyFlow/network_api/connect_tcp.py` | `_take_wire_lines` 缓存 bytes 尾部，完整行才解码；两端 `file_transfer_mode` 累积所有完整 sign；`_forward_pump` 将已确认 marker 作为文本交给既有 `send_message`，由其追加换行 |
| `test/integration/network_api/test_tcp_transfer_resume.py` | 原粘包回归扩展为上传/下载 × 初传/EOF 重放四种真实 socket 场景；注入只合并真实收到的字节，不制造 ACK；断言最终返回值、内容和进度清理 |
| `test/unit/network_api/test_wire_lines.py`（新增） | 枚举每个读取切分点；覆盖多行、空行、CRLF、未终止尾部与逐字节 ACK，未遇换行不得提前交付 |
| `docs/source/File_Transfer/File_Transfer.rst` | 明确 sign 的换行与流式解析契约，以及 ACK 时机、超时和关闭行为不变 |
| `docs/plans/final-ack-delivery-requirement.md` | 用已证实的字节流解析根因替换 RST/FIN 假说；更新实际修复边界 |
| `docs/plans/test-flake-fixes-report.md` | 保留其历史范围，链接后续 ACK 修复，避免继续把本问题记成未处理 |
| 本报告（新增） | 路径逐项验收、历史证据与本次验证分开记录 |

公共 API、FID 分配、五项进度模型、依赖、CI、覆盖率、加密握手均未改动。没有删除/跳过测试、放宽断言或新增 lint 忽略规则；原有失败必须返回 `False` 的要求仍保留。

## 3. 根因与数据流

原实现把 `data.decode("utf-8").strip()` 的全部结果与单个 sign 做 `==` 比较。header sign 与最终 ACK 落在同一次读时，结果包含两行，两次单行比较都失败；随后 EOF 使发送端返回 `False`。它不是“只保留最后一行”，也没有证据支持通过新增 drain 或 ACK 握手修复本缺陷。

现有数据流：真实 socket 字节（加密腿先走原有解密入口）→ `receive_message` → `_take_wire_lines` → 所有完整 sign 加入该腿的已收集合 → 原有 start/ACK/error 等待逻辑 → 真实终态 ACK 才成功并清理上传进度。

未终止尾部以 bytes 保留，下一次读继续拼接；不会把 EOF 当作换行，也不会丢掉 EOF 之前已经解析的 ACK。完整行才 UTF-8 解码，避免跨读字符被替换。原有 ACK/error 判定顺序未重新定义。

中继：目标 writer 读到真实最终 ACK → 保留原有全目标完成门槛 → 同一 `send_message` 发送该 marker 文本并追加换行 → 写成功才删除路由。原 `_forward_read_sign` 返回不带换行的 bytes，而 `send_message` 对 bytes 不加换行；不修正此点会让新分行器一直缓存上游 ACK。没有新增成功信号或绕过统一发送/加密入口。

## 4. 需求卡路径验收

以下为本次独立 Python 3.10 loopback 烟测的实际结果，不是只依据“测试通过”推断覆盖。实例进度、日志及密钥目录隔离到临时目录；观察包装只记录实际发送/返回结果。

| 路径 | 实际执行与证据 | 结论 |
|---|---|---|
| P1 服务端接收普通上传 | 33,568 字节；接收侧 ACK 写成功，发送侧返回 `True`，文件逐字节相同，双侧进度清理 | 覆盖 |
| P2 文件夹项上传 | `bundle/nested` 下 33,569 字节；ACK 写成功、发送侧 `True`、目标内容相同、进度清理 | 覆盖 |
| P3 上传 EOF 重放 | 接收记录和文件已完整，offset=total=33,570；无正文重发，ACK 写成功、发送侧 `True`、记录清理 | 覆盖 |
| P4 服务端下发 | 33,571 字节；客户端 ACK 写成功、服务端发送 `True`、内容相同、进度清理 | 覆盖 |
| P5 下载 EOF 重放 | offset=total=33,572；客户端 ACK 写成功、服务端 `True`、记录清理 | 覆盖 |
| P6 中继 ACK 聚合 | 实际 `_forward_one` 转发 49,159 字节；观察到服务端经文本发送上游 ACK 并写成功；源端 `True`、目标内容相同、源上传记录及中继路由清理 | 覆盖 |
| P7 公钥推送 | 真实加密控制连接建立；双向公钥发送腿均返回 `True`，双向接收 ACK 写成功，控制连接进入 framed 态 | 覆盖 |
| P8 错误路径 | size 不符返回 `False` 且保留原 partial；声明 8,192 字节却只发 7 字节，保留真实 partial 和 `[7,8192]` 进度；header 前断开，不产生成功接收记录或目标文件 | 覆盖 |

另在同一真实加密连接上完成了 66,416 字节上传和 66,417 字节下载：监听 stamp 均为 `enc:`，两端发送调用返回 `True`、落盘内容相同、上传进度清理。本项不等同于全局加密覆盖审计，未据此声称所有连接均已抓包验收。

逐字节读取的独立运行探针还对两种发送端各执行了三种终态：完整 ACK 返回 `True` 并清理上传记录；缺少末尾换行的 ACK 返回 `False` 并保留记录；error sign 返回 `False` 并保留记录。六种结果全部符合契约。该临时探针最初误用临时数据端口查询客户端上传键；产品客户端键使用控制端点 `(self.host, self.port)`，修正探针断言后通过，没有为此改动产品逻辑。

## 5. 机械验证与修复前证据

### 本次恢复后实际执行

```sh
uv run --python 3.10 --with pytest --with flask pytest -q
```

结果：**663 passed in 389.46s**，无失败/跳过。随后只为消除新增 PLR0915，将确定性传输注入逻辑抽成同文件辅助函数；产品代码未再变化。

最终辅助函数抽取后的定向验证：

```sh
uv run --python 3.10 --with pytest --with flask pytest \
  test/unit/network_api/test_wire_lines.py \
  test/integration/network_api/test_tcp_transfer_resume.py::test_coalesced_receiver_signs_keep_the_final_ack -q
```

结果：**6 passed in 8.57s**（两条边界契约与四个真实传输场景）。

最终 Ruff 检查：`connect_tcp.py` 242 条、既有 integration 文件 44 条、新增 unit 文件 0 条；新增 parser/传输注入/回归块没有诊断。没有把全文件 Ruff 声称为通过，也没有处理或屏蔽无关存量问题。

### 从原会话工具记录恢复的历史证据（不是本次重跑）

- 最初粘包回归在未修复产品代码上：`1 failed in 2.18s`，失败断言为实际 `file_transfer_mode=False`，期望 `True`；恢复修复后同一用例通过。
- 修复后的 Python 3.10 原有 ACK 失败/恢复八个用例循环：15 轮全部通过，每轮 `8 passed`。该循环早于本次 bytes 尾部实现与四场景扩展，因此只作历史证据，不冒称最终版本又重复了 15 轮。
- 本次没有通过 stash、回滚工作区或修改既有检查策略来重复证明历史故障。

## 6. 明确未覆盖、风险与交付边界

- Windows、macOS 未在本次实机运行，也未触发远端 CI；不声称跨平台已验收。历史 socketpair 与加密测试修复的 Windows 限制仍见原报告。
- TCP 写成功但 ACK 真正未送达的网络故障仍按既有失败/续传规则处理；本次解决解析导致的“已收到却未识别”，不承诺端到端绝对交付。
- 未增加 ACK 时机重构、shutdown/drain、额外超时、EOF 尾部兜底或新的兼容入口。公钥推送仍使用既有明文文件腿及 TOFU 流程。
- 本地 smoke 为临时进程/临时目录执行；没有留下 smoke 脚本或测试数据文件。
- 人工审查、远端平台验证与提交/合并授权未发生。当前交付是完成实现及本地验收的工作区，不是已合并或已发布版本。

规则沉淀：永久边界契约与四种确定性粘包回归防止重新引入“一次 recv 等于一条消息”；运行文档明确中继 ACK 也必须是换行终止的协议行。
