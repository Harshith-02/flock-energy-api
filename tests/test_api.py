import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.client import UrjaPortalClient


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def test_normalization_parse_float():
    """Verify float parsing handles valid strings, nulls, and dashes."""
    assert UrjaPortalClient.parse_float_safe("124.50") == 124.50
    assert UrjaPortalClient.parse_float_safe("226") == 226.0
    assert UrjaPortalClient.parse_float_safe("—") is None
    assert UrjaPortalClient.parse_float_safe("-") is None
    assert UrjaPortalClient.parse_float_safe("") is None
    assert UrjaPortalClient.parse_float_safe(None) is None
    assert UrjaPortalClient.parse_float_safe("invalid") is None


def test_normalization_parse_indian_timestamp():
    """Verify Indian timestamp 'DD/MM/YYYY HH:mm' is converted to ISO format."""
    iso_ts, raw_ts = UrjaPortalClient.parse_indian_timestamp("23/06/2026 23:30")
    assert raw_ts == "23/06/2026 23:30"
    assert "2026-06-23T23:30:00" in iso_ts
    assert "+05:30" in iso_ts


@pytest.mark.anyio
async def test_health_endpoint(client: AsyncClient):
    """Test /health returns healthy status."""
    response = await client.get("/health")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert "upstream_portal" in data
    assert "version" in data


@pytest.mark.anyio
async def test_list_meters(client: AsyncClient):
    """Test listing smart meters with pagination."""
    response = await client.get("/api/v1/meters?page=1&page_size=10")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] > 0
    assert len(data["items"]) <= 10
    first = data["items"][0]
    assert "meter_id" in first
    assert "serial_no" in first
    assert "make" in first
    assert "phase_type" in first


@pytest.mark.anyio
async def test_list_meters_filtering(client: AsyncClient):
    """Test filtering meters by make and phase."""
    response = await client.get("/api/v1/meters?make=HPL&phase=single&page_size=5")
    assert response.status_code == 200, response.text
    data = response.json()
    assert "items" in data
    for m in data["items"]:
        assert m["make"].lower() == "hpl"
        assert m["phase_type"].lower() == "single"


@pytest.mark.anyio
async def test_meter_details(client: AsyncClient):
    """Test getting details of an existing meter."""
    response = await client.get("/api/v1/meters/J100000")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["meter_id"] == "J100000"
    assert "geo" in data
    assert "hierarchy" in data
    assert data["geo"]["latitude"] is not None
    assert data["geo"]["longitude"] is not None


@pytest.mark.anyio
async def test_meter_details_not_found(client: AsyncClient):
    """Test 404 response for non-existent meter."""
    response = await client.get("/api/v1/meters/NON_EXISTENT_METER_XYZ")
    assert response.status_code == 404, response.text
    data = response.json()
    assert "detail" in data


@pytest.mark.anyio
async def test_meter_consumption(client: AsyncClient):
    """Test consumption endpoint returns normalized readings and analytics."""
    response = await client.get("/api/v1/meters/J100000/consumption")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["meter_id"] == "J100000"
    summary = data["summary"]
    assert summary["reading_count"] > 0
    assert summary["avg_voltage"] is not None
    assert summary["net_kwh_consumed"] is not None
    assert len(data["readings"]) > 0
    r0 = data["readings"][0]
    assert "timestamp" in r0
    assert isinstance(r0["kwh"], float)
    assert isinstance(r0["volt_r"], float)


@pytest.mark.anyio
async def test_transformers_endpoint(client: AsyncClient):
    """Test listing distribution transformers."""
    response = await client.get("/api/v1/transformers?page=1&page_size=5")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] > 0
    assert len(data["items"]) <= 5
    dt0 = data["items"][0]
    assert "code" in dt0
    assert "name" in dt0


@pytest.mark.anyio
async def test_hierarchy_tree(client: AsyncClient):
    """Test reconstructing full network hierarchy tree."""
    response = await client.get("/api/v1/hierarchy")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_meters"] > 0
    assert len(data["roots"]) > 0
    root = data["roots"][0]
    assert root["level"] == "zone"
    assert len(root["children"]) > 0


@pytest.mark.anyio
async def test_bulk_export(client: AsyncClient):
    """Test cryptographic bulk export tunnel."""
    response = await client.get("/api/v1/meters/bulk-export")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] >= 400
    assert len(data["meters"]) == data["total"]


@pytest.mark.anyio
async def test_dashboard_html(client: AsyncClient):
    """Test dashboard web client root returns 200 HTML."""
    response = await client.get("/")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "Flock Energy" in response.text
