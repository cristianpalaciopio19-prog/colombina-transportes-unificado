import os

from flask import Flask, render_template

from blueprints.estado_bus import estado_bus_bp
from blueprints.estado_bus import close_db as close_db_estado_bus
from blueprints.estado_bus import init_db as init_db_estado_bus
from blueprints.ruta_finder import ruta_finder_bp
from blueprints.te_escucha import close_db, init_db, te_escucha_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "colombina-unificado-dev-key")

# --- Registro de Blueprints ---
app.register_blueprint(ruta_finder_bp, url_prefix="/ruta-finder")
app.register_blueprint(te_escucha_bp, url_prefix="/pqr")
app.register_blueprint(estado_bus_bp, url_prefix="/estado-bus")

# --- Base de datos de Te Escucha (PQR) ---
app.teardown_appcontext(close_db)
init_db()

# --- Base de datos de Estado del Bus (Puestos) ---
app.teardown_appcontext(close_db_estado_bus)
init_db_estado_bus()


@app.route("/")
def home():
    return render_template("home.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
