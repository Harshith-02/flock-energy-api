# Protocol Discovery & Reverse-Engineering Report (Urja Meter Ops)

## 1. Overview & Architectural Footprint
The **Urja Meter Ops** legacy system (`https://urja-ops.flockenergy.tech`) is an internal operations web portal deployed for utility engineers to inspect distribution smart meters, transformers, and consumption metrics.

While presented as an "ageing internal portal without an API," network reconnaissance and client-side bundle inspection revealed that the frontend is actually built with **SvelteKit (v2) with Better-Auth session management**, using server-side rendered layouts paired with client-side reactive fetch routines against private `/portal/*` JSON endpoints.

---

## 2. Authentication & Session Protocol

### 2.1 Login Workflow
- **Form URL**: `POST https://urja-ops.flockenergy.tech/login`
- **Payload Format**: Form URL-encoded (`application/x-www-form-urlencoded`)
- **Payload Fields**:
  - `email`: `operator@urja.local` *(Note: Although prompt documentation referred to "username", the HTML form input explicitly requires the `email` attribute).*
  - `password`: `urja-ops-2026`

### 2.2 Critical CSRF Mechanism & Quirks
- **SvelteKit Origin Enforcement**: Standard HTTP POST requests sent without an explicit `Origin` header are blocked by SvelteKit's built-in Cross-Site Request Forgery (CSRF) mitigation:
  ```http
  HTTP/1.1 403 Forbidden
  Content-Type: text/plain

  Cross-site POST form submissions are forbidden
  ```
- **Resolution**: Automated clients must explicitly supply:
  - `Origin: https://urja-ops.flockenergy.tech`
  - `Referer: https://urja-ops.flockenergy.tech/login`

### 2.3 Session Cookies & Response Semantics
- **Set-Cookie**: On authentication, the server issues a persistent, secure session cookie:
  ```http
  Set-Cookie: __Secure-better-auth.session_token=<token_hash>; Path=/; HttpOnly; Secure; SameSite=Lax
  ```
- **Response Status & Body**: The login form action does not return an HTTP 302 directly; instead, it returns an HTTP 200 containing a SvelteKit form action JSON redirect directive:
  ```json
  {"type": "redirect", "status": 303, "location": "/meters"}
  ```
- **Logout Endpoint**: `POST /api/auth/sign-out` clears the session token.

---

## 3. Discovered Internal Endpoints

Deep inspection of the compiled SvelteKit JavaScript bundles (`nodes/4`, `nodes/5`, `nodes/6`, and chunks) revealed the following underlying internal API surfaces:

### 3.1 Meter Search & Directory
- **Endpoint**: `GET /portal/meters/search?q={query}&page={page}`
- **Authentication**: `__Secure-better-auth.session_token` cookie
- **Query Parameters**:
  - `q`: Search substring (matched against `meterId` or `serialNo`). Empty string returns all meters.
  - `page`: 1-indexed integer.
- **Default Page Size**: Fixed at 20 items per page. Total dataset: 403 meters (21 pages).
- **Response Structure**:
  ```json
  {
    "data": [
      {
        "meterId": "J100000",
        "serialNo": "SE33962",
        "make": "HPL",
        "phaseType": "single",
        "installStatus": "Decommissioned",
        "dtCode": "DT-001"
      }
    ],
    "total": 403
  }
  ```

### 3.2 Meter Geo-Location
- **Endpoint**: `GET /portal/meters/{meterId}/geo`
- **Response**:
  ```json
  {
    "data": {
      "latitude": "26.938961002479868",
      "longitude": "75.83095696146852"
    }
  }
  ```
- **Error Case**: Non-existent meter returns `404 Not Found` with `{"error":"not_found","message":"Meter not found"}`.

### 3.3 Meter Energy / Consumption Telemetry
- **Endpoint**: `GET /portal/meters/{meterId}/energy`
- **Response**:
  ```json
  {
    "data": [
      {
        "timestamp": "23/06/2026 23:30",
        "kwh": "48438.74",
        "kvah": "52313.84",
        "voltR": "226"
      },
      ...
    ]
  }
  ```
- **Readings Count**: Fixed interval window of **337 half-hourly readings** (7 full days from `23/06/2026 23:30` to `30/06/2026 23:30`).

### 3.4 Distribution Transformers (DTs)
- **Endpoint**: `GET /portal/dts?page={page}`
- **Total Records**: 40 transformers across Jaipur distribution network.
- **Response**:
  ```json
  {
    "data": [
      {
        "code": "DT-001",
        "name": "Malviya Nagar DT 1",
        "feederCode": "F-001",
        "capacityKva": 100
      }
    ],
    "total": 40
  }
  ```

### 3.5 Cryptographic Key Service & HMAC-Signed Bulk Export
- **Key Retrieval**: `GET /portal/keys`
  - Returns the dynamic HMAC signing secret:
    ```json
    {
      "data": {
        "signingSecret": "I3dZPPf5CgTp7JyGNMI8i6z8LFR7TmSR"
      }
    }
    ```
- **Bulk Export Endpoint**: `GET /portal/export?page=1`
  - Requires request signing via HMAC-SHA256 headers:
    - `x-timestamp`: Unix epoch seconds as string (e.g. `"1788282740"`).
    - `x-signature`: Hex-encoded HMAC-SHA256 signature calculated over:
      ```
      GET\n/portal/export\npage=1\n<timestamp>
      ```
  - **Payload Returned**: Complete, unpaginated dump of **all 403 meters** enriched with full 7-tier distribution hierarchy and geo coordinates in a single high-speed call!

---

## 4. Data Structures, Discrepancies & Quirks

| Domain | Legacy Portal Behavior | Problem / Anomaly | REST API Transformation |
| :--- | :--- | :--- | :--- |
| **Energy Readings** | Numbers are encoded as strings (`"48438.74"`, `"226"`, `"52313.84"`). | Inconvenient for numerical aggregations and client typing. | Parsed into native Python `float` values. |
| **Timestamps** | Non-standard Indian standard string: `"23/06/2026 23:30"` (`DD/MM/YYYY HH:mm`). | Fails standard date parsers in downstream BI/DB systems. | Converted to ISO-8601 with explicit IST offset: `"2026-06-23T23:30:00+05:30"`. |
| **Coordinates** | Geo coordinates returned as strings in `/portal/meters/{id}/geo` (`"26.93896..."`). | Types inconsistent between search and geo. | Strongly typed `latitude: float`, `longitude: float`. |
| **Missing Readings** | Missing or unrecorded meter parameters can contain `"null"`, `""`, or `"—"`. | Causes runtime parsing crashes. | `parse_float_safe` treats dashes/blanks as `None`. |
| **Hierarchy Tiering** | Detail page provides flat key-value pairs or stringified `classData`. | Inconsistent serialization format across different meter batches. | Unified structured model: `Zone -> Circle -> Division -> Subdivision -> Substation -> Feeder -> DT`. |

---

## 5. Security & Session Resiliency Strategy
1. **Double-Checked Locking Session Persistence**:
   The API wrapper maintains an `httpx.AsyncClient` cookie jar. When simultaneous asynchronous coroutines discover an expired token, an `asyncio.Lock` ensures only a single thread performs the re-login dance while others wait, eliminating race conditions.
2. **Transparent Auto-Reauthentication**:
   If any upstream call receives a `401 Unauthorized`, `403 Forbidden`, or `303` redirect towards `/login`, the client triggers an internal re-authentication and retries the original request with exponential backoff.
