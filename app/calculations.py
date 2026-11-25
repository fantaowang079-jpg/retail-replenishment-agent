import math
from datetime import date, timedelta

import pandas as pd

from .schemas import AnalysisRequest, Gap, Recommendation
from .catalog import product


FORMULA = "日均销量=本期销量/自然日；可售=实物−锁定−残次；目标=日均×活动系数×(供应周期+安全天数+复核周期)；建议=max(0,目标−可售−有效在途)，按包装量向上取整。有效在途仅计统计日之后、供应周期内到货的未收货量。"


def calculate(req: AnalysisRequest, data: dict):
    gaps, results = [], []
    factor = req.promotion_multiplier if req.scenario.value == "promotion" else 1.0
    for sku in req.skus:
        sku_gaps = []
        def gap(field, message, blocking=True):
            sku_gaps.append(Gap(sku=sku, field=field, message=message, blocking=blocking))
        s = data["sales"]["items"][sku]
        inv = data["inventory"]["items"][sku]
        supplier = data["supplier"]["items"][sku]
        po = data["incoming"]["items"][sku]
        sales_ok = s["complete"] and s["valid"]
        if not sales_ok:
            gap("sales", "销售数据覆盖不完整或含无效数量，不能将缺失销量当作零")
        if not s["previous_complete"]:
            gap("previous_sales", "上个等长周期销售数据不完整，不计算增长率", False)
        available = None
        reported = None
        if not inv:
            gap("inventory", "缺少库存快照")
        elif inv["duplicate"]:
            gap("inventory", "同一日期出现重复库存快照")
        else:
            amounts = pd.to_numeric(pd.Series([inv["on_hand"], inv["reserved"], inv["damaged"]]), errors="coerce")
            if amounts.isna().any() or not amounts.map(math.isfinite).all() or (amounts < 0).any():
                gap("inventory", "库存分量缺失、为负或不是有效数值")
            else:
                available = float(amounts.iloc[0] - amounts.iloc[1:].sum())
                reported = inv["reported_available"]
                if available < 0:
                    gap("available_stock", "锁定和残次合计超过实物库存")
                if reported is None or not math.isfinite(float(reported)) or abs(available-float(reported)) > 0.001:
                    gap("reported_available", "ERP 可售库存与统一口径不一致，已重新查询仍需确认")
            if inv["day"] != str(req.end_date):
                gap("snapshot_date", "库存快照不在统计截止日，不能混用不同日期")
        lead, pack = None, None
        if not supplier:
            gap("lead_days", "缺少供应周期和采购包装量")
        elif supplier["duplicate"] or supplier["lead_days"] is None or supplier["pack_size"] is None:
            gap("supplier", "供应规则重复或缺失")
        else:
            lead, pack = int(supplier["lead_days"]), int(supplier["pack_size"])
            if lead < 0 or pack <= 0:
                gap("supplier", "供应周期或包装量无效")
        if not po["complete"]:
            gap("incoming", "在途采购数据未声明完整，不能将缺失记录当作零")
        total, eligible = 0.0, 0.0
        incoming_valid = True
        for order in po["orders"]:
            q, received = order["quantity"], order["received"]
            if q is None or received is None or not all(math.isfinite(float(x)) for x in [q, received]) or q < 0 or received < 0 or received > q:
                gap("incoming", "采购数量或已收货数量无效")
                incoming_valid = False
                continue
            remaining = float(pd.Series([q, -received]).sum())
            total += remaining
            if not order["eta"]:
                gap("eta", "在途采购缺少预计到货日期")
                continue
            eta = date.fromisoformat(order["eta"])
            if remaining and eta <= req.end_date:
                gap("overdue_order", "存在逾期未收货采购，未计入有效在途", False)
            if lead is not None and req.end_date < eta <= req.end_date+timedelta(days=max(lead, 0)):
                eligible += remaining
        daily = s["daily"] if sales_ok else None
        cover = available/daily if daily and available is not None else None
        target, suggested = None, None
        if not any(g.blocking for g in sku_gaps):
            frame = pd.DataFrame([dict(daily=daily, factor=factor, horizon=lead+req.safety_days+req.review_days, available=available, incoming=eligible)])
            frame["target"] = frame.daily * frame.factor * frame.horizon
            frame["net"] = (frame.target - frame.available - frame.incoming).clip(lower=0)
            target = float(frame.target.iloc[0])
            suggested = int(math.ceil(float(frame.net.iloc[0])/pack)*pack)
        risk = "待确认" if any(g.blocking for g in sku_gaps) else (
            "滞销风险" if (daily == 0 and available > 0) or (cover is not None and cover > 90) else
            "缺货风险" if cover is not None and cover < lead else "库存正常")
        results.append(Recommendation(sku=sku, name=product(sku, supplier["name"] if supplier else sku)["name"],
            sales_qty=s["quantity"] if sales_ok else None,
            previous_sales_qty=s["previous"] if s["previous_complete"] and s["valid"] else None,
            growth_percent=round(s["growth"], 2) if s["previous_complete"] and s["valid"] and s["growth"] is not None and sales_ok else None,
            daily_sales=daily, available_stock=available, reported_available=reported,
            coverage_days=round(cover, 2) if cover is not None else None,
            incoming_qty=total if po["complete"] and incoming_valid else None, eligible_incoming_qty=eligible if po["complete"] and incoming_valid and lead is not None else None,
            lead_days=lead, pack_size=pack, target_stock=target, suggested_qty=suggested, risk=risk, formula=FORMULA))
        gaps.extend(sku_gaps)
    return [r.model_dump() for r in results], [g.model_dump() for g in gaps]


def cross_check(req, data, recommendations):
    """独立用标量回算，核对 Pandas 输出及所有证据的租户/仓库。"""
    for source in data.values():
        evidence = source["evidence"]
        if evidence["tenant"] != req.tenant or evidence["warehouse"] != req.warehouse:
            raise ValueError("数据来源口径串租户或仓库")
    for row in recommendations:
        if row["suggested_qty"] is None:
            continue
        sku = row["sku"]
        inv = data["inventory"]["items"][sku]
        stock = inv["on_hand"]-inv["reserved"]-inv["damaged"]
        daily = row["sales_qty"] / ((req.end_date-req.start_date).days+1)
        factor = req.promotion_multiplier if req.scenario.value == "promotion" else 1
        horizon = row["lead_days"]+req.safety_days+req.review_days
        orders = data["incoming"]["items"][sku]["orders"]
        incoming = sum(o["quantity"]-o["received"] for o in orders if o["eta"] and req.end_date < date.fromisoformat(o["eta"]) <= req.end_date+timedelta(days=row["lead_days"]))
        pack = data["supplier"]["items"][sku]["pack_size"]
        expected = math.ceil(max(0, daily*factor*horizon-stock-incoming)/pack)*pack
        if expected != row["suggested_qty"] or abs(stock-row["available_stock"]) > 0.001:
            raise ValueError("独立数值回算失败，禁止提交建议")
    return ["租户、仓库与 SKU 范围校验通过", "统计期采用含起止日的自然日；对比期为前一个等长周期", "库存与供应规则使用统计截止日及之前的数据", "Pandas 计算结果已通过独立标量回算", "缺失或冲突数据已加入缺口清单；阻断项不生成补货数量"]
