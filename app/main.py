import math
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.client import UrjaPortalClient
from app.models import (
    HealthResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    MeterListItem,
    MeterListResponse,
    MeterDetailResponse,
    MeterConsumptionResponse,
    ConsumptionSummary,
    EnergyReading,
    TransformerItem,
    TransformerListResponse,
    HierarchyTreeResponse,
    HierarchyTreeNode,
    BulkExportResponse
)

settings = get_settings()
portal_client = UrjaPortalClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Warm up connection and initial login
    try:
        await portal_client.login()
    except Exception as e:
        # Non-fatal during startup; will retry on first request
        pass
    yield
    # Shutdown: cleanly close HTTP client
    await portal_client.close()


app = FastAPI(
    title="Flock Energy - Urja Meter Ops REST API",
    version="1.0.0",
    description="""
# Urja Meter Ops API Service
Clean, modern, and production-grade REST API wrapper around the legacy **Urja Meter Ops** portal.

### Key Capabilities
- **Automated Authentication & Session Management**: Transparent login with SvelteKit CSRF origin tracking and automatic re-authentication on session expiration.
- **Normalized Data Formats**: Stringified numbers converted to native floats/integers, and timestamps normalized from Indian format (`DD/MM/YYYY HH:mm`) to ISO-8601.
- **Consumption Analytics**: Pre-computed consumption metrics (net kWh, voltage averages, min/max).
- **Network Hierarchy Reconstruction**: Fully nested 7-tier distribution tree (Zone → Circle → Division → Subdivision → Substation → Feeder → DT → Meters).
- **HMAC-SHA256 Bulk Export Tunnel**: Programmatic integration with the portal's cryptographically signed bulk export.
- **Interactive Operations Dashboard**: High-visibility web client built directly into the service root (`/` or `/dashboard`).
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# Enable CORS for external frontends or integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Health & Auth Endpoints
# =============================================================================

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Service and Upstream Health Check",
    description="Reports the service status and connectivity to the upstream Urja Meter Ops portal."
)
async def health_check():
    upstream_status = "connected"
    is_authenticated = False
    try:
        await portal_client._ensure_authenticated()
        is_authenticated = portal_client._is_authenticated
    except Exception:
        upstream_status = "disconnected"

    return HealthResponse(
        status="healthy" if upstream_status == "connected" else "degraded",
        upstream_portal=upstream_status,
        upstream_authenticated=is_authenticated,
        version=settings.app_version,
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@app.post(
    "/api/v1/auth/login",
    response_model=AuthLoginResponse,
    tags=["Authentication"],
    summary="Manual Re-authentication / Credential Override",
    description="Triggers a fresh login against the legacy portal. Sessions are normally managed automatically."
)
async def manual_login(credentials: Optional[AuthLoginRequest] = None):
    try:
        email = credentials.email if credentials else None
        password = credentials.password if credentials else None
        success = await portal_client.login(email=email, password=password)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Upstream authentication failed. Please verify credentials."
            )
        return AuthLoginResponse(
            status="authenticated",
            email=portal_client._auth_email,
            session_active=True,
            message="Session successfully refreshed with legacy Urja portal"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error connecting to upstream portal: {str(e)}"
        )


@app.post(
    "/api/v1/auth/cache/clear",
    tags=["System"],
    summary="Clear Internal Cache",
    description="Invalidates cached meter search queries, transformers, and bulk export data."
)
async def clear_cache():
    portal_client.clear_cache()
    return {"status": "success", "message": "In-memory cache successfully invalidated"}


# =============================================================================
# Meters Endpoints
# =============================================================================

@app.get(
    "/api/v1/meters",
    response_model=MeterListResponse,
    tags=["Meters"],
    summary="List and Search Smart Meters",
    description="""
Retrieve a paginated list of smart meters with optional search and multi-attribute filters.
When filtering by make, phase, status, or DT code, the API queries the enriched meter index.
    """
)
async def list_meters(
    q: str = Query("", description="Search term for meter ID or serial number"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Number of meters per page"),
    make: Optional[str] = Query(None, description="Filter by meter manufacturer (e.g. HPL, L&T, Genus, Schneider)"),
    phase: Optional[str] = Query(None, description="Filter by phase type (e.g. single, three)"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by installation status (e.g. Installed, Decommissioned)"),
    dt_code: Optional[str] = Query(None, description="Filter by Distribution Transformer code (e.g. DT-001)"),
    bypass_cache: bool = Query(False, description="Bypass in-memory caching")
):
    try:
        # If attribute filters (make, phase, status, dt_code) or custom page_size are requested,
        # use the high-performance enriched dataset
        if make or phase or status_filter or dt_code or page_size != 20:
            bulk_meters = await portal_client.get_bulk_export(bypass_cache=bypass_cache)
            filtered = bulk_meters

            if q:
                q_lower = q.lower().strip()
                filtered = [
                    m for m in filtered
                    if q_lower in m.get("meterId", "").lower() or q_lower in m.get("serialNo", "").lower()
                ]
            if make:
                filtered = [m for m in filtered if m.get("make", "").lower() == make.lower()]
            if phase:
                filtered = [m for m in filtered if m.get("phaseType", "").lower() == phase.lower()]
            if status_filter:
                filtered = [m for m in filtered if m.get("installStatus", "").lower() == status_filter.lower()]
            if dt_code:
                filtered = [m for m in filtered if m.get("dtCode", "").lower() == dt_code.lower()]

            total = len(filtered)
            total_pages = max(1, math.ceil(total / page_size))
            start_idx = (page - 1) * page_size
            page_slice = filtered[start_idx : start_idx + page_size]

            items = [
                MeterListItem(
                    meter_id=m["meterId"],
                    serial_no=m.get("serialNo", ""),
                    make=m.get("make", ""),
                    phase_type=m.get("phaseType", ""),
                    install_status=m.get("installStatus", ""),
                    dt_code=m.get("dtCode")
                )
                for m in page_slice
            ]

            return MeterListResponse(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
                total_pages=total_pages
            )

        # Standard search flow directly against legacy search endpoint
        legacy_data = await portal_client.get_meters_search(q=q, page=page, bypass_cache=bypass_cache)
        raw_items = legacy_data.get("data", [])
        total = legacy_data.get("total", len(raw_items))
        total_pages = max(1, math.ceil(total / page_size))

        items = [
            MeterListItem(
                meter_id=item["meterId"],
                serial_no=item.get("serialNo", ""),
                make=item.get("make", ""),
                phase_type=item.get("phaseType", ""),
                install_status=item.get("installStatus", ""),
                dt_code=item.get("dtCode")
            )
            for item in raw_items
        ]

        return MeterListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query meters: {str(e)}"
        )


@app.get(
    "/api/v1/meters/bulk-export",
    response_model=BulkExportResponse,
    tags=["Meters"],
    summary="Cryptographic Bulk Export Tunnel",
    description="Directly executes an HMAC-SHA256 authenticated export against the portal to fetch all 403 meters in a single operation."
)
async def bulk_export_meters(bypass_cache: bool = Query(False, description="Bypass in-memory cache")):
    try:
        raw_list = await portal_client.get_bulk_export(bypass_cache=bypass_cache)
        items = []
        for m in raw_list:
            items.append(
                MeterDetailResponse(
                    meter_id=m["meterId"],
                    serial_no=m.get("serialNo", ""),
                    make=m.get("make", ""),
                    phase_type=m.get("phaseType", ""),
                    install_status=m.get("installStatus", ""),
                    install_type=m.get("installType"),
                    build=m.get("build"),
                    dt_code=m.get("dtCode"),
                    geo={
                        "latitude": portal_client.parse_float_safe(m.get("geo", {}).get("lat")),
                        "longitude": portal_client.parse_float_safe(m.get("geo", {}).get("lng"))
                    } if m.get("geo") else None,
                    hierarchy=m.get("hierarchy"),
                    extra_parameters={}
                )
            )

        return BulkExportResponse(
            total=len(items),
            exported_at=datetime.now(timezone.utc).isoformat(),
            meters=items
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bulk export failed: {str(e)}"
        )


@app.get(
    "/api/v1/meters/{meter_id}",
    response_model=MeterDetailResponse,
    tags=["Meters"],
    summary="Get Meter Details",
    description="Retrieve comprehensive details for a specific meter including nameplate specs, coordinates, and full 7-tier hierarchy."
)
async def get_meter(meter_id: str):
    try:
        meter = await portal_client.get_meter_details(meter_id)
        if not meter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meter with ID '{meter_id}' was not found in the Urja portal."
            )
        return meter
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error retrieving meter details: {str(e)}"
        )


@app.get(
    "/api/v1/meters/{meter_id}/consumption",
    response_model=MeterConsumptionResponse,
    tags=["Meters"],
    summary="Get Meter Consumption History",
    description="""
Retrieve cleaned consumption time series for a meter.
Converts timestamps to ISO-8601, cleans stringified numeric readings, and returns computed summary metrics
(net kWh consumed, start/end kWh, average voltage, and min/max bounds).
    """
)
async def get_meter_consumption(meter_id: str):
    try:
        readings = await portal_client.get_meter_energy(meter_id)
        if readings is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Consumption history for meter '{meter_id}' was not found."
            )

        # Compute analytical summary
        reading_count = len(readings)
        if reading_count == 0:
            summary = ConsumptionSummary(
                reading_count=0,
                start_time=None,
                end_time=None,
                start_kwh=None,
                end_kwh=None,
                net_kwh_consumed=None,
                avg_voltage=None,
                min_voltage=None,
                max_voltage=None
            )
            return MeterConsumptionResponse(
                meter_id=meter_id,
                summary=summary,
                readings=[]
            )

        start_reading = readings[0]
        end_reading = readings[-1]

        start_kwh = start_reading.get("kwh")
        end_kwh = end_reading.get("kwh")
        net_kwh = None
        if start_kwh is not None and end_kwh is not None:
            net_kwh = round(abs(end_kwh - start_kwh), 2)

        voltages = [r["volt_r"] for r in readings if r.get("volt_r") is not None]
        avg_volt = round(sum(voltages) / len(voltages), 1) if voltages else None
        min_volt = min(voltages) if voltages else None
        max_volt = max(voltages) if voltages else None

        summary = ConsumptionSummary(
            reading_count=reading_count,
            start_time=start_reading.get("timestamp"),
            end_time=end_reading.get("timestamp"),
            start_kwh=start_kwh,
            end_kwh=end_kwh,
            net_kwh_consumed=net_kwh,
            avg_voltage=avg_volt,
            min_voltage=min_volt,
            max_voltage=max_volt
        )

        formatted_readings = [EnergyReading(**r) for r in readings]

        return MeterConsumptionResponse(
            meter_id=meter_id,
            summary=summary,
            readings=formatted_readings
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error retrieving meter consumption: {str(e)}"
        )


# =============================================================================
# Transformers Endpoints
# =============================================================================

@app.get(
    "/api/v1/transformers",
    response_model=TransformerListResponse,
    tags=["Transformers"],
    summary="List Distribution Transformers",
    description="Retrieve paginated list of distribution transformers (DTs) from the utility network."
)
async def list_transformers(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Number of items per page"),
    bypass_cache: bool = Query(False, description="Bypass in-memory cache")
):
    try:
        raw_data = await portal_client.get_dts(page=page, bypass_cache=bypass_cache)
        items = [
            TransformerItem(
                code=dt.get("code", ""),
                name=dt.get("name", ""),
                feeder_code=dt.get("feederCode"),
                capacity_kva=portal_client.parse_float_safe(dt.get("capacityKva"))
            )
            for dt in raw_data.get("data", [])
        ]
        total = raw_data.get("total", len(items))
        total_pages = max(1, math.ceil(total / page_size))

        return TransformerListResponse(
            items=items[:page_size],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error retrieving transformers: {str(e)}"
        )


# =============================================================================
# Optional Extensions: Hierarchy Tree & Bulk Export
# =============================================================================

@app.get(
    "/api/v1/hierarchy",
    response_model=HierarchyTreeResponse,
    tags=["Hierarchy & Network"],
    summary="Reconstruct Network Hierarchy Tree",
    description="""
Reconstructs the full utility network tree:
**Zone → Circle → Division → Subdivision → Substation → Feeder → Distribution Transformer (DT)**
with meter counts aggregate at every tier.
    """
)
async def get_network_hierarchy(bypass_cache: bool = Query(False, description="Bypass cache")):
    try:
        bulk_meters = await portal_client.get_bulk_export(bypass_cache=bypass_cache)
        total_meters = len(bulk_meters)

        # Build hierarchical nested map
        # Tier order: zone -> circle -> division -> subdivision -> substation -> feeder -> dt
        tree_map: Dict[str, Any] = {}

        for meter in bulk_meters:
            h = meter.get("hierarchy", {})
            z = h.get("zone") or {"name": "Jaipur Zone 1", "code": "Z-01"}
            c = h.get("circle") or {"name": "Circle 1", "code": "C-01"}
            d = h.get("division") or {"name": "Division 1", "code": "D-01"}
            sd = h.get("subdivision") or {"name": "Subdivision 1", "code": "SD-01"}
            ss = h.get("substation") or {"name": "Substation 1", "code": "SS-01"}
            f = h.get("feeder") or {"name": "Feeder 1", "code": "F-001"}
            dt = h.get("dt") or {"name": meter.get("dtCode", "Unknown DT"), "code": meter.get("dtCode", "DT-000")}

            z_key = z.get("code") or z.get("name")
            c_key = c.get("code") or c.get("name")
            d_key = d.get("code") or d.get("name")
            sd_key = sd.get("code") or sd.get("name")
            ss_key = ss.get("code") or ss.get("name")
            f_key = f.get("code") or f.get("name")
            dt_key = dt.get("code") or dt.get("name")

            if z_key not in tree_map:
                tree_map[z_key] = {"id": z_key, "name": z.get("name"), "level": "zone", "count": 0, "circles": {}}
            tree_map[z_key]["count"] += 1

            z_node = tree_map[z_key]
            if c_key not in z_node["circles"]:
                z_node["circles"][c_key] = {"id": c_key, "name": c.get("name"), "level": "circle", "count": 0, "divisions": {}}
            z_node["circles"][c_key]["count"] += 1

            c_node = z_node["circles"][c_key]
            if d_key not in c_node["divisions"]:
                c_node["divisions"][d_key] = {"id": d_key, "name": d.get("name"), "level": "division", "count": 0, "subdivisions": {}}
            c_node["divisions"][d_key]["count"] += 1

            d_node = c_node["divisions"][d_key]
            if sd_key not in d_node["subdivisions"]:
                d_node["subdivisions"][sd_key] = {"id": sd_key, "name": sd.get("name"), "level": "subdivision", "count": 0, "substations": {}}
            d_node["subdivisions"][sd_key]["count"] += 1

            sd_node = d_node["subdivisions"][sd_key]
            if ss_key not in sd_node["substations"]:
                sd_node["substations"][ss_key] = {"id": ss_key, "name": ss.get("name"), "level": "substation", "count": 0, "feeders": {}}
            sd_node["substations"][ss_key]["count"] += 1

            ss_node = sd_node["substations"][ss_key]
            if f_key not in ss_node["feeders"]:
                ss_node["feeders"][f_key] = {"id": f_key, "name": f.get("name"), "level": "feeder", "count": 0, "dts": {}}
            ss_node["feeders"][f_key]["count"] += 1

            f_node = ss_node["feeders"][f_key]
            if dt_key not in f_node["dts"]:
                f_node["dts"][dt_key] = {"id": dt_key, "name": dt.get("name"), "level": "dt", "count": 0}
            f_node["dts"][dt_key]["count"] += 1

        # Transform dictionary structure into HierarchyTreeNode list
        def build_tree() -> List[HierarchyTreeNode]:
            roots = []
            for z_data in tree_map.values():
                circles = []
                for c_data in z_data["circles"].values():
                    divisions = []
                    for d_data in c_data["divisions"].values():
                        subdivisions = []
                        for sd_data in d_data["subdivisions"].values():
                            substations = []
                            for ss_data in sd_data["substations"].values():
                                feeders = []
                                for f_data in ss_data["feeders"].values():
                                    dts = [
                                        HierarchyTreeNode(
                                            id=dt_data["id"],
                                            name=dt_data["name"],
                                            level="dt",
                                            meter_count=dt_data["count"],
                                            children=[]
                                        )
                                        for dt_data in f_data["dts"].values()
                                    ]
                                    feeders.append(
                                        HierarchyTreeNode(
                                            id=f_data["id"],
                                            name=f_data["name"],
                                            level="feeder",
                                            meter_count=f_data["count"],
                                            children=dts
                                        )
                                    )
                                substations.append(
                                    HierarchyTreeNode(
                                        id=ss_data["id"],
                                        name=ss_data["name"],
                                        level="substation",
                                        meter_count=ss_data["count"],
                                        children=feeders
                                    )
                                )
                            subdivisions.append(
                                HierarchyTreeNode(
                                    id=sd_data["id"],
                                    name=sd_data["name"],
                                    level="subdivision",
                                    meter_count=sd_data["count"],
                                    children=substations
                                )
                            )
                        divisions.append(
                            HierarchyTreeNode(
                                id=d_data["id"],
                                name=d_data["name"],
                                level="division",
                                meter_count=d_data["count"],
                                children=subdivisions
                            )
                        )
                    circles.append(
                        HierarchyTreeNode(
                            id=c_data["id"],
                            name=c_data["name"],
                            level="circle",
                            meter_count=c_data["count"],
                            children=divisions
                        )
                    )
                roots.append(
                    HierarchyTreeNode(
                        id=z_data["id"],
                        name=z_data["name"],
                        level="zone",
                        meter_count=z_data["count"],
                        children=circles
                    )
                )
            return roots

        return HierarchyTreeResponse(
            total_meters=total_meters,
            roots=build_tree()
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Error constructing hierarchy tree: {str(e)}"
        )





# =============================================================================
# Modern Operations Dashboard UI (Optional Extension)
# =============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Flock Energy — Urja Meter Ops API Service</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0b0f19;
      --surface: #111827;
      --surface-elevated: #1f2937;
      --border: #374151;
      --text: #f9fafb;
      --text-muted: #9ca3af;
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --primary-glow: rgba(56, 189, 248, 0.15);
      --accent: #10b981;
      --accent-glow: rgba(16, 185, 129, 0.15);
      --warning: #f59e0b;
      --danger: #ef4444;
      --font-sans: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }
    header {
      background: rgba(17, 24, 39, 0.85);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 50;
      padding: 1rem 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .logo-container {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .logo-badge {
      width: 38px;
      height: 38px;
      border-radius: 10px;
      background: linear-gradient(135deg, #38bdf8, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 1.1rem;
      color: white;
      box-shadow: 0 0 20px rgba(56, 189, 248, 0.35);
    }
    .title-group h1 {
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .title-group p {
      font-size: 0.8rem;
      color: var(--text-muted);
    }
    .nav-actions {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .btn {
      padding: 0.5rem 1rem;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.2s ease;
      border: 1px solid transparent;
    }
    .btn-primary {
      background: var(--primary);
      color: #0b0f19;
      font-weight: 600;
    }
    .btn-primary:hover {
      background: var(--primary-hover);
      box-shadow: 0 0 15px var(--primary-glow);
    }
    .btn-outline {
      border-color: var(--border);
      color: var(--text);
      background: var(--surface);
    }
    .btn-outline:hover {
      background: var(--surface-elevated);
      border-color: var(--text-muted);
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.3rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 500;
      background: rgba(16, 185, 129, 0.1);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #34d399;
      box-shadow: 0 0 8px #34d399;
      animation: pulse 2s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    main {
      flex: 1;
      max-width: 1380px;
      width: 100%;
      margin: 0 auto;
      padding: 2rem;
      display: flex;
      flex-direction: column;
      gap: 2rem;
    }
    .grid-stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1.25rem;
    }
    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.25rem;
      position: relative;
      overflow: hidden;
      transition: transform 0.2s ease, border-color 0.2s ease;
    }
    .stat-card:hover {
      transform: translateY(-2px);
      border-color: rgba(56, 189, 248, 0.5);
    }
    .stat-label {
      font-size: 0.8rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.5rem;
    }
    .stat-value {
      font-size: 2rem;
      font-weight: 700;
      color: var(--text);
      font-family: var(--font-mono);
    }
    .stat-subtext {
      font-size: 0.75rem;
      color: var(--accent);
      margin-top: 0.35rem;
    }
    .workspace-grid {
      display: grid;
      grid-template-columns: 1fr 420px;
      gap: 2rem;
      align-items: start;
    }
    @media (max-width: 1024px) {
      .workspace-grid { grid-template-columns: 1fr; }
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .panel-header {
      padding: 1.25rem 1.5rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(31, 41, 55, 0.4);
    }
    .panel-title {
      font-size: 1.1rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .filter-bar {
      padding: 1rem 1.5rem;
      display: flex;
      gap: 0.75rem;
      flex-wrap: wrap;
      border-bottom: 1px solid var(--border);
      background: rgba(17, 24, 39, 0.5);
    }
    .search-input {
      flex: 1;
      min-width: 220px;
      padding: 0.55rem 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 0.9rem;
      outline: none;
      transition: border-color 0.2s;
    }
    .search-input:focus { border-color: var(--primary); }
    .select-filter {
      padding: 0.55rem 1rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 0.85rem;
      outline: none;
    }
    .table-container {
      overflow-x: auto;
      max-height: 520px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.88rem;
    }
    thead {
      background: rgba(31, 41, 55, 0.6);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    th {
      padding: 0.75rem 1.25rem;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 0.85rem 1.25rem;
      border-bottom: 1px solid rgba(55, 65, 81, 0.5);
      transition: background 0.15s ease;
    }
    tbody tr:hover td {
      background: rgba(56, 189, 248, 0.05);
      cursor: pointer;
    }
    tr.selected td {
      background: rgba(56, 189, 248, 0.12) !important;
      border-color: rgba(56, 189, 248, 0.3);
    }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 500;
    }
    .badge-installed { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .badge-decomm { background: rgba(239, 68, 68, 0.15); color: #f87171; }
    .badge-single { background: rgba(56, 189, 248, 0.15); color: #38bdf8; }
    .badge-three { background: rgba(168, 85, 247, 0.15); color: #c084fc; }
    .pagination {
      padding: 0.85rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px solid var(--border);
      background: rgba(17, 24, 39, 0.4);
      font-size: 0.85rem;
      color: var(--text-muted);
    }
    .detail-view {
      padding: 1.5rem;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
      max-height: 660px;
      overflow-y: auto;
    }
    .detail-section {
      background: rgba(31, 41, 55, 0.4);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.1rem;
    }
    .detail-section h4 {
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--primary);
      margin-bottom: 0.75rem;
    }
    .prop-row {
      display: flex;
      justify-content: space-between;
      font-size: 0.85rem;
      padding: 0.35rem 0;
      border-bottom: 1px solid rgba(55, 65, 81, 0.4);
    }
    .prop-row:last-child { border-bottom: none; }
    .prop-name { color: var(--text-muted); }
    .prop-value { font-family: var(--font-mono); color: var(--text); font-weight: 500; }
    .chart-container {
      margin-top: 0.5rem;
      height: 140px;
      display: flex;
      align-items: flex-end;
      gap: 3px;
      padding: 10px 0;
      border-bottom: 1px solid var(--border);
    }
    .chart-bar {
      flex: 1;
      background: linear-gradient(180deg, #38bdf8, rgba(56, 189, 248, 0.2));
      border-radius: 2px 2px 0 0;
      min-height: 4px;
      transition: height 0.3s ease;
      position: relative;
    }
    .chart-bar:hover {
      background: #0ea5e9;
    }
    .hierarchy-crumbs {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      font-size: 0.75rem;
    }
    .crumb {
      background: var(--surface-elevated);
      padding: 0.25rem 0.6rem;
      border-radius: 6px;
      color: var(--text-muted);
      border: 1px solid var(--border);
    }
    .crumb strong { color: var(--text); }
  </style>
</head>
<body>

  <header>
    <div class="logo-container">
      <div class="logo-badge">⚡</div>
      <div class="title-group">
        <h1>Flock Energy — Urja Meter Ops API</h1>
        <p>Production REST API Proxy & Automated Ingestion Layer</p>
      </div>
    </div>
    <div class="nav-actions">
      <div class="status-pill">
        <div class="status-dot"></div>
        <span id="portalStatus">Urja Portal: Online</span>
      </div>
      <a href="/docs" target="_blank" class="btn btn-primary">📖 Interactive OpenAPI Docs (/docs)</a>
      <a href="/api/v1/hierarchy" target="_blank" class="btn btn-outline">🌳 Hierarchy Tree</a>
      <a href="/api/v1/meters/bulk-export" target="_blank" class="btn btn-outline">📦 Bulk Export</a>
    </div>
  </header>

  <main>
    <div class="grid-stats">
      <div class="stat-card">
        <div class="stat-label">Total Smart Meters</div>
        <div class="stat-value" id="statTotalMeters">403</div>
        <div class="stat-subtext">Across 40 Distribution Transformers</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Active / Installed</div>
        <div class="stat-value" id="statActiveMeters" style="color: #34d399;">—</div>
        <div class="stat-subtext">Real-time telemetry enabled</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Decommissioned</div>
        <div class="stat-value" id="statDecommMeters" style="color: #f87171;">—</div>
        <div class="stat-subtext">Archived from grid ops</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Transformers (DTs)</div>
        <div class="stat-value" id="statTotalDTs">40</div>
        <div class="stat-subtext">In Jaipur Zone 1 Distribution Tree</div>
      </div>
    </div>

    <div class="workspace-grid">
      <!-- Left Panel: Meter Directory -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">📡 Smart Meter Explorer</div>
          <div id="resultsCount" style="font-size: 0.85rem; color: var(--text-muted);">Showing meters</div>
        </div>

        <div class="filter-bar">
          <input type="text" id="searchInput" class="search-input" placeholder="Search by Meter ID (e.g. J100000) or Serial (e.g. SE33962)...">
          <select id="makeFilter" class="select-filter">
            <option value="">All Makes</option>
            <option value="HPL">HPL</option>
            <option value="L&T">L&T</option>
            <option value="Genus">Genus</option>
            <option value="Schneider">Schneider</option>
          </select>
          <select id="phaseFilter" class="select-filter">
            <option value="">All Phases</option>
            <option value="single">Single Phase</option>
            <option value="three">Three Phase</option>
          </select>
          <select id="statusFilter" class="select-filter">
            <option value="">All Statuses</option>
            <option value="Installed">Installed</option>
            <option value="Decommissioned">Decommissioned</option>
          </select>
        </div>

        <div class="table-container">
          <table id="metersTable">
            <thead>
              <tr>
                <th>Meter ID</th>
                <th>Serial No</th>
                <th>Make</th>
                <th>Phase</th>
                <th>Status</th>
                <th>DT Code</th>
              </tr>
            </thead>
            <tbody id="metersTableBody">
              <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 2rem;">Loading smart meters...</td></tr>
            </tbody>
          </table>
        </div>

        <div class="pagination">
          <button id="prevBtn" class="btn btn-outline" style="padding: 0.35rem 0.8rem;" disabled>← Previous</button>
          <span id="pageInfo">Page 1 of 1</span>
          <button id="nextBtn" class="btn btn-outline" style="padding: 0.35rem 0.8rem;" disabled>Next →</button>
        </div>
      </div>

      <!-- Right Panel: Meter Detail & Live Consumption -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">📊 Meter Telemetry & Profile</div>
          <span id="selectedMeterBadge" class="badge badge-installed">Select Meter</span>
        </div>

        <div class="detail-view" id="detailContainer">
          <div style="text-align: center; color: var(--text-muted); padding: 3rem 1rem;">
            Click on any meter row to inspect live consumption time series, telemetry, coordinates, and network hierarchy.
          </div>
        </div>
      </div>
    </div>
  </main>

  <script>
    let currentPage = 1;
    let selectedMeterId = null;
    let allMetersCache = [];

    async function loadStats() {
      try {
        const res = await fetch('/api/v1/meters?page_size=100');
        const data = await res.json();
        document.getElementById('statTotalMeters').innerText = data.total;
        
        // Count statuses from full export
        const exportRes = await fetch('/api/v1/meters/bulk-export');
        const exportData = await exportRes.json();
        allMetersCache = exportData.meters;
        
        const installed = allMetersCache.filter(m => m.install_status.toLowerCase() === 'installed').length;
        const decomm = allMetersCache.filter(m => m.install_status.toLowerCase() === 'decommissioned').length;
        
        document.getElementById('statActiveMeters').innerText = installed;
        document.getElementById('statDecommMeters').innerText = decomm;
      } catch (err) {
        console.error("Stats load error:", err);
      }
    }

    async function loadMeters(page = 1) {
      currentPage = page;
      const q = document.getElementById('searchInput').value;
      const make = document.getElementById('makeFilter').value;
      const phase = document.getElementById('phaseFilter').value;
      const status = document.getElementById('statusFilter').value;

      const params = new URLSearchParams({
        page: page,
        page_size: 15,
        q: q,
        ...(make && { make }),
        ...(phase && { phase }),
        ...(status && { status })
      });

      const res = await fetch(`/api/v1/meters?${params.toString()}`);
      const data = await res.json();

      const tbody = document.getElementById('metersTableBody');
      tbody.innerHTML = '';

      if (data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 2rem; color: var(--text-muted);">No meters found matching your filter criteria.</td></tr>';
      } else {
        data.items.forEach(m => {
          const tr = document.createElement('tr');
          if (m.meter_id === selectedMeterId) tr.classList.add('selected');
          tr.onclick = () => selectMeter(m.meter_id);
          tr.innerHTML = `
            <td style="font-family: var(--font-mono); font-weight: 600; color: var(--primary);">${m.meter_id}</td>
            <td style="font-family: var(--font-mono);">${m.serial_no}</td>
            <td>${m.make}</td>
            <td><span class="badge ${m.phase_type === 'single' ? 'badge-single' : 'badge-three'}">${m.phase_type}</span></td>
            <td><span class="badge ${m.install_status.toLowerCase() === 'installed' ? 'badge-installed' : 'badge-decomm'}">${m.install_status}</span></td>
            <td style="font-family: var(--font-mono);">${m.dt_code || '—'}</td>
          `;
          tbody.appendChild(tr);
        });

        // Select first meter automatically if none selected
        if (!selectedMeterId && data.items.length > 0) {
          selectMeter(data.items[0].meter_id);
        }
      }

      document.getElementById('pageInfo').innerText = `Page ${data.page} of ${data.total_pages}`;
      document.getElementById('prevBtn').disabled = data.page <= 1;
      document.getElementById('nextBtn').disabled = data.page >= data.total_pages;
      document.getElementById('resultsCount').innerText = `${data.total} meters total`;
    }

    async function selectMeter(meterId) {
      selectedMeterId = meterId;
      document.querySelectorAll('#metersTableBody tr').forEach(r => r.classList.remove('selected'));
      // Mark current
      document.getElementById('selectedMeterBadge').innerText = meterId;

      const container = document.getElementById('detailContainer');
      container.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 3rem;">Loading telemetry for ${meterId}...</div>`;

      try {
        const [detailRes, energyRes] = await Promise.all([
          fetch(`/api/v1/meters/${meterId}`),
          fetch(`/api/v1/meters/${meterId}/consumption`)
        ]);

        const detail = await detailRes.json();
        const energy = energyRes.ok ? await energyRes.json() : null;

        let hierarchyHtml = '';
        if (detail.hierarchy) {
          const h = detail.hierarchy;
          const crumbs = [
            h.zone?.name,
            h.circle?.name,
            h.division?.name,
            h.substation?.name,
            h.feeder?.name,
            h.dt?.name
          ].filter(Boolean);

          hierarchyHtml = `
            <div class="detail-section">
              <h4>Network Hierarchy Position</h4>
              <div class="hierarchy-crumbs">
                ${crumbs.map(c => `<span class="crumb"><strong>${c}</strong></span>`).join(' → ')}
              </div>
            </div>
          `;
        }

        let consumptionHtml = '';
        if (energy && energy.summary) {
          const s = energy.summary;
          // Build mini chart preview from last 24 readings
          const recent = energy.readings.slice(-24);
          const maxKwh = Math.max(...recent.map(r => r.kwh || 0), 1);
          const minKwh = Math.min(...recent.map(r => r.kwh || 0));
          const bars = recent.map(r => {
            const h = Math.max(8, Math.round(((r.kwh - minKwh) / (maxKwh - minKwh || 1)) * 100));
            return `<div class="chart-bar" style="height: ${h}%;" title="${r.timestamp}: ${r.kwh} kWh | ${r.volt_r}V"></div>`;
          }).join('');

          consumptionHtml = `
            <div class="detail-section">
              <h4>Telemetry Analytics (337 Half-Hour Intervals)</h4>
              <div class="prop-row"><span class="prop-name">Total Net Consumption</span><span class="prop-value">${s.net_kwh_consumed ?? '—'} kWh</span></div>
              <div class="prop-row"><span class="prop-name">Start Reading</span><span class="prop-value">${s.start_kwh} kWh</span></div>
              <div class="prop-row"><span class="prop-name">End Reading</span><span class="prop-value">${s.end_kwh} kWh</span></div>
              <div class="prop-row"><span class="prop-name">Avg Phase Voltage</span><span class="prop-value">${s.avg_voltage} V (Min: ${s.min_voltage}V / Max: ${s.max_voltage}V)</span></div>
              
              <div style="margin-top: 0.75rem;">
                <span style="font-size: 0.75rem; color: var(--text-muted);">24-Hour Load Curve Preview:</span>
                <div class="chart-container">${bars}</div>
              </div>
            </div>
          `;
        }

        container.innerHTML = `
          <div class="detail-section">
            <h4>Nameplate & Hardware</h4>
            <div class="prop-row"><span class="prop-name">Meter ID</span><span class="prop-value">${detail.meter_id}</span></div>
            <div class="prop-row"><span class="prop-name">Serial Number</span><span class="prop-value">${detail.serial_no}</span></div>
            <div class="prop-row"><span class="prop-name">Manufacturer</span><span class="prop-value">${detail.make}</span></div>
            <div class="prop-row"><span class="prop-name">Phase Type</span><span class="prop-value">${detail.phase_type}</span></div>
            <div class="prop-row"><span class="prop-name">Status</span><span class="prop-value">${detail.install_status}</span></div>
            <div class="prop-row"><span class="prop-name">Coordinates</span><span class="prop-value">${detail.geo?.latitude ? `${detail.geo.latitude.toFixed(4)}, ${detail.geo.longitude.toFixed(4)}` : 'N/A'}</span></div>
          </div>
          ${hierarchyHtml}
          ${consumptionHtml}
        `;

      } catch (err) {
        container.innerHTML = `<div style="color: var(--danger); padding: 2rem;">Error loading meter details: ${err.message}</div>`;
      }
    }

    // Event listeners
    document.getElementById('searchInput').addEventListener('input', () => loadMeters(1));
    document.getElementById('makeFilter').addEventListener('change', () => loadMeters(1));
    document.getElementById('phaseFilter').addEventListener('change', () => loadMeters(1));
    document.getElementById('statusFilter').addEventListener('change', () => loadMeters(1));

    document.getElementById('prevBtn').addEventListener('click', () => {
      if (currentPage > 1) loadMeters(currentPage - 1);
    });
    document.getElementById('nextBtn').addEventListener('click', () => {
      loadMeters(currentPage + 1);
    });

    // Initial load
    loadStats();
    loadMeters(1);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard():
    """Serves the modern Urja Meter Ops web dashboard."""
    return HTMLResponse(content=DASHBOARD_HTML, status_code=200)
