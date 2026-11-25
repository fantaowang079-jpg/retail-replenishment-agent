import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, Date, Float, Integer, MetaData, String, Table, create_engine, select


metadata = MetaData()
sales = Table("sales", metadata,
    Column("id", Integer, primary_key=True), Column("tenant", String(50)),
    Column("warehouse", String(50)), Column("sku", String(50)),
    Column("day", Date), Column("quantity", Float))
inventory = Table("inventory", metadata,
    Column("id", Integer, primary_key=True), Column("tenant", String(50)),
    Column("warehouse", String(50)), Column("sku", String(50)), Column("day", Date),
    Column("on_hand", Float), Column("reserved", Float), Column("damaged", Float),
    Column("reported_available", Float))
purchase_orders = Table("purchase_orders", metadata,
    Column("id", Integer, primary_key=True), Column("tenant", String(50)),
    Column("warehouse", String(50)), Column("sku", String(50)), Column("ordered_day", Date),
    Column("eta", Date), Column("quantity", Float), Column("received", Float), Column("status", String(20)))
suppliers = Table("suppliers", metadata,
    Column("id", Integer, primary_key=True), Column("tenant", String(50)),
    Column("warehouse", String(50)), Column("sku", String(50)), Column("name", String(100)),
    Column("effective_day", Date), Column("lead_days", Integer), Column("pack_size", Integer))
# 来源覆盖声明：没有销售明细不等于零销量，只有声明完整的周期才能补零。
coverage = Table("source_coverage", metadata,
    Column("id", Integer, primary_key=True), Column("tenant", String(50)),
    Column("warehouse", String(50)), Column("sku", String(50)), Column("source", String(30)),
    Column("start_day", Date), Column("end_day", Date))


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourceDB:
    def __init__(self, settings):
        url = settings.app.database_url or f"sqlite:///{(settings.data_path / 'business.db').as_posix()}"
        args = {"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15} if url.startswith("mysql") else {"check_same_thread": False}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        metadata.create_all(self.engine)
        if settings.app.seed_demo:
            self.seed()

    def seed(self):
        today = date.today()
        with self.engine.begin() as conn:
            if conn.execute(select(sales.c.id).limit(1)).first():
                return
            for tenant in ["demo", "other"]:
                for wh in ["WH-SH", "WH-BJ"]:
                    for idx, sku in enumerate(["SKU-001", "SKU-002", "SKU-003"]):
                        scope = dict(tenant=tenant, warehouse=wh, sku=sku)
                        rows = []
                        for offset in range(90):
                            qty = ([10, 1, 6][idx] if offset < 30 else [7, 2, 4][idx])
                            if tenant == "other":
                                qty *= 10
                            if wh == "WH-BJ":
                                qty *= 2
                            rows.append(dict(**scope, day=today-timedelta(days=offset), quantity=qty))
                        conn.execute(sales.insert(), rows)
                        stock = [35, 450, 80][idx]
                        conn.execute(inventory.insert(), dict(**scope, day=today, on_hand=stock+15, reserved=10, damaged=5, reported_available=stock+(12 if idx == 2 else 0)))
                        if idx != 2:  # 演示供应周期缺失。
                            conn.execute(suppliers.insert(), dict(**scope, name=["畅销咖啡", "经典马克杯"][idx], effective_day=today-timedelta(days=100), lead_days=10+idx*4, pack_size=12))
                        conn.execute(purchase_orders.insert(), dict(**scope, ordered_day=today-timedelta(days=3), eta=today+timedelta(days=5), quantity=[40, 0, 30][idx], received=0, status="open"))
                        for source in ["sales", "purchase_orders"]:
                            conn.execute(coverage.insert(), dict(**scope, source=source, start_day=today-timedelta(days=89), end_day=today))

    def demo_date(self):
        with self.engine.connect() as conn:
            result = conn.execute(select(inventory.c.day).where(inventory.c.tenant == "demo").order_by(inventory.c.day.desc()).limit(1)).scalar()
            return result or date.today()


class Store:
    """任务、事件、审核记录落本地盘；每个人独立部署独立 data 目录。"""
    def __init__(self, directory: Path):
        self.path = directory / "tasks.db"
        self.archives = directory / "archives"
        self.archives.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
                report TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_task ON events(task_id, id);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, task_id, request):
        now = utcnow()
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL, ?, ?)", (task_id, "queued", json.dumps(request, ensure_ascii=False), now, now))
        self.event(task_id, "system", "任务已创建")

    def update(self, task_id, status, report=None, error=None):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status=?, report=COALESCE(?, report), error=?, updated_at=? WHERE id=?",
                         (status, json.dumps(report, ensure_ascii=False) if report else None, error, utcnow(), task_id))

    def get(self, task_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in ["request", "report"]:
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def list(self):
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT id,status,request,created_at,updated_at FROM tasks ORDER BY created_at DESC LIMIT 50")]
        for row in rows:
            row["request"] = json.loads(row["request"])
        return rows

    def event(self, task_id, node, message, **extra):
        payload = dict(node=node, message=message, time=utcnow(), **extra)
        with self.connect() as conn:
            conn.execute("INSERT INTO events(task_id,payload) VALUES (?,?)", (task_id, json.dumps(payload, ensure_ascii=False)))

    def events(self, task_id, after=0):
        with self.connect() as conn:
            return [dict(id=r["id"], **json.loads(r["payload"])) for r in conn.execute("SELECT id,payload FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 500", (task_id, after))]

    def recover(self):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status='interrupted' WHERE status IN ('running','queued')")

    def archive(self, task_id, report):
        path = self.archives / f"{task_id}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
