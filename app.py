import os

from flask import Flask, render_template

from blueprints.ruta_finder import ruta_finder_bp
from blueprints.te_escucha import close_db, init_db, te_escucha_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "colombina-unificado-dev-key")

# --- Registro de Blueprints ---
app.register_blueprint(ruta_finder_bp, url_prefix="/ruta-finder")
app.register_blueprint(te_escucha_bp, url_prefix="/pqr")

# --- Base de datos de Te Escucha (PQR) ---
app.teardown_appcontext(close_db)
init_db()


@app.route("/")
def home():
    return render_template("home.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
