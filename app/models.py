from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(..., json_schema_extra={"example": "healthy"})
    upstream_portal: str = Field(..., json_schema_extra={"example": "connected"})
    upstream_authenticated: bool = Field(..., json_schema_extra={"example": True})
    version: str = Field(..., json_schema_extra={"example": "1.0.0"})
    timestamp: str = Field(..., json_schema_extra={"example": "2026-09-10T16:50:00Z"})


class AuthLoginRequest(BaseModel):
    email: str = Field(..., json_schema_extra={"example": "operator@urja.local"})
    password: str = Field(..., json_schema_extra={"example": "urja-ops-2026"})


class AuthLoginResponse(BaseModel):
    status: str = Field(..., json_schema_extra={"example": "authenticated"})
    email: str = Field(..., json_schema_extra={"example": "operator@urja.local"})
    session_active: bool = Field(..., json_schema_extra={"example": True})
    message: str = Field(..., json_schema_extra={"example": "Session established with legacy Urja portal"})


class MeterListItem(BaseModel):
    meter_id: str = Field(..., json_schema_extra={"example": "J100000"})
    serial_no: str = Field(..., json_schema_extra={"example": "SE33962"})
    make: str = Field(..., json_schema_extra={"example": "HPL"})
    phase_type: str = Field(..., json_schema_extra={"example": "single"})
    install_status: str = Field(..., json_schema_extra={"example": "Decommissioned"})
    dt_code: Optional[str] = Field(None, json_schema_extra={"example": "DT-001"})


class MeterListResponse(BaseModel):
    items: List[MeterListItem]
    total: int = Field(..., json_schema_extra={"example": 403})
    page: int = Field(..., json_schema_extra={"example": 1})
    page_size: int = Field(..., json_schema_extra={"example": 20})
    total_pages: int = Field(..., json_schema_extra={"example": 21})


class GeoLocation(BaseModel):
    latitude: Optional[float] = Field(None, json_schema_extra={"example": 26.938961})
    longitude: Optional[float] = Field(None, json_schema_extra={"example": 75.830957})


class EnergyReading(BaseModel):
    timestamp: str = Field(..., json_schema_extra={"example": "2026-06-23T23:30:00+05:30"})
    raw_timestamp: str = Field(..., json_schema_extra={"example": "23/06/2026 23:30"})
    kwh: Optional[float] = Field(None, json_schema_extra={"example": 48438.74})
    kvah: Optional[float] = Field(None, json_schema_extra={"example": 52313.84})
    volt_r: Optional[float] = Field(None, json_schema_extra={"example": 226.0})


class ConsumptionSummary(BaseModel):
    reading_count: int = Field(..., json_schema_extra={"example": 337})
    start_time: Optional[str] = Field(None, json_schema_extra={"example": "2026-06-23T23:30:00+05:30"})
    end_time: Optional[str] = Field(None, json_schema_extra={"example": "2026-06-30T23:30:00+05:30"})
    start_kwh: Optional[float] = Field(None, json_schema_extra={"example": 48438.74})
    end_kwh: Optional[float] = Field(None, json_schema_extra={"example": 48580.79})
    net_kwh_consumed: Optional[float] = Field(None, json_schema_extra={"example": 142.05})
    avg_voltage: Optional[float] = Field(None, json_schema_extra={"example": 228.4})
    min_voltage: Optional[float] = Field(None, json_schema_extra={"example": 214.0})
    max_voltage: Optional[float] = Field(None, json_schema_extra={"example": 245.0})


class MeterConsumptionResponse(BaseModel):
    meter_id: str = Field(..., json_schema_extra={"example": "J100000"})
    summary: ConsumptionSummary
    readings: List[EnergyReading]


class HierarchyUnit(BaseModel):
    name: str = Field(..., json_schema_extra={"example": "Jaipur Zone 1"})
    code: Optional[str] = Field(None, json_schema_extra={"example": "Z-01"})


class HierarchyInfo(BaseModel):
    zone: Optional[HierarchyUnit] = None
    circle: Optional[HierarchyUnit] = None
    division: Optional[HierarchyUnit] = None
    subdivision: Optional[HierarchyUnit] = None
    substation: Optional[HierarchyUnit] = None
    feeder: Optional[HierarchyUnit] = None
    dt: Optional[HierarchyUnit] = None


class MeterDetailResponse(BaseModel):
    meter_id: str = Field(..., json_schema_extra={"example": "J100000"})
    serial_no: str = Field(..., json_schema_extra={"example": "SE33962"})
    make: str = Field(..., json_schema_extra={"example": "HPL"})
    phase_type: str = Field(..., json_schema_extra={"example": "single"})
    install_status: str = Field(..., json_schema_extra={"example": "Decommissioned"})
    install_type: Optional[str] = Field(None, json_schema_extra={"example": "Whole Current"})
    build: Optional[str] = Field(None, json_schema_extra={"example": "legacy"})
    dt_code: Optional[str] = Field(None, json_schema_extra={"example": "DT-001"})
    geo: Optional[GeoLocation] = None
    hierarchy: Optional[HierarchyInfo] = None
    extra_parameters: Dict[str, Any] = Field(default_factory=dict)


class TransformerItem(BaseModel):
    code: str = Field(..., json_schema_extra={"example": "DT-001"})
    name: str = Field(..., json_schema_extra={"example": "Malviya Nagar DT 1"})
    feeder_code: Optional[str] = Field(None, json_schema_extra={"example": "F-001"})
    capacity_kva: Optional[float] = Field(None, json_schema_extra={"example": 100.0})


class TransformerListResponse(BaseModel):
    items: List[TransformerItem]
    total: int = Field(..., json_schema_extra={"example": 40})
    page: int = Field(..., json_schema_extra={"example": 1})
    page_size: int = Field(..., json_schema_extra={"example": 20})
    total_pages: int = Field(..., json_schema_extra={"example": 2})


class HierarchyTreeNode(BaseModel):
    id: str = Field(..., json_schema_extra={"example": "Z-01"})
    name: str = Field(..., json_schema_extra={"example": "Jaipur Zone 1"})
    level: str = Field(..., json_schema_extra={"example": "zone"})
    meter_count: int = Field(default=0, json_schema_extra={"example": 403})
    children: List['HierarchyTreeNode'] = Field(default_factory=list)


class HierarchyTreeResponse(BaseModel):
    total_meters: int = Field(..., json_schema_extra={"example": 403})
    roots: List[HierarchyTreeNode]


class BulkExportResponse(BaseModel):
    total: int = Field(..., json_schema_extra={"example": 403})
    exported_at: str = Field(..., json_schema_extra={"example": "2026-09-10T16:50:00Z"})
    meters: List[MeterDetailResponse]
