# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、开标后专家回避与评审席位恢复（失效留痕/交接/补评）、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/recusals`：开标后专家临时回避（可传 `vendor_id` 指定单供应商，省略为全场回避），状态一变相关评分立即失效但保留历史
- `POST /api/recusals/revoke`：撤销回避（未交接的席位恢复原专家与评分；已完成交接不可回退）
- `POST /api/seats/handover`：监督员确认席位交接给接替人；同一接替人重试幂等，资格失败留痕，可对原席位重试
- `POST /api/evaluations/rescore`：接替人对回避席位补评；补评先于确认时登记挂起提名，确认后生效
- `GET /api/tenders/{id}/review`：按供应商还原回避记录、席位交接、失效历史与补评结果（procurement/supervisor/auditor）
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照；回避席位未交接、交接未确认或补评缺项的供应商会被挡住，其他供应商不受影响

## 回避与席位恢复规则

- 回避记录（`recusals`）与评审席位（`evaluation_seats`，按 项目×供应商×轮次×席位号）构成可恢复流程；评分行带 `status`（`valid`/`pending`/`invalidated`），失效后历史、时间与原因仍可审计。
- 同一专家在同一项目、同一供应商、同一轮次只能占用一个有效席位（部分唯一索引约束）；不同供应商可分别占席。
- 监督员确认交接与专家补评并发提交时，通过写锁与唯一交接索引收敛到**同一个接替人**：先到者生效，撞车返回 409；监督员改派时原提名失败留痕、挂起补评分失效保留。
- 授标按供应商逐项检查：任一供应商存在未交接回避、待确认交接或缺项补评即拦截（返回 `details.blocked_vendors`），汇总时只计入 `valid` 评分；授标快照含回避/交接/失效统计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及开标后回避失效、席位交接、并发补评收敛、失败重试、按供应商拦截授标和评审还原。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
