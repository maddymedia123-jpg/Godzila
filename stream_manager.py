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

        self._bids_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._asks_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}

        self.scanner_active: bool = True
        self.account_balance: float = 25000.0
        self.student_balance: float = 100.0
        self.risk_pct: float = 1.0
        self.structural_zones: List[float] = []

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_background_loop, daemon=True)
        self._thread.start()

    def update_config(self, active_symbol: str, scanner_active: bool, account_balance: float, student_balance: float, risk_pct: float, decay_lambda: float, imbalance_threshold: float, max_spread_phi: float, garch_k: float, structural_zones: List[float]):
        sym = active_symbol.upper().strip()
        with self._lock:
            if sym not in self.engines:
                self.engines[sym] = QuantEngine()
            self.active_symbol = sym
            self.scanner_active = scanner_active
            self.student_balance = student_balance
            for eng in self.engines.values():
                eng.update_params(decay_lambda, max_spread_phi, imbalance_threshold, garch_k)

    def get_snapshot(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            sym = (target_symbol or self.active_symbol).upper()
            if sym not in self.engines: sym = "BTCUSDT"
            eng = self.engines[sym]

            coin_tickers = {}
            for s in WATCHLIST_SYMBOLS:
                st_data = self._latest_states.get(s, {})
                coin_tickers[s] = {"mid_price": st_data.get("mid_price", 0.0), "imbalance_t": st_data.get("imbalance_t", 0.0)}

            all_audit = []
            for ce in self.engines.values():
                all_audit.extend(list(ce.audit_logbook))

            return {
                "symbol": sym,
                "status": self._connection_status,
                "metrics": dict(self._latest_states.get(sym, {})),
                "liq_stats": eng.process_orderbook(time.time(), [], [], sym, 0, 0, []).get("liq_stats", {}),
                "candidate_signals": list(eng.candidate_signals),
                "audit_logbook": all_audit[:20],
                "coin_tickers": coin_tickers,
                "msg_count": self._msg_count,
            }

    def _run_background_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._supervisor())

    async def _supervisor(self) -> None:
        async def kline_loop():
            async with aiohttp.ClientSession() as session:
                while not self._stop_event.is_set():
                    for sym in WATCHLIST_SYMBOLS:
                        params = {"symbol": sym, "interval": "15m", "limit": 200}
                        try:
                            async with session.get(self.BINANCE_REST_KLINES, params=params, timeout=5) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    closes = [float(c[4]) for c in data]
                                    with self._lock:
                                        self.engines[sym].fit_garch(closes)
                        except Exception:
                            pass
                        await asyncio.sleep(0.2)
                    await asyncio.sleep(600)

        async def ws_loop():
            while not self._stop_event.is_set():
                try:
                    d_streams = "/".join([f"{s.lower()}@depth20@100ms" for s in WATCHLIST_SYMBOLS])
                    t_streams = "/".join([f"{s.lower()}@aggTrade" for s in WATCHLIST_SYMBOLS])
                    async with websockets.connect(f"{self.BINANCE_WS_PUBLIC}?streams={d_streams}", ping_timeout=20) as ws_d, \
                               websockets.connect(f"{self.BINANCE_WS_MARKET}?streams={t_streams}", ping_timeout=20) as ws_t:
                        with self._lock: self._connection_status = "LIVE BINANCE"
                        async def consume(ws):
                            while not self._stop_event.is_set():
                                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                                p = json.loads(raw)
                                stream, data = p.get("stream", ""), p.get("data", {})
                                sym = stream.split("@")[0].upper()
                                if sym in self.engines:
                                    with self._lock:
                                        self._msg_count += 1
                                        eng = self.engines[sym]
                                        if "@aggTrade" in stream:
                                            eng.ingest_trade(float(data["T"]) / 1000.0, float(data["p"]), float(data["q"]), bool(data["m"]))
                                        elif "@depth20" in stream:
                                            bids = [(float(x), float(y)) for x, y in data.get("b", []) if float(y) > 0]
                                            asks = [(float(x), float(y)) for x, y in data.get("a", []) if float(y) > 0]
                                            metrics = eng.process_orderbook(time.time(), bids, asks, sym, self.account_balance, self.risk_pct, self.structural_zones, self.scanner_active, self.student_balance)
                                            if metrics: self._latest_states[sym] = metrics
                        await asyncio.gather(consume(ws_d), consume(ws_t))
                except Exception:
                    await asyncio.sleep(3.0)

        await asyncio.gather(kline_loop(), ws_loop())

    def shutdown(self): self._stop_event.set()
