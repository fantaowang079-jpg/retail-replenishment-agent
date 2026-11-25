import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from urllib.parse import quote
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .config import ROOT, load_settings
from .database import SourceDB, Store
from .schemas import AnalysisRequest, ReviewRequest
from .tools import QueryTools
from .workflow import build_graph
from .catalog import SHOPS, WAREHOUSES, PRODUCTS, SCENARIOS, TOOLS, NODES, AGENTS, TABLES, WORDS
from .presentation import present_report, readable_report


def create_app(settings=None):
    settings = settings or load_settings()
    jobs = {}

    @asynccontextmanager
    async def lifespan(app):
        settings.data_path.mkdir(parents=True, exist_ok=True)
        store = Store(settings.data_path)
        source = SourceDB(settings)
        tools = QueryTools(source, settings)
        store.recover()
        async with AsyncSqliteSaver.from_conn_string(str(settings.data_path / "checkpoints.db")) as saver:
            await saver.setup()
            app.state.graph = build_graph(settings, tools, store, saver)
            app.state.store, app.state.source = store, source
            yield
            pending = list(jobs.values())
            for job in pending:
                job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await tools.close()
        source.engine.dispose()

    app = FastAPI(title=settings.app.title, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        labels = {"tenant":"商家", "warehouse":"仓库", "skus":"要分析的商品", "start_date":"开始日期", "end_date":"结束日期", "scenario":"业务场景", "safety_days":"备用天数", "review_days":"距下次盘点天数", "promotion_multiplier":"预计销量倍数", "reviewer":"审核人", "quantity":"确认数量", "reason":"调整依据", "note":"审核说明", "decision":"审核操作", "question":"关注的问题"}
        fields = list(dict.fromkeys(labels.get(str(error["loc"][-1]), "表单内容") for error in exc.errors()))
        return JSONResponse(status_code=422, content={"detail": "请检查" + "、".join(fields) + "：必填项不能留空，日期需按先后顺序，数量和天数需在允许范围内。"})

    def get_task(task_id):
        task = app.state.store.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    def graph_config(task_id):
        return {"configurable": {"thread_id": task_id}, "recursion_limit": settings.app.max_graph_steps}

    async def run(task_id, payload):
        store = app.state.store
        store.update(task_id, "running")
        try:
            await app.state.graph.ainvoke(payload, config=graph_config(task_id))
            snapshot = await app.state.graph.aget_state(graph_config(task_id))
            report = snapshot.values.get("report")
            if snapshot.next == ("human_review",):
                store.update(task_id, "pending_review", report)
                store.event(task_id, "human_review", "等待人工复核，检查点已保存")
            elif not snapshot.next and report:
                store.update(task_id, "archived", report)
                store.event(task_id, "archive", "报告已归档；未下发采购订单")
            else:
                raise RuntimeError("工作流未到预期状态")
        except asyncio.CancelledError:
            store.update(task_id, "interrupted", error="服务停止，可从检查点恢复")
            raise
        except Exception as exc:
            # 错误类型可诊断，原文不写浏览器，避免数据库连接串或密钥泄露。
            message = f"节点失败（{type(exc).__name__}），可检查数据与配置后恢复"
            store.update(task_id, "failed", error=message)
            store.event(task_id, "error", message)

    def launch(task_id, payload):
        if task_id in jobs and not jobs[task_id].done():
            raise HTTPException(409, "任务正在执行，请勿重复提交")
        # 在返回 HTTP 前标记状态，避免并发审核/恢复重复提交。
        app.state.store.update(task_id, "running")
        job = asyncio.create_task(run(task_id, payload))
        jobs[task_id] = job
        job.add_done_callback(lambda done: jobs.pop(task_id, None) if jobs.get(task_id) is done else None)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/meta")
    def meta():
        end = app.state.source.demo_date()
        return {"title": settings.app.title, "llm_enabled": bool(settings.deepseek.api_key.strip()),
                "model": settings.deepseek.model, "start_date": str(end-timedelta(days=29)), "end_date": str(end),
                "tenants": SHOPS, "warehouses": WAREHOUSES, "products": PRODUCTS,
                "scenarios": SCENARIOS, "tools": TOOLS, "nodes": NODES, "agents": AGENTS, "tables": TABLES, "words": WORDS}

    @app.get("/api/tasks")
    def tasks():
        return app.state.store.list()

    @app.post("/api/tasks", status_code=202)
    async def create(req: AnalysisRequest):
        if sum(not j.done() for j in jobs.values()) >= 4:
            raise HTTPException(429, "演示服务最多同时执行四个任务")
        task_id = uuid4().hex
        request = req.model_dump(mode="json")
        app.state.store.create(task_id, request)
        launch(task_id, dict(task_id=task_id, request=request))
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        task = get_task(task_id)
        task["display"] = present_report(task["report"]) if task["report"] else None
        return task

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    async def resume(task_id: str):
        task = get_task(task_id)
        if task["status"] not in ["failed", "interrupted", "queued"]:
            raise HTTPException(409, "只有失败或中断任务可以恢复；待审核任务请提交审核")
        snapshot = await app.state.graph.aget_state(graph_config(task_id))
        # 若进程在初始 checkpoint 前退出，重新使用已持久化的请求。
        payload = None if snapshot.values else dict(task_id=task_id, request=task["request"])
        launch(task_id, payload)
        return {"task_id": task_id}

    @app.post("/api/tasks/{task_id}/review", status_code=202)
    async def review(task_id: str, req: ReviewRequest):
        task = get_task(task_id)
        if task["status"] != "pending_review":
            raise HTTPException(409, "任务当前不在待审核状态")
        report = task["report"]
        rows = {r["sku"]: r for r in report["recommendations"]}
        adjustments = {a.sku: a for a in req.adjustments}
        if set(adjustments)-set(rows):
            raise HTTPException(422, "调整清单含不属于本报告的商品")
        if req.decision == "approve":
            if report["gaps"] and (not req.acknowledge_gaps or not req.note.strip()):
                raise HTTPException(422, "存在缺口时须确认已核对并填写审核说明")
            if any(row["suggested_qty"] is None and sku not in adjustments for sku, row in rows.items()):
                raise HTTPException(422, "缺少建议数量的商品须人工填写数量和依据，也可填零暂不采购")
        launch(task_id, Command(resume=req.model_dump(mode="json")))
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        get_task(task_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "无效的事件游标")
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = app.state.store.events(task_id, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                current = get_task(task_id)
                if current["status"] in ["pending_review", "archived", "failed", "interrupted"] and len(batch) < 500:
                    yield f"event: done\ndata: {json.dumps({'status': current['status']})}\n\n"
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/tasks/{task_id}/report-readable", response_class=HTMLResponse)
    def download_readable(task_id: str):
        task = get_task(task_id)
        if task["status"] != "archived":
            raise HTTPException(409, "确认并保存后才可下载报告")
        return HTMLResponse(readable_report(task["report"]), headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("补货建议与审核记录.html")})

    @app.get("/api/tasks/{task_id}/report")
    def download(task_id: str):
        task = get_task(task_id)
        if task["status"] != "archived":
            raise HTTPException(409, "审核归档后才可下载")
        path = app.state.store.archives / f"{task_id}.json"
        if not path.exists():
            raise HTTPException(404, "归档文件不存在")
        return FileResponse(path, filename=f"report-{task_id[:8]}.json", media_type="application/json")

    return app


app = create_app()
