"""Simplified Binance Futures Client for Moonshot Paper Trading.
"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field


class SymbolInfo(BaseModel):
    symbol: str
    quote_asset: str = Field(alias="quoteAsset")
    contract_type: str = Field(alias="contractType")
    status: str

class ExchangeInfoResponse(BaseModel):
    symbols: list[SymbolInfo]

class Kline(BaseModel):
    open_time: int
    open: str
    high: str
    low: str
    close: str
    volume: str
    quote_asset_volume: str
    taker_buy_quote_asset_volume: str

    @classmethod
    def from_array(cls, data: list):
        # fapi/v1/klines array indices:
        # 0 open_time, 1 open, 2 high, 3 low, 4 close, 5 volume,
        # 7 quote_asset_volume, 10 taker_buy_quote_asset_volume
        return cls(
            open_time=data[0],
            open=data[1],
            high=data[2],
            low=data[3],
            close=data[4],
            volume=data[5],
            quote_asset_volume=data[7],
            taker_buy_quote_asset_volume=data[10],
        )

class TickerPrice(BaseModel):
    symbol: str
    price: str

class LongShortRatio(BaseModel):
    symbol: str
    long_short_ratio: str = Field(alias="longShortRatio")

class FundingRateHistory(BaseModel):
    symbol: str
    funding_rate: str = Field(alias="fundingRate")
    funding_time: int = Field(alias="fundingTime")

class BinanceFuturesClient:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, api_key: str = "", secret_key: str = ""):
        self.api_key = api_key
        self.secret_key = secret_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        limits = httpx.Limits(
            max_connections=40,
            max_keepalive_connections=10,
            keepalive_expiry=30.0,
        )
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"X-MBX-APIKEY": self.api_key} if self.api_key else {},
            limits=limits,
            timeout=httpx.Timeout(10.0),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client: await self._client.aclose()

    async def _request(self, method: str, endpoint: str, params: dict = None, signed: bool = False):
        if params is None: params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query = urlencode(params)
            params["signature"] = hmac.new(self.secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()

        resp = await self._client.request(method, endpoint, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_exchange_info(self) -> ExchangeInfoResponse:
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        return ExchangeInfoResponse.model_validate(data)

    async def get_klines(self, symbol: str, interval: str, limit: int = 100, start_time: int = None, end_time: int = None) -> list[Kline]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None: params["startTime"] = start_time
        if end_time is not None: params["endTime"] = end_time
        data = await self._request("GET", "/fapi/v1/klines", params)
        return [Kline.from_array(k) for k in data]

    async def get_ticker_price(self, symbol: str) -> TickerPrice:
        data = await self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return TickerPrice.model_validate(data)

    async def get_24hr_tickers(self) -> list[dict]:
        """Fetch 24hr ticker for ALL symbols (single call)."""
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def get_top_long_short_account_ratio(self, symbol: str, period: str, limit: int = 1) -> list[LongShortRatio]:
        data = await self._request("GET", "/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": limit})
        return [LongShortRatio.model_validate(r) for r in data]

    async def get_funding_rate(self, symbol: str, start_time: int = None, end_time: int = None, limit: int = 100) -> list[FundingRateHistory]:
        params = {"symbol": symbol, "limit": limit}
        if start_time is not None: params["startTime"] = start_time
        if end_time is not None: params["endTime"] = end_time
        data = await self._request("GET", "/fapi/v1/fundingRate", params)
        return [FundingRateHistory.model_validate(f) for f in data]
