import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import aiohttp
import websockets
from quant_engine import QuantEngine

logger = logging.getLogger("StreamManager")
WATCHLIST_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]


class BinanceFuturesStreamManager:
    """
    Background worker maintaining live depth & trades across 5 coins simultaneously,
    updating multi-timeframe auto-S/R structure and GARCH volatility.
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

        self._bids_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._asks_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}

        self.scanner_active: bool = True
        self.account_balance: float = 25000.0
        self.student_balance: float = 100.0  # Default $100 student balance
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
                engine.update_params(decay_lambda, max_spread_phi, imbalance_threshold, garch_k)

    def get_snapshot(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            sym = (target_symbol or self.active_symbol).upper()
            if sym not in self.engines:
                sym = "BTCUSDT"
            eng = self.engines[sym]

            coin_tickers = {}
            for s in WATCHLIST_SYMBOLS:
                st_data = self._latest_states.get(s, {})
                coin_tickers[s] = {
                    "mid_price": st_data.get("mid_price", 0.0),
                    "imbalance_t": st_data.get("imbalance_t", 0.0),
                    "garch_regime": self.engines[s].garch_regime,
                }

            all_audit = []
            for coin_eng in self.engines.values():
                all_audit.extend(list(coin_eng.audit_logbook))
            all_audit.sort(key=lambda x: x["timestamp"], reverse=True)

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
                "locked_signals": dict(eng.locked_timeframe_signals),
                "liq_stats": eng.process_orderbook(time.time(), [], [], sym, 0, 0, []).get("liq_stats", {}),
                "audit_logbook": all_audit[:30],
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
        self, session: aiohttp.ClientSession, symbol: str, binance_int: str, bybit_int: str, limit: int = 300
    ) -> List[Tuple[float, float, float, float]]:
        if self._venue == "BINANCE":
            params = {"symbol": symbol, "interval": binance_int, "limit": limit}
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

        bybit_params = {"category": "linear", "symbol": symbol, "interval": bybit_int, "limit": limit}
        try:
            async with session.get(self.BYBIT_REST_KLINES, params=bybit_params, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.json()
                rows = payload.get("result", {}).get("list", [])
                return [(float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in reversed(rows)]
        except Exception as exc:
            logger.error(f"Kline error ({symbol}): {exc}")
            return []

    async def _multi_tf_calibration_loop(self) -> None:
        async with aiohttp.ClientSession() as session:
            while not self._stop_event.is_set():
                for sym in WATCHLIST_SYMBOLS:
                    k15 = await self._fetch_klines(session, sym, "15m", "15", 300)
                    k1h = await self._fetch_klines(session, sym, "1h", "60", 150)
                    k4h = await self._fetch_klines(session, sym, "4h", "240", 120)

                    with self._lock:
                        eng = self.engines[sym]
                        if k15:
                            eng.fit_garch([c[3] for c in k15])
                            eng.update_timeframe_structure("15m", k15[-100:])
                        if k1h:
                            eng.update_timeframe_structure("1H", k1h)
                        if k4h:
                            eng.update_timeframe_structure("4H", k4h)
                    await asyncio.sleep(0.3)
                await asyncio.sleep(900.0)

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
                if "451" in str(exc) or "403" in str(exc):
                    with self._lock:
                        self._venue = "BYBIT"
                    continue
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 20.0)

    async def _run_binance_multiplex(self) -> None:
        d_streams = "/".join([f"{s.lower()}@depth20@100ms" for s in WATCHLIST_SYMBOLS])
        t_streams = "/".join([f"{s.lower()}@aggTrade" for s in WATCHLIST_SYMBOLS])
        async with websockets.connect(f"{self.BINANCE_WS_PUBLIC}?streams={d_streams}", ping_timeout=20) as ws_d, \
                   websockets.connect(f"{self.BINANCE_WS_MARKET}?streams={t_streams}", ping_timeout=20) as ws_t:
            with self._lock:
                self._connection_status = "LIVE BINANCE (5 COINS · 100ms)"
            async def consume(ws):
                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        self._handle_binance_msg(raw)
                    except asyncio.TimeoutError:
                        continue
            await asyncio.gather(consume(ws_d), consume(ws_t))

    async def _run_bybit_multiplex(self) -> None:
        async with websockets.connect(self.BYBIT_WS_LINEAR, ping_timeout=20) as ws:
            args = [f"orderbook.50.{s}" for s in WATCHLIST_SYMBOLS] + [f"publicTrade.{s}" for s in WATCHLIST_SYMBOLS]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            with self._lock:
                self._connection_status = "LIVE BYBIT PERPS (US-SAFE)"
            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    self._handle_bybit_msg(raw)
                except asyncio.TimeoutError:
                    continue

    def _handle_binance_msg(self, raw: str) -> None:
        try:
            p = json.loads(raw)
            stream, data = p.get("stream", ""), p.get("data", {})
            sym = stream.split("@")[0].upper()
            if sym not in self.engines:
                return
            now = time.time()
            with self._lock:
                self._msg_count += 1
                eng = self.engines[sym]
                if "@aggTrade" in stream:
                    eng.ingest_trade(float(data["T"]) / 1000.0, float(data["p"]), float(data["q"]), bool(data["m"]))
                elif "@depth20" in stream:
                    bids = [(float(x), float(y)) for x, y in data.get("b", []) if float(y) > 0]
                    asks = [(float(x), float(y)) for x, y in data.get("a", []) if float(y) > 0]
                    self._process_book(sym, now, bids, asks)
        except Exception:
            pass

    def _handle_bybit_msg(self, raw: str) -> None:
        try:
            p = json.loads(raw)
            topic, msg_type, data = p.get("topic", ""), p.get("type", ""), p.get("data", {})
            sym = topic.split(".")[-1].upper()
            if sym not in self.engines:
                return
            now = time.time()
            with self._lock:
                self._msg_count += 1
                if topic.startswith("publicTrade."):
                    for t in data:
                        self.engines[sym].ingest_trade(float(t["T"]) / 1000.0, float(t["p"]), float(t["v"]), t["S"] == "Sell")
                elif topic.startswith("orderbook.50."):
                    if msg_type == "snapshot":
                        self._bids_books[sym] = {float(x): float(y) for x, y in data.get("b", []) if float(y) > 0}
                        self._asks_books[sym] = {float(x): float(y) for x, y in data.get("a", []) if float(y) > 0}
                    elif msg_type == "delta":
                        for x, y in data.get("b", []):
                            if float(y) == 0: self._bids_books[sym].pop(float(x), None)
                            else: self._bids_books[sym][float(x)] = float(y)
                        for x, y in data.get("a", []):
                            if float(y) == 0: self._asks_books[sym].pop(float(x), None)
                            else: self._asks_books[sym][float(x)] = float(y)
                    bids = sorted(self._bids_books[sym].items(), key=lambda z: z[0], reverse=True)[:20]
                    asks = sorted(self._asks_books[sym].items(), key=lambda z: z[0])[:20]
                    self._process_book(sym, now, bids, asks)
        except Exception:
            pass

    def _process_book(self, sym: str, now: float, bids: List, asks: List) -> None:
        eng = self.engines[sym]
        metrics = eng.process_orderbook(
            timestamp_sec=now, bids=bids, asks=asks, symbol=sym,
            account_balance=self.account_balance, risk_pct=self.risk_pct,
            structural_zones=self.structural_zones, scanner_active=self.scanner_active,
            student_balance=self.student_balance,
        )
        if metrics:
            self._latest_states[sym] = metrics

    def shutdown(self) -> None:
        self._stop_event.set()
