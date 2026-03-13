"""
SheGuard Backend - Flask API Server
Women's Safety Wearable System
"""

import sqlite3
import json
import uuid
import math
import sys
import os
from datetime import datetime
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
from twilio.rest import Client

# Force UTF-8 stdout so emoji in log lines don't crash on Windows cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ──────────────────────────────────────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────────────────────────────────────

# Resolve the frontend directory (one level up from backend/)
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

app = Flask(__name__)
app.config["SECRET_KEY"] = "sheguard-secret-2026"

# ──────────────────────────────────────────────────────────────────────────────
# Twilio Configuration (Replace with your actual credentials)
# Set these environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
# ──────────────────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "your_twilio_account_sid_here")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN",  "your_twilio_auth_token_here")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+1234567890")

# Allow ALL origins including file:// (origin=null) for local dev.
# Using explicit header approach to cover the null-origin edge case.
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "allow_headers": ["Content-Type", "Authorization"],
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    }
})

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DATABASE = "sheguard.db"

# ──────────────────────────────────────────────────────────────────────────────
# Database Helpers
# ──────────────────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    """Create all tables if they don't exist."""
    with app.app_context():
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        cursor = db.cursor()

        # Users / Devices table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id        TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                phone     TEXT UNIQUE NOT NULL,
                password  TEXT NOT NULL,
                is_admin  INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Add is_admin column if it doesn't exist (for existing databases)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        except:
            pass  # Column already exists

        # Emergency contacts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS emergency_contacts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   TEXT NOT NULL,
                name      TEXT NOT NULL,
                phone     TEXT NOT NULL,
                relation  TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # SOS Events
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sos_events (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                user_name   TEXT,
                latitude    REAL NOT NULL,
                longitude   REAL NOT NULL,
                timestamp   TEXT DEFAULT (datetime('now')),
                status      TEXT DEFAULT 'active',
                resolved_at TEXT
            )
        """)

        # Location history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS location_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id  TEXT NOT NULL,
                latitude   REAL NOT NULL,
                longitude  REAL NOT NULL,
                accuracy   REAL,
                timestamp  TEXT DEFAULT (datetime('now'))
            )
        """)

        # Incident / crime reports
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                reported_by TEXT,
                latitude    REAL NOT NULL,
                longitude   REAL NOT NULL,
                description TEXT,
                severity    TEXT DEFAULT 'medium',
                verified    INTEGER DEFAULT 0,
                timestamp   TEXT DEFAULT (datetime('now'))
            )
        """)

        # Alert logs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alert_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sos_id      TEXT,
                contact     TEXT NOT NULL,
                method      TEXT DEFAULT 'sms',
                status      TEXT DEFAULT 'sent',
                timestamp   TEXT DEFAULT (datetime('now'))
            )
        """)

        # Seed some default incident data (Vijaywada station zone)
        cursor.execute("SELECT COUNT(*) as cnt FROM incidents")
        row = cursor.fetchone()
        if row["cnt"] == 0:
            seed_incidents = [
                (16.4955, 80.5003, "Isolated pathway near Block C", "high"),
                (16.4975, 80.4988, "Poor lighting near sports complex", "medium"),
                (16.4960, 80.4979, "Deserted area behind library", "high"),
                (16.4972, 80.5010, "Parking zone with limited visibility", "medium"),
                (16.4980, 80.5000, "Night-time activity reported near gate", "low"),
            ]
            cursor.executemany(
                "INSERT INTO incidents (latitude, longitude, description, severity, verified) VALUES (?,?,?,?,1)",
                seed_incidents,
            )

        db.commit()
        db.close()
        print("[OK] Database initialised successfully.")

# ──────────────────────────────────────────────────────────────────────────────
# Utility
# ──────────────────────────────────────────────────────────────────────────────
def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

def haversine(lat1, lon1, lat2, lon2):
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ──────────────────────────────────────────────────────────────────────────────
# Frontend – serve index.html at the root so origin = http://localhost:5000
# This permanently eliminates all CORS issues (same-origin, no preflight needed)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def serve_frontend():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/favicon.ico", methods=["GET"])
def favicon():
    # Inline SVG shield icon in SheGuard pink — no external file needed
    from flask import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<path d="M16 2 L28 7 L28 17 C28 24 22 29 16 31 C10 29 4 24 4 17 L4 7 Z"'
        ' fill="#ff2e63" stroke="#ff2e63" stroke-width="1"/>'
        '<text x="16" y="22" text-anchor="middle" font-size="14" fill="white"'
        ' font-family="Arial" font-weight="bold">S</text>'
        '</svg>'
    )
    return Response(svg, mimetype="image/svg+xml")

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "service": "SheGuard API",
        "status": "online",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

# ──────────────────────────────────────────────────────────────────────────────
# USER REGISTRATION & AUTH  (simple – no JWT for hackathon simplicity)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    name  = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    password = data.get("password", "")

    if not name or not phone or not password:
        return jsonify({"error": "name, phone and password are required"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
    if existing:
        return jsonify({"error": "Phone number already registered"}), 409

    # Check if this is the first user - if so, make them admin
    total_users = db.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
    is_admin = 1 if total_users == 0 else 0

    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, name, phone, password, is_admin) VALUES (?,?,?,?,?)",
        (user_id, name, phone, password, is_admin)
    )
    db.commit()
    
    message = "🎉 You are the FIRST USER - Admin access granted!" if is_admin else "✅ User registered successfully"
    
    return jsonify({
        "message": message, 
        "user_id": user_id,
        "is_admin": is_admin
    }), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    phone    = data.get("phone", "").strip()
    password = data.get("password", "")

    db = get_db()
    user = db.execute(
        "SELECT id, name, phone, is_admin FROM users WHERE phone=? AND password=?",
        (phone, password)
    ).fetchone()

    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    return jsonify({"message": "Login successful", "user": row_to_dict(user)}), 200


# ──────────────────────────────────────────────────────────────────────────────
# 👨‍💼 ADMIN ACCESS
# ──────────────────────────────────────────────────────────────────────────────
ADMIN_SECRET_KEY = "sheguard-admin-2026"  # Secret key to promote user to admin

@app.route("/api/admin/promote", methods=["POST"])
def promote_to_admin():
    """Promote user to admin with secret key"""
    data = request.get_json()
    user_id = data.get("user_id")
    secret_key = data.get("secret_key")

    if secret_key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Invalid secret key"}), 401

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    db = get_db()
    db.execute("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
    db.commit()
    
    return jsonify({"message": "User promoted to admin"}), 200


@app.route("/api/admin/users", methods=["GET"])
def get_all_users():
    """Get all users (admin only)"""
    user_id = request.args.get("user_id")
    secret_key = request.args.get("secret_key")

    if secret_key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    user = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user or not user["is_admin"]:
        return jsonify({"error": "Admin access required"}), 403

    all_users = db.execute("SELECT id, name, phone, is_admin, created_at FROM users").fetchall()
    return jsonify(rows_to_list(all_users))


@app.route("/api/admin/incidents", methods=["GET"])
def get_all_incidents_admin():
    """Get all incidents with user info (admin only)"""
    user_id = request.args.get("user_id")
    secret_key = request.args.get("secret_key")

    if secret_key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    user = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user or not user["is_admin"]:
        return jsonify({"error": "Admin access required"}), 403

    incidents = db.execute("""
        SELECT i.*, u.name as reported_by_name 
        FROM incidents i 
        LEFT JOIN users u ON i.reported_by = u.id 
        ORDER BY i.timestamp DESC
    """).fetchall()
    return jsonify(rows_to_list(incidents))


@app.route("/api/admin/sos-events", methods=["GET"])
def get_all_sos_admin():
    """Get all SOS events (admin only)"""
    user_id = request.args.get("user_id")
    secret_key = request.args.get("secret_key")

    if secret_key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    user = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user or not user["is_admin"]:
        return jsonify({"error": "Admin access required"}), 403

    sos_events = db.execute("""
        SELECT * FROM sos_events 
        ORDER BY timestamp DESC
    """).fetchall()
    return jsonify(rows_to_list(sos_events))


@app.route("/api/admin/stats", methods=["GET"])
def get_admin_stats():
    """Get admin dashboard stats (admin only)"""
    user_id = request.args.get("user_id")
    secret_key = request.args.get("secret_key")

    if secret_key != ADMIN_SECRET_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    user = db.execute("SELECT is_admin FROM users WHERE id=?", (user_id,)).fetchone()
    
    if not user or not user["is_admin"]:
        return jsonify({"error": "Admin access required"}), 403

    total_users = db.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
    total_sos = db.execute("SELECT COUNT(*) as count FROM sos_events").fetchone()["count"]
    total_incidents = db.execute("SELECT COUNT(*) as count FROM incidents").fetchone()["count"]
    active_sos = db.execute("SELECT COUNT(*) as count FROM sos_events WHERE status='active'").fetchone()["count"]

    return jsonify({
        "total_users": total_users,
        "total_sos_events": total_sos,
        "total_incidents": total_incidents,
        "active_sos_events": active_sos
    })


# ──────────────────────────────────────────────────────────────────────────────
# EMERGENCY CONTACTS
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/contacts/<user_id>", methods=["GET"])
def get_contacts(user_id):
    db = get_db()
    contacts = db.execute(
        "SELECT * FROM emergency_contacts WHERE user_id=?", (user_id,)
    ).fetchall()
    return jsonify(rows_to_list(contacts))


@app.route("/api/contacts/<user_id>", methods=["POST"])
def add_contact(user_id):
    data = request.get_json()
    name     = data.get("name", "").strip()
    phone    = data.get("phone", "").strip()
    relation = data.get("relation", "other")

    if not name or not phone:
        return jsonify({"error": "name and phone are required"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO emergency_contacts (user_id, name, phone, relation) VALUES (?,?,?,?)",
        (user_id, name, phone, relation)
    )
    db.commit()
    return jsonify({"message": "Contact added successfully"}), 201


@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
def delete_contact(contact_id):
    db = get_db()
    db.execute("DELETE FROM emergency_contacts WHERE id=?", (contact_id,))
    db.commit()
    return jsonify({"message": "Contact deleted"})


# ──────────────────────────────────────────────────────────────────────────────
# 🚨  SOS  ALERTS HELPERS
# ──────────────────────────────────────────────────────────────────────────────

# Nearby police stations (Vijaywada area)
POLICE_STATIONS = [
    {"name": "Vijaywada Central Police", "phone": "+918809443332", "lat": 16.5070, "lng": 80.6370},
    {"name": "RTC X Road Police Station", "phone": "+918809443332", "lat": 16.5100, "lng": 80.6300},
    {"name": "Kanuru Police Station", "phone": "+918809443332", "lat": 16.4900, "lng": 80.6200},
]

def find_nearest_police_station(lat, lng):
    """Find nearest police station to given coordinates"""
    nearest = None
    min_distance = float('inf')
    
    for station in POLICE_STATIONS:
        # Simple distance calculation
        distance = math.sqrt((lat - station['lat'])**2 + (lng - station['lng'])**2)
        if distance < min_distance:
            min_distance = distance
            nearest = station
    
    return nearest or POLICE_STATIONS[0]

def send_emergency_sms(phone_number, user_name, latitude, longitude):
    """Send emergency SMS via Twilio"""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message_text = f"🚨 EMERGENCY ALERT!\n\n{user_name} needs help!\n\nLocation: {latitude:.4f}, {longitude:.4f}\n\nGoogle Maps: https://maps.google.com/?q={latitude},{longitude}\n\nSheGuard Alert System"
        
        message = client.messages.create(
            body=message_text,
            from_=TWILIO_PHONE_NUMBER,
            to=phone_number
        )
        print(f"[SMS] Sent to {phone_number} - SID: {message.sid}")
        return True
    except Exception as e:
        print(f"[SMS ERROR] Failed to send to {phone_number}: {str(e)}")
        return False

def send_emergency_call(phone_number, user_name, latitude, longitude):
    """Initiate emergency call via Twilio (TwiML)"""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # TwiML for voice call
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Say voice="alice">Emergency alert! {user_name} needs immediate help!</Say>
            <Say voice="alice">Location coordinates: {latitude:.4f} degrees latitude, {longitude:.4f} degrees longitude.</Say>
            <Say voice="alice">Please respond to help this person immediately.</Say>
            <Pause length="2"/>
            <Say voice="alice">Repeating the location.</Say>
            <Say voice="alice">Visit Google Maps at: maps.google.com with coordinates {latitude} {longitude}</Say>
        </Response>"""
        
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            twiml=twiml
        )
        print(f"[CALL] Initiated to {phone_number} - SID: {call.sid}")
        return True
    except Exception as e:
        print(f"[CALL ERROR] Failed to call {phone_number}: {str(e)}")
        return False

# ──────────────────────────────────────────────────────────────────────────────
# 🚨  SOS  TRIGGER ENDPOINT
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/sos", methods=["POST"])
def trigger_sos():
    data      = request.get_json()
    user_id   = data.get("user_id", "anonymous")
    user_name = data.get("user_name", "Unknown User")
    lat       = data.get("latitude")
    lng       = data.get("longitude")
    contacts  = data.get("contacts", [])   # list of phone strings

    if lat is None or lng is None:
        return jsonify({"error": "latitude and longitude are required"}), 400

    sos_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"

    db = get_db()
    db.execute(
        "INSERT INTO sos_events (id, user_id, user_name, latitude, longitude, timestamp) VALUES (?,?,?,?,?,?)",
        (sos_id, user_id, user_name, lat, lng, timestamp)
    )

    # Find nearest police station
    nearest_police = find_nearest_police_station(lat, lng)
    
    # List of all recipients (parents + nearby police)
    all_recipients = contacts + [nearest_police["phone"]]
    
    print(f"\n[🚨 SOS TRIGGERED]")
    print(f"SOS ID: {sos_id}")
    print(f"User: {user_name}")
    print(f"Location: ({lat}, {lng})")
    print(f"Nearest Police: {nearest_police['name']}")
    print(f"Total Recipients: {len(all_recipients)}")

    # Send SMS and Calls to all recipients in background
    for i, phone in enumerate(all_recipients):
        is_police = phone == nearest_police["phone"]
        recipient_type = "🚔 Police Station" if is_police else "👨‍👩‍👧 Parent/Guardian"
        
        # Send SMS
        print(f"\n[SMS {i+1}/{len(all_recipients)}] Sending to {recipient_type} ({phone})...")
        sms_success = send_emergency_sms(phone, user_name, lat, lng)
        
        # Send Call
        print(f"[CALL {i+1}/{len(all_recipients)}] Initiating call to {recipient_type} ({phone})...")
        call_success = send_emergency_call(phone, user_name, lat, lng)
        
        # Log in database
        status = "sent" if (sms_success and call_success) else "failed"
        db.execute(
            "INSERT INTO alert_logs (sos_id, contact, method, status) VALUES (?,?,?,?)",
            (sos_id, phone, "sms+call", status)
        )

    db.commit()

    # Broadcast via WebSocket to all monitoring dashboards
    sos_payload = {
        "sos_id":    sos_id,
        "user_id":   user_id,
        "user_name": user_name,
        "latitude":  lat,
        "longitude": lng,
        "timestamp": timestamp,
        "nearest_police": nearest_police["name"],
        "contacts_alerted": len(all_recipients)
    }
    socketio.emit("sos_alert", sos_payload, namespace="/safety")

    return jsonify({
        "message": "🚨 SOS triggered! SMS and calls sent to parents and nearby police station.",
        "sos_id": sos_id,
        "timestamp": timestamp,
        "parents_alerted": len(contacts),
        "police_station": nearest_police["name"],
        "total_recipients": len(all_recipients)
    }), 201


@app.route("/api/sos", methods=["GET"])
def get_sos_events():
    status = request.args.get("status", "active")
    db = get_db()
    events = db.execute(
        "SELECT * FROM sos_events WHERE status=? ORDER BY timestamp DESC LIMIT 50",
        (status,)
    ).fetchall()
    return jsonify(rows_to_list(events))


@app.route("/api/sos/<sos_id>/resolve", methods=["PATCH"])
def resolve_sos(sos_id):
    db = get_db()
    resolved_at = datetime.utcnow().isoformat() + "Z"
    db.execute(
        "UPDATE sos_events SET status='resolved', resolved_at=? WHERE id=?",
        (resolved_at, sos_id)
    )
    db.commit()
    socketio.emit("sos_resolved", {"sos_id": sos_id, "resolved_at": resolved_at}, namespace="/safety")
    return jsonify({"message": "SOS event resolved", "sos_id": sos_id})


# ──────────────────────────────────────────────────────────────────────────────
# 📍 LOCATION TRACKING
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/location", methods=["POST"])
def update_location():
    data      = request.get_json()
    device_id = data.get("device_id", "unknown")
    lat       = data.get("latitude")
    lng       = data.get("longitude")
    accuracy  = data.get("accuracy", None)

    if lat is None or lng is None:
        return jsonify({"error": "latitude and longitude are required"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO location_history (device_id, latitude, longitude, accuracy) VALUES (?,?,?,?)",
        (device_id, lat, lng, accuracy)
    )
    db.commit()

    # Broadcast live location over WebSocket
    socketio.emit("location_update", {
        "device_id": device_id,
        "latitude":  lat,
        "longitude": lng,
        "accuracy":  accuracy,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }, namespace="/safety")

    return jsonify({"message": "Location updated", "device_id": device_id}), 201


@app.route("/api/location/<device_id>", methods=["GET"])
def get_location_history(device_id):
    limit = int(request.args.get("limit", 20))
    db = get_db()
    history = db.execute(
        "SELECT * FROM location_history WHERE device_id=? ORDER BY timestamp DESC LIMIT ?",
        (device_id, limit)
    ).fetchall()
    return jsonify(rows_to_list(history))


@app.route("/api/location/<device_id>/latest", methods=["GET"])
def get_latest_location(device_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM location_history WHERE device_id=? ORDER BY timestamp DESC LIMIT 1",
        (device_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "No location data for this device"}), 404
    return jsonify(row_to_dict(row))


# ──────────────────────────────────────────────────────────────────────────────
# 🔥 INCIDENTS / CRIME HEATMAP
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/incidents", methods=["GET"])
def get_incidents():
    """Get all incidents (public heatmap data)"""
    db = get_db()

    # Optional bounding box filter
    min_lat = request.args.get("min_lat", type=float)
    max_lat = request.args.get("max_lat", type=float)
    min_lng = request.args.get("min_lng", type=float)
    max_lng = request.args.get("max_lng", type=float)

    query = "SELECT * FROM incidents WHERE 1=1"
    params = []
    if min_lat is not None: query += " AND latitude >= ?";  params.append(min_lat)
    if max_lat is not None: query += " AND latitude <= ?";  params.append(max_lat)
    if min_lng is not None: query += " AND longitude >= ?"; params.append(min_lng)
    if max_lng is not None: query += " AND longitude <= ?"; params.append(max_lng)

    query += " ORDER BY timestamp DESC"
    incidents = db.execute(query, params).fetchall()
    return jsonify(rows_to_list(incidents))


@app.route("/api/incidents/user/<user_id>", methods=["GET"])
def get_user_incidents(user_id):
    """Get only incidents reported by specific user (PRIVATE)"""
    db = get_db()
    
    incidents = db.execute(
        "SELECT * FROM incidents WHERE reported_by=? ORDER BY timestamp DESC",
        (user_id,)
    ).fetchall()
    
    return jsonify(rows_to_list(incidents))


@app.route("/api/incidents", methods=["POST"])
def report_incident():
    data        = request.get_json()
    user_id     = data.get("user_id")  # Get from logged-in user
    reported_by = user_id or data.get("reported_by", "anonymous")  # Fallback
    lat         = data.get("latitude")
    lng         = data.get("longitude")
    description = data.get("description", "")
    severity    = data.get("severity", "medium")

    if lat is None or lng is None:
        return jsonify({"error": "latitude and longitude are required"}), 400
    if severity not in ("low", "medium", "high"):
        return jsonify({"error": "severity must be low, medium or high"}), 400

    db = get_db()
    cursor = db.execute(
        "INSERT INTO incidents (reported_by, latitude, longitude, description, severity) VALUES (?,?,?,?,?)",
        (reported_by, lat, lng, description, severity)
    )
    db.commit()

    new_incident = row_to_dict(db.execute(
        "SELECT * FROM incidents WHERE id=?", (cursor.lastrowid,)
    ).fetchone())

    # Broadcast new incident
    socketio.emit("new_incident", new_incident, namespace="/safety")

    return jsonify({"message": "Incident reported", "incident": new_incident}), 201


@app.route("/api/incidents/<int:incident_id>/verify", methods=["PATCH"])
def verify_incident(incident_id):
    db = get_db()
    db.execute("UPDATE incidents SET verified=1 WHERE id=?", (incident_id,))
    db.commit()
    return jsonify({"message": "Incident verified", "incident_id": incident_id})


# ──────────────────────────────────────────────────────────────────────────────
# 📡 SEND EMERGENCY ALERT (simulate SMS/call)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/alert/send", methods=["POST"])
def send_alert():
    data    = request.get_json()
    contact = data.get("contact", "").strip()
    message = data.get("message", "SOS! I need help. Please call me immediately.")
    lat     = data.get("latitude")
    lng     = data.get("longitude")

    if not contact:
        return jsonify({"error": "contact number is required"}), 400

    # Build full message with location link if available
    if lat and lng:
        maps_link = f"https://maps.google.com/?q={lat},{lng}"
        message = f"{message}\n📍 My location: {maps_link}"

    # Real SMS Integration
    # We are providing a setup for Fast2SMS as it is the easiest free way to send SMS in India
    FAST2SMS_API_KEY = "SGTBs65Ke2lWMOkHzcw1gmyoFbn0pJ3CUhLQDRiNquxfXarAdthOIWnS71pjseM3GJKct9DERNdovril"
    sms_status = "simulated"
    
    if FAST2SMS_API_KEY != "your_fast2sms_api_key_here":
        try:
            import urllib.request
            import json
            url = "https://www.fast2sms.com/dev/bulkV2"
            payload = {
                "route": "v3",
                "sender_id": "TXTIND",
                "message": message,
                "language": "english",
                "flash": 0,
                "numbers": contact.replace("+91", "").replace(" ", "").strip()
            }
            req = urllib.request.Request(url)
            req.add_header('authorization', FAST2SMS_API_KEY)
            req.add_header('Content-Type', 'application/json')
            
            data = json.dumps(payload).encode('utf-8')
            response = urllib.request.urlopen(req, data=data)
            res_json = json.loads(response.read().decode('utf-8'))
            
            if res_json.get('return'):
                sms_status = "sent"
                print(f"[FAST2SMS] SMS sent to {contact}")
            else:
                print(f"[FAST2SMS ERROR] {res_json}")
                sms_status = "simulated"
        except Exception as e:
            print(f"[FAST2SMS EXCEPTION] {e} - falling back to simulated SMS")
            sms_status = "simulated"

    db = get_db()
    db.execute(
        "INSERT INTO alert_logs (contact, method, status) VALUES (?,?,?)",
        (contact, "sms", sms_status)
    )
    db.commit()

    safe_msg = message.encode('ascii', errors='replace').decode('ascii')
    print(f"[ALERT] -> {contact}: {safe_msg}")

    return jsonify({
        "message": f"✅ Emergency alert {sms_status} successfully" if sms_status != "failed" else "❌ Failed to send SMS",
        "contact": contact,
        "alert_message": message,
        "method": "sms",
        "status": sms_status
    }), 201 if sms_status != "failed" else 500


# ──────────────────────────────────────────────────────────────────────────────
# 📊 STATISTICS / DASHBOARD
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def get_stats():
    db = get_db()
    total_users    = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    active_sos     = db.execute("SELECT COUNT(*) as c FROM sos_events WHERE status='active'").fetchone()["c"]
    total_sos      = db.execute("SELECT COUNT(*) as c FROM sos_events").fetchone()["c"]
    total_incidents = db.execute("SELECT COUNT(*) as c FROM incidents").fetchone()["c"]
    high_risk      = db.execute("SELECT COUNT(*) as c FROM incidents WHERE severity='high'").fetchone()["c"]
    alerts_sent    = db.execute("SELECT COUNT(*) as c FROM alert_logs").fetchone()["c"]

    return jsonify({
        "total_users":     total_users,
        "active_sos":      active_sos,
        "total_sos":       total_sos,
        "total_incidents": total_incidents,
        "high_risk_zones": high_risk,
        "alerts_sent":     alerts_sent
    })


# ──────────────────────────────────────────────────────────────────────────────
# 🗺️  SAFE ROUTE CHECK (nearest incident check)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/api/safe-route", methods=["POST"])
def safe_route():
    data = request.get_json()
    lat  = data.get("latitude")
    lng  = data.get("longitude")

    if lat is None or lng is None:
        return jsonify({"error": "latitude and longitude are required"}), 400

    db = get_db()
    incidents = db.execute("SELECT * FROM incidents WHERE verified=1").fetchall()

    nearby = []
    for inc in incidents:
        dist = haversine(lat, lng, inc["latitude"], inc["longitude"])
        if dist <= 300:  # within 300 metres
            d = dict(inc)
            d["distance_m"] = round(dist, 1)
            nearby.append(d)

    nearby.sort(key=lambda x: x["distance_m"])

    safe = len(nearby) == 0
    return jsonify({
        "safe": safe,
        "message": "✅ Area appears safe" if safe else f"⚠️ {len(nearby)} risk zone(s) detected nearby",
        "nearby_incidents": nearby[:5]
    })


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Events
# ──────────────────────────────────────────────────────────────────────────────
@socketio.on("connect", namespace="/safety")
def on_connect():
    print(f"[WS] Client connected")
    emit("connected", {"message": "Connected to SheGuard safety network"})

@socketio.on("disconnect", namespace="/safety")
def on_disconnect():
    print(f"[WS] Client disconnected")

@socketio.on("join_room", namespace="/safety")
def on_join(data):
    room = data.get("room", "global")
    join_room(room)
    emit("joined", {"room": room})

@socketio.on("leave_room", namespace="/safety")
def on_leave(data):
    room = data.get("room", "global")
    leave_room(room)

# ──────────────────────────────────────────────────────────────────────────────
# 🤖 AI SAFETY ASSISTANT (Antigravity)
# ──────────────────────────────────────────────────────────────────────────────
class AntigravitySafeAssistant:
    def __init__(self):
        self.system_prompt = (
            "You are SheGuard AI Safety Assistant. You help women in unsafe situations. "
            "Provide safety tips, emergency guidance, calm reassurance, and help users "
            "activate SOS or share location. Always be empathetic and concise."
        )

    def process(self, user_message):
        msg = user_message.lower()
        
        # Priority 1: High Danger Keywords
        danger_keywords = ["unsafe", "help", "danger", "scared", "threat", "emergency", "stalk", "follow"]
        if any(word in msg for word in danger_keywords):
            return (
                "I'm here for you. Please stay calm. 🛡️\n"
                "1. PRESS THE SOS BUTTON IMMEDIATELY in the dashboard.\n"
                "2. Call EMERGENCY: 112 (India) right now.\n"
                "3. Head to a crowded, well-lit public area.\n"
                "I am monitoring your safety status. Stay strong."
            )

        # Priority 2: Feature Guidance
        if "location" in msg or "tracking" in msg:
            return (
                "SheGuard keeps your location secure. To share it, ensure the 'GPS History' "
                "is active. If you press SOS, your live coordinates are instantly sent to "
                "your verified emergency contacts via SMS."
            )
        
        if "wearable" in msg or "device" in msg or "features" in msg:
            return (
                "The SheGuard wearable features a dual-pressure SOS trigger, "
                "haptic safety alerts, and AI-predicted safe routing based on "
                "real-time crime data in your area."
            )

        # Priority 3: General Reassurance/Tips
        if "tip" in msg or "safe" in msg or "hello" in msg or "hi" in msg:
            return (
                "Hello! I am your 24/7 SheGuard companion. My tip: always keep "
                "your emergency contacts updated and your device charged. "
                "Is there anything specific you need help with?"
            )

        # Fallback (Simulating AI response)
        return (
            "I understand. Your safety is my priority. Remember, if things feel "
            "wrong, trust your instincts and don't hesitate to use the SOS button "
            "on your screen or your wearable device."
        )

ai_assistant = AntigravitySafeAssistant()

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")
    
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    # Process via our AI logic
    response_text = ai_assistant.process(user_message)
    
    return jsonify({
        "response": response_text,
        "status": "success",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })

# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print("[START] SheGuard API running at http://localhost:5000")
    print("[WS] WebSocket namespace: /safety")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True,
                 allow_unsafe_werkzeug=True, use_reloader=False)
