"""CLI Entrypoint for Duo-Moonshot Paper Trading.
"""

import asyncio
import argparse
import logging
import uvicorn
from moonshot.strategy import MoonshotConfig
from moonshot.paper.runner import PaperRunner
from moonshot.paper import api

async def run_start(args):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    config = MoonshotConfig()
    runner = PaperRunner(config)
    api._runner = runner
    
    await runner.start()
    try:
        config_uvicorn = uvicorn.Config(api.app, host="0.0.0.0", port=args.port, log_level="info")
        server = uvicorn.Server(config_uvicorn)
        await server.serve()
    finally:
        await runner.stop()

def main():
    parser = argparse.ArgumentParser(description="Duo-Moonshot Paper Trading CLI")
    subparsers = parser.add_subparsers(dest="command")
    
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--port", type=int, default=8100)
    
    subparsers.add_parser("status")
    subparsers.add_parser("scan")
    
    args = parser.parse_args()
    
    if args.command == "start":
        asyncio.run(run_start(args))
    elif args.command == "status":
        # Direct short status
        from moonshot.paper.paper_store import PaperStore
        store = PaperStore()
        print(f"Capital: ${store.get_state('capital')}")
        print(f"Positions: {store.position_count()}")
    elif args.command == "scan":
        async def run_scan():
            config = MoonshotConfig()
            runner = PaperRunner(config)
            await runner.client.__aenter__()
            await runner.scanner.scan()
            await runner.client.__aexit__(None, None, None)
        asyncio.run(run_scan())

if __name__ == "__main__":
    main()
