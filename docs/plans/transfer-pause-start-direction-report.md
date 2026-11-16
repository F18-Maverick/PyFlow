# 双向文件暂停、继续与按对象恢复 — 实施报告

状态：需求卡和七项拆分已由用户确认，本轮明确授权 C1。**C1 已实现并通过定向验证，等待人工审查；C2a–C5 未开始。** 本报告随已完成的原子变更增补，不将未实现或未运行的路径标为完成。未创建提交、合并或推送。

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
- 下一项为 C2a：发送端权威状态机与顺序、去重、预置暂停和暂停键生命周期；须待本 C1 审查放行及下一阶段授权。

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
