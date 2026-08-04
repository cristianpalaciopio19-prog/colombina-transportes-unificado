import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from data.routes_data import RUTAS, TIPOS_PQR

te_escucha_bp = Blueprint("te_escucha", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "pqr.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "colombina2026")
ZONA_COLOMBIA = ZoneInfo("America/Bogota")


def hora_colombia():
    return datetime.now(ZONA_COLOMBIA).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db_te_escucha" not in g:
        g.db_te_escucha = sqlite3.connect(DB_PATH)
        g.db_te_escucha.row_factory = sqlite3.Row
    return g.db_te_escucha


def close_db(exception=None):
    db = g.pop("db_te_escucha", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS pqr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            nombre TEXT NOT NULL,
            cin TEXT NOT NULL,
            ruta TEXT NOT NULL,
            municipio TEXT NOT NULL,
            conductor TEXT NOT NULL,
            placa TEXT NOT NULL,
            tipo_pqr TEXT NOT NULL,
            detalle TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


@te_escucha_bp.route("/", methods=["GET"])
def index():
    return redirect("https://colombina-te-escucha.onrender.com/", code=302)


@te_escucha_bp.route("/enviar", methods=["POST"])
def enviar():
    nombre = request.form.get("nombre", "").strip()
    cin = request.form.get("cin", "").strip()
    ruta = request.form.get("ruta", "").strip()
    municipio = request.form.get("municipio", "").strip()
    conductor = request.form.get("conductor", "").strip()
    placa = request.form.get("placa", "").strip()
    tipo_pqr = request.form.get("tipo_pqr", "").strip()
    detalle = request.form.get("detalle", "").strip()

    if not all([nombre, cin, ruta, municipio, conductor, placa, tipo_pqr, detalle]):
        flash("Por favor completa todos los campos antes de enviar.", "error")
        return redirect(url_for("te_escucha.index"))

    if tipo_pqr not in TIPOS_PQR:
        flash("Tipo de PQR no válido.", "error")
        return redirect(url_for("te_escucha.index"))

    db = get_db()
    db.execute(
        """
        INSERT INTO pqr (fecha, nombre, cin, ruta, municipio, conductor, placa, tipo_pqr, detalle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hora_colombia(),
            nombre,
            cin,
            ruta,
            municipio,
            conductor,
            placa,
            tipo_pqr,
            detalle,
        ),
    )
    db.commit()

    flash("¡Gracias! Tu PQR fue registrada correctamente.", "success")
    return redirect(url_for("te_escucha.index"))


@te_escucha_bp.route("/admin")
def admin():
    clave = request.args.get("clave", "")
    return redirect(f"https://colombina-te-escucha.onrender.com/admin?clave={clave}", code=302)


@te_escucha_bp.route("/admin/detalle/<int:pqr_id>")
def admin_detalle(pqr_id):
    clave = request.args.get("clave", "")
    if clave != ADMIN_PASSWORD:
        return render_template("te_escucha/admin_login.html")

    db = get_db()
    registro = db.execute("SELECT * FROM pqr WHERE id = ?", (pqr_id,)).fetchone()
    if registro is None:
        flash("Ese registro ya no existe.", "error")
        return redirect(url_for("te_escucha.admin", clave=clave))

    return render_template("te_escucha/admin_detalle.html", r=registro, clave=clave)


@te_escucha_bp.route("/admin/eliminar/<int:pqr_id>", methods=["POST"])
def admin_eliminar(pqr_id):
    clave = request.form.get("clave", "")
    if clave != ADMIN_PASSWORD:
        return render_template("te_escucha/admin_login.html")

    tipo = request.form.get("tipo", "")

    db = get_db()
    db.execute("DELETE FROM pqr WHERE id = ?", (pqr_id,))
    db.commit()
    flash("Registro eliminado.", "success")
    return redirect(url_for("te_escucha.admin", clave=clave, tipo=tipo))
