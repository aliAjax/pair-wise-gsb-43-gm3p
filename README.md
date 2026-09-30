# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

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
- `POST /api/evaluation-seats`：开标后把专家固定到评审席位（评分时也会自动落席）
- `POST /api/recusals`：开标后登记专家对某供应商的临时回避，其本轮评分立即置为 `invalidated`（保留历史，不进汇总）
- `POST /api/recusals/transfer`：监督员确认交接，条件更新保证并发确认只留一个接替人；失败可凭原回避记录重试
- `POST /api/recusals/withdraw`：交接完成前撤回回避，并恢复因此失效的原评分
- `POST /api/evaluations`（带 `recusal_id`）：接替专家按回避记录补评对应供应商
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

### 回避与席位规则

- 回避按 `(项目, 专家, 供应商, 轮次)` 生效，回避状态变化只影响该供应商；其他供应商评分照常汇总。
- 失效评分（`evaluations.status='invalidated'`）保留完整历史并记录 `invalidated_recusal_id`，授标平均只统计 `valid` 评分；补评记录带 `source_recusal_id`。
- 同一专家在同一项目不能同时占用两个席位：有效席位与"已接手回避"互斥；接替人也不能接手第二个回避。
- 授标前逐项校验：存在待交接回避，或已交接但接替人未补齐本轮全部评分项，即阻断授标（错误信息标明供应商和缺失评分项）。
- `GET /api/tenders/{id}`（采购/监督员/审计）返回 `evaluation_seats`、`recusals` 和按供应商组织的 `review_trace`，页面"评审回避与席位还原"区块与授标快照中的 `recusal_review` 共用同一视图，审计时间线记录交接失败尝试。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
