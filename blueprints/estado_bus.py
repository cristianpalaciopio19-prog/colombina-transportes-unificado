import os
import random
import sqlite3

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for

from blueprints.te_escucha import ADMIN_PASSWORD
from data.routes_data import RUTAS

estado_bus_bp = Blueprint("estado_bus", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("ESTADO_BUS_DB_PATH", os.path.join(BASE_DIR, "estado_bus.db"))

# --- Configuración del bus ---------------------------------------------
# Distribución 2+2, 10 filas => 40 puestos por bus. Cada ruta operativa
# tiene su propio bus, y cada uno de sus 3 turnos diarios tiene su propio
# mapa de puestos independiente (Turno 1, Turno 2, Turno 3).
TOTAL_ASIENTOS = 40
TURNOS = [1, 2, 3]

RUTAS_POR_CODIGO = {r["ruta"]: r for r in RUTAS}


def get_db():
    if "db_estado_bus" not in g:
        g.db_estado_bus = sqlite3.connect(DB_PATH)
        g.db_estado_bus.row_factory = sqlite3.Row
    return g.db_estado_bus


def close_db(exception=None):
    db = g.pop("db_estado_bus", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS asientos (
            ruta TEXT NOT NULL,
            turno INTEGER NOT NULL,
            numero INTEGER NOT NULL,
            estado TEXT NOT NULL DEFAULT 'libre',
            PRIMARY KEY (ruta, turno, numero)
        )
        """
    )
    for ruta in RUTAS_POR_CODIGO:
        for turno in TURNOS:
            existentes = {
                row[0]
                for row in db.execute(
                    "SELECT numero FROM asientos WHERE ruta = ? AND turno = ?",
                    (ruta, turno),
                )
            }
            faltantes = [n for n in range(1, TOTAL_ASIENTOS + 1) if n not in existentes]
            db.executemany(
                "INSERT INTO asientos (ruta, turno, numero, estado) VALUES (?, ?, ?, 'libre')",
                [(ruta, turno, n) for n in faltantes],
            )
    db.commit()
    db.close()


def obtener_ruta_o_404(codigo_ruta):
    ruta = RUTAS_POR_CODIGO.get(codigo_ruta)
    if ruta is None:
        abort(404)
    return ruta


def validar_turno_o_404(turno):
    if turno not in TURNOS:
        abort(404)
    return turno


def obtener_asientos(codigo_ruta, turno):
    db = get_db()
    filas = db.execute(
        "SELECT numero, estado FROM asientos WHERE ruta = ? AND turno = ? ORDER BY numero",
        (codigo_ruta, turno),
    ).fetchall()
    return {row["numero"]: row["estado"] for row in filas}


def distribucion_bus(total):
    """Arma filas de asientos en formato 2+2 (pasillo al centro).

    Devuelve una lista de filas; cada fila es una lista de 5 posiciones:
    [asiento, asiento, None, asiento, asiento] donde None es el pasillo.
    La numeración coincide con la foto real del bus (frente = 1-4).
    """
    filas = []
    numero = 1
    while numero <= total:
        fila = []
        for _ in range(2):
            fila.append(numero if numero <= total else None)
            numero += 1
        fila.append(None)  # pasillo
        for _ in range(2):
            fila.append(numero if numero <= total else None)
            numero += 1
        filas.append(fila)
    return filas


# --- Inicio --------------------------------------------------------------
@estado_bus_bp.route("/")
def inicio():
    rutas_azar = random.sample(RUTAS, k=min(6, len(RUTAS)))
    return render_template("estado_bus/inicio.html", rutas_azar=rutas_azar, active="inicio")


# --- Puestos ---------------------------------------------------------------
@estado_bus_bp.route("/puestos")
def puestos():
    return render_template("estado_bus/puestos_lista.html", rutas=RUTAS, active="puestos")


@estado_bus_bp.route("/puestos/<codigo_ruta>")
def puestos_detalle(codigo_ruta):
    ruta = obtener_ruta_o_404(codigo_ruta)
    return render_template(
        "estado_bus/puestos_detalle.html", ruta=ruta, turnos=TURNOS, active="puestos"
    )


@estado_bus_bp.route("/puestos/<codigo_ruta>/<int:turno>")
def puestos_ruta(codigo_ruta, turno):
    ruta = obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    asientos = obtener_asientos(codigo_ruta, turno)
    return render_template(
        "estado_bus/puestos.html",
        ruta=ruta,
        turno=turno,
        asientos=asientos,
        filas=distribucion_bus(TOTAL_ASIENTOS),
        active="puestos",
    )


@estado_bus_bp.route("/puestos/<codigo_ruta>/<int:turno>/admin")
def puestos_admin(codigo_ruta, turno):
    ruta = obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    clave = request.args.get("clave", "")
    if clave != ADMIN_PASSWORD:
        return render_template("estado_bus/admin_login.html", ruta=ruta, turno=turno)

    asientos = obtener_asientos(codigo_ruta, turno)
    return render_template(
        "estado_bus/puestos_admin.html",
        ruta=ruta,
        turno=turno,
        asientos=asientos,
        filas=distribucion_bus(TOTAL_ASIENTOS),
        clave=clave,
        active="puestos",
    )


@estado_bus_bp.route(
    "/puestos/<codigo_ruta>/<int:turno>/admin/toggle/<int:numero>", methods=["POST"]
)
def puestos_admin_toggle(codigo_ruta, turno, numero):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    clave = request.form.get("clave", "")
    if clave != ADMIN_PASSWORD:
        return redirect(
            url_for("estado_bus.puestos_admin", codigo_ruta=codigo_ruta, turno=turno)
        )

    db = get_db()
    fila = db.execute(
        "SELECT estado FROM asientos WHERE ruta = ? AND turno = ? AND numero = ?",
        (codigo_ruta, turno, numero),
    ).fetchone()
    if fila is not None:
        nuevo_estado = "libre" if fila["estado"] == "ocupado" else "ocupado"
        db.execute(
            "UPDATE asientos SET estado = ? WHERE ruta = ? AND turno = ? AND numero = ?",
            (nuevo_estado, codigo_ruta, turno, numero),
        )
        db.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(numero=numero, estado=nuevo_estado if fila else None)
    return redirect(
        url_for(
            "estado_bus.puestos_admin", codigo_ruta=codigo_ruta, turno=turno, clave=clave
        )
    )


# --- Rutas -------------------------------------------------------------
@estado_bus_bp.route("/rutas")
def rutas():
    return render_template("estado_bus/rutas.html", rutas=RUTAS, active="rutas")
