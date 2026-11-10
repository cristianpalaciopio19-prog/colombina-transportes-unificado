"""Capa de datos de Estado del Bus.

Funciona con dos motores:

* PostgreSQL (Supabase) cuando existe la variable de entorno
  ``ESTADO_BUS_DATABASE_URL``. Es lo que se usa en Render, porque el disco
  del plan gratuito de Render se borra en cada despliegue o reinicio y
  SQLite perdería las reservas.
* SQLite como respaldo local (desarrollo y pruebas), igual que antes.

En Postgres las tablas viven en un esquema propio (``estado_bus``) para no
mezclarse con las tablas de otras apps que comparten la misma base.

Todas las consultas se escriben con ``?`` como marcador; la capa las
traduce a ``%s`` cuando el motor es Postgres.
"""

import logging
import os
import sqlite3

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_PATH = os.environ.get("ESTADO_BUS_DB_PATH", os.path.join(BASE_DIR, "estado_bus.db"))
PG_URL = os.environ.get("ESTADO_BUS_DATABASE_URL", "").strip()
USE_PG = bool(PG_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    IntegrityError = psycopg2.IntegrityError
    OperationalErrors = (psycopg2.OperationalError, psycopg2.InterfaceError)
    _PREFIJO = "estado_bus."
else:
    IntegrityError = sqlite3.IntegrityError
    OperationalErrors = (sqlite3.OperationalError,)
    _PREFIJO = ""

# Nombres de tabla (en Postgres van calificados con el esquema).
T = {
    "asientos": f"{_PREFIJO}asientos",
    "actividad": f"{_PREFIJO}actividad",
    "reservas": f"{_PREFIJO}reservas",
}

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            0, 5, PG_URL, connect_timeout=10, application_name="colombina-estado-bus"
        )
    return _pool


class Conexion:
    """Envoltura mínima para usar el mismo código con SQLite y Postgres."""

    def __init__(self):
        self._pooled = False
        if USE_PG:
            pool = _get_pool()
            try:
                conn = pool.getconn()
                self._pooled = True
                if conn.closed:
                    pool.putconn(conn, close=True)
                    conn = pool.getconn()
            except psycopg2.pool.PoolError:
                # Pool lleno (picos de tráfico): conexión directa temporal.
                conn = psycopg2.connect(PG_URL, connect_timeout=10, application_name="colombina-estado-bus")
                self._pooled = False
            conn.autocommit = False
            self.raw = conn
        else:
            conn = sqlite3.connect(SQLITE_PATH, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self.raw = conn
        self._en_tx = False
        self._roto = False

    # --- consultas -----------------------------------------------------
    def _sql(self, sql):
        sql = sql.format(**T)
        return sql.replace("?", "%s") if USE_PG else sql

    def execute(self, sql, params=()):
        try:
            if USE_PG:
                cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(self._sql(sql), params)
                if not self._en_tx:
                    self.raw.commit()
                return cur
            return self.raw.execute(self._sql(sql), params)
        except OperationalErrors:
            self._roto = True
            raise
        except Exception:
            if USE_PG and not self._en_tx:
                self.raw.rollback()
            raise

    def fetchone(self, sql, params=()):
        fila = self.execute(sql, params).fetchone()
        return dict(fila) if fila is not None else None

    def fetchall(self, sql, params=()):
        return [dict(f) for f in self.execute(sql, params).fetchall()]

    # --- transacciones -------------------------------------------------
    def begin(self):
        """Abre una transacción que bloquea escrituras concurrentes.

        En SQLite se usa BEGIN IMMEDIATE (un solo escritor a la vez); en
        Postgres el bloqueo es a nivel de fila al hacer UPDATE.
        """
        if not USE_PG:
            self.raw.execute("BEGIN IMMEDIATE")
        self._en_tx = True

    def commit(self):
        if USE_PG:
            self.raw.commit()
        elif self.raw.in_transaction:
            self.raw.execute("COMMIT")
        self._en_tx = False

    def rollback(self):
        try:
            if USE_PG:
                self.raw.rollback()
            elif self.raw.in_transaction:
                self.raw.execute("ROLLBACK")
        finally:
            self._en_tx = False

    def close(self):
        if USE_PG:
            try:
                if not self._roto:
                    self.raw.rollback()
            except Exception:
                self._roto = True
            if self._pooled:
                _get_pool().putconn(self.raw, close=self._roto)
            else:
                self.raw.close()
        else:
            self.raw.close()


# --- Esquema -------------------------------------------------------------
DDL_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS asientos (
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'libre',
        PRIMARY KEY (ruta, turno, numero)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actividad (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        estado TEXT NOT NULL,
        ts REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reservas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        cin TEXT NOT NULL,
        nombre TEXT NOT NULL,
        origen TEXT NOT NULL DEFAULT 'qr',
        estado TEXT NOT NULL DEFAULT 'activa',
        creado_ts REAL NOT NULL,
        liberado_ts REAL,
        liberado_por TEXT,
        nota TEXT
    )
    """,
]

DDL_PG = [
    "CREATE SCHEMA IF NOT EXISTS estado_bus",
    """
    CREATE TABLE IF NOT EXISTS estado_bus.asientos (
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        estado TEXT NOT NULL DEFAULT 'libre',
        asignado_por TEXT,
        asignado_ts DOUBLE PRECISION,
        reserva_id BIGINT,
        PRIMARY KEY (ruta, turno, numero)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS estado_bus.actividad (
        id BIGSERIAL PRIMARY KEY,
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        nombre TEXT NOT NULL,
        estado TEXT NOT NULL,
        ts DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS estado_bus.reservas (
        id BIGSERIAL PRIMARY KEY,
        ruta TEXT NOT NULL,
        turno INTEGER NOT NULL,
        numero INTEGER NOT NULL,
        cin TEXT NOT NULL,
        nombre TEXT NOT NULL,
        origen TEXT NOT NULL DEFAULT 'qr',
        estado TEXT NOT NULL DEFAULT 'activa',
        creado_ts DOUBLE PRECISION NOT NULL,
        liberado_ts DOUBLE PRECISION,
        liberado_por TEXT,
        nota TEXT
    )
    """,
    # RLS activo y sin políticas: la API pública de Supabase (anon) no puede
    # leer ni escribir estas tablas. La app entra con el usuario de la base,
    # que no está sujeto a RLS.
    "ALTER TABLE estado_bus.asientos ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE estado_bus.actividad ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE estado_bus.reservas ENABLE ROW LEVEL SECURITY",
]

# Índices comunes. Los índices únicos parciales son la garantía final de
# que nunca existan dos reservas activas para el mismo puesto, ni dos
# reservas activas del mismo CIN en la misma ruta y turno.
INDICES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS {p}ux_reserva_puesto_activa "
    "ON {t} (ruta, turno, numero) WHERE estado = 'activa'",
    "CREATE UNIQUE INDEX IF NOT EXISTS {p}ux_reserva_cin_activa "
    "ON {t} (ruta, turno, cin) WHERE estado = 'activa'",
    "CREATE INDEX IF NOT EXISTS {p}ix_reservas_creado ON {t} (creado_ts)",
]


def crear_esquema(conn):
    if USE_PG:
        for sql in DDL_PG:
            conn.execute(sql)
        for sql in INDICES:
            # En Postgres el índice se crea en el esquema de la tabla.
            conn.execute(sql.replace("{p}", "").replace("{t}", T["reservas"]))
    else:
        for sql in DDL_SQLITE:
            conn.execute(sql)
        columnas = {r["name"] for r in conn.fetchall("PRAGMA table_info(asientos)")}
        for col, tipo in (("asignado_por", "TEXT"), ("asignado_ts", "REAL"), ("reserva_id", "INTEGER")):
            if col not in columnas:
                conn.execute(f"ALTER TABLE asientos ADD COLUMN {col} {tipo}")
        for sql in INDICES:
            conn.execute(sql.replace("{p}", "").replace("{t}", "reservas"))


def sembrar_asientos(conn, rutas, turnos, total):
    filas = [(r, t, n) for r in rutas for t in turnos for n in range(1, total + 1)]
    if USE_PG:
        cur = conn.raw.cursor()
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO estado_bus.asientos (ruta, turno, numero, estado) VALUES %s "
            "ON CONFLICT (ruta, turno, numero) DO NOTHING",
            [(r, t, n, "libre") for r, t, n in filas],
            page_size=1000,
        )
        conn.raw.commit()
    else:
        conn.raw.executemany(
            "INSERT OR IGNORE INTO asientos (ruta, turno, numero, estado) VALUES (?, ?, ?, 'libre')",
            filas,
        )


def insertar_devolviendo_id(conn, sql, params):
    """INSERT que devuelve el id generado en ambos motores."""
    if USE_PG:
        cur = conn.execute(sql + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur = conn.execute(sql, params)
    return cur.lastrowid
