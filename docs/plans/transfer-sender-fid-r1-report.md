# R1 报告：中继 upstream FID 由发送方预分配

状态：R1 主要更改已完成；用户明确要求继续 R2，不单独修补即将删除的测试 `request_id` 参数。本报告记录 R1 阶段实际证据，不声明 R1 已由人类审计通过，也不把 R2 后验证冒充 R1 独立验收。未创建提交、未合并、未推送。

## 意图、范围与数据流

对应 `transfer-sender-fid-revision-plan.md` 的 R1：所有新数据腿由该腿发送方通过唯一 `_FidAllocator` 分配。普通文件、目录项、公钥分配方不变；中继源 Client 在发命令前分配 upstream FID 并绑定既有五项上传行；Server 仅登记该 ID，再为每个目标分配独立 child FID。保留 route/targets/completed、进度 JSON、原路径、字节协议和最终数据 ACK。

执行路径：源端 `_forward_one` → upstream FID/上传行绑定 → `/forward_item ... fid=<upstream_fid>` → Server 核对占用并登记 route → 每目标 child FID → 原数据泵/目标 ACK → 原聚合 ACK → 成功才清理。失败保留上传及 route。

恢复必须显式携带 `resume=<offset>`，包括 `resume=0`；Server 核对实际源端、已有 route 和未完成目标，再复用原上下游 FID。新请求省略 `resume=`。旧 `relay_fid=` 线路不保留别名。

## 占用边界

Server 不覆盖已有同 upstream FID 的 route 或接收行，也不占用同目标下已有的上传、活动 FID/socket、暂停、relay 或反向 route 身份。冲突明确返回错误；不同 receiver 作用域不被误合并。登记遵循 progress → pause → route 锁顺序，分配器调用在这些锁之外。

## 修改范围

- `PyFlow/network_api/connect_tcp.py`：源分配、解析/恢复调用、逐目标分配与 route 占用拒绝。
- `test/unit/network_api/test_fid_routes.py`：占用冲突和 receiver 作用域。
- `test/integration/network_api/test_tcp_transfer_resume.py`：不同目标乱序、source-owned FID、零 offset 重放、两源同值冲突；现有 ACK/路径/进度断言保留。
- TCP 两端 API、File_Transfer、活动变更说明与主/修订计划同步。历史 atomic-1–5/O 报告不回写。

R1 保留 UUID 关联直至 R2；当前最终线路与会话隔离以 R2 报告为准。

## R1 阶段验证

1. 新增冲突回归先运行：6 个 target-leg 所有者分支均失败，直接复现登记未拒绝已占用上游别名的问题。
2. 修复并补齐接收/活动/暂停/route 边界后运行：

```text
.venv/bin/python -m pytest test/unit/network_api/test_fid_routes.py \
  test/integration/network_api/test_tcp_transfer_resume.py \
  -k 'registration_rejects_upstream_fid_that_collides or registration_scopes_live_downstream_upload_conflicts or sender_owned_fids_to_distinct_targets_survive_out_of_order_replies or forward_resume_with_zero_offset_reuses_existing_route or duplicate_upstream_fid_is_rejected_without_replacing_existing_route' -q
```

实际结果：**13 passed, 55 deselected in 5.73s**。

3. R1 阶段合并受影响套件实际结果：**427 passed, 1 failed in 274.89s**。唯一失败为 `test_public_forward_api` 调用当时仍要求 `request_id` 的 `forward_target_command`，不是文件内容/恢复失败。用户明确要求不为旧参数修测试，而直接进入删除该参数的 R2；临时修补已撤回。
4. R2 独立运行另验证明文/加密并发、逐目标 ACK 和 EOF 复用原 ID；它是最终整合实现证据，准确结果见 `transfer-sender-fid-r2-report.md`。

## 风险与闸门

不支持新旧线路混跑；不迁移旧进度，不处理 client_port 身份迁移，不新增自动重试或第二张路径表。R1 本身不能替代 R2 的未决 FID 隔离。R2 已获用户明确授权；最终报告完成后等待人工审查，不自动创建提交或推送。
