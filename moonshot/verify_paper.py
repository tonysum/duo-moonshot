"""Verify Paper Trading Module — tests connectivity and data feed.
"""

import asyncio
import logging
from moonshot.client import BinanceFuturesClient
from moonshot.paper.live_feed import LiveFeed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def test_live_feed():
    async with BinanceFuturesClient() as client:
        feed = LiveFeed(client)
        
        logger.info("1. Testing get_usdt_symbols...")
        symbols = await feed.get_usdt_symbols()
        logger.info(f"   Found {len(symbols)} symbols. Example: {symbols[:5]}")
        
        if not symbols:
            logger.error("No symbols found. Check internet or API.")
            return

        test_sym = "BTCUSDT"
        logger.info(f"2. Testing current price for {test_sym}...")
        price = await feed.get_current_price(test_sym)
        logger.info(f"   {test_sym} price: {price}")

        logger.info(f"3. Testing 30d avg price for {test_sym}...")
        avg_30d = await feed.load_30d_avg_price(test_sym)
        logger.info(f"   {test_sym} 30d avg: {avg_30d}")

        logger.info(f"4. Testing daily top gainers scan (top 5)...")
        gainers = await feed.scan_daily_top_gainers(min_pct_chg=1.0, top_n=5)
        logger.info(f"   Top gainers: {gainers}")

        logger.info(f"5. Testing Supertrend (1h) for {test_sym}...")
        trend = await feed.load_supertrend(test_sym, period=10, multiplier=3.0, timeframe="1h")
        logger.info(f"   {test_sym} Supertrend (1h): {trend}")

        logger.info(f"6. Testing Supertrend (15m) for {test_sym}...")
        trend_15m = await feed.load_supertrend(test_sym, period=10, multiplier=3.0, timeframe="15m")
        logger.info(f"   {test_sym} Supertrend (15m): {trend_15m}")

if __name__ == "__main__":
    asyncio.run(test_live_feed())
