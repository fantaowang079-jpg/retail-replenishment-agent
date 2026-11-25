import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .calculations import calculate, cross_check
from .database import utcnow
from .llm import interpret
from .schemas import AnalysisRequest, Report, ReviewRequest


class State(TypedDict, total=False):
    task_id: str
    request: dict
    plan: list[str]
    data: dict
    calls: int
    agent_notes: dict
    recommendations: list
    gaps: list
    checks: list
    report: dict
    review: dict


def build_graph(settings, tools, store, checkpointer):
    def limited(name, fn):
        async def wrapped(state):
            store.event(state["task_id"], name, "开始执行", kind="start", agent=name)
            async with asyncio.timeout(settings.app.node_timeout_seconds):
                result = await fn(state)
            store.event(state["task_id"], name, "执行完成", kind="complete", agent=name)
            return result
        return wrapped

    async def query(state, tool, fresh=False, owner=None):
        if state.get("calls", 0) >= settings.app.max_tool_calls:
            raise RuntimeError("工具调用达到上限")
        state["calls"] = state.get("calls", 0) + 1
        result = await tools.query(tool, AnalysisRequest.model_validate(state["request"]), fresh)
        store.event(state["task_id"], "tool", f"{tool} · {result['evidence']['rows']} 条数据", kind="tool", agent=owner or "replenishment_agent", tool=tool, evidence=result["evidence"])
        return result

    async def supervisor(state):
        req = AnalysisRequest.model_validate(state["request"])
        plan = ["inventory_agent", "sales_agent", "replenishment_agent"] if req.scenario.value == "slow" else ["sales_agent", "inventory_agent", "replenishment_agent"]
        store.event(state["task_id"], "supervisor", "任务分解：销售趋势 → 库存口径 → 在途与供应周期 → 建议及核验", kind="plan", agent="supervisor", plan=plan)
        return dict(plan=plan, data={}, calls=0, agent_notes={})

    async def sales_agent(state):
        data = dict(state.get("data", {}))
        store.event(state["task_id"], "sales_agent", "读取销售记录，准备计算销量趋势", kind="action", agent="sales_agent")
        data["sales"] = await query(state, "sales_summary", owner="sales_agent")
        narrative, status = await interpret(settings.deepseek, data["sales"], "销售分析 Agent")
        notes = {**state.get("agent_notes", {}), "sales": dict(narrative=narrative, status=status)}
        store.event(state["task_id"], "sales_agent", status, kind="result", agent="sales_agent")
        return dict(data=data, calls=state["calls"], agent_notes=notes)

    async def inventory_agent(state):
        data = dict(state.get("data", {}))
        store.event(state["task_id"], "inventory_agent", "读取仓库库存，核对可售口径", kind="action", agent="inventory_agent")
        data["inventory"] = await query(state, "inventory_snapshot", owner="inventory_agent")
        # 业务 Agent 根据工具返回补充查询，最多重查一次，不无限猜数。
        def mismatch(v):
            if not v or any(v.get(k) is None for k in ["on_hand", "reserved", "damaged", "reported_available"]):
                return True
            return abs(v["on_hand"]-v["reserved"]-v["damaged"]-v["reported_available"]) > 0.001
        remaining = 2 + (0 if "sales" in data else 1)
        if any(mismatch(v) for v in data["inventory"]["items"].values()) and state["calls"] < settings.app.max_tool_calls-remaining:
            store.event(state["task_id"], "inventory_agent", "发现库存缺失或口径差异，绕过缓存补查一次", kind="action", agent="inventory_agent")
            data["inventory"] = await query(state, "inventory_snapshot", fresh=True, owner="inventory_agent")
        narrative, status = await interpret(settings.deepseek, data["inventory"], "库存核查 Agent")
        notes = {**state.get("agent_notes", {}), "inventory": dict(narrative=narrative, status=status)}
        store.event(state["task_id"], "inventory_agent", status, kind="result", agent="inventory_agent")
        return dict(data=data, calls=state["calls"], agent_notes=notes)

    async def replenishment_agent(state):
        data = dict(state["data"])
        store.event(state["task_id"], "replenishment_agent", "查询已订未到货，避免把同一批货重复计算", kind="action", agent="replenishment_agent")
        data["incoming"] = await query(state, "incoming_orders", owner="replenishment_agent")
        store.event(state["task_id"], "replenishment_agent", "查询供应商送货时间，准备计算目标库存", kind="action", agent="replenishment_agent")
        data["supplier"] = await query(state, "supplier_terms", owner="replenishment_agent")
        recommendations, gaps = calculate(AnalysisRequest.model_validate(state["request"]), data)
        store.event(state["task_id"], "replenishment_agent", "已完成补货数量计算，缺失资料已列入待核实清单", kind="result", agent="replenishment_agent", recommendation_count=len(recommendations), gap_count=len(gaps))
        return dict(data=data, calls=state["calls"], recommendations=recommendations, gaps=gaps)

    async def verify(state):
        req = AnalysisRequest.model_validate(state["request"])
        checks = cross_check(req, state["data"], state["recommendations"])
        store.event(state["task_id"], "cross_check", "独立回算销量、库存、在途和建议数量", kind="action", agent="cross_check")
        narrative, status = await interpret(settings.deepseek,
            dict(recommendations=state["recommendations"], gaps=state["gaps"], checks=checks,
                 agent_notes=state["agent_notes"], question=req.question), "补货建议 Agent")
        report = Report(task_id=state["task_id"], request=req,
            recommendations=state["recommendations"], evidence=[v["evidence"] for v in state["data"].values()],
            gaps=state["gaps"], checks=checks, narrative=narrative, llm_status=status, generated_at=utcnow())
        store.event(state["task_id"], "cross_check", "口径和数值核对完成", kind="result", agent="cross_check", check_count=len(checks))
        return dict(checks=checks, report=report.model_dump(mode="json"))

    def human_review(state):
        # LangGraph 将暂停点写入 SQLite；重启进程后仍可用 Command(resume=...) 恢复。
        store.event(state["task_id"], "human_review", "建议已准备好，请核对数量和待确认事项", kind="waiting", agent="human_review")
        review = interrupt({"reason": "请审核建议及缺口清单", "report": state["report"]})
        store.event(state["task_id"], "human_review", "已收到你的确认，准备保存审核结果", kind="result", agent="human_review")
        return {"review": ReviewRequest.model_validate(review).model_dump(mode="json")}

    async def archive(state):
        report = dict(state["report"])
        report["review"] = dict(**state["review"], reviewed_at=utcnow())
        report["status"] = "approved" if state["review"]["decision"] == "approve" else "rejected"
        adjustments = {a["sku"]: a for a in state["review"]["adjustments"]}
        # 原始建议保持不变，人工修改作为独立字段留痕。
        report["review"]["final_quantities"] = {r["sku"]: adjustments[r["sku"]]["quantity"] if r["sku"] in adjustments else r["suggested_qty"] for r in report["recommendations"]}
        report = Report.model_validate(report).model_dump(mode="json")
        store.archive(state["task_id"], report)
        return {"report": report}

    graph = StateGraph(State)
    for name, fn in [("supervisor", supervisor), ("sales_agent", sales_agent), ("inventory_agent", inventory_agent), ("replenishment_agent", replenishment_agent), ("cross_check", verify), ("archive", archive)]:
        graph.add_node(name, limited(name, fn))
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", lambda s: s["plan"][0], ["sales_agent", "inventory_agent"])
    graph.add_conditional_edges("sales_agent", lambda s: "replenishment_agent" if "inventory" in s["data"] else "inventory_agent", ["replenishment_agent", "inventory_agent"])
    graph.add_conditional_edges("inventory_agent", lambda s: "replenishment_agent" if "sales" in s["data"] else "sales_agent", ["replenishment_agent", "sales_agent"])
    graph.add_edge("replenishment_agent", "cross_check")
    graph.add_edge("cross_check", "human_review")
    graph.add_edge("human_review", "archive")
    graph.add_edge("archive", END)
    return graph.compile(checkpointer=checkpointer)
