import asyncio
import json
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update, delete

from app.config import Settings
from app.database import inventory, sales, suppliers
from app.main import create_app
from app.schemas import AnalysisRequest, Narrative


@pytest.fixture
def settings(tmp_path):
    return Settings(app={"data_dir": str(tmp_path), "cache_ttl_seconds": 0})


def payload(**overrides):
    return dict(start_date=str(date.today()-timedelta(days=29)), end_date=str(date.today()), **overrides)


def wait_task(client, task_id):
    for _ in range(160):
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ["running", "queued"]:
            return task
        time.sleep(0.05)
    pytest.fail("任务执行超时")


def start(client, **overrides):
    response = client.post("/api/tasks", json=payload(**overrides))
    assert response.status_code == 202, response.text
    return wait_task(client, response.json()["task_id"])


@pytest.mark.parametrize("scenario,expected", [("shortage", 168), ("slow", 168), ("promotion", 288)])
def test_scenarios_and_numeric_evidence(settings, scenario, expected):
    with TestClient(create_app(settings)) as client:
        task = start(client, scenario=scenario)
        assert task["status"] == "pending_review", task
        report = task["report"]
        first, slow, missing = report["recommendations"]
        assert first["sales_qty"] == 300
        assert first["previous_sales_qty"] == 210
        assert first["daily_sales"] == 10
        assert first["available_stock"] == 35
        assert first["suggested_qty"] == expected
        assert first["coverage_days"] == 3.5
        assert task["display"]["shop"] == "邻里生活超市"
        assert task["display"]["cards"][0]["action"] == f"建议补 {expected} 盒"
        assert "每箱 12 盒" in task["display"]["cards"][0]["calculation"]
        assert "92" in task["display"]["cards"][2]["why"]
        assert slow["risk"] == "滞销风险"
        assert slow["suggested_qty"] == 0
        assert missing["suggested_qty"] is None
        assert {g["field"] for g in report["gaps"]} >= {"lead_days", "reported_available"}
        assert len(report["evidence"]) == 4
        assert "未配置 API Key" in report["llm_status"]
        events = client.get(f"/api/tasks/{task['id']}/events")
        assert events.status_code == 200
        assert "event: done" in events.text
        assert "inventory_snapshot" in events.text
        assert "补查" in events.text
        assert '"kind": "start"' in events.text
        assert '"agent": "sales_agent"' in events.text
        assert '"kind": "tool"' in events.text


def test_tenant_warehouse_isolation(settings):
    with TestClient(create_app(settings)) as client:
        a = start(client, tenant="demo", warehouse="WH-SH", skus=["SKU-001"])
        b = start(client, tenant="other", warehouse="WH-SH", skus=["SKU-001"])
        c = start(client, tenant="demo", warehouse="WH-BJ", skus=["SKU-001"])
        assert [t["report"]["recommendations"][0]["sales_qty"] for t in [a,b,c]] == [300,3000,600]


def test_agent_step_catalog_is_available(settings):
    with TestClient(create_app(settings)) as client:
        meta = client.get("/api/meta").json()
        assert meta["agents"]["sales_agent"]["name"] == "销售分析"
        assert "每天平均卖多少" in meta["agents"]["sales_agent"]["description"]


def test_unknown_data_is_not_zero(settings):
    with TestClient(create_app(settings)) as client:
        task = start(client, skus=["UNKNOWN"])
        row = task["report"]["recommendations"][0]
        assert row["sales_qty"] is None
        assert row["incoming_qty"] is None
        assert row["suggested_qty"] is None
        assert len(task["report"]["gaps"]) >= 4


def test_review_guards_and_restart_resume(settings):
    with TestClient(create_app(settings)) as client:
        task = start(client)
        task_id = task["id"]
        review = dict(reviewer="演示审核员", decision="approve")
        assert client.post(f"/api/tasks/{task_id}/review", json=review).status_code == 422
        assert client.get(f"/api/tasks/{task_id}/report").status_code == 409
        assert client.get(f"/api/tasks/{task_id}/report-readable").status_code == 409
    # 新建应用实例，检查点从磁盘恢复，并提交人工审核。
    with TestClient(create_app(settings)) as client:
        review.update(acknowledge_gaps=True, note="已电话确认缺失商品暂不采购<script>不应执行</script>", adjustments=[dict(sku="SKU-003", quantity=0, reason="等待供应商补充交期")])
        assert client.post(f"/api/tasks/{task_id}/review", json=review).status_code == 202
        final = wait_task(client, task_id)
        assert final["status"] == "archived", final
        report = final["report"]
        assert report["status"] == "approved"
        assert report["recommendations"][2]["suggested_qty"] is None
        assert report["review"]["final_quantities"]["SKU-003"] == 0
        download = client.get(f"/api/tasks/{task_id}/report")
        assert download.status_code == 200
        assert download.json() == report
        readable = client.get(f"/api/tasks/{task_id}/report-readable")
        assert readable.status_code == 200
        assert "挂耳咖啡" in readable.text and "SKU-" not in readable.text
        assert "&lt;script&gt;" in readable.text and "<script>" not in readable.text
        meta = client.get("/api/meta").json()
        assert meta["tenants"]["other"] == "悦享生活百货"
        assert client.post(f"/api/tasks/{task_id}/review", json=review).status_code == 409


def test_reject_with_missing_data(settings):
    with TestClient(create_app(settings)) as client:
        task = start(client)
        response = client.post(f"/api/tasks/{task['id']}/review", json=dict(reviewer="审核员", decision="reject", note="数据补齐后重建分析"))
        assert response.status_code == 202
        assert wait_task(client, task["id"])["report"]["status"] == "rejected"


def test_stale_inventory_blocks_recommendation(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.source.engine.begin() as conn:
            conn.execute(update(inventory).values(day=date.today()-timedelta(days=1)))
        task = start(client, skus=["SKU-001"])
        assert task["report"]["recommendations"][0]["suggested_qty"] is None
        assert any(g["field"] == "snapshot_date" for g in task["report"]["gaps"])


def test_zero_sales_and_no_division_error(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.source.engine.begin() as conn:
            conn.execute(delete(sales).where(sales.c.sku == "SKU-001"))
        task = start(client, skus=["SKU-001"])
        row = task["report"]["recommendations"][0]
        assert row["sales_qty"] == 0  # 覆盖声明完整时，无订单才是真正的零。
        assert row["coverage_days"] is None
        assert row["growth_percent"] is None
        assert row["suggested_qty"] == 0
        assert row["risk"] == "滞销风险"


def test_tool_budget_works_for_both_orders(settings):
    settings.app.max_tool_calls = 4
    with TestClient(create_app(settings)) as client:
        for scenario in ["slow", "shortage"]:
            assert start(client, scenario=scenario)["status"] == "pending_review"


def test_node_timeout_can_resume(settings, monkeypatch):
    import app.workflow as workflow
    original = workflow.interpret
    async def slow(*args, **kwargs):
        await asyncio.sleep(2)
    settings.app.node_timeout_seconds = 1
    monkeypatch.setattr(workflow, "interpret", slow)
    with TestClient(create_app(settings)) as client:
        task = start(client, skus=["SKU-001"])
        assert task["status"] == "failed"
        assert "TimeoutError" in task["error"]
        monkeypatch.setattr(workflow, "interpret", original)
        assert client.post(f"/api/tasks/{task['id']}/resume").status_code == 202
        resumed = wait_task(client, task["id"])
        assert resumed["status"] == "pending_review", resumed


def test_request_validation(settings):
    with TestClient(create_app(settings)) as client:
        for overrides in [dict(skus=[]), dict(skus=["bad' OR 1=1"]), dict(promotion_multiplier=0), dict(safety_days=-1)]:
            assert client.post("/api/tasks", json=payload(**overrides)).status_code == 422
        assert client.get("/api/tasks/missing").status_code == 404


def test_llm_response_contract_and_failure(monkeypatch):
    import app.llm as llm
    from app.config import DeepSeekConfig
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "test-only"
            assert kwargs["max_retries"] == 0
        async def ainvoke(self, *args, **kwargs):
            class Response:
                content = json.dumps(dict(overview="库存存在缺货风险", reasoning="请参考补货计算表", cautions="需人工复核"), ensure_ascii=False)
            return Response()
    monkeypatch.setattr(llm, "ChatOpenAI", FakeClient)
    result, status = asyncio.run(llm.interpret(DeepSeekConfig(api_key="test-only"), {}, "测试"))
    assert status == "DeepSeek 解读成功"
    assert result["overview"] == "库存存在缺货风险"
    class BadClient(FakeClient):
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("secret-must-not-leak")
    monkeypatch.setattr(llm, "ChatOpenAI", BadClient)
    _, status = asyncio.run(llm.interpret(DeepSeekConfig(api_key="test-only"), {}, "测试"))
    assert "降级" in status and "secret" not in status
    with pytest.raises(ValueError):
        Narrative(overview="建议采购999件", reasoning="", cautions="")


def test_actual_langchain_deepseek_wire_format(monkeypatch):
    import httpx
    import app.llm as llm
    from app.config import DeepSeekConfig
    real_client = llm.ChatOpenAI
    async def verify():
        def transport(request):
            assert str(request.url) == "https://api.deepseek.com/chat/completions"
            assert request.headers["authorization"] == "Bearer test-only"
            body = json.loads(request.content)
            assert body["model"] == "deepseek-chat"
            assert body["response_format"] == {"type": "json_object"}
            content = json.dumps(dict(overview="供需存在风险", reasoning="请依据工具结果复核", cautions="需人工审核"), ensure_ascii=False)
            return httpx.Response(200, json={"id":"test", "object":"chat.completion", "created":0, "model":"deepseek-chat", "choices":[{"index":0,"message":{"role":"assistant","content":content},"finish_reason":"stop"}], "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            monkeypatch.setattr(llm, "ChatOpenAI", lambda **kw: real_client(**kw, http_async_client=client))
            result, status = await llm.interpret(DeepSeekConfig(api_key="test-only"), {}, "测试")
            assert status == "DeepSeek 解读成功", status
            assert result["overview"] == "供需存在风险"
    asyncio.run(verify())


def test_null_source_fields_become_gaps(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.source.engine.begin() as conn:
            conn.execute(update(inventory).where(inventory.c.sku == "SKU-001").values(on_hand=None))
            conn.execute(update(suppliers).where(suppliers.c.sku == "SKU-002").values(lead_days=None))
        task = start(client)
        assert task["status"] == "pending_review", task
        assert all(r["suggested_qty"] is None for r in task["report"]["recommendations"])
