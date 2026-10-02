# -*- coding: utf-8 -*-
"""Mis Finanzas — app personal para seguir ingresos, gastos y pagos a compañías.

PWA instalable en la pantalla principal. Moneda: USD.
Backend Flask + SQLite local / réplica embebida libsql (Turso) en producción.
"""
import calendar as _calendar
import datetime as _dt
import os
import sqlite3

from flask import Flask, g, jsonify, request, send_from_directory

try:
    import libsql
    _HAS_LIBSQL = True
except ImportError:  # pragma: no cover
    libsql = None
    _HAS_LIBSQL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("FINANZAS_DB_PATH") or os.path.join(DATA_DIR, "finanzas.db")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# ---------------- Base de datos ----------------
# En Render (plan gratuito) el disco es temporal: cada despliegue borra el
# archivo SQLite local. Si existen TURSO_URL y TURSO_TOKEN, la app usa Turso
# (réplica embebida libsql, compatible con SQLite) y los datos sobreviven a
# los despliegues. Sin esas variables, usa SQLite local.


class _Row:
    """Fila compatible con sqlite3.Row: acceso por índice y por nombre."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = tuple(vals)

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._cols.index(key)
        return self._vals[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self._cols = [d[0] for d in (cur.description or [])]

    def _wrap(self, row):
        return _Row(self._cols, row) if row is not None else None

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        # Siempre fetchall(): el Cursor de libsql (producción) no es iterable,
        # a diferencia del de sqlite3.
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for r in self._cur.fetchall():
            yield self._wrap(r)

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount


class _TursoConn:
    """Conexión libsql con la misma interfaz que la app espera de sqlite3."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _Cursor(self._conn.execute(sql, params))

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()

    def sync(self):
        return self._conn.sync()


def _turso_sync_url():
    url = (os.environ.get("TURSO_URL") or "").strip().rstrip("/")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url


def _turso_enabled():
    return (
        _HAS_LIBSQL
        and bool(_turso_sync_url())
        and bool(os.environ.get("TURSO_TOKEN"))
    )


def _connect_db():
    if _turso_enabled():
        try:
            conn = libsql.connect(
                DB_PATH,
                sync_url=_turso_sync_url(),
                auth_token=os.environ.get("TURSO_TOKEN"),
            )
            wrapped = _TursoConn(conn)
            try:
                wrapped.sync()
            except Exception as e:
                print(f"[DB] Turso sync inicial falló: {e}", flush=True)
            return wrapped
        except Exception as e:
            print(f"[DB] No se pudo conectar a Turso, usando SQLite local: {e}", flush=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            if isinstance(db, _TursoConn):
                db.sync()
        except Exception as e:
            print(f"[DB] Turso sync final falló: {e}", flush=True)
        db.close()


DEFAULT_CATEGORIES = [
    # (name, type, color, sort)
    ("Trabajo", "income", "#22c55e", 1),
    ("Ventas", "income", "#16a34a", 2),
    ("Otros ingresos", "income", "#86efac", 3),
    ("Vivienda", "expense", "#3b82f6", 1),
    ("Alimentación", "expense", "#22c55e", 2),
    ("Transporte", "expense", "#f59e0b", 3),
    ("Servicios", "expense", "#8b5cf6", 4),
    ("Ocio", "expense", "#ec4899", 5),
    ("Salud", "expense", "#ef4444", 6),
    ("Otros gastos", "expense", "#94a3b8", 7),
]


def init_db():
    print(
        "[DB] modo=%s libsql=%s TURSO_URL=%s TURSO_TOKEN=%s"
        % (
            "turso" if _turso_enabled() else "local",
            _HAS_LIBSQL,
            "sí" if _turso_sync_url() else "no",
            "sí" if os.environ.get("TURSO_TOKEN") else "no",
        ),
        flush=True,
    )
    db = _connect_db()
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,           -- income | expense
                amount REAL NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,           -- YYYY-MM-DD
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,           -- income | expense
                color TEXT NOT NULL DEFAULT '#94a3b8',
                sort INTEGER NOT NULL DEFAULT 99
            );
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                amount REAL NOT NULL,
                due_date TEXT NOT NULL,       -- YYYY-MM-DD
                paid INTEGER NOT NULL DEFAULT 0,
                paid_date TEXT,
                recurring INTEGER NOT NULL DEFAULT 1,
                parent_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_bills_due ON bills(due_date);
            CREATE TABLE IF NOT EXISTS budget_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,           -- income | expense
                label TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                sort INTEGER NOT NULL DEFAULT 99
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,           -- income | expense
                title TEXT NOT NULL,
                amount REAL NOT NULL,
                freq TEXT NOT NULL,           -- weekly | biweekly | monthly
                weekday INTEGER,              -- 0=Lun..6=Dom (weekly)
                anchor_date TEXT,             -- YYYY-MM-DD (biweekly)
                day INTEGER,                  -- 1..31 (monthly)
                category TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )
        # Migración: la factura pagada genera un movimiento de gasto (tx_id)
        bcols = [r["name"] for r in db.execute("PRAGMA table_info(bills)").fetchall()]
        if "tx_id" not in bcols:
            db.execute("ALTER TABLE bills ADD COLUMN tx_id INTEGER")
        # Categorías iniciales (solo si la tabla está vacía)
        n = db.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
        if n == 0:
            db.executemany(
                "INSERT INTO categories (name, type, color, sort) VALUES (?, ?, ?, ?)",
                DEFAULT_CATEGORIES,
            )
            print("[DB] categorías iniciales creadas", flush=True)
        # Presupuesto inicial (solo si la tabla está vacía)
        nb = db.execute("SELECT COUNT(*) AS c FROM budget_items").fetchone()["c"]
        if nb == 0:
            budget_seed = [
                ("income", "Simple delicious", 4000, 1),
                ("income", "Hashi", 2200, 2),
                ("income", "Uber", 1200, 3),
                ("expense", "Vivienda (renta, hipoteca, impuestos, seguro)", 443, 1),
                ("expense", "Alimentación", 600, 2),
                ("expense", "Gas", 400, 3),
                ("expense", "Chase loan", 233, 4),
                ("expense", "Teléfono", 261, 5),
                ("expense", "Seguro", 185, 6),
                ("expense", "Emily", 230, 7),
                ("expense", "Abuela", 265, 8),
                ("expense", "Bill", 50, 9),
                ("expense", "Papi", 150, 10),
                ("expense", "Tarjeta del dentista", 275, 11),
                ("expense", "Cámara affirm", 113, 12),
                ("expense", "Hoduras", 0, 13),
                ("expense", "Personal", 0, 14),
                ("expense", "Casa", 0, 15),
                ("expense", "Car Shaw", 0, 16),
                ("expense", "Disney", 8, 17),
                ("expense", "Apple", 3, 18),
                ("expense", "Extras", 0, 19),
            ]
            db.executemany(
                "INSERT INTO budget_items (kind, label, amount, sort) VALUES (?, ?, ?, ?)",
                budget_seed,
            )
            print("[DB] presupuesto inicial creado", flush=True)
        # Ingresos programados (solo si la tabla está vacía)
        ns = db.execute("SELECT COUNT(*) AS c FROM schedules").fetchone()["c"]
        if ns == 0:
            now = _dt.datetime.now().isoformat(timespec="seconds")
            sched_seed = [
                # kind, title, amount, freq, weekday, anchor_date, day, category
                ("income", "Simple delicious", 1000, "weekly", 3, None, None, "Trabajo"),
                ("income", "Uber", 300, "weekly", 6, None, None, "Trabajo"),
                ("income", "Hashi", 1100, "biweekly", None, "2026-09-27", None, "Trabajo"),
            ]
            for kind, title, amount, freq, wd, anchor, day, cat in sched_seed:
                db.execute(
                    "INSERT INTO schedules (kind, title, amount, freq, weekday,"
                    " anchor_date, day, category, active, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (kind, title, amount, freq, wd, anchor, day, cat, now),
                )
            print("[DB] ingresos programados creados", flush=True)
        db.commit()
    finally:
        try:
            if isinstance(db, _TursoConn):
                db.sync()
        except Exception:
            pass
        db.close()


# ---------------- utilidades ----------------

def _today():
    return _dt.date.today().isoformat()


def _parse_ym(year, month):
    try:
        y, m = int(year), int(month)
        if not (1900 <= y <= 2100 and 1 <= m <= 12):
            raise ValueError
        return y, m
    except (TypeError, ValueError):
        now = _dt.date.today()
        return now.year, now.month


def _month_bounds(y, m):
    first = f"{y:04d}-{m:02d}-01"
    last_day = _calendar.monthrange(y, m)[1]
    last = f"{y:04d}-{m:02d}-{last_day:02d}"
    return first, last


def _add_months(date_str, months):
    y, m, d = (int(x) for x in date_str.split("-"))
    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    last_day = _calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-{min(d, last_day):02d}"


def _schedule_occurrences(sched, y, m):
    """Fechas (YYYY-MM-DD) del mes y/m en que cae un ingreso/gasto programado."""
    freq = sched["freq"]
    first, last = _month_bounds(y, m)
    f = _dt.date.fromisoformat(first)
    l = _dt.date.fromisoformat(last)
    out = []
    if freq == "weekly" and sched["weekday"] is not None:
        wd = int(sched["weekday"])  # 0=Lun..6=Dom
        d = f
        while d.weekday() != wd:
            d += _dt.timedelta(days=1)
        while d <= l:
            out.append(d.isoformat())
            d += _dt.timedelta(days=7)
    elif freq == "biweekly" and sched["anchor_date"]:
        anchor = _dt.date.fromisoformat(sched["anchor_date"])
        if anchor <= l:
            d = anchor
            while d < f:
                d += _dt.timedelta(days=15)
            while d <= l:
                out.append(d.isoformat())
                d += _dt.timedelta(days=15)
    elif freq == "monthly" and sched["day"]:
        day = min(int(sched["day"]), _calendar.monthrange(y, m)[1])
        out.append(f"{y:04d}-{m:02d}-{day:02d}")
    return out


def _freq_label(s):
    if s["freq"] == "weekly":
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        wd = s["weekday"]
        return f"Cada {dias[wd]}" if wd is not None else "Semanal"
    if s["freq"] == "biweekly":
        return "Cada 15 días"
    if s["freq"] == "monthly":
        return f"Cada mes (día {s['day']})"
    return s["freq"]


# ---------------- API: presupuesto ----------------

@app.route("/api/budget")
def api_budget_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM budget_items ORDER BY kind DESC, sort, id"
    ).fetchall()
    income = [r for r in rows if r["kind"] == "income"]
    expense = [r for r in rows if r["kind"] == "expense"]

    def j(r):
        return {"id": r["id"], "kind": r["kind"], "label": r["label"],
                "amount": r["amount"]}

    ti = sum(r["amount"] for r in income)
    te = sum(r["amount"] for r in expense)
    return jsonify({
        "income": [j(r) for r in income],
        "expense": [j(r) for r in expense],
        "total_income": ti,
        "total_expense": te,
        "remaining": ti - te,
    })


@app.route("/api/budget", methods=["POST"])
def api_budget_create():
    data = request.get_json(force=True)
    kind = data.get("kind")
    label = (data.get("label") or "").strip()
    if kind not in ("income", "expense") or not label:
        return jsonify({"error": "kind (income|expense) y label requeridos"}), 400
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "monto inválido"}), 400
    if amount < 0:
        return jsonify({"error": "el monto no puede ser negativo"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO budget_items (kind, label, amount, sort) VALUES (?, ?, ?, 99)",
        (kind, label, round(amount, 2)),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/budget/<int:item_id>", methods=["PUT"])
def api_budget_update(item_id):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT id FROM budget_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    fields, params = [], []
    if "label" in data and (data["label"] or "").strip():
        fields.append("label=?")
        params.append(data["label"].strip())
    if "amount" in data:
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError):
            return jsonify({"error": "monto inválido"}), 400
        if amount < 0:
            return jsonify({"error": "el monto no puede ser negativo"}), 400
        fields.append("amount=?")
        params.append(round(amount, 2))
    if fields:
        params.append(item_id)
        db.execute(f"UPDATE budget_items SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/budget/<int:item_id>", methods=["DELETE"])
def api_budget_delete(item_id):
    db = get_db()
    db.execute("DELETE FROM budget_items WHERE id=?", (item_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------- API: programados ----------------

def _sched_json(r):
    return {
        "id": r["id"],
        "kind": r["kind"],
        "title": r["title"],
        "amount": r["amount"],
        "freq": r["freq"],
        "freq_label": _freq_label({
            "freq": r["freq"], "weekday": r["weekday"], "day": r["day"]}),
        "weekday": r["weekday"],
        "anchor_date": r["anchor_date"],
        "day": r["day"],
        "category": r["category"],
        "active": bool(r["active"]),
    }


@app.route("/api/schedules")
def api_sched_list():
    db = get_db()
    rows = db.execute("SELECT * FROM schedules ORDER BY kind DESC, id").fetchall()
    return jsonify([_sched_json(r) for r in rows])


@app.route("/api/schedules", methods=["POST"])
def api_sched_create():
    data = request.get_json(force=True)
    kind = data.get("kind")
    title = (data.get("title") or "").strip()
    freq = data.get("freq")
    if kind not in ("income", "expense") or not title:
        return jsonify({"error": "kind (income|expense) y título requeridos"}), 400
    if freq not in ("weekly", "biweekly", "monthly"):
        return jsonify({"error": "freq debe ser weekly, biweekly o monthly"}), 400
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "monto inválido"}), 400
    if amount <= 0:
        return jsonify({"error": "el monto debe ser mayor a 0"}), 400
    weekday = data.get("weekday")
    anchor_date = (data.get("anchor_date") or "").strip() or None
    day = data.get("day")
    if freq == "weekly":
        try:
            weekday = int(weekday)
            assert 0 <= weekday <= 6
        except (TypeError, ValueError, AssertionError):
            return jsonify({"error": "weekday 0-6 requerido (0=lunes)"}), 400
    if freq == "biweekly":
        try:
            _dt.date.fromisoformat(anchor_date)
        except (ValueError, TypeError):
            return jsonify({"error": "anchor_date inválida (YYYY-MM-DD)"}), 400
    if freq == "monthly":
        try:
            day = int(day)
            assert 1 <= day <= 31
        except (TypeError, ValueError, AssertionError):
            return jsonify({"error": "day 1-31 requerido"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO schedules (kind, title, amount, freq, weekday, anchor_date,"
        " day, category, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (kind, title, round(amount, 2), freq, weekday, anchor_date, day,
         (data.get("category") or "").strip(),
         _dt.datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/schedules/<int:sched_id>", methods=["PUT"])
def api_sched_update(sched_id):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT id FROM schedules WHERE id=?", (sched_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    fields, params = [], []
    if "title" in data and (data["title"] or "").strip():
        fields.append("title=?")
        params.append(data["title"].strip())
    if "amount" in data:
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError):
            return jsonify({"error": "monto inválido"}), 400
        if amount <= 0:
            return jsonify({"error": "el monto debe ser mayor a 0"}), 400
        fields.append("amount=?")
        params.append(round(amount, 2))
    if "active" in data:
        fields.append("active=?")
        params.append(1 if data["active"] else 0)
    if "category" in data:
        fields.append("category=?")
        params.append((data["category"] or "").strip())
    if fields:
        params.append(sched_id)
        db.execute(f"UPDATE schedules SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/schedules/<int:sched_id>", methods=["DELETE"])
def api_sched_delete(sched_id):
    db = get_db()
    db.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
    db.commit()
    return jsonify({"ok": True})


def _tx_json(r):
    return {
        "id": r["id"],
        "type": r["type"],
        "amount": r["amount"],
        "category": r["category"],
        "description": r["description"],
        "date": r["date"],
    }


def _bill_json(r):
    return {
        "id": r["id"],
        "company": r["company"],
        "amount": r["amount"],
        "due_date": r["due_date"],
        "paid": bool(r["paid"]),
        "paid_date": r["paid_date"],
        "recurring": bool(r["recurring"]),
        "notes": r["notes"],
    }


# ---------------- vistas ----------------

@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "templates"), "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "manifest.json")


@app.route("/sw.js")
def sw():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js")


@app.route("/apple-touch-icon.png")
def apple_icon():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "apple-touch-icon.png")


# ---------------- API: dashboard ----------------

@app.route("/api/dashboard")
def api_dashboard():
    y, m = _parse_ym(request.args.get("year"), request.args.get("month"))
    first, last = _month_bounds(y, m)
    db = get_db()

    row = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS income,"
        "       COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS expense"
        " FROM transactions WHERE date BETWEEN ? AND ?",
        (first, last),
    ).fetchone()
    income = row["income"] or 0
    expense = row["expense"] or 0

    # Gastos por categoría (con color)
    cat_rows = db.execute(
        "SELECT t.category AS name, SUM(t.amount) AS total, MAX(c.color) AS color"
        " FROM transactions t LEFT JOIN categories c"
        "   ON c.name = t.category AND c.type='expense'"
        " WHERE t.type='expense' AND t.date BETWEEN ? AND ?"
        " GROUP BY t.category ORDER BY total DESC",
        (first, last),
    ).fetchall()
    by_category = [
        {
            "name": r["name"] or "Sin categoría",
            "total": r["total"],
            "pct": round(100 * r["total"] / expense, 1) if expense else 0,
            "color": r["color"] or "#94a3b8",
        }
        for r in cat_rows
    ]

    # Evolución mensual del año
    monthly = []
    for mm in range(1, 13):
        f, l = _month_bounds(y, mm)
        r = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS i,"
            "       COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS e"
            " FROM transactions WHERE date BETWEEN ? AND ?",
            (f, l),
        ).fetchone()
        monthly.append({"month": mm, "income": r["i"] or 0, "expense": r["e"] or 0})

    year_income = sum(x["income"] for x in monthly)
    year_expense = sum(x["expense"] for x in monthly)

    # Pagos a compañías pendientes (vencidos + próximos 30 días)
    today = _today()
    soon = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    bills_due = db.execute(
        "SELECT COUNT(*) AS c FROM bills WHERE paid=0 AND due_date <= ?",
        (soon,),
    ).fetchone()["c"]

    return jsonify(
        {
            "year": y,
            "month": m,
            "income": income,
            "expense": expense,
            "balance": income - expense,
            "by_category": by_category,
            "monthly": monthly,
            "year_income": year_income,
            "year_expense": year_expense,
            "year_balance": year_income - year_expense,
            "bills_due": bills_due,
            "today": today,
        }
    )


# ---------------- API: transacciones ----------------

@app.route("/api/transactions")
def api_tx_list():
    y, m = _parse_ym(request.args.get("year"), request.args.get("month"))
    first, last = _month_bounds(y, m)
    ttype = request.args.get("type")
    db = get_db()
    if ttype in ("income", "expense"):
        rows = db.execute(
            "SELECT * FROM transactions WHERE date BETWEEN ? AND ? AND type=?"
            " ORDER BY date DESC, id DESC",
            (first, last, ttype),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM transactions WHERE date BETWEEN ? AND ?"
            " ORDER BY date DESC, id DESC",
            (first, last),
        ).fetchall()
    return jsonify([_tx_json(r) for r in rows])


@app.route("/api/transactions", methods=["POST"])
def api_tx_create():
    data = request.get_json(force=True)
    ttype = data.get("type")
    if ttype not in ("income", "expense"):
        return jsonify({"error": "type debe ser income o expense"}), 400
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "monto inválido"}), 400
    if amount <= 0:
        return jsonify({"error": "el monto debe ser mayor a 0"}), 400
    date = (data.get("date") or _today()).strip()
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        return jsonify({"error": "fecha inválida (YYYY-MM-DD)"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO transactions (type, amount, category, description, date, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            ttype,
            round(amount, 2),
            (data.get("category") or "").strip(),
            (data.get("description") or "").strip(),
            date,
            _dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/transactions/<int:tx_id>", methods=["PUT"])
def api_tx_update(tx_id):
    data = request.get_json(force=True)
    db = get_db()
    cur = db.execute("SELECT id FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not cur:
        return jsonify({"error": "no existe"}), 404
    fields, params = [], []
    if data.get("type") in ("income", "expense"):
        fields.append("type=?")
        params.append(data["type"])
    if "amount" in data:
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError):
            return jsonify({"error": "monto inválido"}), 400
        if amount <= 0:
            return jsonify({"error": "el monto debe ser mayor a 0"}), 400
        fields.append("amount=?")
        params.append(round(amount, 2))
    for key in ("category", "description"):
        if key in data:
            fields.append(f"{key}=?")
            params.append((data[key] or "").strip())
    if "date" in data:
        try:
            _dt.date.fromisoformat(data["date"])
        except (ValueError, TypeError):
            return jsonify({"error": "fecha inválida"}), 400
        fields.append("date=?")
        params.append(data["date"])
    if fields:
        params.append(tx_id)
        db.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
def api_tx_delete(tx_id):
    db = get_db()
    db.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------- API: categorías ----------------

@app.route("/api/categories")
def api_cat_list():
    ttype = request.args.get("type")
    db = get_db()
    if ttype in ("income", "expense"):
        rows = db.execute(
            "SELECT * FROM categories WHERE type=? ORDER BY sort, name", (ttype,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM categories ORDER BY type, sort, name").fetchall()
    return jsonify(
        [
            {"id": r["id"], "name": r["name"], "type": r["type"], "color": r["color"]}
            for r in rows
        ]
    )


@app.route("/api/categories", methods=["POST"])
def api_cat_create():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    ttype = data.get("type")
    if not name or ttype not in ("income", "expense"):
        return jsonify({"error": "nombre y type (income|expense) requeridos"}), 400
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO categories (name, type, color, sort) VALUES (?, ?, ?, 99)",
            (name, ttype, (data.get("color") or "#94a3b8").strip()),
        )
        db.commit()
    except Exception:
        return jsonify({"error": "esa categoría ya existe"}), 400
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
def api_cat_delete(cat_id):
    db = get_db()
    row = db.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    used = db.execute(
        "SELECT COUNT(*) AS c FROM transactions WHERE category=?", (row["name"],)
    ).fetchone()["c"]
    if used:
        return jsonify({"error": "tiene movimientos, no se puede borrar"}), 400
    db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------- API: pagos a compañías ----------------

@app.route("/api/bills")
def api_bills_list():
    db = get_db()
    # Pendientes primero (vencidos y próximos), luego pagados recientes
    rows = db.execute(
        "SELECT * FROM bills ORDER BY paid ASC, due_date ASC, id DESC LIMIT 200"
    ).fetchall()
    return jsonify([_bill_json(r) for r in rows])


@app.route("/api/bills", methods=["POST"])
def api_bills_create():
    data = request.get_json(force=True)
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify({"error": "la compañía es requerida"}), 400
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "monto inválido"}), 400
    if amount <= 0:
        return jsonify({"error": "el monto debe ser mayor a 0"}), 400
    due_date = (data.get("due_date") or "").strip()
    try:
        _dt.date.fromisoformat(due_date)
    except ValueError:
        return jsonify({"error": "fecha de pago inválida (YYYY-MM-DD)"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO bills (company, amount, due_date, paid, recurring, notes, created_at)"
        " VALUES (?, ?, ?, 0, ?, ?, ?)",
        (
            company,
            round(amount, 2),
            due_date,
            1 if data.get("recurring", True) else 0,
            (data.get("notes") or "").strip(),
            _dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/bills/<int:bill_id>/pay", methods=["POST"])
def api_bill_pay(bill_id):
    db = get_db()
    row = db.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    if row["paid"]:
        return jsonify({"ok": True})
    today = _today()
    # Al pagar se genera el movimiento de gasto para que el Panel,
    # Movimientos y Calendario lo reflejen.
    cur = db.execute(
        "INSERT INTO transactions (type, amount, category, description, date, created_at)"
        " VALUES ('expense', ?, 'Servicios', ?, ?, ?)",
        (
            round(row["amount"], 2),
            "🏢 " + row["company"],
            today,
            _dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.execute(
        "UPDATE bills SET paid=1, paid_date=?, tx_id=? WHERE id=?",
        (today, cur.lastrowid, bill_id),
    )
    new_id = None
    if row["recurring"]:
        next_due = _add_months(row["due_date"], 1)
        cur = db.execute(
            "INSERT INTO bills (company, amount, due_date, paid, recurring, parent_id, notes, created_at)"
            " VALUES (?, ?, ?, 0, 1, ?, ?, ?)",
            (
                row["company"],
                row["amount"],
                next_due,
                bill_id,
                row["notes"],
                _dt.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        new_id = cur.lastrowid
    db.commit()
    return jsonify({"ok": True, "next_id": new_id})


@app.route("/api/bills/<int:bill_id>/unpay", methods=["POST"])
def api_bill_unpay(bill_id):
    db = get_db()
    row = db.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    # Al quitar el pagado se elimina también el movimiento generado
    if row["tx_id"]:
        db.execute("DELETE FROM transactions WHERE id=?", (row["tx_id"],))
    db.execute("UPDATE bills SET paid=0, paid_date=NULL, tx_id=NULL WHERE id=?", (bill_id,))
    # Si al pagarlo se generó el siguiente mes automáticamente, quitarlo
    db.execute(
        "DELETE FROM bills WHERE parent_id=? AND paid=0", (bill_id,)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/bills/<int:bill_id>", methods=["PUT"])
def api_bill_update(bill_id):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT id FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not row:
        return jsonify({"error": "no existe"}), 404
    fields, params = [], []
    if "company" in data and (data["company"] or "").strip():
        fields.append("company=?")
        params.append(data["company"].strip())
    if "amount" in data:
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError):
            return jsonify({"error": "monto inválido"}), 400
        if amount <= 0:
            return jsonify({"error": "el monto debe ser mayor a 0"}), 400
        fields.append("amount=?")
        params.append(round(amount, 2))
    if "due_date" in data:
        try:
            _dt.date.fromisoformat(data["due_date"])
        except (ValueError, TypeError):
            return jsonify({"error": "fecha inválida"}), 400
        fields.append("due_date=?")
        params.append(data["due_date"])
    if "notes" in data:
        fields.append("notes=?")
        params.append((data["notes"] or "").strip())
    if "recurring" in data:
        fields.append("recurring=?")
        params.append(1 if data["recurring"] else 0)
    if "tx_id" in data:
        fields.append("tx_id=?")
        params.append(data["tx_id"])
    if fields:
        params.append(bill_id)
        db.execute(f"UPDATE bills SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/bills/<int:bill_id>", methods=["DELETE"])
def api_bill_delete(bill_id):
    db = get_db()
    db.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    db.execute("DELETE FROM bills WHERE parent_id=?", (bill_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------- API: calendario ----------------

@app.route("/api/calendar")
def api_calendar():
    y, m = _parse_ym(request.args.get("year"), request.args.get("month"))
    first, last = _month_bounds(y, m)
    db = get_db()
    txs = db.execute(
        "SELECT date, type, SUM(amount) AS total FROM transactions"
        " WHERE date BETWEEN ? AND ? GROUP BY date, type",
        (first, last),
    ).fetchall()
    bills = db.execute(
        "SELECT due_date, company, amount, paid FROM bills WHERE due_date BETWEEN ? AND ?",
        (first, last),
    ).fetchall()
    days = {}
    for r in txs:
        d = days.setdefault(r["date"], {"income": 0, "expense": 0, "bills": [],
                                        "scheduled": []})
        d[r["type"]] = r["total"]
    for b in bills:
        d = days.setdefault(
            b["due_date"], {"income": 0, "expense": 0, "bills": [], "scheduled": []}
        )
        d["bills"].append(
            {"company": b["company"], "amount": b["amount"], "paid": bool(b["paid"])}
        )
    # Ingresos/gastos programados (aproximados) del mes
    scheds = db.execute("SELECT * FROM schedules WHERE active=1").fetchall()
    for s in scheds:
        for ds in _schedule_occurrences(
                {"freq": s["freq"], "weekday": s["weekday"],
                 "anchor_date": s["anchor_date"], "day": s["day"]}, y, m):
            d = days.setdefault(
                ds, {"income": 0, "expense": 0, "bills": [], "scheduled": []}
            )
            d["scheduled"].append(
                {"title": s["title"], "amount": s["amount"], "kind": s["kind"]}
            )
    return jsonify({"year": y, "month": m, "days": days})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
