"""
🧠 Second Brain — Local-first personal knowledge management.

Run with: python app/main.py
Open: http://localhost:8000
"""
import os
import json
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
from app.database import init_db, validate_api_key, has_api_keys
from app.routes import api

app = Flask(__name__, static_folder="static")
sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Resolve static folder path relative to project root
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Initialize database on startup
with app.app_context():
    init_db()

# Start auto-backup scheduler if enabled
try:
    from app.autobackup import get_config, start_autobackup
    cfg = get_config()
    if cfg.get("enabled"):
        start_autobackup()
except Exception as e:
    print(f"⚠️ Auto-backup init skipped: {e}")

# Mount API routes
app.register_blueprint(api, url_prefix="/api/v1")


# ─── AUTH MIDDLEWARE ────────────────────────────────────────────────────────

# Endpoints that don't require authentication
PUBLIC_ENDPOINTS = {
    "health",
    "serve_ui",
    "serve_graph",
    "serve_hub",
    "serve_skills",
    "serve_repo",
    "serve_browser",
    "serve_settings",
    "serve_colleagues",
    "serve_calendar",
    "serve_knowledge",
    "cors_test",
    "serve_vendor",
    "static",
    # Auth endpoints — always accessible
    "api.setup",
    "api.auth_status",
    "api.trigger_reindex",
}


@app.before_request
def handle_options_and_favicon():
    if request.method == "OPTIONS":
        return Response(status=204)
    if request.path == "/favicon.ico":
        return Response(status=204)
    return None


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        return Response(status=204)


@app.before_request
def check_auth():
    """
    Check API key on every request.
    - If no keys exist yet, allow all requests (first-time setup).
    - If keys exist, require X-API-Key header or ?api_key= query param.
    - Public endpoints are always accessible.
    """
    # Allow public endpoints
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    # Allow all static files (CSS, JS, images, vendor scripts)
    if request.endpoint == 'static' or request.path.startswith('/static/') or request.path.startswith('/vendor/'):
        return None

    # Allow socket.io polling endpoint (WebSocket auth handled separately)
    if request.path.startswith('/socket.io/'):
        return None

    # Allow if no keys exist yet (first-time setup)
    if not has_api_keys():
        return None

    # Check for API key in header or query param
    api_key = request.headers.get("X-API-Key") or request.args.get("api_key")

    if not api_key:
        return jsonify(
            error="API key required. Pass via X-API-Key header or ?api_key= query param.",
            setup_url="/api/v1/setup"
        ), 401

    key_info = validate_api_key(api_key)
    if not key_info:
        return jsonify(error="Invalid or revoked API key"), 401

    # Store key info on request for downstream use
    request.api_key_info = key_info
    return None


@app.route("/")
def serve_ui():
    import time
    resp = send_from_directory(STATIC_DIR, "index.html")
    v = int(time.time())
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Version"] = str(v)
    resp.headers["ETag"] = str(v)
    return resp


@app.route("/api/v1/cors-test")
def cors_test():
    """Debug endpoint to check browser connectivity."""
    return jsonify(status="ok", message="API is reachable")


@app.route("/graph")
def serve_graph():
    """Serve the vis.js knowledge graph page."""
    resp = send_from_directory(STATIC_DIR, "graph.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/knowledge")
def serve_knowledge():
    """Serve the full-screen canvas knowledge graph."""
    resp = send_from_directory(STATIC_DIR, "knowledge.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/hub")
def serve_hub():
    """Serve the Personal Hub page."""
    resp = send_from_directory(STATIC_DIR, "hub.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/skills")
def serve_skills():
    """Serve the Skills page."""
    resp = send_from_directory(STATIC_DIR, "skills.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/repo/<int:item_id>")
def serve_repo(item_id):
    """Serve the Repository Detail page."""
    resp = send_from_directory(STATIC_DIR, "repo.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/browser")
def serve_browser():
    """Serve the Knowledge Browser page."""
    resp = send_from_directory(STATIC_DIR, "browser.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/settings")
def serve_settings():
    """Serve the Settings page."""
    resp = send_from_directory(STATIC_DIR, "settings.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/colleagues")
def serve_colleagues():
    """Serve the Colleagues page."""
    resp = send_from_directory(STATIC_DIR, "colleagues.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/calendar")
def serve_calendar():
    """Serve the Calendar page."""
    resp = send_from_directory(STATIC_DIR, "calendar.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.route("/vendor/<path:filename>")
def serve_vendor(filename):
    """Serve vendor JS files (Socket.IO client)."""
    vendor_dir = os.path.join(STATIC_DIR, "vendor")
    resp = send_from_directory(vendor_dir, filename)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/health")
def health():
    return jsonify(status="ok", service="second-brain")


# ─── WEBSOCKET EVENTS ──────────────────────────────────────────────────────

connected_users = {}  # sid -> {"name": str, "page": str}


@sio.on("connect")
def handle_connect():
    connected_users[request.sid] = {"name": "Anonymous", "page": "/"}
    emit("user_count", {"count": len(connected_users)}, broadcast=True)
    emit("users_list", {"users": list(connected_users.values())}, broadcast=True)


@sio.on("disconnect")
def handle_disconnect():
    connected_users.pop(request.sid, None)
    emit("user_count", {"count": len(connected_users)}, broadcast=True)
    emit("users_list", {"users": list(connected_users.values())}, broadcast=True)


@sio.on("identify")
def handle_identify(data):
    if request.sid in connected_users:
        connected_users[request.sid]["name"] = data.get("name", "Anonymous")
        emit("users_list", {"users": list(connected_users.values())}, broadcast=True)


@sio.on("navigate")
def handle_navigate(data):
    if request.sid in connected_users:
        connected_users[request.sid]["page"] = data.get("page", "/")
        emit("user_navigated", {
            "name": connected_users[request.sid]["name"],
            "page": data.get("page", "/")
        }, broadcast=True, include_self=False)


@sio.on("item_created")
def handle_item_created(data):
    emit("item_created", data, broadcast=True, include_self=False)


@sio.on("item_updated")
def handle_item_updated(data):
    emit("item_updated", data, broadcast=True, include_self=False)


@sio.on("item_deleted")
def handle_item_deleted(data):
    emit("item_deleted", data, broadcast=True, include_self=False)


@sio.on("chat_message")
def handle_chat_message(data):
    emit("chat_message", {
        "user": connected_users.get(request.sid, {}).get("name", "Anonymous"),
        "message": data.get("message", ""),
        "timestamp": data.get("timestamp", "")
    }, broadcast=True)


@sio.on("cursor_move")
def handle_cursor_move(data):
    emit("cursor_move", {
        "user": connected_users.get(request.sid, {}).get("name", "Anonymous"),
        "x": data.get("x", 0),
        "y": data.get("y", 0)
    }, broadcast=True, include_self=False)


if __name__ == "__main__":
    print("🧠 Second Brain starting at http://localhost:8000")
    sio.run(app, host="0.0.0.0", port=8000, debug=False, allow_unsafe_werkzeug=True)
