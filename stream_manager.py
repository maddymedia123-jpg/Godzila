import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
import aiohttp
import websockets
import streamlit as st
from libsql_client import create_client_sync
from quant_engine import QuantEngine

logger = logging.getLogger("StreamManager")
WATCHLIST_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
OKX_MAP = {
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
    "SOLUSDT": "SOL-USDT-SWAP",
    "XRPUSDT": "XRP-USDT-SWAP",
    "DOGEUSDT": "DOGE-USDT-SWAP",
}
REV_OKX_MAP = {v: k for k, v in OKX_MAP.items()}


class BinanceFuturesStreamManager:
    BINANCE_WS_COMBINED = "wss://fstream.binance.com/stream"
    BINANCE_REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"
    BYBIT_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_REST_KLINES = "https://api.bybit.com/v5/market/kline"
    OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
    OKX_REST_BOOKS = "https://www.okx.com/api/v5/market/books"
    OKX_REST_CANDLES = "https://www.okx.com/api/v5/market/candles"

    def __init__(self, active_symbol: str = "BTCUSDT"):
        self.active_symbol = active_symbol.upper()
        self.engines: Dict[str, QuantEngine] = {sym: QuantEngine() for sym in WATCHLIST_SYMBOLS}
        self._lock = threading.Lock()
        self._latest_states: Dict[str, Dict[str, Any]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._connection_status: str = "INITIALIZING"
        self._venue: str = "OKX"
        self._msg_count: int = 0

        self._bids_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._asks_books: Dict[str, Dict[float, float]] = {sym: {} for sym in WATCHLIST_SYMBOLS}

        self.scanner_active: bool = True
        self.account_balance: float = 25000.0
        self.student_balance: float = 100.0
        self.risk_pct: float = 1.0
        self.structural_zones: List[float] = []

        self.db = None
        self._init_turso_db()

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_background_loop, daemon=True)
        self._thread.start()

    def _init_turso_db(self):
        try:
            turso_url = st.secrets.get("turso", {}).get("database_url", "libsql://godzila-db-maddymedia123.turso.io")
            turso_token = st.secrets.get("turso", {}).get("auth_token", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTMxMTc0NjYsImlkIjoiMDFhMTdlMWYtMWQwMS03MmNhLTg4MjItNzk1MjY2NDExMTgwIiwia2lkIjoia3l0RU9uVVlMSXBwS3ZQMGVzWEFDR3lYRHM3blFhbmhCQkJiOHdqNDRMayIsInJpZCI6IjRhM2ZhZDUyLTEwMGYtNGE4My1hNTIyLTZkYmViM2E1N2I2NyJ9.eXwFwdN73oP3yarkOIo_ZjEQyR2nTCjUZoEEa3LPYfV48c07sLD4d2ocTstAqAwBiltyAHsVe6osnzbs0pFJAQ")

            self.db = create_client_sync(url=turso_url, auth_token=turso_token)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS global_trading_memory (
                    key TEXT PRIMARY KEY,
                    payload TEXT,
                    updated_at REAL
                );
            """)
        except Exception as exc:
            logger.error(f"Turso DB initialization failed: {exc}")

    def save_state_to_turso(self, key: str, data: dict):
        if not self.db:
            return
        try:
            payload = json.dumps(data)
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (key, payload, time.time())
            )
        except Exception as exc:
            logger.error(f"Failed to save to Turso: {exc}")

    def load_state_from_turso(self, key: str) -> Optional[dict]:
        if not self.db:
            return None
        try:
            res = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (key,))
            rows = res.rows
            if rows:
                return json.loads(rows[0][0])
        except Exception as exc:
            logger.error(f"Failed to load from Turso: {exc}")
        return None

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
    ):
        sym = active_symbol.upper().strip()
        with self._lock:
            if sym not in self.engines:
                self.engines[sym] = QuantEngine()
            self.active_symbol = sym
            self.scanner_active = scanner_active
            self.account_balance = account_balance
            self.student_balance = student_balance
            self.risk_pct = risk_pct
            self.structural_zones = structural_zones
            for eng in self.engines.values():
                eng.update_params(decay_lambda, max_spread_phi, imbalance_threshold, garch_k)

    def get_snapshot(self, target_symbol: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            sym = (target_symbol or self.active_symbol).upper()
            if sym not in self.engines:
                sym = "BTCUSDT"
            eng = self.engines[sym]
            latest = dict(self._latest_states.get(sym, {}))

            coin_tickers = {}
            for s in WATCHLIST_SYMBOLS:
                st_data = self._latest_states.get(s, {})
                coin_tickers[s] = {
                    "price": st_data.get("mid_price", 0.0),
                    "mid_price": st_data.get("mid_price", 0.0),
                    "imbalance_t": st_data.get("imbalance_t", 0.0),
                }

            all_audit = []
            for ce in self.engines.values():
                all_audit.extend(list(getattr(ce, "audit_logbook", [])))

            return {
                "symbol": sym,
                "status": self._connection_status,
                "metrics": latest,
                "liq_stats": latest.get("liq_stats", {}),
                "candidate_signals": list(getattr(eng, "candidate_signals", [])),
                "audit_logbook": all_audit[:20],
                "coin_tickers": coin_tickers,
                "msg_count": self._msg_count,
            }

    def _run_background_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._supervisor())

    async def _supervisor(self) -> None:
        async def seed_and_kline_loop():
            async with aiohttp.ClientSession() as session:
                while not self._stop_event.is_set():
                    for sym in WATCHLIST_SYMBOLS:
                        inst_id = OKX_MAP[sym]
                        # 1. Immediate REST Orderbook Seed via OKX (unblocked globally)
                        try:
                            async with session.get(
                                self.OKX_REST_BOOKS,
                                params={"instId": inst_id, "sz": "20"},
                                timeout=5,
                            ) as resp:
                                if resp.status == 200:
                                    payload = await resp.json()
                                    book = payload.get("data", [{}])[0]
                                    bids = [(float(x[0]), float(x[1])) for x in book.get("bids", []) if float(x[1]) > 0]
                                    asks = [(float(x[0]), float(x[1])) for x in book.get("asks", []) if float(x[1]) > 0]
                                    if bids and asks:
                                        with self._lock:
                                            self._msg_count += 1
                                            eng = self.engines[sym]
                                            metrics = eng.process_orderbook(
                                                time.time(),
                                                bids,
                                                asks,
                                                sym,
                                                self.account_balance,
                                                self.risk_pct,
                                                self.structural_zones,
                                                self.scanner_active,
                                                self.student_balance,
                                            )
                                            if metrics:
                                                self._latest_states[sym] = metrics
                        except Exception:
                            pass

                        # 2. GARCH Volatility Calibration via OKX Candles
                        try:
                            async with session.get(
                                self.OKX_REST_CANDLES,
                                params={"instId": inst_id, "bar": "15m", "limit": "200"},
                                timeout=5,
                            ) as resp:
                                if resp.status == 200:
                                    payload = await resp.json()
                                    rows = payload.get("data", [])
                                    closes = [float(r[4]) for r in reversed(rows)]
                                    with self._lock:
                                        self.engines[sym].fit_garch(closes)
                        except Exception:
                            pass
                        await asyncio.sleep(0.15)
                    await asyncio.sleep(300)

        async def run_okx_ws() -> bool:
            async with websockets.connect(self.OKX_WS_PUBLIC, ping_interval=15, ping_timeout=10, open_timeout=8) as ws:
                args = []
                for inst_id in OKX_MAP.values():
                    args.append({"channel": "books5", "instId": inst_id})
                    args.append({"channel": "trades", "instId": inst_id})
                await ws.send(json.dumps({"op": "subscribe", "args": args}))

                with self._lock:
                    self._connection_status = "LIVE OKX PERP"
                    self._venue = "OKX"

                while not self._stop_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    if raw == "pong":
                        continue
                    p = json.loads(raw)
                    arg = p.get("arg", {})
                    channel = arg.get("channel", "")
                    inst_id = arg.get("instId", "")
                    sym = REV_OKX_MAP.get(inst_id)
                    if not sym or "data" not in p:
                        continue

                    with self._lock:
                        self._msg_count += 1
                        eng = self.engines[sym]
                        if channel == "trades":
                            for tr in p["data"]:
                                eng.ingest_trade(
                                    float(tr["ts"]) / 1000.0,
                                    float(tr["px"]),
                                    float(tr["sz"]),
                                    tr.get("side") == "sell",
                                )
                        elif channel == "books5":
                            book = p["data"][0]
                            bids = [(float(x[0]), float(x[1])) for x in book.get("bids", []) if float(x[1]) > 0]
                            asks = [(float(x[0]), float(x[1])) for x in book.get("asks", []) if float(x[1]) > 0]
                            if bids and asks:
                                metrics = eng.process_orderbook(
                                    time.time(),
                                    bids,
                                    asks,
                                    sym,
                                    self.account_balance,
                                    self.risk_pct,
                                    self.structural_zones,
                                    self.scanner_active,
                                    self.student_balance,
                                )
                                if metrics:
                                    self._latest_states[sym] = metrics
            return True

        async def ws_loop():
            while not self._stop_event.is_set():
                try:
                    await run_okx_ws()
                except Exception as exc:
                    with self._lock:
                        self._connection_status = "RECONNECTING"
                    logger.error(f"OKX WS error ({exc}), retrying in 2s...")
                    await asyncio.sleep(2.0)

        await asyncio.gather(seed_and_kline_loop(), ws_loop())

    def shutdown(self):
        self._stop_event.set()
