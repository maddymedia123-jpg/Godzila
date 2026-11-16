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
    Background asyncio worker managing USD-M Perpetual Futures WebSocket streams
    (L2 depth + real-time trades) and 15m REST klines for GARCH calibration.
    Includes automatic failover to Bybit Linear Perps if hosted on a US IP (HTTP 451).
    """

    # Binance USD-M Futures routed endpoints
    BINANCE_WS_PUBLIC = "wss://fstream.binance.com/public/stream"
    BINANCE_WS_MARKET = "wss://fstream.binance.com/market/stream"
    BINANCE_REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"

    # Bybit V5 Linear Perpetuals fallback (no US market-data geo-block)
    BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_REST_KLINES = "https://api.bybit.com/v5/market/kline"

    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol.upper()
        self.engine = QuantEngine()

        self._lock = threading.Lock()
        self._latest_state: Dict[str, Any] = {}
        self._connection_status: str = "INITIALIZING"
        self._venue: str = "BINANCE"  # Switches to "BYBIT" automatically on HTTP 451
        self._msg_count: int = 0
        self._last_msg_ts: float = 0.0

        # Local order book state for delta-based feeds
        self._bids_book: Dict[float, float] = {}
        self._asks_book: Dict[float, float] = {}

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
                self._bids_book.clear()
                self._asks_book.clear()
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
        garch_task = asyncio.create_task(self._garch_updater_loop())
        ws_task = asyncio.create_task(self._ws_stream_loop())
        await asyncio.gather(garch_task, ws_task)

    async def _fetch_historical_klines(self, session: aiohttp.ClientSession, symbol: str) -> List[float]:
        # Try Binance REST first unless already failed over to Bybit
        if self._venue == "BINANCE":
            params = {"symbol": symbol, "interval": "15m", "limit": 500}
            try:
                async with session.get(self.BINANCE_REST_KLINES, params=params, timeout=8) as resp:
                    if resp.status == 451:
                        logger.warning("Binance REST returned 451 (US geo-block). Switching to Bybit.")
                        with self._lock:
                            self._venue = "BYBIT"
                    elif resp.status == 200:
                        data = await resp.json()
                        return [float(candle[4]) for candle in data]
            except Exception as exc:
                logger.warning(f"Binance REST unreachable ({exc}), falling back to Bybit klines.")
                with self._lock:
                    self._venue = "BYBIT"

        # Bybit V5 Linear 15m Klines fallback
        bybit_params = {"category": "linear", "symbol": symbol, "interval": "15", "limit": 500}
        try:
            async with session.get(self.BYBIT_REST_KLINES, params=bybit_params, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.json()
                rows = payload.get("result", {}).get("list", [])
                # Bybit returns newest candle first; reverse to chronological order (index 4 = close)
                return [float(candle[4]) for candle in reversed(rows)]
        except Exception as exc:
            logger.error(f"Failed fetching 15m klines from fallback venue for {symbol}: {exc}")
            return []

    async def _garch_updater_loop(self) -> None:
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
        backoff = 1.0
        while not self._stop_event.is_set():
            with self._lock:
                curr_symbol = self.symbol
                venue = self._venue
                self._restart_stream_flag = False
                self._connection_status = f"CONNECTING ({venue})"

            try:
                if venue == "BINANCE":
                    await self._run_binance_streams(curr_symbol)
                else:
                    await self._run_bybit_stream(curr_symbol)
                backoff = 1.0
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "403" in err_str:
                    logger.warning("Detected US geo-block (HTTP 451/403). Failing over to Bybit Perps...")
                    with self._lock:
                        self._venue = "BYBIT"
                        self._connection_status = "FAILOVER -> BYBIT"
                    await asyncio.sleep(0.5)
                    continue

                with self._lock:
                    self._connection_status = f"RECONNECTING ({backoff:.0f}s)"
                logger.warning(f"{venue} WebSocket error ({exc}). Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 20.0)

    async def _run_binance_streams(self, symbol: str) -> None:
        """Connects to Binance's routed /public (depth) and /market (aggTrade) endpoints."""
        s_lower = symbol.lower()
        depth_url = f"{self.BINANCE_WS_PUBLIC}?streams={s_lower}@depth20@100ms"
        trade_url = f"{self.BINANCE_WS_MARKET}?streams={s_lower}@aggTrade"

        async with websockets.connect(depth_url, ping_interval=20, ping_timeout=20) as ws_depth, \
                   websockets.connect(trade_url, ping_interval=20, ping_timeout=20) as ws_trade:
            with self._lock:
                self._connection_status = "LIVE BINANCE (100ms)"

            async def consume(ws):
                while not self._stop_event.is_set():
                    with self._lock:
                        if self._restart_stream_flag:
                            return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        self._handle_binance_msg(raw)
                    except asyncio.TimeoutError:
                        continue

            await asyncio.gather(consume(ws_depth), consume(ws_trade))

    async def _run_bybit_stream(self, symbol: str) -> None:
        """Connects to Bybit V5 Linear Perps (orderbook.50 + publicTrade)."""
        async with websockets.connect(self.BYBIT_WS_LINEAR, ping_interval=20, ping_timeout=20) as ws:
            sub_msg = {
                "op": "subscribe",
                "args": [f"orderbook.50.{symbol}", f"publicTrade.{symbol}"],
            }
            await ws.send(json.dumps(sub_msg))
            with self._lock:
                self._connection_status = "LIVE BYBIT PERP (US-SAFE)"

            while not self._stop_event.is_set():
                with self._lock:
                    if self._restart_stream_flag:
                        return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    self._handle_bybit_msg(raw)
                except asyncio.TimeoutError:
                    continue

    def _handle_binance_msg(self, raw_msg: str) -> None:
        try:
            payload = json.loads(raw_msg)
            stream = payload.get("stream", "")
            data = payload.get("data", {})
            now = time.time()

            with self._lock:
                self._msg_count += 1
                self._last_msg_ts = now

                if "@aggTrade" in stream:
                    self.engine.ingest_trade(
                        float(data["T"]) / 1000.0,
                        float(data["p"]),
                        float(data["q"]),
                        bool(data["m"]),
                    )
                elif "@depth20" in stream:
                    raw_bids = data.get("b") or data.get("bids") or []
                    raw_asks = data.get("a") or data.get("asks") or []
                    bids = [(float(p), float(q)) for p, q in raw_bids if float(q) > 0]
                    asks = [(float(p), float(q)) for p, q in raw_asks if float(q) > 0]
                    self._update_engine_book(now, bids, asks)
        except Exception as exc:
            logger.debug(f"Binance parse error: {exc}")

    def _handle_bybit_msg(self, raw_msg: str) -> None:
        try:
            payload = json.loads(raw_msg)
            topic = payload.get("topic", "")
            msg_type = payload.get("type", "")
            data = payload.get("data", {})
            now = time.time()

            with self._lock:
                self._msg_count += 1
                self._last_msg_ts = now

                if topic.startswith("publicTrade."):
                    for trade in data:
                        price = float(trade["p"])
                        qty = float(trade["v"])
                        # Bybit Side == "Sell" means taker sold into bid -> is_buyer_maker = True
                        is_buyer_maker = trade["S"] == "Sell"
                        ts_sec = float(trade["T"]) / 1000.0
                        self.engine.ingest_trade(ts_sec, price, qty, is_buyer_maker)

                elif topic.startswith("orderbook.50."):
                    if msg_type == "snapshot":
                        self._bids_book = {float(p): float(q) for p, q in data.get("b", []) if float(q) > 0}
                        self._asks_book = {float(p): float(q) for p, q in data.get("a", []) if float(q) > 0}
                    elif msg_type == "delta":
                        for p_str, q_str in data.get("b", []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0:
                                self._bids_book.pop(p, None)
                            else:
                                self._bids_book[p] = q
                        for p_str, q_str in data.get("a", []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0:
                                self._asks_book.pop(p, None)
                            else:
                                self._asks_book[p] = q

                    bids = sorted(self._bids_book.items(), key=lambda x: x[0], reverse=True)[:20]
                    asks = sorted(self._asks_book.items(), key=lambda x: x[0])[:20]
                    self._update_engine_book(now, bids, asks)
        except Exception as exc:
            logger.debug(f"Bybit parse error: {exc}")

    def _update_engine_book(self, now: float, bids: List, asks: List) -> None:
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

    def shutdown(self) -> None:
        self._stop_event.set()
