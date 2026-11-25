"""把已核验的结果翻译成业务人员能读懂的行动说明，不调用模型算数。"""
from html import escape

from .catalog import SHOPS, WAREHOUSES, SCENARIOS, TOOLS, chinese, product


def number(value):
    return "待核实" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")


def present_report(report):
    req = report["request"]
    cards = []
    for row in report["recommendations"]:
        p = product(row["sku"], row["name"])
        unit, qty = p["unit"], row["suggested_qty"]
        gaps = [chinese(g["message"]) for g in report["gaps"] if g["sku"] == row["sku"]]
        if qty is None:
            action, tone = "先核实，暂不生成补货数量", "uncertain"
            why = "；".join(gaps) or "当前资料不足，请先补齐业务数据。"
            if row["available_stock"] is not None and row["reported_available"] is not None and row["available_stock"] != row["reported_available"]:
                why = f"业务系统记为 {number(row['reported_available'])} {unit}，按实物减去已预留及损坏商品计算只有 {number(row['available_stock'])} {unit}。" + why
        elif row["risk"] == "滞销风险" and qty == 0:
            action, tone = "先卖库存，暂停补货", "slow"
            why = (f"目前还有 {number(row['available_stock'])} {unit}，按历史销量大约能卖 {number(row['coverage_days'])} 天。可以考虑陈列调整或促销，再观察销量。" if row["coverage_days"] is not None else "统计期内没有销量，应先了解原因，再决定如何处理库存。")
        elif qty > 0:
            action, tone = f"建议补 {number(qty)} {unit}", "urgent"
            why = f"按历史速度每天卖 {number(row['daily_sales'])} {unit}，现货还能卖约 {number(row['coverage_days'])} 天；供应商送货需要 {number(row['lead_days'])} 天。"
            if row["risk"] == "缺货风险":
                why += "到货前可能断货，建议联系供应商加急或从其他仓库调货。"
            else:
                why += "建议补足下次盘点前的预期需求和备用库存。"
        else:
            action, tone = "库存够用，暂不补货", "normal"
            why = "按当前销量、可售库存和预计到货量计算，本次无需新增采购。"
        calculation = ""
        if qty is not None:
            factor = req["promotion_multiplier"] if req["scenario"] == "promotion" else 1
            calculation = f"目标库存：每天 {number(row['daily_sales'])} {unit} × {number(factor)} 倍销量 ×（送货 {row['lead_days']} 天 + 备用 {req['safety_days']} 天 + 距下次盘点 {req['review_days']} 天）= {number(row['target_stock'])} {unit}。再减去现货 {number(row['available_stock'])} {unit}和预计及时到货的 {number(row['eligible_incoming_qty'])} {unit}。"
            if row.get("pack_size"):
                calculation += f"不足零按零计，按每箱 {row['pack_size']} {unit}向上凑整，得到 {number(qty)} {unit}。"
            else:
                calculation += f"不足零按零计，按供应商包装数量向上凑整，得到 {number(qty)} {unit}。"
        cards.append(dict(sku=row["sku"], **p, action=action, tone=tone, why=why, calculation=calculation))
    return dict(shop=SHOPS.get(req["tenant"], req["tenant"]), warehouse=WAREHOUSES.get(req["warehouse"], req["warehouse"]), scenario=SCENARIOS.get(req["scenario"], "经营分析"), cards=cards)


def readable_report(report):
    view = present_report(report)
    e = lambda value: escape(str(value))
    cards = "".join(f"<section><h2>{e(c['name'])}：{e(c['action'])}</h2><p>{e(c['why'])}</p><p>{e(c['calculation'])}</p></section>" for c in view["cards"])
    gaps = "".join(f"<li>{e(product(g['sku'])['name'])}：{e(chinese(g['message']))}</li>" for g in report["gaps"])
    sources = "".join(f"<li>{e(TOOLS.get(s['tool'], '业务数据'))}：{e(s['period'])}，共 {s['rows']} 条；获取时间 {e(s['fetched_at'])}。</li>" for s in report["evidence"])
    review = report.get("review") or {}
    status = "审核通过" if report["status"] == "approved" else "已驳回"
    quantities = "".join(f"<li>{e(product(k)['name'])}：{e(number(v))} {e(product(k)['unit'])}</li>" for k,v in review.get("final_quantities", {}).items())
    changes = "".join(f"<li>{e(product(a['sku'])['name'])}：改为 {a['quantity']} {e(product(a['sku'])['unit'])}，依据：{e(a['reason'])}</li>" for a in review.get("adjustments", []))
    checks = "".join(f"<li>{e(chinese(c))}</li>" for c in report["checks"])
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>补货建议与审核记录</title>
    <style>body{{max-width:900px;margin:40px auto;padding:0 24px;font:16px/1.9 system-ui;color:#263d31}}h1{{font-size:28px}}h2{{font-size:20px}}section{{border-top:1px solid #ddd;padding:16px 0}}small{{color:#687b6b}}@media print{{body{{margin:0}}}}</style>
    <h1>补货建议与审核记录</h1><p>{e(view['shop'])} · {e(view['warehouse'])} · {e(view['scenario'])}<br>统计日期：{e(report['request']['start_date'])} 至 {e(report['request']['end_date'])}</p>
    <p>审核结果：{status}。所有系统建议见下方；人工确认的数量以审核记录为准。系统未下采购单。</p>{cards}
    <section><h2>需要核实的问题</h2><ul>{gaps or '<li>未发现缺口。</li>'}</ul></section>
    <section><h2>分析说明</h2><p>{e(chinese(report['narrative']['overview']))}</p><p>{e(chinese(report['narrative']['reasoning']))}</p><p>{e(chinese(report['narrative']['cautions']))}</p></section>
    <section><h2>人工审核记录</h2><p>审核人：{e(review.get('reviewer',''))}；审核时间：{e(review.get('reviewed_at',''))}<br>说明：{e(review.get('note',''))}</p><h3>{'确认后的数量' if report['status']=='approved' else '驳回时的参考数量，不作为采购安排'}</h3><ul>{quantities}</ul><h3>人工调整及依据</h3><ul>{changes or '<li>未调整数量。</li>'}</ul></section>
    <section><h2>核对记录</h2><ul>{checks}</ul><h2>数据来源</h2><ul>{sources}</ul></section><small>这是演示业务数据形成的建议。下载后可直接用浏览器打开，也可打印保存。</small></html>"""
