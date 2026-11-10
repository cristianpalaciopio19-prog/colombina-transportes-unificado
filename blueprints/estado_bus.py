import csv
import hashlib
import hmac
import io
import logging
import os
import random
import re
import secrets
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from blueprints import estado_bus_db as dbmod
from blueprints.te_escucha import ADMIN_PASSWORD
from data.routes_data import RUTAS

log = logging.getLogger(__name__)

estado_bus_bp = Blueprint("estado_bus", __name__)

# Se conserva por compatibilidad (antes lo exportaba este módulo).
DB_PATH = dbmod.SQLITE_PATH

# --- Configuración del bus ---------------------------------------------
# Distribución 2+2, 10 filas => 40 puestos por bus. Cada ruta operativa
# tiene su propio bus, y cada uno de sus 3 turnos diarios tiene su propio
# mapa de puestos independiente (Turno 1, Turno 2, Turno 3).
TOTAL_ASIENTOS = 40
TURNOS = [1, 2, 3]

RUTAS_POR_CODIGO = {r["ruta"]: r for r in RUTAS}
ZONA_COLOMBIA = ZoneInfo("America/Bogota")

# Clave para firmar los enlaces de los QR. Debe fijarse UNA vez en Render
# (variable QR_SECRET) antes de imprimir los códigos: si cambia, los QR
# impresos dejan de servir para reservar.
QR_SECRET = os.environ.get("QR_SECRET") or os.environ.get("SECRET_KEY", "colombina-unificado-dev-key")

_estado_db = {"lista": False, "ultimo_intento": 0.0}

# Vistas que no consultan la base y deben verse aunque la base falle.
VISTAS_SIN_DB = {"estado_bus.inicio", "estado_bus.rutas", "estado_bus.puestos", "estado_bus.puestos_detalle"}


# --- Base de datos -------------------------------------------------------
def get_db():
    if "db_estado_bus" not in g:
        g.db_estado_bus = dbmod.Conexion()
    return g.db_estado_bus


def close_db(exception=None):
    db = g.pop("db_estado_bus", None)
    if db is not None:
        try:
            db.close()
        except Exception:  # pragma: no cover - nunca debe tumbar la respuesta
            log.exception("Error cerrando la conexión de Estado del Bus")


def init_db():
    """Crea tablas y siembra los 40 puestos por ruta y turno.

    Si la base no responde (por ejemplo, Supabase en pausa) no se detiene
    el arranque de la app: el resto del portal sigue funcionando y Estado
    del Bus reintenta en la siguiente visita.
    """
    _estado_db["ultimo_intento"] = time.time()
    try:
        conn = dbmod.Conexion()
        try:
            dbmod.crear_esquema(conn)
            dbmod.sembrar_asientos(conn, list(RUTAS_POR_CODIGO), TURNOS, TOTAL_ASIENTOS)
        finally:
            conn.close()
        _estado_db["lista"] = True
        log.info("Estado del Bus: base lista (%s)", "Postgres" if dbmod.USE_PG else "SQLite")
    except Exception:
        _estado_db["lista"] = False
        log.exception("Estado del Bus: no se pudo preparar la base de datos")


@estado_bus_bp.before_request
def asegurar_db():
    if not _estado_db["lista"] and request.endpoint not in VISTAS_SIN_DB:
        # Reintenta como máximo cada 20 s para no ralentizar cada visita.
        if time.time() - _estado_db["ultimo_intento"] > 20:
            init_db()
        if not _estado_db["lista"]:
            if request.accept_mimetypes.best == "application/json" or request.is_json:
                return jsonify(ok=False, error="Servicio no disponible. Intenta en un momento."), 503
            return render_template("estado_bus/no_disponible.html"), 503
    return None


# --- Utilidades ----------------------------------------------------------
def obtener_ruta_o_404(codigo_ruta):
    ruta = RUTAS_POR_CODIGO.get(codigo_ruta)
    if ruta is None:
        abort(404)
    return ruta


def validar_turno_o_404(turno):
    if turno not in TURNOS:
        abort(404)
    return turno


def validar_numero_o_404(numero):
    if not 1 <= numero <= TOTAL_ASIENTOS:
        abort(404)
    return numero


def hora_col(ts, formato="%d/%m/%Y %I:%M %p"):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, ZONA_COLOMBIA).strftime(formato)


estado_bus_bp.add_app_template_filter(hora_col, "hora_col")


def enmascarar_cin(cin):
    cin = cin or ""
    return ("•" * max(len(cin) - 4, 0)) + cin[-4:] if len(cin) > 4 else "••••"


def qr_token(codigo_ruta, turno):
    msg = f"{codigo_ruta}:{turno}".encode()
    return hmac.new(QR_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:12]


def token_valido(codigo_ruta, turno, token):
    return hmac.compare_digest(qr_token(codigo_ruta, turno), (token or "")[:12])


def url_publica_qr(codigo_ruta, turno):
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    ruta = url_for("estado_bus.qr_reservar", codigo_ruta=codigo_ruta, turno=turno, k=qr_token(codigo_ruta, turno))
    return base + ruta


def obtener_asientos(codigo_ruta, turno):
    """Estado público de los puestos (nunca incluye el CIN)."""
    filas = get_db().fetchall(
        "SELECT numero, estado, asignado_por, asignado_ts FROM {asientos} "
        "WHERE ruta = ? AND turno = ? ORDER BY numero",
        (codigo_ruta, turno),
    )
    return {
        row["numero"]: {
            "estado": row["estado"],
            "asignado_por": row["asignado_por"],
            "asignado_ts": row["asignado_ts"],
        }
        for row in filas
    }


def registrar_actividad(codigo_ruta, turno, numero, nombre, estado):
    db = get_db()
    db.execute(
        "INSERT INTO {actividad} (ruta, turno, numero, nombre, estado, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (codigo_ruta, turno, numero, (nombre or "Alguien").strip()[:40], estado, time.time()),
    )
    # Se conservan solo los últimos 200 eventos para no crecer indefinidamente.
    db.execute(
        "DELETE FROM {actividad} WHERE id NOT IN "
        "(SELECT id FROM {actividad} ORDER BY id DESC LIMIT 200)"
    )


def obtener_actividad_reciente(segundos=8, codigo_ruta=None, turno=None):
    db = get_db()
    desde = time.time() - segundos
    if codigo_ruta and turno:
        return db.fetchall(
            "SELECT ruta, turno, numero, nombre, estado, ts FROM {actividad} "
            "WHERE ts >= ? AND ruta = ? AND turno = ? ORDER BY ts DESC LIMIT 10",
            (desde, codigo_ruta, turno),
        )
    return db.fetchall(
        "SELECT ruta, turno, numero, nombre, estado, ts FROM {actividad} "
        "WHERE ts >= ? ORDER BY ts DESC LIMIT 10",
        (desde,),
    )


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


# --- Operaciones de reserva (transaccionales) ----------------------------
class ReservaError(Exception):
    pass


def limpiar_cin(valor):
    cin = re.sub(r"[\s.\-]", "", (valor or "")).upper()
    if not re.fullmatch(r"[0-9A-Z]{3,20}", cin):
        raise ReservaError("Ingresa un CIN válido (solo números o letras, de 3 a 20 caracteres).")
    return cin


def limpiar_nombre(valor):
    nombre = re.sub(r"\s+", " ", (valor or "")).strip()
    if len(nombre) < 3 or len(nombre) > 60:
        raise ReservaError("Ingresa tu nombre completo (entre 3 y 60 caracteres).")
    if re.search(r"[<>{}]", nombre):
        raise ReservaError("El nombre contiene caracteres no permitidos.")
    return nombre


def reservar_puesto(codigo_ruta, turno, numero, cin, nombre, origen="qr"):
    """Reserva atómica. Si dos personas confirman el mismo puesto a la vez,
    solo una lo obtiene: el UPDATE exige estado='libre' y además hay un
    índice único sobre las reservas activas de cada puesto."""
    db = get_db()
    ahora = time.time()
    db.begin()
    try:
        previa = db.fetchone(
            "SELECT numero FROM {reservas} WHERE ruta = ? AND turno = ? AND cin = ? AND estado = 'activa'",
            (codigo_ruta, turno, cin),
        )
        if previa:
            raise ReservaError(
                f"Este CIN ya tiene reservado el puesto {previa['numero']} en esta ruta y turno."
            )
        reserva_id = dbmod.insertar_devolviendo_id(
            db,
            "INSERT INTO {reservas} (ruta, turno, numero, cin, nombre, origen, estado, creado_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, 'activa', ?)",
            (codigo_ruta, turno, numero, cin, nombre, origen, ahora),
        )
        cur = db.execute(
            "UPDATE {asientos} SET estado = 'ocupado', asignado_por = ?, asignado_ts = ?, reserva_id = ? "
            "WHERE ruta = ? AND turno = ? AND numero = ? AND estado = 'libre'",
            (nombre, ahora, reserva_id, codigo_ruta, turno, numero),
        )
        if cur.rowcount != 1:
            raise ReservaError("Ese puesto acaba de ser ocupado por otra persona. Elige otro.")
        db.commit()
    except ReservaError:
        db.rollback()
        raise
    except dbmod.IntegrityError:
        db.rollback()
        raise ReservaError("Ese puesto acaba de ser ocupado o tu CIN ya tiene una reserva. Actualiza e intenta de nuevo.")
    except Exception:
        db.rollback()
        raise
    registrar_actividad(codigo_ruta, turno, numero, nombre, "ocupado")
    return reserva_id


def liberar_asiento(codigo_ruta, turno, numero, quien, nota=None):
    """Libera un puesto y cierra su reserva activa (si la tiene)."""
    db = get_db()
    ahora = time.time()
    db.begin()
    try:
        db.execute(
            "UPDATE {reservas} SET estado = 'liberada', liberado_ts = ?, liberado_por = ?, nota = COALESCE(?, nota) "
            "WHERE ruta = ? AND turno = ? AND numero = ? AND estado = 'activa'",
            (ahora, quien, nota, codigo_ruta, turno, numero),
        )
        cur = db.execute(
            "UPDATE {asientos} SET estado = 'libre', asignado_por = NULL, asignado_ts = NULL, reserva_id = NULL "
            "WHERE ruta = ? AND turno = ? AND numero = ? AND estado = 'ocupado'",
            (codigo_ruta, turno, numero),
        )
        cambio = cur.rowcount == 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    if cambio:
        registrar_actividad(codigo_ruta, turno, numero, quien, "libre")
    return cambio


def ocupar_asiento_admin(codigo_ruta, turno, numero, nombre):
    """Marca un puesto ocupado desde el panel por puesto (sin CIN), como antes."""
    db = get_db()
    cur = db.execute(
        "UPDATE {asientos} SET estado = 'ocupado', asignado_por = ?, asignado_ts = ?, reserva_id = NULL "
        "WHERE ruta = ? AND turno = ? AND numero = ? AND estado = 'libre'",
        (nombre, time.time(), codigo_ruta, turno, numero),
    )
    if cur.rowcount == 1:
        registrar_actividad(codigo_ruta, turno, numero, nombre, "ocupado")
        return True
    return False


def mover_reserva(reserva_id, nuevo_numero, quien):
    db = get_db()
    ahora = time.time()
    db.begin()
    try:
        r = db.fetchone("SELECT * FROM {reservas} WHERE id = ? AND estado = 'activa'", (reserva_id,))
        if not r:
            raise ReservaError("La reserva ya no está activa.")
        if r["numero"] == nuevo_numero:
            raise ReservaError("La reserva ya está en ese puesto.")
        cur = db.execute(
            "UPDATE {asientos} SET estado = 'ocupado', asignado_por = ?, asignado_ts = ?, reserva_id = ? "
            "WHERE ruta = ? AND turno = ? AND numero = ? AND estado = 'libre'",
            (r["nombre"], ahora, reserva_id, r["ruta"], r["turno"], nuevo_numero),
        )
        if cur.rowcount != 1:
            raise ReservaError(f"El puesto {nuevo_numero} no está libre.")
        db.execute(
            "UPDATE {asientos} SET estado = 'libre', asignado_por = NULL, asignado_ts = NULL, reserva_id = NULL "
            "WHERE ruta = ? AND turno = ? AND numero = ?",
            (r["ruta"], r["turno"], r["numero"]),
        )
        db.execute(
            "UPDATE {reservas} SET numero = ?, nota = ? WHERE id = ?",
            (nuevo_numero, f"Movida del puesto {r['numero']} por {quien}", reserva_id),
        )
        db.commit()
    except dbmod.IntegrityError:
        db.rollback()
        raise ReservaError(f"El puesto {nuevo_numero} ya tiene una reserva activa.")
    except Exception:
        db.rollback()
        raise
    registrar_actividad(r["ruta"], r["turno"], r["numero"], quien, "libre")
    registrar_actividad(r["ruta"], r["turno"], nuevo_numero, r["nombre"], "ocupado")
    return r


def editar_reserva(reserva_id, cin, nombre):
    db = get_db()
    db.begin()
    try:
        r = db.fetchone("SELECT * FROM {reservas} WHERE id = ? AND estado = 'activa'", (reserva_id,))
        if not r:
            raise ReservaError("La reserva ya no está activa.")
        db.execute("UPDATE {reservas} SET cin = ?, nombre = ? WHERE id = ?", (cin, nombre, reserva_id))
        db.execute(
            "UPDATE {asientos} SET asignado_por = ? WHERE ruta = ? AND turno = ? AND numero = ? AND reserva_id = ?",
            (nombre, r["ruta"], r["turno"], r["numero"], reserva_id),
        )
        db.commit()
    except dbmod.IntegrityError:
        db.rollback()
        raise ReservaError("Ese CIN ya tiene otra reserva activa en esta ruta y turno.")
    except Exception:
        db.rollback()
        raise
    return r


# --- Seguridad del panel -------------------------------------------------
def es_admin():
    return bool(session.get("eb_admin"))


def csrf_token():
    if "eb_csrf" not in session:
        session["eb_csrf"] = secrets.token_hex(16)
    return session["eb_csrf"]


estado_bus_bp.add_app_template_global(csrf_token, "eb_csrf_token")


def admin_requerido(vista):
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not es_admin():
            return redirect(url_for("estado_bus.admin_login", siguiente=request.full_path))
        if request.method == "POST":
            enviado = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(enviado, session.get("eb_csrf", "")):
                abort(400)
        return vista(*args, **kwargs)

    return envoltura


def nombre_admin():
    return session.get("eb_admin_nombre") or "Administrador"


# --- Inicio --------------------------------------------------------------
@estado_bus_bp.route("/")
def inicio():
    rutas_azar = random.sample(RUTAS, k=min(6, len(RUTAS)))
    return render_template(
        "estado_bus/inicio.html",
        rutas_azar=rutas_azar,
        total_rutas=len(RUTAS),
        total_turnos=len(TURNOS),
        total_asientos=TOTAL_ASIENTOS,
        active="inicio",
    )


# --- Puestos (vista pública de consulta, sin cambios de comportamiento) ----
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
    nombre = request.form.get("nombre", "").strip()[:40] or "Alguien"
    if clave != ADMIN_PASSWORD:
        return redirect(
            url_for("estado_bus.puestos_admin", codigo_ruta=codigo_ruta, turno=turno)
        )

    fila = get_db().fetchone(
        "SELECT estado FROM {asientos} WHERE ruta = ? AND turno = ? AND numero = ?",
        (codigo_ruta, turno, numero),
    )
    nuevo_estado = None
    if fila is not None:
        if fila["estado"] == "ocupado":
            liberar_asiento(codigo_ruta, turno, numero, nombre, nota="Liberado desde el plano admin")
            nuevo_estado = "libre"
        else:
            ocupar_asiento_admin(codigo_ruta, turno, numero, nombre)
            nuevo_estado = "ocupado"

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(
            numero=numero,
            estado=nuevo_estado,
            asignado_por=nombre if nuevo_estado == "ocupado" else None,
        )
    return redirect(
        url_for(
            "estado_bus.puestos_admin", codigo_ruta=codigo_ruta, turno=turno, clave=clave
        )
    )


@estado_bus_bp.route("/puestos/<codigo_ruta>/<int:turno>/actividad")
def puestos_actividad(codigo_ruta, turno):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    return jsonify(eventos=obtener_actividad_reciente(codigo_ruta=codigo_ruta, turno=turno))


@estado_bus_bp.route("/puestos/<codigo_ruta>/<int:turno>/estado")
def puestos_estado(codigo_ruta, turno):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    return jsonify(asientos=obtener_asientos(codigo_ruta, turno))


@estado_bus_bp.route("/actividad")
def actividad_global():
    return jsonify(eventos=obtener_actividad_reciente())


# --- Reserva por QR (lo que abre el trabajador al escanear) ----------------
@estado_bus_bp.route("/qr/<codigo_ruta>/<int:turno>")
def qr_reservar(codigo_ruta, turno):
    ruta = obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    token = request.args.get("k", "")
    if not token_valido(codigo_ruta, turno, token):
        return render_template("estado_bus/qr_invalido.html", ruta=ruta, turno=turno), 403
    hora = ruta["horarios"][turno - 1] if ruta.get("horarios") and len(ruta["horarios"]) >= turno else None
    return render_template(
        "estado_bus/reservar.html",
        ruta=ruta,
        turno=turno,
        hora=hora,
        token=token,
        asientos=obtener_asientos(codigo_ruta, turno),
        filas=distribucion_bus(TOTAL_ASIENTOS),
        total=TOTAL_ASIENTOS,
    )


@estado_bus_bp.route("/qr/<codigo_ruta>/<int:turno>/reservar", methods=["POST"])
def qr_reservar_post(codigo_ruta, turno):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    datos = request.get_json(silent=True) or request.form
    if not token_valido(codigo_ruta, turno, datos.get("k")):
        return jsonify(ok=False, error="Código QR no válido. Escanea el QR del bus."), 403
    try:
        numero = int(datos.get("numero", 0))
    except (TypeError, ValueError):
        numero = 0
    if not 1 <= numero <= TOTAL_ASIENTOS:
        return jsonify(ok=False, error="Puesto no válido."), 400
    try:
        cin = limpiar_cin(datos.get("cin"))
        nombre = limpiar_nombre(datos.get("nombre"))
        reservar_puesto(codigo_ruta, turno, numero, cin, nombre)
    except ReservaError as e:
        return jsonify(ok=False, error=str(e), asientos=obtener_asientos(codigo_ruta, turno)), 409
    return jsonify(
        ok=True,
        numero=numero,
        nombre=nombre,
        cin=enmascarar_cin(cin),
        hora=hora_col(time.time()),
        asientos=obtener_asientos(codigo_ruta, turno),
    )


# --- Panel administrativo de reservas ------------------------------------
@estado_bus_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    siguiente = request.values.get("siguiente") or url_for("estado_bus.admin_panel")
    if not siguiente.startswith("/"):
        siguiente = url_for("estado_bus.admin_panel")
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("clave", ""), ADMIN_PASSWORD):
            session["eb_admin"] = True
            session["eb_admin_nombre"] = request.form.get("nombre", "").strip()[:40] or "Administrador"
            csrf_token()
            return redirect(siguiente)
        error = "Clave incorrecta."
    return render_template("estado_bus/admin_reservas_login.html", error=error, siguiente=siguiente, active="admin")


@estado_bus_bp.route("/admin/salir")
def admin_logout():
    session.pop("eb_admin", None)
    session.pop("eb_admin_nombre", None)
    return redirect(url_for("estado_bus.inicio"))


def resumen_ocupacion():
    filas = get_db().fetchall(
        "SELECT ruta, turno, "
        "SUM(CASE WHEN estado = 'ocupado' THEN 1 ELSE 0 END) AS ocupados, COUNT(*) AS total "
        "FROM {asientos} GROUP BY ruta, turno"
    )
    return {(f["ruta"], f["turno"]): {"ocupados": int(f["ocupados"] or 0), "total": int(f["total"])} for f in filas}


@estado_bus_bp.route("/admin")
@admin_requerido
def admin_panel():
    q = request.args.get("q", "").strip()
    resumen = resumen_ocupacion()
    total_ocupados = sum(v["ocupados"] for v in resumen.values())
    sql = "SELECT * FROM {reservas} WHERE estado = 'activa'"
    params = []
    if q:
        sql += " AND (UPPER(nombre) LIKE ? OR UPPER(cin) LIKE ? OR UPPER(ruta) LIKE ?)"
        patron = "%" + q.upper() + "%"
        params = [patron, patron, patron]
    sql += " ORDER BY creado_ts DESC LIMIT 300"
    reservas = get_db().fetchall(sql, params)
    historial = get_db().fetchall(
        "SELECT * FROM {reservas} WHERE estado = 'liberada' ORDER BY liberado_ts DESC LIMIT 30"
    )
    return render_template(
        "estado_bus/admin_panel.html",
        rutas=RUTAS,
        turnos=TURNOS,
        resumen=resumen,
        total_ocupados=total_ocupados,
        total_puestos=len(RUTAS) * len(TURNOS) * TOTAL_ASIENTOS,
        reservas=reservas,
        historial=historial,
        q=q,
        active="admin",
    )


@estado_bus_bp.route("/admin/bus/<codigo_ruta>/<int:turno>")
@admin_requerido
def admin_bus(codigo_ruta, turno):
    ruta = obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    asientos = obtener_asientos(codigo_ruta, turno)
    reservas = get_db().fetchall(
        "SELECT * FROM {reservas} WHERE ruta = ? AND turno = ? AND estado = 'activa' ORDER BY numero",
        (codigo_ruta, turno),
    )
    por_numero = {r["numero"]: r for r in reservas}
    libres = [n for n, a in asientos.items() if a["estado"] == "libre"]
    return render_template(
        "estado_bus/admin_bus.html",
        ruta=ruta,
        turno=turno,
        asientos=asientos,
        por_numero=por_numero,
        libres=sorted(libres),
        filas=distribucion_bus(TOTAL_ASIENTOS),
        qr_url=url_publica_qr(codigo_ruta, turno),
        active="admin",
    )


def volver_a_bus(codigo_ruta, turno):
    return redirect(url_for("estado_bus.admin_bus", codigo_ruta=codigo_ruta, turno=turno))


@estado_bus_bp.route("/admin/bus/<codigo_ruta>/<int:turno>/liberar/<int:numero>", methods=["POST"])
@admin_requerido
def admin_liberar(codigo_ruta, turno, numero):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    validar_numero_o_404(numero)
    if liberar_asiento(codigo_ruta, turno, numero, nombre_admin(), nota=request.form.get("nota") or None):
        flash(f"Puesto {numero} liberado.", "ok")
    else:
        flash(f"El puesto {numero} ya estaba libre.", "info")
    return volver_a_bus(codigo_ruta, turno)


@estado_bus_bp.route("/admin/bus/<codigo_ruta>/<int:turno>/liberar-todo", methods=["POST"])
@admin_requerido
def admin_liberar_todo(codigo_ruta, turno):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    ocupados = [n for n, a in obtener_asientos(codigo_ruta, turno).items() if a["estado"] == "ocupado"]
    for numero in ocupados:
        liberar_asiento(codigo_ruta, turno, numero, nombre_admin(), nota="Liberación total del turno")
    flash(f"Se liberaron {len(ocupados)} puestos de la ruta {codigo_ruta}, turno {turno}.", "ok")
    return volver_a_bus(codigo_ruta, turno)


@estado_bus_bp.route("/admin/reserva/<int:reserva_id>/mover", methods=["POST"])
@admin_requerido
def admin_mover(reserva_id):
    try:
        nuevo = int(request.form.get("numero", 0))
        validar_numero_o_404(nuevo)
        r = mover_reserva(reserva_id, nuevo, nombre_admin())
        flash(f"Reserva de {r['nombre']} movida del puesto {r['numero']} al {nuevo}.", "ok")
    except ReservaError as e:
        flash(str(e), "error")
        r = get_db().fetchone("SELECT ruta, turno FROM {reservas} WHERE id = ?", (reserva_id,))
    except ValueError:
        abort(400)
    if not r:
        return redirect(url_for("estado_bus.admin_panel"))
    return volver_a_bus(r["ruta"], r["turno"])


@estado_bus_bp.route("/admin/reserva/<int:reserva_id>/editar", methods=["POST"])
@admin_requerido
def admin_editar(reserva_id):
    r = None
    try:
        cin = limpiar_cin(request.form.get("cin"))
        nombre = limpiar_nombre(request.form.get("nombre"))
        r = editar_reserva(reserva_id, cin, nombre)
        flash(f"Reserva del puesto {r['numero']} actualizada.", "ok")
    except ReservaError as e:
        flash(str(e), "error")
    if r is None:
        r = get_db().fetchone("SELECT ruta, turno FROM {reservas} WHERE id = ?", (reserva_id,))
    if not r:
        return redirect(url_for("estado_bus.admin_panel"))
    return volver_a_bus(r["ruta"], r["turno"])


@estado_bus_bp.route("/admin/bus/<codigo_ruta>/<int:turno>/reservar", methods=["POST"])
@admin_requerido
def admin_reservar(codigo_ruta, turno):
    """El administrador registra una reserva a nombre de un trabajador."""
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    try:
        numero = int(request.form.get("numero", 0))
        validar_numero_o_404(numero)
        cin = limpiar_cin(request.form.get("cin"))
        nombre = limpiar_nombre(request.form.get("nombre"))
        reservar_puesto(codigo_ruta, turno, numero, cin, nombre, origen="admin")
        flash(f"Puesto {numero} reservado para {nombre}.", "ok")
    except ReservaError as e:
        flash(str(e), "error")
    except ValueError:
        abort(400)
    return volver_a_bus(codigo_ruta, turno)


@estado_bus_bp.route("/admin/exportar.csv")
@admin_requerido
def admin_exportar():
    filas = get_db().fetchall("SELECT * FROM {reservas} ORDER BY creado_ts DESC")
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    w.writerow(["ID", "Ruta", "Turno", "Puesto", "CIN", "Nombre", "Origen", "Estado",
                "Reservado", "Liberado", "Liberado por", "Nota"])
    for r in filas:
        w.writerow([r["id"], r["ruta"], r["turno"], r["numero"], r["cin"], r["nombre"], r["origen"],
                    r["estado"], hora_col(r["creado_ts"]), hora_col(r["liberado_ts"]),
                    r["liberado_por"] or "", r["nota"] or ""])
    nombre = datetime.now(ZONA_COLOMBIA).strftime("reservas_puestos_%Y%m%d_%H%M.csv")
    return Response(
        "﻿" + salida.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


# --- Códigos QR ------------------------------------------------------------
@estado_bus_bp.route("/admin/qr")
@admin_requerido
def admin_qr():
    filtro = request.args.get("ruta", "")
    rutas = [r for r in RUTAS if not filtro or r["ruta"] == filtro]
    items = [
        {"ruta": r, "turno": t, "url": url_publica_qr(r["ruta"], t),
         "hora": r["horarios"][t - 1] if r.get("horarios") and len(r["horarios"]) >= t else None}
        for r in rutas
        for t in TURNOS
    ]
    return render_template("estado_bus/admin_qr.html", items=items, rutas=RUTAS, filtro=filtro, active="admin")


@estado_bus_bp.route("/admin/qr/<codigo_ruta>/<int:turno>.svg")
@admin_requerido
def admin_qr_svg(codigo_ruta, turno):
    obtener_ruta_o_404(codigo_ruta)
    validar_turno_o_404(turno)
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(
        url_publica_qr(codigo_ruta, turno),
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=3600"})


# --- Rutas -------------------------------------------------------------
@estado_bus_bp.route("/rutas")
def rutas():
    total_paradas = sum(len(r["paradas"]) for r in RUTAS)
    municipios = sorted({r["municipio"] for r in RUTAS})
    return render_template(
        "estado_bus/rutas.html",
        rutas=RUTAS,
        total_paradas=total_paradas,
        total_municipios=len(municipios),
        active="rutas",
    )
