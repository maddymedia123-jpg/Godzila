import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
import aiohttp
import websockets
from quant_engine import QuantEngine

logger = logging.getLogger("StreamManager")


class BinanceFuturesStreamManager:
    """
    Background asyncio worker managing multiplexed Binance USD-M Futures WebSocket
    streams (depth20@100ms + aggTrade) and periodic 15m REST klines for GARCH calibration.
    """

    WS_BASE = "wss://fstream.binance.com/stream"
    REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"

    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol.upper()
        self.engine = QuantEngine()

        self._lock = threading.Lock()
        self._latest_state: Dict[str, Any] = {}
        self._connection_status: str = "INITIALIZING"
        self._msg_count: int = 0
        self._last_msg_ts: float = 0.0

        # Configurable runtime parameters synced from UI
        self.account_balance: float = 10000.0
        self.risk_pct: float = 1.0
        self.structural_zones: List[float] = []

        self._stop_event = threading.Event()
        self._restart_stream_flag = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = threading.Thread(target=self._run_background_loop, daemon=True)
        self._thread.start()

    def update_config(
        self,
        symbol: str,
        account_balance: float,
        risk_pct: float,
        decay_lambda: float,
        imbalance_threshold: float,
        max_spread_phi: float,
        garch_k: float,
        structural_zones: List[float],
    ) -> None:
        symbol = symbol.upper().strip()
        with self._lock:
            self.account_balance = account_balance
            self.risk_pct = risk_pct
            self.structural_zones = structural_zones
            self.engine.update_params(
                decay_lambda=decay_lambda,
                max_spread_phi=max_spread_phi,
                imbalance_threshold=imbalance_threshold,
                garch_k=garch_k,
            )
            if symbol != self.symbol:
                logger.info(f"Switching stream symbol from {self.symbol} to {symbol}")
                self.symbol = symbol
                self.engine = QuantEngine(
                    decay_lambda=decay_lambda,
                    max_spread_phi=max_spread_phi,
                    imbalance_threshold=imbalance_threshold,
                    garch_k=garch_k,
                )
                self._latest_state = {}
                self._restart_stream_flag = True

    def get_snapshot(self) -> Dict[str, Any]:
        """Thread-safe state accessor for Streamlit UI rendering."""
        with self._lock:
            return {
                "symbol": self.symbol,
                "status": self._connection_status,
                "msg_count": self._msg_count,
                "last_msg_ts": self._last_msg_ts,
                "metrics": dict(self._latest_state),
                "imbalance_history": list(self.engine.imbalance_history),
                "events_log": list(self.engine.events_log),
                "latest_signal": self.engine.latest_signal,
                "garch_sigma": self.engine.garch_sigma,
                "garch_regime": self.engine.garch_regime,
                "garch_last_fit": self.engine.garch_last_fit,
            }

    def _run_background_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._supervisor())

    async def _supervisor(self) -> None:
        """Runs concurrent GARCH scheduler and resilient WebSocket consumer."""
        garch_task = asyncio.create_task(self._garch_updater_loop())
        ws_task = asyncio.create_task(self._ws_stream_loop())
        await asyncio.gather(garch_task, ws_task)

    async def _fetch_historical_klines(self, session: aiohttp.ClientSession, symbol: str) -> List[float]:
        params = {"symbol": symbol, "interval": "15m", "limit": 500}
        try:
            async with session.get(self.REST_KLINES, params=params, timeout=10) as resp:
                if resp.status == 429:
                    logger.warning("Binance REST rate limit hit (429). Backing off...")
                    await asyncio.sleep(10)
                    return []
                resp.raise_for_status()
                data = await resp.json()
                # Kline index 4 is Close price
                return [float(candle[4]) for candle in data]
        except Exception as exc:
            logger.error(f"Failed fetching 15m klines for {symbol}: {exc}")
            return []

    async def _garch_updater_loop(self) -> None:
        """Fetches 15m historical candles immediately on startup/symbol change, then every 15 minutes."""
        last_symbol = None
        last_fetch_ts = 0.0

        async with aiohttp.ClientSession() as session:
            while not self._stop_event.is_set():
                now = time.time()
                with self._lock:
                    curr_symbol = self.symbol

                needs_refresh = (curr_symbol != last_symbol) or ((now - last_fetch_ts) >= 900.0)
                if needs_refresh:
                    closes = await self._fetch_historical_klines(session, curr_symbol)
                    if closes:
                        with self._lock:
                            if self.symbol == curr_symbol:
                                self.engine.fit_garch(closes)
                        last_symbol = curr_symbol
                        last_fetch_ts = time.time()

                await asyncio.sleep(2.0)

    async def _ws_stream_loop(self) -> None:
        """Connects to Binance Futures multiplexed WebSocket with exponential backoff."""
        backoff = 1.0
        while not self._stop_event.is_set():
            with self._lock:
                curr_symbol = self.symbol.lower()
                self._restart_stream_flag = False
                self._connection_status = f"CONNECTING ({curr_symbol.upper()})"

            streams = f"{curr_symbol}@depth20@100ms/{curr_symbol}@aggTrade"
            url = f"{self.WS_BASE}?streams={streams}"

            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=512,
                ) as ws:
                    backoff = 1.0
                    with self._lock:
                        self._connection_status = "LIVE (100ms)"
                    logger.info(f"Connected to Binance Futures WS: {streams}")

                    while not self._stop_event.is_set():
                        with self._lock:
                            if self._restart_stream_flag:
                                break

                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue

                        self._handle_message(raw_msg)

            except Exception as exc:
                with self._lock:
                    self._connection_status = f"RECONNECTING ({backoff:.0f}s)"
                logger.warning(f"WebSocket disconnected ({exc}). Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    def _handle_message(self, raw_msg: str) -> None:
        try:
            payload = json.loads(raw_msg)
            stream = payload.get("stream", "")
            data = payload.get("data", {})
            now = time.time()

            with self._lock:
                self._msg_count += 1
                self._last_msg_ts = now

                if "@aggTrade" in stream:
                    price = float(data["p"])
                    qty = float(data["q"])
                    is_buyer_maker = bool(data["m"])
                    ts_sec = float(data["T"]) / 1000.0
                    self.engine.ingest_trade(ts_sec, price, qty, is_buyer_maker)

                elif "@depth20" in stream:
                    # Binance Futures depth20 uses 'b' and 'a' keys
                    raw_bids = data.get("b") or data.get("bids") or []
                    raw_asks = data.get("a") or data.get("asks") or []
                    bids = [(float(p), float(q)) for p, q in raw_bids if float(q) > 0]
                    asks = [(float(p), float(q)) for p, q in raw_asks if float(q) > 0]

                    metrics = self.engine.process_orderbook(
                        timestamp_sec=now,
                        bids=bids,
                        asks=asks,
                        symbol=self.symbol,
                        account_balance=self.account_balance,
                        risk_pct=self.risk_pct,
                        structural_zones=self.structural_zones,
                    )
                    if metrics:
                        self._latest_state = metrics

        except Exception as exc:
            logger.debug(f"Message parse error: {exc}")

    def shutdown(self) -> None:
        self._stop_event.set()
