# Flock Energy — Urja Meter Ops REST API Wrapper

A clean, resilient, and production-grade REST API service built over the legacy **Urja Meter Ops** utility web portal (`https://urja-ops.flockenergy.tech`).

This service automates authentication, session lifecycle management, and transparent re-authentication; normalizes messy, untyped HTML/JSON responses into strongly typed OpenAPI-compliant models; and exposes structured endpoints for downstream consumers, complete with a full network hierarchy tree, cryptographic bulk export tunnel, and an interactive operations dashboard.

---

## 📑 Table of Contents
- [Architecture & Design](#-architecture--design)
- [Project Directory Structure](#-project-directory-structure)
- [Quickstart: Installation & Running](#-quickstart-installation--running)
- [API Documentation & Endpoints](#-api-documentation--endpoints)
- [Sample Requests & Responses](#-sample-requests--responses)
- [Optional Extensions Implemented](#-optional-extensions-implemented)
- [Assumptions & Design Trade-offs](#-assumptions--design-trade-offs)
- [What Was Intentionally Omitted & Future Improvements](#-what-was-intentionally-omitted--future-improvements)
- [Important Deliverables Links](#-important-deliverables-links)

---

## 🏗 Architecture & Design

The wrapper acts as an intelligent intermediary proxy between downstream analytics/client services and the upstream Urja Ops portal:

```
┌────────────────────────────────────────────────────────┐
│               Downstream Client / UI                   │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTP REST / JSON
                           ▼
┌────────────────────────────────────────────────────────┐
│          Flock Energy API Wrapper (FastAPI)            │
│  - OpenAPI 3.1 Documentation & Swagger UI (/docs)      │
│  - Operations Dashboard & Telemetry Visualizer (/)     │
│  - Data Validation, Normalization & Pydantic Models    │
│  - In-Memory Cache & Freshness Management              │
│  - Network Hierarchy Tree Reconstructor                │
└──────────────────────────┬─────────────────────────────┘
                           │ Async HTTP with Auto-Reauth & HMAC
                           ▼
┌────────────────────────────────────────────────────────┐
│          UrjaPortalClient Adapter Layer                │
│  - Persistent Cookie Jar (__Secure-better-auth)        │
│  - SvelteKit CSRF Origin Enforcer                      │
│  - HMAC-SHA256 Request Signer (/portal/export)         │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTPS (Form URL-Encoded / JSON / SvelteKit)
                           ▼
┌────────────────────────────────────────────────────────┐
│        Upstream Legacy Portal (Urja Meter Ops)         │
│             https://urja-ops.flockenergy.tech          │
└────────────────────────────────────────────────────────┘
```

### Core Components
1. **Client Adapter (`app/client.py`)**:
   - Manages asynchronous HTTP sessions using `httpx.AsyncClient` with automatic cookie jar persistence.
   - Bypasses SvelteKit CSRF barriers by sending mandatory `Origin` and `Referer` headers.
   - Employs double-checked asynchronous locking (`asyncio.Lock`) to prevent thundering-herd re-authentication during session expiration.
   - Implements automated HMAC-SHA256 signature generation for authenticated bulk data ingestion.
2. **Data Transformation & Schemas (`app/models.py`)**:
   - Converts stringified metrics (`kwh`, `kvah`, `volt_r`, `latitude`, `longitude`) to typed Python floats.
   - Translates Indian timestamps (`DD/MM/YYYY HH:mm`) into standardized ISO-8601 strings with explicit IST offset (`+05:30`).
   - Pre-computes analytical consumption aggregates (net kWh consumption, start/end readings, voltage averages, and extremes).
3. **API Routing Layer (`app/main.py`)**:
   - Clean, modular FastAPI endpoints with automatic OpenAPI 3.1 documentation generation.
   - Built-in lightweight, responsive Web Operations Dashboard served at `/` and `/dashboard`.

---

## 📂 Project Directory Structure

```
flock-energy-api/
├── app/
│   ├── __init__.py           # Application package definition
│   ├── config.py             # Pydantic Settings (environment variables & defaults)
│   ├── models.py             # Pydantic schemas (typed models for responses)
│   ├── client.py             # Urja Portal HTTP adapter, scraper, and HMAC signer
│   └── main.py               # FastAPI routes, OpenAPI config, & dashboard UI
├── tests/
│   ├── __init__.py
│   └── test_api.py           # Comprehensive pytest suite (12 tests)
├── PROTOCOL.md               # Detailed write-up of legacy portal behavior
├── REFLECTION.md             # In-depth answers to the 5 reflection questions
├── openapi.json              # Exported OpenAPI 3.1.0 specification
├── export_openapi.py         # Script to regenerate openapi.json from live app
├── requirements.txt          # Python project dependencies
├── pytest.ini                # Pytest configuration
├── .gitignore                # Git ignore rules
└── README.md                 # Project documentation (this document)
```

---

## 🚀 Quickstart: Installation & Running

### Prerequisites
- Python 3.10+ (tested on Python 3.12)
- `uv` (recommended) or standard `pip`

### 1. Clone & Set Up Environment

#### Option A: Using `uv` (Recommended - Ultra Fast)
```bash
git clone https://github.com/your-username/flock-energy-api.git
cd flock-energy-api

# Create virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt --python .venv/Scripts/python.exe
```

#### Option B: Using standard Python `venv` + `pip`
```bash
python -m venv .venv

# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables (Optional)
The service runs out-of-the-box with default portal credentials. If you wish to override them, create a `.env` file:
```env
PORTAL_BASE_URL=https://urja-ops.flockenergy.tech
PORTAL_EMAIL=operator@urja.local
PORTAL_PASSWORD=urja-ops-2026
CACHE_TTL_SECONDS=300
HTTP_TIMEOUT_SECONDS=20.0
```

### 3. Run the Service
Start the service using `uvicorn`:
```bash
# On Windows PowerShell:
$env:PYTHONPATH="."
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

# On Linux / macOS / Bash:
PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

Once started:
- **Operations Dashboard**: [http://localhost:8080/](http://localhost:8080/)
- **Interactive Swagger UI Documentation**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **ReDoc Documentation**: [http://localhost:8080/redoc](http://localhost:8080/redoc)
- **Raw OpenAPI Schema**: [http://localhost:8080/openapi.json](http://localhost:8080/openapi.json)

### 4. Run the Test Suite
Execute the complete test suite:
```bash
$env:PYTHONPATH="."
.venv\Scripts\python.exe -m pytest tests/test_api.py -v
```
*(All 12 tests pass, verifying normalization, error cases, filtering, and endpoints).*

---

## 📡 API Documentation & Endpoints

| Method | Path | Summary | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Health Check | Verifies service and upstream Urja portal connectivity. |
| `POST` | `/api/v1/auth/login` | Manual Login | Explicitly re-authenticates or overrides credentials. |
| `POST` | `/api/v1/auth/cache/clear` | Clear Cache | Flushes in-memory search and export caches. |
| `GET` | `/api/v1/meters` | List / Search Meters | Supports pagination, search (`q`), and multi-attribute filters (`make`, `phase`, `status`, `dt_code`). |
| `GET` | `/api/v1/meters/bulk-export` | Bulk Export Tunnel | HMAC-SHA256 signed direct export of all 403 meters. |
| `GET` | `/api/v1/meters/{id}` | Meter Details | Full meter profile (nameplate, coordinates, 7-tier hierarchy). |
| `GET` | `/api/v1/meters/{id}/consumption` | Consumption Telemetry | Normalized 337 half-hourly readings with computed analytics. |
| `GET` | `/api/v1/transformers` | List Transformers | Paginated list of utility distribution transformers. |
| `GET` | `/api/v1/hierarchy` | Network Hierarchy | Reconstructed 7-tier distribution network tree with meter counts. |

---

## 💻 Sample Requests & Responses

### 1. Health Check
```bash
curl -X GET "http://localhost:8080/health"
```
**Response (200 OK):**
```json
{
  "status": "healthy",
  "upstream_portal": "connected",
  "upstream_authenticated": true,
  "version": "1.0.0",
  "timestamp": "2026-09-10T17:15:00.000000+00:00"
}
```

### 2. List & Filter Meters
Query single-phase meters manufactured by HPL:
```bash
curl -X GET "http://localhost:8080/api/v1/meters?make=HPL&phase=single&page=1&page_size=2"
```
**Response (200 OK):**
```json
{
  "items": [
    {
      "meter_id": "J100000",
      "serial_no": "SE33962",
      "make": "HPL",
      "phase_type": "single",
      "install_status": "Decommissioned",
      "dt_code": "DT-001"
    },
    {
      "meter_id": "J100005",
      "serial_no": "SE38622",
      "make": "HPL",
      "phase_type": "single",
      "install_status": "Installed",
      "dt_code": "DT-006"
    }
  ],
  "total": 102,
  "page": 1,
  "page_size": 2,
  "total_pages": 51
}
```

### 3. Get Meter Details (Unified Profile)
```bash
curl -X GET "http://localhost:8080/api/v1/meters/J100000"
```
**Response (200 OK):**
```json
{
  "meter_id": "J100000",
  "serial_no": "SE33962",
  "make": "HPL",
  "phase_type": "single",
  "install_status": "Decommissioned",
  "install_type": "Whole Current",
  "build": "legacy",
  "dt_code": "DT-001",
  "geo": {
    "latitude": 26.938961,
    "longitude": 75.830957
  },
  "hierarchy": {
    "zone": { "name": "Jaipur Zone 1", "code": "Z-01" },
    "circle": { "name": "Circle 1", "code": "C-01" },
    "division": { "name": "Division 1", "code": "D-01" },
    "subdivision": { "name": "Subdivision 1", "code": "SD-01" },
    "substation": { "name": "Substation 1", "code": "SS-01" },
    "feeder": { "name": "Feeder 1", "code": "F-001" },
    "dt": { "name": "Malviya Nagar DT 1", "code": "DT-001" }
  },
  "extra_parameters": {}
}
```

### 4. Meter Consumption Telemetry & Analytics
```bash
curl -X GET "http://localhost:8080/api/v1/meters/J100000/consumption"
```
**Response (200 OK):**
```json
{
  "meter_id": "J100000",
  "summary": {
    "reading_count": 337,
    "start_time": "2026-06-23T23:30:00+05:30",
    "end_time": "2026-06-30T23:30:00+05:30",
    "start_kwh": 48438.74,
    "end_kwh": 48580.79,
    "net_kwh_consumed": 142.05,
    "avg_voltage": 228.4,
    "min_voltage": 214.0,
    "max_voltage": 245.0
  },
  "readings": [
    {
      "timestamp": "2026-06-23T23:30:00+05:30",
      "raw_timestamp": "23/06/2026 23:30",
      "kwh": 48438.74,
      "kvah": 52313.84,
      "volt_r": 226.0
    },
    {
      "timestamp": "2026-06-24T00:00:00+05:30",
      "raw_timestamp": "24/06/2026 00:00",
      "kwh": 48439.16,
      "kvah": 52314.3,
      "volt_r": 234.0
    }
  ]
}
```

### 5. Reconstructed Network Hierarchy Tree
```bash
curl -X GET "http://localhost:8080/api/v1/hierarchy"
```
**Response Snippet (200 OK):**
```json
{
  "total_meters": 403,
  "roots": [
    {
      "id": "Z-01",
      "name": "Jaipur Zone 1",
      "level": "zone",
      "meter_count": 137,
      "children": [
        {
          "id": "C-01",
          "name": "Circle 1",
          "level": "circle",
          "meter_count": 78,
          "children": [
            {
              "id": "D-01",
              "name": "Division 1",
              "level": "division",
              "meter_count": 20,
              "children": [...]
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 🌟 Optional Extensions Implemented

All optional extensions outlined in the assignment take-home brief have been implemented:

1. **Network Hierarchy Reconstruction (`/api/v1/hierarchy`)**:
   Rebuilds the complete 7-tier utility hierarchy tree (`Zone → Circle → Division → Subdivision → Substation → Feeder → DT → Meters`) with aggregate meter counts at every level.
2. **Cryptographic Bulk Export Tunnel (`/api/v1/meters/bulk-export`)**:
   Discovered and reverse-engineered the internal HMAC-SHA256 signature scheme (`GET /portal/keys` and `GET /portal/export`), enabling high-throughput bulk dumps of all 403 meters in a single operation.
3. **Local Indexing & Query Layer**:
   Expanded the `GET /api/v1/meters` endpoint to support multi-dimensional filtering (`make`, `phase`, `status`, `dt_code`) that the legacy portal was incapable of serving natively.
4. **Data Freshness & Invalidation**:
   Configurable in-memory caching with automatic TTL expiration (5 minutes default) and an explicit cache invalidation endpoint (`POST /api/v1/auth/cache/clear`).
5. **Modern Operations Dashboard UI**:
   A sleek, dark-mode operations dashboard rendered directly from the server at `/` and `/dashboard` featuring live stat counters, meter filtering, consumption sparklines/charts, and direct navigation links to OpenAPI docs.

---

## ⚖️ Assumptions & Design Trade-offs

### Assumptions
1. **Timezone**: Non-standard timestamps (`DD/MM/YYYY HH:mm`) represent Indian Standard Time (IST, `UTC+05:30`) based on Jaipur geographical coordinates and utility naming.
2. **Read-Only Ingestion**: The upstream portal operates in read-only mode; data changes occur periodically rather than per-millisecond, enabling safe in-memory caching.
3. **Double-Checked Concurrency**: Asynchronous requests can occur simultaneously, requiring thread-safe session acquisition to avoid duplicate concurrent logins.

### Architectural Trade-offs
- **In-Memory Cache vs. Distributed Redis**: For ease of local evaluation and zero-external-dependency requirement, an in-process cache was chosen over Redis. In production, Redis would be used.
- **Enriched In-Memory Filtering vs. Database Engine**: Rather than standing up an external SQLite or Postgres database, filtered queries leverage the cached bulk export dataset in memory. This is ultra-fast for ~403 meters (< 5ms response time), but would require an indexed DB if scaling to > 100,000 meters.

---

## 🚧 What Was Intentionally Omitted & Future Improvements

### Intentionally Omitted
- **Database Persistence**: The prompt emphasized evaluating architecture and reasoning over complex local infrastructure; thus, in-memory caching was selected over setting up external DBMS services.
- **Write / Mutation Operations**: In accordance with the ground rules to "treat the portal as read-only," meter creation, deletion, or editing endpoints were not introduced.

### Future Improvements with More Time
1. **PostGIS Geospatial Radius Searching**:
   Implement spatial queries (e.g. `GET /api/v1/meters/nearby?lat=...&lng=...&radius=5km`) to identify meters within proximity of a grid fault.
2. **Automatic Anomaly Detection & Webhooks**:
   Monitor voltage levels across the 337 readings to trigger alerts on brownouts (< 210V) or suspected meter tampering (prolonged 0 kWh consumption).
3. **Historical Consumption Range Queries**:
   Add query parameter pass-through (`?start_date=...&end_date=...`) if the legacy portal adds arbitrary historical windows.

---

## 🔗 Important Deliverables Links

- **[PROTOCOL.md](file:///c:/Users/harsh/OneDrive/Desktop/flock-energy-api/PROTOCOL.md)**: Comprehensive protocol analysis, CSRF mechanisms, discovered endpoints, and data anomalies.
- **[REFLECTION.md](file:///c:/Users/harsh/OneDrive/Desktop/flock-energy-api/REFLECTION.md)**: Complete answers to all 5 required engineering reflection questions.
- **[openapi.json](file:///c:/Users/harsh/OneDrive/Desktop/flock-energy-api/openapi.json)**: Fully formatted OpenAPI 3.1.0 specification.
