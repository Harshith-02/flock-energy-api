import time
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger("urja_client")
logging.basicConfig(level=logging.INFO)


class UrjaPortalClient:
    """
    HTTP Adapter and Scraper client for the legacy Urja Meter Ops portal.
    Handles session persistence, CSRF origin verification, auto-reauthentication,
    HMAC-SHA256 request signing, data parsing, and caching.
    """

    def __init__(self, base_url: Optional[str] = None):
        self.settings = get_settings()
        self.base_url = (base_url or self.settings.portal_base_url).rstrip("/")
        self.client: Optional[httpx.AsyncClient] = None
        self._lock: Optional[asyncio.Lock] = None
        self._is_authenticated = False
        self._auth_email = self.settings.portal_email
        self._auth_password = self.settings.portal_password
        self._last_auth_time = 0.0

        # In-memory caches: key -> (timestamp, data)
        self._cache: Dict[str, Tuple[float, Any]] = {}

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_client(self) -> httpx.AsyncClient:
        """Returns or initializes the persistent AsyncClient."""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.settings.http_timeout_seconds),
                follow_redirects=False,
                headers={"User-Agent": "FlockEnergy-API-Wrapper/1.0"}
            )
        return self.client

    async def close(self):
        """Close the underlying HTTP client session."""
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    def _get_cached(self, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        """Retrieve item from in-memory cache if not expired."""
        if not self.settings.cache_enabled:
            return None
        ttl_seconds = ttl or self.settings.cache_ttl_seconds
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < ttl_seconds:
                return val
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, val: Any):
        """Store item into in-memory cache."""
        if self.settings.cache_enabled:
            self._cache[key] = (time.time(), val)

    def clear_cache(self):
        """Invalidate the in-memory cache."""
        self._cache.clear()

    async def _do_login(self, email: Optional[str] = None, password: Optional[str] = None) -> bool:
        """Internal login execution without acquiring lock (caller must hold lock or be safe)."""
        client = await self._get_client()
        login_email = email or self._auth_email
        login_password = password or self._auth_password

        logger.info(f"Initiating authentication for {login_email}")
        login_url = f"{self.base_url}/login"

        payload = {
            "email": login_email,
            "password": login_password
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": login_url
        }

        try:
            response = await client.post(login_url, data=payload, headers=headers)
            # Successful SvelteKit login returns 200 or 303 redirect JSON
            if response.status_code in (200, 302, 303):
                # Verify session cookie was set
                has_session = any(
                    "session_token" in cookie_name
                    for cookie_name in client.cookies.keys()
                )
                if has_session or response.status_code in (200, 303):
                    self._is_authenticated = True
                    self._auth_email = login_email
                    self._auth_password = login_password
                    self._last_auth_time = time.time()
                    logger.info("Successfully authenticated with Urja portal")
                    return True

            logger.error(f"Login failed with status {response.status_code}: {response.text}")
            self._is_authenticated = False
            return False
        except Exception as e:
            logger.error(f"Error during login: {e}")
            self._is_authenticated = False
            raise

    async def login(self, email: Optional[str] = None, password: Optional[str] = None) -> bool:
        """Authenticate against the Urja portal with CSRF and Origin headers."""
        async with self.lock:
            return await self._do_login(email, password)

    async def _ensure_authenticated(self):
        """Ensures an active session exists before making protected requests."""
        if not self._is_authenticated:
            async with self.lock:
                if not self._is_authenticated:
                    success = await self._do_login()
                    if not success:
                        raise RuntimeError("Failed to authenticate with upstream Urja portal")

    async def request_with_retry(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Any] = None,
        is_retry: bool = False
    ) -> httpx.Response:
        """
        Make an HTTP request with automatic re-authentication on 401/403/session expiry.
        """
        await self._ensure_authenticated()
        client = await self._get_client()

        req_headers = {
            "Referer": self.base_url,
            "Accept": "application/json, text/html, */*"
        }
        if headers:
            req_headers.update(headers)

        full_url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"

        for attempt in range(self.settings.max_retries):
            try:
                response = await client.request(
                    method=method,
                    url=full_url,
                    params=params,
                    headers=req_headers,
                    data=data,
                    json=json_body
                )

                # Check if session expired (401, or 303/302 redirecting to /login, or 403)
                if response.status_code in (401, 403) or (
                    response.status_code in (302, 303) and "/login" in response.headers.get("location", "")
                ):
                    if not is_retry:
                        logger.warning("Session expired or rejected; re-authenticating...")
                        self._is_authenticated = False
                        await self.login()
                        return await self.request_with_retry(
                            method, path, params, headers, data, json_body, is_retry=True
                        )

                return response
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                if attempt < self.settings.max_retries - 1:
                    await asyncio.sleep(self.settings.retry_backoff_seconds * (attempt + 1))
                    continue
                raise e

        raise RuntimeError(f"Request failed after {self.settings.max_retries} attempts: {method} {path}")

    # =========================================================================
    # Data Normalization Helpers
    # =========================================================================

    @staticmethod
    def parse_float_safe(val: Any) -> Optional[float]:
        """Safely parse numbers that may be strings, dashes, or missing."""
        if val is None or val == "" or val == "—" or val == "-":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def parse_indian_timestamp(ts_str: str) -> Tuple[str, str]:
        """
        Convert Indian standard timestamp format 'DD/MM/YYYY HH:mm'
        into an ISO-8601 UTC/IST timestamp. Returns (iso_timestamp, raw_timestamp).
        """
        if not ts_str:
            return "", ""
        try:
            # Parse DD/MM/YYYY HH:mm
            dt = datetime.strptime(ts_str.strip(), "%d/%m/%Y %H:%M")
            # Urja portal operates in IST (UTC+05:30)
            ist_tz = timezone(timedelta(hours=5, minutes=30))
            dt_ist = dt.replace(tzinfo=ist_tz)
            return dt_ist.isoformat(), ts_str
        except Exception:
            return ts_str, ts_str

    # =========================================================================
    # API Methods
    # =========================================================================

    async def get_meters_search(
        self,
        q: str = "",
        page: int = 1,
        bypass_cache: bool = False
    ) -> Dict[str, Any]:
        """Fetch paginated meter search from legacy portal."""
        cache_key = f"meters_search_{q}_{page}"
        if not bypass_cache:
            cached = self._get_cached(cache_key, ttl=60)
            if cached:
                return cached

        resp = await self.request_with_retry(
            "GET",
            "/portal/meters/search",
            params={"q": q, "page": page}
        )

        if resp.status_code == 200:
            data = resp.json()
            self._set_cached(cache_key, data)
            return data
        elif resp.status_code == 404:
            return {"data": [], "total": 0}
        else:
            raise RuntimeError(f"Failed to fetch meters: HTTP {resp.status_code} - {resp.text}")

    async def get_meter_geo(self, meter_id: str) -> Optional[Dict[str, float]]:
        """Fetch meter coordinates from /portal/meters/{id}/geo."""
        cache_key = f"meter_geo_{meter_id}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        resp = await self.request_with_retry("GET", f"/portal/meters/{meter_id}/geo")
        if resp.status_code == 200:
            raw_geo = resp.json().get("data", {})
            geo = {
                "latitude": self.parse_float_safe(raw_geo.get("latitude")),
                "longitude": self.parse_float_safe(raw_geo.get("longitude"))
            }
            self._set_cached(cache_key, geo)
            return geo
        elif resp.status_code == 404:
            return None
        return None

    async def get_meter_energy(self, meter_id: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch consumption time series from /portal/meters/{id}/energy."""
        cache_key = f"meter_energy_{meter_id}"
        cached = self._get_cached(cache_key, ttl=120)
        if cached:
            return cached

        resp = await self.request_with_retry("GET", f"/portal/meters/{meter_id}/energy")
        if resp.status_code == 200:
            raw_data = resp.json().get("data", [])
            normalized = []
            for r in raw_data:
                iso_ts, raw_ts = self.parse_indian_timestamp(r.get("timestamp", ""))
                normalized.append({
                    "timestamp": iso_ts,
                    "raw_timestamp": raw_ts,
                    "kwh": self.parse_float_safe(r.get("kwh")),
                    "kvah": self.parse_float_safe(r.get("kvah")),
                    "volt_r": self.parse_float_safe(r.get("voltR"))
                })
            self._set_cached(cache_key, normalized)
            return normalized
        elif resp.status_code == 404:
            return None
        return None

    async def get_dts(self, page: int = 1, bypass_cache: bool = False) -> Dict[str, Any]:
        """Fetch distribution transformers from /portal/dts."""
        cache_key = f"dts_{page}"
        if not bypass_cache:
            cached = self._get_cached(cache_key, ttl=300)
            if cached:
                return cached

        resp = await self.request_with_retry("GET", "/portal/dts", params={"page": page})
        if resp.status_code == 200:
            data = resp.json()
            self._set_cached(cache_key, data)
            return data
        raise RuntimeError(f"Failed to fetch transformers: HTTP {resp.status_code}")

    async def get_signing_secret(self) -> str:
        """Fetch the export signing secret from /portal/keys."""
        cache_key = "portal_signing_secret"
        cached = self._get_cached(cache_key, ttl=600)
        if cached:
            return cached

        resp = await self.request_with_retry("GET", "/portal/keys")
        if resp.status_code == 200:
            secret = resp.json().get("data", {}).get("signingSecret", "")
            if secret:
                self._set_cached(cache_key, secret)
                return secret
        raise RuntimeError("Failed to obtain signing secret from /portal/keys")

    async def get_bulk_export(self, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        """
        Perform HMAC-SHA256 signed export of all 403 meters from /portal/export.
        Returns complete meter details with full 7-tier hierarchy and coordinates.
        """
        cache_key = "bulk_export_all"
        if not bypass_cache:
            cached = self._get_cached(cache_key, ttl=300)
            if cached:
                return cached

        signing_secret = await self.get_signing_secret()
        timestamp = str(int(time.time()))
        method = "GET"
        path = "/portal/export"
        query = "page=1"

        message = f"{method}\n{path}\n{query}\n{timestamp}".encode("utf-8")
        signature = hmac.new(
            signing_secret.encode("utf-8"),
            message,
            hashlib.sha256
        ).hexdigest()

        headers = {
            "x-timestamp": timestamp,
            "x-signature": signature
        }

        resp = await self.request_with_retry("GET", f"/portal/export?{query}", headers=headers)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            self._set_cached(cache_key, data)
            return data
        raise RuntimeError(f"HMAC bulk export failed: HTTP {resp.status_code} - {resp.text}")

    async def get_meter_details(self, meter_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve unified meter details.
        First checks bulk export / cache for rich hierarchy;
        falls back to HTML scraping / geo endpoints.
        """
        # 1. Try fast lookup in cached bulk export dataset
        bulk_data = await self.get_bulk_export()
        for m in bulk_data:
            if m.get("meterId") == meter_id:
                # Merge with fresh geo if available
                geo = await self.get_meter_geo(meter_id)
                lat = geo["latitude"] if geo else m.get("geo", {}).get("lat")
                lng = geo["longitude"] if geo else m.get("geo", {}).get("lng")

                return {
                    "meter_id": m.get("meterId"),
                    "serial_no": m.get("serialNo"),
                    "make": m.get("make"),
                    "phase_type": m.get("phaseType"),
                    "install_status": m.get("installStatus"),
                    "install_type": m.get("installType"),
                    "build": m.get("build"),
                    "dt_code": m.get("dtCode"),
                    "geo": {
                        "latitude": self.parse_float_safe(lat),
                        "longitude": self.parse_float_safe(lng)
                    },
                    "hierarchy": m.get("hierarchy"),
                    "extra_parameters": {}
                }

        # 2. If not found in bulk export, fall back to checking /meters/{meter_id} HTML & endpoints
        resp = await self.request_with_retry("GET", f"/meters/{meter_id}")
        if resp.status_code == 404 or "Meter not found" in resp.text:
            return None

        # Parse HTML fallback
        soup = BeautifulSoup(resp.text, "html.parser")
        h1 = soup.find("h1")
        title_text = h1.text.strip() if h1 else ""
        if "Meter" not in title_text and not h1:
            return None

        geo = await self.get_meter_geo(meter_id)
        return {
            "meter_id": meter_id,
            "serial_no": "UNKNOWN",
            "make": "UNKNOWN",
            "phase_type": "UNKNOWN",
            "install_status": "UNKNOWN",
            "install_type": None,
            "build": None,
            "dt_code": None,
            "geo": geo,
            "hierarchy": None,
            "extra_parameters": {}
        }
