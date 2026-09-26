# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

批次血缘：登记上下游批次与投入数量，仅允许同厂关系；投入超出来源批可用量或关系成环时拒绝登记、不落库。上游批次被拒收或存在未关闭关键偏差时，全部下游批次（含已放行批次，原放行决定保留在决定历史中）自动进入召回待审 `recall_review`，由质量人员重新决定。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次（批次可带 `quantity` 批量）。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/links`：登记血缘，请求体 `factory_id`、`upstream_batch_id`、`quantity`。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查。
- `GET /api/batches/{id}`：批次详情，含 `links.upstream` / `links.downstream`（带投入量与阻塞标记）及 `links.recall_sources`（召回待审的阻塞来源）。
- `GET /api/state`、`GET /api/health`：状态（含全部血缘关系与各批可用量）和健康检查。

页面 `/` 提供批次登记、血缘登记和批次详情视图，可逐级展开上下游链路并标出投入量与阻塞来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。
