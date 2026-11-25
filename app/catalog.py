"""业务名称集中维护。稳定的数据编号不展示给使用者，也不改写旧报告。"""
SHOPS = {"demo": "邻里生活超市", "other": "悦享生活百货"}
WAREHOUSES = {"WH-SH": "上海中心仓", "WH-BJ": "北京中心仓"}
PRODUCTS = {
    "SKU-001": {"name": "挂耳咖啡", "spec": "十袋装", "unit": "盒", "story": "卖得快，看看现货能否撑到下一批到货。"},
    "SKU-002": {"name": "陶瓷马克杯", "spec": "经典白色款", "unit": "个", "story": "仓库积压较多，看看是否应该暂停进货。"},
    "SKU-003": {"name": "茉莉花茶礼盒", "spec": "双罐礼盒装", "unit": "盒", "story": "库存记录有出入，先核对清楚再决定买多少。"},
}
SCENARIOS = {"shortage": "日常补货", "slow": "清理积压", "promotion": "促销备货"}
TOOLS = {"sales_summary": "销售记录汇总", "inventory_snapshot": "仓库库存核对", "incoming_orders": "已订未到货查询", "supplier_terms": "供应商送货规则"}
NODES = {"system": "受理需求", "supervisor": "安排分析", "sales_agent": "看销售", "inventory_agent": "查库存", "replenishment_agent": "算补货", "cross_check": "复核结果", "human_review": "等你确认", "archive": "保存报告", "tool": "查业务数据", "error": "需要处理"}
AGENTS = {
    "supervisor": {"name": "分析安排", "description": "根据你选择的场景安排先看销售还是先查库存。"},
    "sales_agent": {"name": "销售分析", "description": "汇总统计期销量，计算每天平均卖多少，并和上一段时间比较。"},
    "inventory_agent": {"name": "库存核查", "description": "核对实物、已预留、损坏和业务系统可售库存，发现对不上会重新查询。"},
    "replenishment_agent": {"name": "补货建议", "description": "查询已订未到货和供应商送货时间，按统一口径计算补货数量。"},
    "cross_check": {"name": "交叉校验", "description": "重新回算关键数字，检查来源、日期和商品范围是否一致。"},
    "human_review": {"name": "人工确认", "description": "把建议交给你审核；缺资料的商品必须由你决定是否暂不采购。"},
}
TABLES = {"sales": "商品销售流水", "inventory": "仓库库存记录", "purchase_orders": "采购到货记录", "suppliers": "供应商供货资料"}
WORDS = {
    **SHOPS, **WAREHOUSES, **{k: v["name"] for k, v in PRODUCTS.items()}, **TOOLS, **NODES,
    "未配置 API Key": "未填写模型密钥", "DeepSeek": "深度求索", "ERP": "业务系统",
    "Pandas": "程序", "SKU": "商品", "租户": "商家", "检查点已保存": "进度已保存", "检查点": "保存的进度",
    "独立标量回算": "独立重新计算", "max(0,目标−可售−有效在途)": "目标库存减去可售库存及有效在途（不足零按零计）",
    "供应周期": "供应商送货天数", "复核周期": "距下次盘点天数", "安全天数": "额外备用天数",
    "有效在途": "预计及时到货量", "可售库存": "现在能卖的库存", "阻断项": "必须核实的问题",
}


def chinese(text):
    text = str(text or "")
    for key in sorted(WORDS, key=len, reverse=True):
        text = text.replace(key, WORDS[key])
    return text


def product(sku, fallback=None):
    return PRODUCTS.get(sku, {"name": fallback or sku, "unit": "件", "spec": "", "story": ""})
