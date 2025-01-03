import logging
from asyncio import Lock
import asyncio
from aiohttp import ClientSession, ClientError
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

base_url = 'https://api.irail.be/{}/'

methods = {
    'stations': [],
    'liveboard': ['id', 'station', 'date', 'time', 'arrdep', 'alerts'],
    'connections': ['from', 'to', 'date', 'time', 'timesel', 'typeOfTransport', 'alerts', 'results'],
    'vehicle': ['id', 'date', 'alerts'],
    'disturbances': []
    }

headers = {'user-agent': 'pyRail (tielemans.jorim@gmail.com)'}

"""
This module provides the iRail class for interacting with the iRail API.
"""

class iRail:
    """A Python wrapper for the iRail API.

    Attributes:
        format (str): The data format for API responses ('json', 'xml', 'jsonp').
        lang (str): The language for API responses ('nl', 'fr', 'en', 'de').

    """

    def __init__(self, format='json', lang='en'):
        """Initialize the iRail API client.

        Args:
            format (str): The format of the API responses. Default is 'json'.
            lang (str): The language for API responses. Default is 'en'.

        """
        self.format = format
        self.lang = lang
        self.tokens = 3
        self.burst_tokens = 5
        self.last_request_time = time.time()
        self.lock = Lock()
        self.etag_cache = {}
        self.session = None
        logger.info("iRail instance created")

    async def __aenter__(self):
        self.session = ClientSession(headers=headers)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    @property
    def format(self):
        return self.__format

    @format.setter
    def format(self, value):
        if value in ['xml', 'json', 'jsonp']:
            self.__format = value
        else:
            self.__format = 'json'

    @property
    def lang(self):
        return self.__lang

    @lang.setter
    def lang(self, value):
        if value in ['nl', 'fr', 'en', 'de']:
            self.__lang = value
        else:
            self.__lang = 'en'

    def _refill_tokens(self):
        logger.debug("Refilling tokens")
        current_time = time.time()
        elapsed = current_time - self.last_request_time
        self.last_request_time = current_time

        # Refill tokens based on elapsed time
        self.tokens += elapsed * 3  # 3 tokens per second
        if self.tokens > 3:
            self.tokens = 3

        # Refill burst tokens
        self.burst_tokens += elapsed * 3  # 3 burst tokens per second
        if self.burst_tokens > 5:
            self.burst_tokens = 5

    async def do_request(self, method, args=None):
        logger.info("Starting request to endpoint: %s", method)
        async with self.lock:
            self._refill_tokens()

            if self.tokens < 1:
                if self.burst_tokens >= 1:
                    self.burst_tokens -= 1
                else:
                    logger.warning("Rate limiting, waiting for tokens")
                    await asyncio.sleep(1 - (time.time() - self.last_request_time))
                    self._refill_tokens()
                    self.tokens -= 1
            else:
                self.tokens -= 1

        if method in methods:
            url = base_url.format(method)
            params = {'format': self.format, 'lang': self.lang}
            if args:
                params.update(args)
            headers = {}

            # Add If-None-Match header if we have a cached ETag
            if method in self.etag_cache:
                logger.debug("Adding If-None-Match header with value: %s", self.etag_cache[method])
                headers['If-None-Match'] = self.etag_cache[method]

            try:
                async with self.session.get(url, params=params, headers=headers) as response:
                    if response.status == 429:
                        logger.warning("Rate limited, waiting for retry-after header")
                        retry_after = int(response.headers.get("Retry-After", 1))
                        await asyncio.sleep(retry_after)
                        return await self.do_request(method, args)
                    if response.status == 200:
                        # Cache the ETag from the response
                        if 'Etag' in response.headers:
                            self.etag_cache[method] = response.headers['Etag']
                        try:
                            json_data = await response.json()
                            return json_data
                        except ValueError:
                            return -1
                    elif response.status == 304:
                        logger.info("Data not modified, using cached data")
                        return None
                    else:
                        logger.error("Request failed with status code: %s", response.status)
                        return None
            except ClientError as e:
                logger.error("Request failed: %s", e)
                try:
                    await self.session.get('https://1.1.1.1/', timeout=1)
                except ClientError:
                    logger.error("Internet connection failed")
                    return -1
                else:
                    logger.error("iRail API failed")
                    return -1

    async def get_stations(self):
        """Retrieve a list of all stations."""
        json_data = await self.do_request('stations')
        return json_data

    async def get_liveboard(self, station=None, id=None):
        if bool(station) ^ bool(id):
            extra_params = {'station': station, 'id': id}
            json_data = await self.do_request('liveboard', extra_params)
            return json_data

    async def get_connections(self, from_station=None, to_station=None):
        if from_station and to_station:
            extra_params = {'from': from_station, 'to': to_station}
            json_data = await self.do_request('connections', extra_params)
            return json_data

    async def get_vehicle(self, id=None):
        if id:
            extra_params = {'id': id}
            json_data = await self.do_request('vehicle', extra_params)
            return json_data
