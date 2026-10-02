# -*- coding: utf-8 -*-
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["FINANZAS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "test_finanzas.db"
)

import app as appmod


@pytest.fixture()
def client():
    p = os.environ["FINANZAS_DB_PATH"]
    if os.path.exists(p):
        os.remove(p)
    appmod.init_db()
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        yield c
    if os.path.exists(p):
        os.remove(p)


def test_row_wrapper_keys():
    # Regresión: en producción libsql usa _Row (no sqlite3.Row real);
    # debe exponer .keys() como sqlite3.Row.
    r = appmod._Row(["a", "b"], [1, 2])
    assert r.keys() == ["a", "b"]
    assert r["a"] == 1 and r[1] == 2


def test_categories_seeded(client):
    r = client.get("/api/categories")
    assert r.status_code == 200
    cats = r.get_json()
    assert len(cats) >= 10
    assert any(c["name"] == "Vivienda" and c["type"] == "expense" for c in cats)
    assert any(c["name"] == "Trabajo" and c["type"] == "income" for c in cats)


def test_transaction_crud_and_dashboard(client):
    # crear ingreso y gasto en octubre 2026
    r = client.post("/api/transactions", json={
        "type": "income", "amount": 3000, "category": "Trabajo",
        "description": "Sueldo", "date": "2026-10-05"})
    assert r.status_code == 201
    inc_id = r.get_json()["id"]
    r = client.post("/api/transactions", json={
        "type": "expense", "amount": 850.50, "category": "Vivienda",
        "description": "Renta", "date": "2026-10-01"})
    assert r.status_code == 201

    d = client.get("/api/dashboard?year=2026&month=10").get_json()
    assert d["income"] == 3000
    assert d["expense"] == 850.50
    assert d["balance"] == pytest.approx(2149.50)
    assert any(c["name"] == "Vivienda" and c["total"] == 850.50 for c in d["by_category"])
    assert d["monthly"][9]["income"] == 3000  # octubre = índice 9
    assert d["year_income"] == 3000

    # editar
    r = client.put(f"/api/transactions/{inc_id}", json={"amount": 3200})
    assert r.status_code == 200
    d = client.get("/api/dashboard?year=2026&month=10").get_json()
    assert d["income"] == 3200

    # borrar
    r = client.delete(f"/api/transactions/{inc_id}")
    assert r.status_code == 200
    d = client.get("/api/dashboard?year=2026&month=10").get_json()
    assert d["income"] == 0

    # validaciones
    assert client.post("/api/transactions", json={"type": "expense", "amount": -5}).status_code == 400
    assert client.post("/api/transactions", json={"type": "otro", "amount": 5}).status_code == 400


def test_calendar(client):
    client.post("/api/transactions", json={
        "type": "expense", "amount": 100, "category": "Ocio",
        "description": "Cine", "date": "2026-10-12"})
    client.post("/api/bills", json={
        "company": "Luz", "amount": 60, "due_date": "2026-10-12", "recurring": False})
    d = client.get("/api/calendar?year=2026&month=10").get_json()
    day = d["days"]["2026-10-12"]
    assert day["expense"] == 100
    assert day["bills"][0]["company"] == "Luz"


def test_bills_recurring_rollover(client):
    r = client.post("/api/bills", json={
        "company": "Internet", "amount": 45, "due_date": "2026-10-15", "recurring": True})
    bid = r.get_json()["id"]
    # pagar -> crea el del mes siguiente
    r = client.post(f"/api/bills/{bid}/pay")
    assert r.status_code == 200
    nxt = r.get_json()["next_id"]
    assert nxt
    bills = client.get("/api/bills").get_json()
    nb = next(b for b in bills if b["id"] == nxt)
    assert nb["due_date"] == "2026-11-15" and not nb["paid"]
    # quitar el pago -> borra el generado
    client.post(f"/api/bills/{bid}/unpay")
    bills = client.get("/api/bills").get_json()
    assert not any(b["id"] == nxt for b in bills)
    # día 31 -> se ajusta al último día del mes siguiente
    r = client.post("/api/bills", json={
        "company": "Renta", "amount": 900, "due_date": "2026-10-31", "recurring": True})
    bid2 = r.get_json()["id"]
    nxt2 = client.post(f"/api/bills/{bid2}/pay").get_json()["next_id"]
    bills = client.get("/api/bills").get_json()
    nb2 = next(b for b in bills if b["id"] == nxt2)
    assert nb2["due_date"] == "2026-11-30"


def test_category_delete_protection(client):
    cats = client.get("/api/categories?type=expense").get_json()
    viv = next(c for c in cats if c["name"] == "Vivienda")
    client.post("/api/transactions", json={
        "type": "expense", "amount": 10, "category": "Vivienda",
        "description": "x", "date": "2026-10-01"})
    assert client.delete(f"/api/categories/{viv['id']}").status_code == 400
    # crear y borrar una sin uso
    r = client.post("/api/categories", json={"name": "Mascotas", "type": "expense"})
    assert r.status_code == 201
    assert client.delete(f"/api/categories/{r.get_json()['id']}").status_code == 200
