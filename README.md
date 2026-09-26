# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

批次血缘：生产批分装成包装批、返工多批并回一批时，登记上下游批次与投入数量。跨厂关系拒绝登记；投入超出来源批可用量或登记后成环时不落库。上游批次拒收或存在未关闭关键偏差时，全部下游批次进入召回待审（`recall_pending`），已放行批撤回但保留原放行决定；阻塞来源未解除前下游不能放行。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。旧库自动迁移（批次表补 `quantity` 列与 `recall_pending` 状态）。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次（批次可带 `quantity` 作为可投入量）。
- `POST /api/lineage`：登记上下游血缘，参数 `factory_id`、`upstream_id`、`downstream_id`、`quantity`、`upstream_revision`。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差（关键偏差开启即触发下游召回待审）。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查；拒收触发下游召回待审，上游存在阻塞来源时禁止放行。
- `POST /api/batches/{id}/recall-review`：召回待审批次审查（`note`/`reject`/`release`/`conditional`），阻塞未解除时放行类审查不生效。
- `GET /api/batches/{id}`：详情含 `upstream_lineage`、`downstream_lineage`、`available_quantity`、`active_blockers`、`recall_events`、`recall_reviews`。
- `GET /api/state`、`GET /api/health`：状态（含血缘边与各批阻塞来源）和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
