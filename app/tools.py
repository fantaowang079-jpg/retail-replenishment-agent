import asyncio
import hashlib
import json
import math
import time
from datetime import timedelta

import pandas as pd
from sqlalchemy import select

from .database import sales, inventory, purchase_orders, suppliers, coverage, utcnow


class QueryTools:
    def __init__(self, source, settings):
        self.source, self.settings = source, settings
        self.memory = {}
        self.redis = None
        if settings.app.redis_url:
            from redis.asyncio import Redis
            self.redis = Redis.from_url(settings.app.redis_url, socket_connect_timeout=1, socket_timeout=1)

    async def close(self):
        if self.redis:
            await self.redis.aclose()

    async def query(self, tool, req, fresh=False):
        # 包含租户/仓库/SKU/完整请求，缓存绝不跨口径复用。
        key = "retail:v1:" + hashlib.sha256((tool + req.model_dump_json()).encode()).hexdigest()
        ttl = self.settings.app.cache_ttl_seconds
        if not fresh and ttl:
            try:
                cached = await self.redis.get(key) if self.redis else None
                if cached:
                    result = json.loads(cached)
                    result["evidence"]["cached"] = True
                    return result
            except Exception:
                pass
            if key in self.memory and self.memory[key][0] > time.monotonic():
                result = json.loads(self.memory[key][1])
                result["evidence"]["cached"] = True
                return result
        result = await asyncio.to_thread(self._read, tool, req)
        encoded = json.dumps(result, ensure_ascii=False)
        if ttl:
            # 轻量有界内存缓存，Redis 不可用时自动回退。
            if len(self.memory) >= 256:
                self.memory.pop(next(iter(self.memory)))
            self.memory[key] = (time.monotonic()+ttl, encoded)
            if self.redis:
                try:
                    await self.redis.setex(key, ttl, encoded)
                except Exception:
                    pass
        return result

    def _read(self, tool, req):
        table = {"sales_summary": sales, "inventory_snapshot": inventory, "incoming_orders": purchase_orders, "supplier_terms": suppliers}[tool]
        stmt = select(table).where(table.c.tenant == req.tenant, table.c.warehouse == req.warehouse, table.c.sku.in_(req.skus))
        days = (req.end_date - req.start_date).days + 1
        previous_start = req.start_date - timedelta(days=days)
        if table is sales:
            stmt = stmt.where(sales.c.day.between(previous_start, req.end_date))
        elif table is inventory:
            stmt = stmt.where(inventory.c.day <= req.end_date)
        elif table is purchase_orders:
            stmt = stmt.where(purchase_orders.c.ordered_day <= req.end_date)
        else:
            stmt = stmt.where(suppliers.c.effective_day <= req.end_date)
        with self.source.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
            covers = [dict(r) for r in conn.execute(select(coverage).where(coverage.c.tenant == req.tenant, coverage.c.warehouse == req.warehouse, coverage.c.sku.in_(req.skus))).mappings()]
        items = {}
        frame = pd.DataFrame(rows)
        for sku in req.skus:
            subset = frame[frame.sku == sku] if not frame.empty else frame
            def complete(source, start, end):
                return any(c["sku"] == sku and c["source"] == source and c["start_day"] <= start and c["end_day"] >= end for c in covers)
            if table is sales:
                current = subset[subset.day >= req.start_date] if not subset.empty else subset
                previous = subset[subset.day < req.start_date] if not subset.empty else subset
                valid = subset.empty or bool(pd.to_numeric(subset.quantity, errors="coerce").map(lambda v: pd.notna(v) and math.isfinite(v) and v >= 0).all())
                qty = float(current.quantity.sum()) if not current.empty else 0.0
                prev = float(previous.quantity.sum()) if not previous.empty else 0.0
                items[sku] = dict(quantity=qty, previous=prev, daily=qty/days, days=days, valid=valid,
                                  complete=complete("sales", req.start_date, req.end_date), previous_complete=complete("sales", previous_start, req.start_date-timedelta(days=1)),
                                  growth=(qty-prev)/prev*100 if prev else None)
            elif table is inventory:
                if subset.empty:
                    items[sku] = None
                else:
                    latest = subset[subset.day == subset.day.max()]
                    items[sku] = dict(latest.iloc[0].to_dict(), duplicate=len(latest) != 1)
            elif table is suppliers:
                if subset.empty:
                    items[sku] = None
                else:
                    latest = subset[subset.effective_day == subset.effective_day.max()]
                    items[sku] = dict(latest.iloc[0].to_dict(), duplicate=len(latest) != 1)
            else:
                active = subset[subset.status == "open"] if not subset.empty else subset
                items[sku] = dict(orders=active.to_dict("records"), complete=complete("purchase_orders", req.end_date, req.end_date))
        # 将 SQL NULL / Pandas NaN 保留为缺失值，不能导致整个任务异常或补成零。
        def clean(value):
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, float) and not math.isfinite(value):
                return None
            return value
        return json.loads(json.dumps(clean(dict(items=items, evidence=dict(tool=tool, table=table.name,
            tenant=req.tenant, warehouse=req.warehouse,
            period=f"{previous_start} ~ {req.end_date}" if table is sales else f"截至 {req.end_date}",
            rows=len(rows), cached=False, fetched_at=utcnow()))), default=str, allow_nan=False))
