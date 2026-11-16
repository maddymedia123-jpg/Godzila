import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
import websockets
from quant_engine import QuantEngine, StudentAutoSignal

logger = logging.getLogger("StreamManager")

WATCHLIST_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]


class BinanceFuturesStreamManager:
    """
    Multi-coin background asyncio worker streaming real-time L2 depth & trades across
    5 major perpetual futures contracts simultaneously, computing 15m/1H/4H auto-S/R
    levels, Student-t GARCH(1,1) volatility, and automated student & pro signals.
    """

    BINANCE_WS_PUBLIC = "wss://fstream.binance.com/public/stream"
    BINANCE_WS_MARKET = "wss://fstream.binance.com/market/stream"
    BINANCE_REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"

    BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_REST_KLINES = "https://api.bybit.com/v5/market/kline"

    def __init__(self, active_symbol: str = "BTCUSDT"):
        self.active_symbol = active_symbol.upper()
        self.engines: Dict[str, QuantEngine] = {sym: QuantEngine() for sym in WATCHLIST_SYMBOLS}

        self._lock = threading.Lock()
        self._latest_states: Dict[str, Dict[str, Any]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._connection_status: str = "INITIALIZING"
        self._venue: str = "BINANCE"
        self._msg_count: int = 0
        self._last_msg_ts: float = 0.0

        # Local order books per coin for delta feeds
        self._bids_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._asks_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}

        # Runtime UI parameters
        self.scanner_active: bool = True
        self.account_balance: float = 25000.0
        self.student_balance: float = 1000.0
        self.risk_pct: float = 1.0
        self.structural_zones: List[float] = []

        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = threading.Thread(target=self._run_background_loop, daemon=True)
        self._thread.start()

    def update_config(
        self,
        active_symbol: str,
        scanner_active: bool,
        account_balance: float,
        student_balance: float,
        risk_pct: float,
        decay_lambda: float,
        imbalance_threshold: float,
        max_spread_phi: float,
        garch_k: float,
        structural_zones: List[float],
    ) -> None:
        sym = active_symbol.upper().strip()
        with self._lock:
            if sym not in self.engines:
                self.engines[sym] = QuantEngine()
                self._latest_states[sym] = {}
                self._bids_books[sym] = {}
                self._asks_books[sym] = {}

            self.active_symbol = sym
            self.scanner_active = scanner_active
            self.account_balance = account_balance
            self.student_balance = student_balance
            self.risk_pct = risk_pct
            self.structural_zones = structural_zones

            for engine in self.engines.values():
                engine.update_params(
                    decay_lambda=decay_lambda,
                    max_spread_phi=max_spread_phi,
                    imbalance_threshold=imbalance_threshold,
                    garch_k=garch_k,
                )

    def get_snapshot(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        """Thread-safe state snapshot for both Pro and Student tabs."""
        with self._lock:
            sym = (target_symbol or self.active_symbol).upper()
            if sym not in self.engines:
                sym = "BTCUSDT"
            eng = self.engines[sym]

            # Aggregate recent student signals across all 5 scanned coins
            all_student_signals: List[StudentAutoSignal] = []
            for coin_eng in self.engines.values():
                all_student_signals.extend(list(coin_eng.student_signal_feed))
            all_student_signals.sort(key=lambda s: s.timestamp, reverse=True)

            # Collect live mid-prices for quick multi-coin ticker strip
            coin_tickers = {}
            for s in WATCHLIST_SYMBOLS:
                st_data = self._latest_states.get(s, {})
                coin_tickers[s] = {
                    "mid_price": st_data.get("mid_price", 0.0),
                    "imbalance_t": st_data.get("imbalance_t", 0.0),
                    "garch_regime": self.engines[s].garch_regime,
                }

            return {
                "symbol": sym,
                "status": self._connection_status,
                "scanner_active": self.scanner_active,
                "msg_count": self._msg_count,
                "last_msg_ts": self._last_msg_ts,
                "metrics": dict(self._latest_states.get(sym, {})),
                "imbalance_history": list(eng.imbalance_history),
                "events_log": list(eng.events_log),
                "latest_signal": eng.latest_signal,
                "garch_sigma": eng.garch_sigma,
                "garch_regime": eng.garch_regime,
                "student_profiles": dict(eng.latest_student_profiles),
                "student_signal_feed": all_student_signals[:25],
                "coin_tickers": coin_tickers,
            }

    def _run_background_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._supervisor())

    async def _supervisor(self) -> None:
        garch_task = asyncio.create_task(self._multi_tf_calibration_loop())
        ws_task = asyncio.create_task(self._ws_stream_loop())
        await asyncio.gather(garch_task, ws_task)

    async def _fetch_klines(
        self, session: aiohttp.ClientSession, symbol: str, interval_binance: str, interval_bybit: str, limit: int = 300
    ) -> List[Tuple[float, float, float, float]]:
        """Returns list of (open, high, low, close) tuples chronologically."""
        if self._venue == "BINANCE":
            params = {"symbol": symbol, "interval": interval_binance, "limit": limit}
            try:
                async with session.get(self.BINANCE_REST_KLINES, params=params, timeout=8) as resp:
                    if resp.status == 451:
                        with self._lock:
                            self._venue = "BYBIT"
                    elif resp.status == 200:
                        data = await resp.json()
                        return [(float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in data]
            except Exception:
                with self._lock:
                    self._venue = "BYBIT"

        bybit_params = {"category": "linear", "symbol": symbol, "interval": interval_bybit, "limit": limit}
        try:
            async with session.get(self.BYBIT_REST_KLINES, params=bybit_params, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.json()
                rows = payload.get("result", {}).get("list", [])
                return [(float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in reversed(rows)]
        except Exception as exc:
            logger.error(f"Kline fetch error ({symbol} {interval_binance}): {exc}")
            return []

    async def _multi_tf_calibration_loop(self) -> None:
        """
        Calibrates GARCH(1,1)-t volatility and 15m/1H/4H auto-structural S/R levels
        across all 5 watchlist coins on startup and every 15 minutes.
        """
        last_refresh = 0.0
        async with aiohttp.ClientSession() as session:
            while not self._stop_event.is_set():
                now = time.time()
                if (now - last_refresh) >= 900.0:
                    for sym in WATCHLIST_SYMBOLS:
                        ohlcv_15m = await self._fetch_klines(session, sym, "15m", "15", limit=400)
                        ohlcv_1h = await self._fetch_klines(session, sym, "1h", "60", limit=150)
                        ohlcv_4h = await self._fetch_klines(session, sym, "4h", "240", limit=120)

                        with self._lock:
                            eng = self.engines[sym]
                            if ohlcv_15m:
                                eng.fit_garch([c[3] for c in ohlcv_15m])
                                eng.update_timeframe_structure("15m", ohlcv_15m[-100:])
                            if ohlcv_1h:
                                eng.update_timeframe_structure("1H", ohlcv_1h)
                            if ohlcv_4h:
                                eng.update_timeframe_structure("4H", ohlcv_4h)

                        await asyncio.sleep(0.25)
                    last_refresh = time.time()

                await asyncio.sleep(3.0)

    async def _ws_stream_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            with self._lock:
                venue = self._venue
                self._connection_status = f"CONNECTING ({venue})"

            try:
                if venue == "BINANCE":
                    await self._run_binance_multiplex()
                else:
                    await self._run_bybit_multiplex()
                backoff = 1.0
            except Exception as exc:
                err_str = str(exc)
                if "451" in err_str or "403" in err_str:
                    with self._lock:
                        self._venue = "BYBIT"
                        self._connection_status = "FAILOVER -> BYBIT"
                    await asyncio.sleep(0.5)
                    continue

                with self._lock:
                    self._connection_status = f"RECONNECTING ({backoff:.0f}s)"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 20.0)

    async def _run_binance_multiplex(self) -> None:
        depth_streams = "/".join([f"{s.lower()}@depth20@100ms" for s in WATCHLIST_SYMBOLS])
        trade_streams = "/".join([f"{s.lower()}@aggTrade" for s in WATCHLIST_SYMBOLS])

        depth_url = f"{self.BINANCE_WS_PUBLIC}?streams={depth_streams}"
        trade_url = f"{self.BINANCE_WS_MARKET}?streams={trade_streams}"

        async with websockets.connect(depth_url, ping_interval=20, ping_timeout=20) as ws_depth, \
                   websockets.connect(trade_url, ping_interval=20, ping_timeout=20) as ws_trade:
            with self._lock:
                self._connection_status = "LIVE BINANCE (5 COINS · 100ms)"

            async def consume(ws):
                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        self._handle_binance_msg(raw)
                    except asyncio.TimeoutError:
                        continue

            await asyncio.gather(consume(ws_depth), consume(ws_trade))

    async def _run_bybit_multiplex(self) -> None:
        async with websockets.connect(self.BYBIT_WS_LINEAR, ping_interval=20, ping_timeout=20) as ws:
            args = []
            for sym in WATCHLIST_SYMBOLS:
                args.append(f"orderbook.50.{sym}")
                args.append(f"publicTrade.{sym}")

            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            with self._lock:
                self._connection_status = "LIVE BYBIT PERPS (5 COINS · US-SAFE)"

            while not self._stop_event.is_set():
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
            if not stream:
                return

            sym = stream.split("@")[0].upper()
            if sym not in self.engines:
                return

            now = time.time()
            with self._lock:
                self._msg_count += 1
                self._last_msg_ts = now
                eng = self.engines[sym]

                if "@aggTrade" in stream:
                    eng.ingest_trade(
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
                    self._process_book_locked(sym, now, bids, asks)
        except Exception:
            pass

    def _handle_bybit_msg(self, raw_msg: str) -> None:
        try:
            payload = json.loads(raw_msg)
            topic = payload.get("topic", "")
            msg_type = payload.get("type", "")
            data = payload.get("data", {})
            if not topic:
                return

            sym = topic.split(".")[-1].upper()
            if sym not in self.engines:
                return

            now = time.time()
            with self._lock:
                self._msg_count += 1
                self._last_msg_ts = now
                eng = self.engines[sym]

                if topic.startswith("publicTrade."):
                    for trade in data:
                        eng.ingest_trade(
                            float(trade["T"]) / 1000.0,
                            float(trade["p"]),
                            float(trade["v"]),
                            trade["S"] == "Sell",
                        )
                elif topic.startswith("orderbook.50."):
                    if msg_type == "snapshot":
                        self._bids_books[sym] = {float(p): float(q) for p, q in data.get("b", []) if float(q) > 0}
                        self._asks_books[sym] = {float(p): float(q) for p, q in data.get("a", []) if float(q) > 0}
                    elif msg_type == "delta":
                        for p_str, q_str in data.get("b", []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0:
                                self._bids_books[sym].pop(p, None)
                            else:
                                self._bids_books[sym][p] = q
                        for p_str, q_str in data.get("a", []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0:
                                self._asks_books[sym].pop(p, None)
                            else:
                                self._asks_books[sym][p] = q

                    bids = sorted(self._bids_books[sym].items(), key=lambda x: x[0], reverse=True)[:20]
                    asks = sorted(self._asks_books[sym].items(), key=lambda x: x[0])[:20]
                    self._process_book_locked(sym, now, bids, asks)
        except Exception:
            pass

    def _process_book_locked(self, sym: str, now: float, bids: List, asks: List) -> None:
        eng = self.engines[sym]
        zones = self.structural_zones if sym == self.active_symbol else []
        metrics = eng.process_orderbook(
            timestamp_sec=now,
            bids=bids,
            asks=asks,
            symbol=sym,
            account_balance=self.account_balance,
            risk_pct=self.risk_pct,
            structural_zones=zones,
            scanner_active=self.scanner_active,
            student_balance=self.student_balance,
        )
        if metrics:
            self._latest_states[sym] = metrics

    def shutdown(self) -> None:
        self._stop_event.set()
