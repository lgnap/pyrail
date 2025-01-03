"""Module providing the iRail class for interacting with the iRail API."""

import asyncio
from asyncio import Lock
from datetime import datetime
import logging
import time
from types import TracebackType
from typing import Any, Dict, Optional, Type

from aiohttp import ClientError, ClientSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger: logging.Logger = logging.getLogger(__name__)

base_url: str = "https://api.irail.be/{}/"


class iRail:
    """A Python wrapper for the iRail API, handling rate limiting and endpoint requests.

    Attributes:
        format (str): The data format for API responses ('json', 'xml', 'jsonp').
        lang (str): The language for API responses ('nl', 'fr', 'en', 'de').

    Endpoints:
        stations: Retrieve all stations.
        liveboard: Retrieve a liveboard for a station or ID.
        connections: Retrieve connections between two stations.
        vehicle: Retrieve information about a specific train.
        composition: Retrieve the composition of a train.
        disturbances: Retrieve information about current disturbances on the rail network.

    """

    # Available iRail API endpoints and their parameter requirements.
    # Each endpoint is configured with required parameters, optional parameters, and XOR
    # parameter groups (where exactly one parameter from the group must be provided).
    endpoints: Dict[str, Dict[str, Any]] = {
        "stations": {},
        "liveboard": {"xor": ["station", "id"], "optional": ["date", "time", "arrdep", "alerts"]},
        "connections": {
            "required": ["from", "to"],
            "optional": ["date", "time", "timesel", "typeOfTransport", "alerts", "results"],
        },
        "vehicle": {"required": ["id"], "optional": ["date", "alerts"]},
        "composition": {"required": ["id"], "optional": ["data"]},
        "disturbances": {"optional": ["lineBreakCharacter"]},
    }

    def __init__(self, format: str = "json", lang: str = "en") -> None:
        """Initialize the iRail API client.

        Args:
            format (str): The format of the API responses. Default is 'json'.
            lang (str): The language for API responses. Default is 'en'.

        """
        self.format: str = format
        self.lang: str = lang
        self.tokens: int = 3
        self.burst_tokens: int = 5
        self.last_request_time: float = time.time()
        self.lock: Lock = Lock()
        self.session: Optional[ClientSession] = None
        self.etag_cache: Dict[str, str] = {}
        logger.info("iRail instance created")

    async def __aenter__(self):
        """Initialize and return the aiohttp client session when entering the async context."""
        self.session = ClientSession()
        return self

    async def __aexit__(
        self, exc_type: Optional[Type[BaseException]], exc: Optional[BaseException], tb: Optional[TracebackType]
    ) -> None:
        """Close the aiohttp client session when exiting the async context."""
        if self.session:
            try:
                await self.session.close()
            except Exception as e:
                logger.error("Error closing session: %s", e)

    @property
    def format(self) -> str:
        return self.__format

    @format.setter
    def format(self, value: str) -> None:
        if value in ["xml", "json", "jsonp"]:
            self.__format = value
        else:
            self.__format = "json"

    @property
    def lang(self) -> str:
        return self.__lang

    @lang.setter
    def lang(self, value: str) -> None:
        if value in ["nl", "fr", "en", "de"]:
            self.__lang = value
        else:
            self.__lang = "en"

    def _refill_tokens(self) -> None:
        """Refill rate limit tokens based on elapsed time."""
        logger.debug("Refilling tokens")
        current_time: float = time.time()
        elapsed: float = current_time - self.last_request_time
        self.last_request_time = current_time

        # Refill tokens, 3 tokens per second, cap tokens at 3
        self.tokens = min(3, self.tokens + int(elapsed * 3))
        # Refill burst tokens, 3 burst tokens per second, cap burst tokens at 5
        self.burst_tokens = min(5, self.burst_tokens + int(elapsed * 3))

    async def _handle_rate_limit(self) -> None:
        """Handle rate limiting using a token bucket algorithm.

        The implementation uses two buckets:
        - Normal bucket: 3 tokens/second
        - Burst bucket: 5 tokens/second
        """
        logger.debug("Handling rate limit")
        self._refill_tokens()
        if self.tokens < 1:
            if self.burst_tokens >= 1:
                self.burst_tokens -= 1
            else:
                logger.warning("Rate limiting active, waiting for tokens")
                wait_time = max(0, 1 - (time.time() - self.last_request_time))
                await asyncio.sleep(wait_time)
                self._refill_tokens()
                self.tokens -= 1
        else:
            self.tokens -= 1

    def _add_etag_header(self, method: str) -> Dict[str, str]:
        """Add ETag header if a cached ETag exists."""
        headers: Dict[str, str] = {"User-Agent": "pyRail (https://github.com/tjorim/pyrail; tielemans.jorim@gmail.com)"}
        if method in self.etag_cache:
            logger.debug("Adding If-None-Match header with value: %s", self.etag_cache[method])
            headers["If-None-Match"] = self.etag_cache[method]
        return headers

    def _validate_date(self, date: Optional[str]) -> bool:
        """Validate date format (DDMMYY)."""
        if not date:
            return True
        try:
            datetime.strptime(date, "%d%m%y")
            return True
        except ValueError:
            logger.error("Invalid date format. Expected DDMMYY (e.g., 150923 for September 15, 2023), got: %s", date)
            return False

    def _validate_time(self, time: Optional[str]) -> bool:
        """Validate time format (HHMM)."""
        if not time:
            return True
        try:
            datetime.strptime(time, "%H%M")
            return True
        except ValueError:
            logger.error("Invalid time format. Expected HHMM (e.g., 1430 for 2:30 PM), got: %s", time)
            return False

    def validate_params(self, method: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """Validate parameters and XOR conditions for a given endpoint."""
        if method not in self.endpoints:
            logger.error("Unknown API endpoint: %s", method)
            return False

        endpoint = self.endpoints[method]
        required = endpoint.get("required", [])
        xor = endpoint.get("xor", [])
        optional = endpoint.get("optional", [])

        params = params or {}

        # Validate date and time formats if present
        if "date" in params and not self._validate_date(params["date"]):
            return False
        if "time" in params and not self._validate_time(params["time"]):
            return False

        # Ensure all required parameters are present
        for param in required:
            if param not in params or params[param] is None:
                logger.error("Missing required parameter: %s for endpoint: %s", param, method)
                return False

        # Check XOR logic (only one of XOR parameters can be set)
        if xor:
            xor_values = [params.get(param) is not None for param in xor]
            if sum(xor_values) != 1:
                logger.error("Exactly one of the XOR parameters %s must be provided for endpoint: %s", xor, method)
                return False

        # Ensure no unexpected parameters are included
        all_params = required + xor + optional
        for param in params.keys():
            if param not in all_params:
                logger.error("Unexpected parameter: %s for endpoint: %s", param, method)
                return False

        return True

    async def do_request(self, method: str, args: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Send a request to the specified iRail API endpoint."""
        logger.info("Starting request to endpoint: %s", method)
        if self.session is None:
            logger.error("Session not initialized. Use 'async with' context manager to initialize the client.")
            return None
        if not self.validate_params(method, args or {}):
            logger.error("Validation failed for method: %s with args: %s", method, args)
            return None
        async with self.lock:
            await self._handle_rate_limit()

        # Construct the request URL and parameters
        url: str = base_url.format(method)
        params = {"format": self.format, "lang": self.lang}
        if args:
            params.update(args)

        request_headers: Dict[str, str] = self._add_etag_header(method)

        try:
            async with self.session.get(url, params=params, headers=request_headers) as response:
                if response.status == 429:
                    retry_after: int = int(response.headers.get("Retry-After", 1))
                    logger.warning("Rate limited, retrying after %d seconds", retry_after)
                    await asyncio.sleep(retry_after)
                    return await self.do_request(method, args)
                elif response.status == 400:
                    error_text = await response.text()
                    logger.error("Bad request: %s", error_text)
                    return None
                elif response.status == 404:
                    logger.error("Endpoint not found: %s", url)
                    return None
                if response.status == 200:
                    # Cache the ETag from the response
                    if "Etag" in response.headers:
                        self.etag_cache[method] = response.headers["Etag"]
                    try:
                        json_data = await response.json()
                        if not json_data:
                            logger.warning("Empty response received")
                        return json_data
                    except ValueError:
                        logger.error("Failed to parse JSON response")
                        return None
                elif response.status == 304:
                    logger.info("Data not modified, using cached data")
                    return None
                else:
                    logger.error(
                        "Request failed with status code: %s, response: %s", response.status, await response.text()
                    )
                    return None
        except ClientError as e:
            logger.error("Request failed due to an exception: %s", e)
            return None

    async def get_stations(self) -> Optional[Dict[str, Any]]:
        """Retrieve a list of all stations."""
        return await self.do_request("stations")

    async def get_liveboard(
        self,
        station: Optional[str] = None,
        id: Optional[str] = None,
        date: Optional[str] = None,
        time: Optional[str] = None,
        arrdep: str = "departure",
        alerts: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a liveboard for a station or station ID."""
        extra_params: Dict[str, Optional[Any]] = {
            "station": station,
            "id": id,
            "date": date,
            "time": time,
            "arrdep": arrdep,
            "alerts": "true" if alerts else "false",
        }
        return await self.do_request("liveboard", {k: v for k, v in extra_params.items() if v is not None})

    async def get_connections(
        self,
        from_station: str,
        to_station: str,
        date: Optional[str] = None,
        time: Optional[str] = None,
        timesel: str = "departure",
        type_of_transport: str = "automatic",
        alerts: bool = False,
        results: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve connections between two stations."""
        extra_params: Dict[str, Optional[Any]] = {
            "from": from_station,
            "to": to_station,
            "date": date,
            "time": time,
            "timesel": timesel,
            "typeOfTransport": type_of_transport,
            "alerts": "true" if alerts else "false",
            "results": results,
        }
        return await self.do_request("connections", {k: v for k, v in extra_params.items() if v is not None})

    async def get_vehicle(self, id: str, date: Optional[str] = None, alerts: bool = False) -> Optional[Dict[str, Any]]:
        """Retrieve information about a vehicle (train)."""
        extra_params: Dict[str, Optional[Any]] = {"id": id, "date": date, "alerts": "true" if alerts else "false"}
        return await self.do_request("vehicle", {k: v for k, v in extra_params.items() if v is not None})

    async def get_composition(self, id: str, data: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieve the composition of a train."""
        extra_params: Dict[str, Optional[str]] = {"id": id, "data": data}
        return await self.do_request("composition", {k: v for k, v in extra_params.items() if v is not None})

    async def get_disturbances(self, line_break_character: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieve information about current disturbances on the rail network."""
        extra_params = {"lineBreakCharacter": line_break_character}
        return await self.do_request("disturbances", {k: v for k, v in extra_params.items() if v is not None})
