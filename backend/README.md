# SheGuard Backend

## Quick Start

```bash
# Install dependencies (one-time)
pip install -r requirements.txt

# Run the server
python app.py
```

Server will be available at: **http://localhost:5000**

---

## API Reference

### Health
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| GET | `/api/stats` | Live dashboard stats |

### Authentication
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/register` | `{name, phone, password}` | Register user |
| POST | `/api/login` | `{phone, password}` | Login |

### Emergency Contacts
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/contacts/<user_id>` | Get all contacts |
| POST | `/api/contacts/<user_id>` | Add contact |
| DELETE | `/api/contacts/<contact_id>` | Remove contact |

### SOS Alerts 🚨
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/sos` | `{user_id, user_name, latitude, longitude, contacts[]}` | Trigger SOS |
| GET | `/api/sos?status=active` | — | Get active SOS events |
| PATCH | `/api/sos/<sos_id>/resolve` | — | Mark SOS as resolved |

### Location Tracking 📍
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/location` | `{device_id, latitude, longitude, accuracy}` | Push location |
| GET | `/api/location/<device_id>` | — | Get location history |
| GET | `/api/location/<device_id>/latest` | — | Get latest location |

### Incidents / Crime Heatmap 🔥
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| GET | `/api/incidents` | — | Get all incidents |
| POST | `/api/incidents` | `{latitude, longitude, description, severity}` | Report incident |
| PATCH | `/api/incidents/<id>/verify` | — | Verify incident |

### Alert Dispatch 📡
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/alert/send` | `{contact, message, latitude, longitude}` | Send emergency alert |

### Safe Route Check 🗺️
| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/api/safe-route` | `{latitude, longitude}` | Check if area is safe (within 300m of incidents) |

---

## WebSocket Events (namespace: `/safety`)

| Event (Server → Client) | Payload | Description |
|--------------------------|---------|-------------|
| `sos_alert` | `{sos_id, user_name, latitude, longitude, timestamp}` | New SOS triggered |
| `sos_resolved` | `{sos_id, resolved_at}` | SOS resolved |
| `location_update` | `{device_id, latitude, longitude, timestamp}` | Live location ping |
| `new_incident` | Incident object | New incident reported |

---

## Database

SQLite file: `sheguard.db` (auto-created on first run)

Tables: `users`, `emergency_contacts`, `sos_events`, `location_history`, `incidents`, `alert_logs`
