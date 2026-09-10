# Engineering Reflection & Post-Mortem

### 1. What assumptions did you make?
- **Timezone and Timestamp Context**: The legacy portal timestamps (`DD/MM/YYYY HH:mm`) lacked an explicit timezone offset. Given that the utility network is located in Jaipur, Rajasthan (indicated by Zone 1 / Malviya Nagar references), I assumed all meter readings operate in Indian Standard Time (`UTC+05:30`) and converted them accordingly to ISO-8601 strings (`YYYY-MM-DDTHH:mm:ss+05:30`).
- **Read-Only Portal Guarantees**: I assumed the legacy portal data is strictly read-only and that upstream records change at operational reporting intervals (e.g. 30-minute intervals for smart meters) rather than millisecond sub-second frequencies. This justified implementing an in-memory caching layer with a 5-minute TTL to shield the legacy server from redundant traffic.
- **Bulk Dataset Availability**: When discovering the HMAC-SHA256 authenticated `/portal/export` endpoint, I assumed this bulk tunnel could be leveraged as a fast index provider for rich queries that the legacy search endpoint couldn't handle natively (such as filtering by make, phase, and DT code simultaneously).
- **Single Utility Domain**: I assumed the service currently wraps a single utility deployment ("Jaipur Zone 1") while maintaining design modularity so credentials and base URLs can be injected per utility tenant via environment variables.

---

### 2. Which part was the most difficult, and how did you get unstuck?
The most challenging part was discovering why initial POST login requests were being rejected with `403 Forbidden: Cross-site POST form submissions are forbidden`. 

In traditional legacy scraper exercises, form posts usually require just standard credentials and cookie capture. However, because Urja Ops is secretly powered by **SvelteKit** under the hood, its built-in security middleware inspects the HTTP `Origin` header against the server's Host header. If the client omits `Origin` or provides a non-matching origin, SvelteKit immediately halts the request.

I got unstuck by:
1. Inspecting the client-side JavaScript entry points (`start.*.js`, `app.*.js`, and `nodes/7.*.js`) to analyze how SvelteKit form actions are submitted.
2. Observing that SvelteKit's fetch routines bind form submissions with explicit `Origin: https://urja-ops.flockenergy.tech` and `Referer: https://urja-ops.flockenergy.tech/login`.
3. Adding these headers into our Python client adapter, which immediately yielded a 303 redirect response and established the `__Secure-better-auth.session_token` cookie.

---

### 3. If you had another day, what would you improve?
1. **Persistent Distributed Cache (Redis) & Background Sync Worker**:
   Replace the in-memory cache with Redis and an asynchronous background celery/rq worker that periodically polls `/portal/export` and `/portal/meters/{id}/energy` to maintain a replica PostgreSQL/TimescaleDB time-series database. This would provide instant sub-millisecond query latency and insulate downstream consumers from any portal downtime.
2. **Geospatial Proximity Queries (PostGIS)**:
   Add endpoints like `GET /api/v1/meters/nearby?lat=...&lng=...&radius_km=5` utilizing PostGIS or KD-trees over the normalized coordinates.
3. **Webhook Notifications on Meter Anomalies**:
   Implement an automated anomaly detection worker that scans the 337 half-hourly readings and issues webhook alerts on voltage drops (< 210V), phase imbalances, or zero-consumption periods indicative of meter tampering or feeder outages.
4. **Interactive Dashboard Enhancements**:
   Add a Leaflet/Mapbox map view to the web dashboard to visually plot smart meters across Jaipur distribution transformers with color-coded operational statuses.

---

### 4. What mistake did you make while solving this (there's always one)?
During early route organization in `app/main.py`, I initially declared `@app.get("/api/v1/meters/{meter_id}")` before the `@app.get("/api/v1/meters/bulk-export")` endpoint. 

Because Starlette/FastAPI matches routes in the order they are defined, requests to `/api/v1/meters/bulk-export` were greedily matched with `{meter_id} = "bulk-export"`. The client then attempted to look up a meter with ID `"bulk-export"`, resulting in a 404 error.

I diagnosed this by inspecting the exact 404 JSON response text during automated tests, which showed `"Meter with ID 'bulk-export' was not found"`. Reordering the static route `/api/v1/meters/bulk-export` above the parameterized `{meter_id}` route immediately resolved the issue.

---

### 5. If you were reviewing your own submission, what would you criticise?
1. **In-Memory Cache Single-Process Constraint**:
   The caching layer uses an in-process dictionary (`Dict[str, Tuple[float, Any]]`). If the API is scaled horizontally across multiple Uvicorn worker processes or Kubernetes pods, each worker maintains its own isolated cache and session, potentially causing redundant upstream logins and cache fragmentation.
2. **Coupling of Upstream Bulk Dataset for Filtering**:
   While leveraging `/portal/export` for rich multi-attribute filters (make, phase, status) was a clever architectural shortcut, if the utility network scaled from 403 meters to 500,000 meters, fetching the complete export into memory for in-process slicing would cause high memory pressure. At higher scale, an external database (e.g. SQLite, Postgres, or Elasticsearch) would be required to index the dataset incrementally.
3. **Fixed Interval Consumption Assumption**:
   The consumption analyzer assumes a 7-day default window (337 readings). If the utility expands the portal to allow arbitrary date range queries (e.g., `?from=...&to=...`), the consumption endpoint would need query parameter pass-through and dynamic date filtering.
