import json
import math
import os

import requests
from flask import Blueprint, jsonify, render_template, request

ruta_finder_bp = Blueprint("ruta_finder", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

with open(os.path.join(DATA_DIR, "routes_data.geojson"), encoding="utf-8") as f:
    ROUTES_GEOJSON = json.load(f)

with open(os.path.join(DATA_DIR, "routes_adm.geojson"), encoding="utf-8") as f:
    ROUTES_ADM_GEOJSON = json.load(f)

# Bounding box aproximado de la zona operativa (Valle del Cauca: Tulua, Zarzal, Roldanillo, Andalucia/Bugalagrande)
# usado para sesgar la geocodificación y evitar resultados fuera de la región.
VIEWBOX = "-76.35,4.55,-75.95,3.95"  # left,top,right,bottom (lon,lat)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def haversine_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos lat/lon (formula de Haversine)."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def to_local_xy(lat, lon, ref_lat, ref_lon):
    """Proyección equirectangular simple centrada en ref_lat/ref_lon (válida para distancias regionales cortas)."""
    R = 6371000.0
    x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * R
    y = math.radians(lat - ref_lat) * R
    return x, y


def point_to_segment_distance_m(plat, plon, alat, alon, blat, blon):
    """Distancia mínima (m) de un punto a un segmento de línea, vía proyección local plana."""
    ref_lat, ref_lon = alat, alon
    px, py = to_local_xy(plat, plon, ref_lat, ref_lon)
    ax, ay = 0.0, 0.0
    bx, by = to_local_xy(blat, blon, ref_lat, ref_lon)

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return haversine_m(plat, plon, alat, alon)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy

    return math.hypot(px - cx, py - cy)


def nearest_routes(lat, lon, top_n=5):
    results = []
    for feature in ROUTES_GEOJSON["features"]:
        coords = feature["geometry"]["coordinates"]  # [ [lon,lat], ... ]
        min_dist = float("inf")
        for i in range(len(coords) - 1):
            lon_a, lat_a = coords[i]
            lon_b, lat_b = coords[i + 1]
            d = point_to_segment_distance_m(lat, lon, lat_a, lon_a, lat_b, lon_b)
            if d < min_dist:
                min_dist = d
        if len(coords) == 1:
            lon_a, lat_a = coords[0]
            min_dist = haversine_m(lat, lon, lat_a, lon_a)

        results.append(
            {
                "ruta": feature["properties"]["ruta"],
                "zona": feature["properties"]["zona"],
                "distancia_m": round(min_dist, 1),
                "id": feature["properties"]["id"],
            }
        )

    results.sort(key=lambda r: r["distancia_m"])
    return results[:top_n]


def nearest_routes_adm(lat, lon, top_n=5):
    """Igual que nearest_routes pero sobre los trazados de rutas administrativas
    (routes_adm.geojson contiene trazados y paradas; solo los trazados tienen geometría de línea)."""
    results = []
    for feature in ROUTES_ADM_GEOJSON["features"]:
        props = feature["properties"]
        if props.get("tipo") != "trazado":
            continue

        coords = feature["geometry"]["coordinates"]  # [ [lon,lat], ... ]
        min_dist = float("inf")
        for i in range(len(coords) - 1):
            lon_a, lat_a = coords[i]
            lon_b, lat_b = coords[i + 1]
            d = point_to_segment_distance_m(lat, lon, lat_a, lon_a, lat_b, lon_b)
            if d < min_dist:
                min_dist = d
        if len(coords) == 1:
            lon_a, lat_a = coords[0]
            min_dist = haversine_m(lat, lon, lat_a, lon_a)

        results.append(
            {
                "ruta": props["ruta"],
                "horario": props.get("horario", ""),
                "color": props.get("color", "#0B3D91"),
                "distancia_m": round(min_dist, 1),
                "id": props["id"],
            }
        )

    results.sort(key=lambda r: r["distancia_m"])
    return results[:top_n]


@ruta_finder_bp.route("/")
def index():
    return render_template("ruta_finder/index.html")


@ruta_finder_bp.route("/api/routes")
def api_routes():
    return jsonify(ROUTES_GEOJSON)


@ruta_finder_bp.route("/api/routes_adm")
def api_routes_adm():
    return jsonify(ROUTES_ADM_GEOJSON)


@ruta_finder_bp.route("/api/geocode")
def api_geocode():
    direccion = request.args.get("direccion", "").strip()
    if not direccion:
        return jsonify({"error": "Debe indicar una dirección"}), 400

    params = {
        "q": direccion,
        "format": "json",
        "countrycodes": "co",
        "viewbox": VIEWBOX,
        "bounded": 0,
        "limit": 5,
    }
    headers = {"User-Agent": "ColombinaRutaFinder/1.0 (uso interno)"}

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return jsonify({"error": f"Error consultando el geocodificador: {e}"}), 502

    if not data:
        return jsonify({"error": "No se encontró la dirección. Intenta ser más específico (incluye ciudad)."}), 404

    candidatos = [
        {
            "display_name": d["display_name"],
            "lat": float(d["lat"]),
            "lon": float(d["lon"]),
        }
        for d in data
    ]
    return jsonify({"candidatos": candidatos})


@ruta_finder_bp.route("/api/buscar")
def api_buscar():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros lat/lon inválidos"}), 400

    rutas = nearest_routes(lat, lon, top_n=5)
    return jsonify({"punto": {"lat": lat, "lon": lon}, "rutas": rutas})


@ruta_finder_bp.route("/api/buscar_adm")
def api_buscar_adm():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Parámetros lat/lon inválidos"}), 400

    rutas = nearest_routes_adm(lat, lon, top_n=5)
    return jsonify({"punto": {"lat": lat, "lon": lon}, "rutas": rutas})
