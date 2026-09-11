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
from pathlib import Path
from fastapi.responses import HTMLResponse, RedirectResponse

TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_portal_html() -> str:
    template_path = TEMPLATES_DIR / "portal.html"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "<h1>Portal template not found</h1>"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard():
    """Serves the full modern operations portal."""
    return HTMLResponse(content=get_portal_html(), status_code=200)


@app.get("/hierarchy-view", response_class=HTMLResponse, include_in_schema=False)
@app.get("/hierarchy/view", response_class=HTMLResponse, include_in_schema=False)
async def serve_hierarchy_view():
    """Serves the interactive network hierarchy tree view."""
    html = get_portal_html().replace(
        "let currentTab = 'meters';",
        "let currentTab = 'hierarchy';"
    ).replace(
        'class="tab-content active" id="tabMeters"',
        'class="tab-content" id="tabMeters"'
    ).replace(
        'class="tab-content" id="tabHierarchy"',
        'class="tab-content active" id="tabHierarchy"'
    ).replace(
        'class="tab-btn active" id="tabBtnMeters"',
        'class="tab-btn" id="tabBtnMeters"'
    ).replace(
        'class="tab-btn" id="tabBtnHierarchy"',
        'class="tab-btn active" id="tabBtnHierarchy"'
    )
    return HTMLResponse(content=html, status_code=200)


@app.get("/export-view", response_class=HTMLResponse, include_in_schema=False)
@app.get("/export/view", response_class=HTMLResponse, include_in_schema=False)
async def serve_export_view():
    """Serves the bulk data export center web view."""
    html = get_portal_html().replace(
        "let currentTab = 'meters';",
        "let currentTab = 'export';"
    ).replace(
        'class="tab-content active" id="tabMeters"',
        'class="tab-content" id="tabMeters"'
    ).replace(
        'class="tab-content" id="tabExport"',
        'class="tab-content active" id="tabExport"'
    ).replace(
        'class="tab-btn active" id="tabBtnMeters"',
        'class="tab-btn" id="tabBtnMeters"'
    ).replace(
        'class="tab-btn" id="tabBtnExport"',
        'class="tab-btn active" id="tabBtnExport"'
    )
    return HTMLResponse(content=html, status_code=200)

