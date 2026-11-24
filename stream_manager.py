import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import streamlit as st
import websockets
from libsql_client import create_client_sync

from quant_engine import CommittedSignal, QuantEngine

logger = logging.getLogger("StreamManager")

WATCHLIST_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "TAOUSDT",
    "AAVEUSDT",
]
OKX_MAP = {
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
    "SOLUSDT": "SOL-USDT-SWAP",
    "XRPUSDT": "XRP-USDT-SWAP",
    "DOGEUSDT": "DOGE-USDT-SWAP",
    "TAOUSDT": "TAO-USDT-SWAP",
    "AAVEUSDT": "AAVE-USDT-SWAP",
}
REV_OKX_MAP = {v: k for k, v in OKX_MAP.items()}

PORTFOLIO_DB_KEY = "godzilla_active_portfolio_permanent"
AUDIT_DB_KEY = "godzilla_audit_archive_permanent"
BACKTEST_DB_KEY = "godzilla_backtest_snapshots_permanent"
AUDIT_NOTES_DB_KEY = "godzilla_audit_reviews_permanent"
DINO_MEMORY_DB_KEY = "godzilla_dino_memory_permanent"
LEGACY_AUDIT_KEYS = ["godzilla_audit_archive_v10", "godzilla_audit_archive_v9"]
LEGACY_PORTFOLIO_KEYS = ["godzilla_active_portfolio_v10", "godzilla_active_portfolio_v9"]
LOCAL_BACKUP_FILE = "godzilla_audit_backup.json"
MAX_ACTIVE_TRADES = 10


class BinanceFuturesStreamManager:
    OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
    OKX_REST_BOOKS = "https://www.okx.com/api/v5/market/books"
    OKX_REST_CANDLES = "https://www.okx.com/api/v5/market/candles"

    def __init__(self, active_symbol: str = "BTCUSDT"):
        self.active_symbol = active_symbol.upper()
        self.engines: Dict[str, QuantEngine] = {sym: QuantEngine() for sym in WATCHLIST_SYMBOLS}
        self._lock = threading.Lock()
        self._latest_states: Dict[str, Dict[str, Any]] = {sym: {} for sym in WATCHLIST_SYMBOLS}
        self._connection_status: str = "INITIALIZING"
        self._msg_count: int = 0

        self.scanner_active: bool = True
        self.account_balance: float = 25000.0
        self.student_balance: float = 100.0
        self.risk_pct: float = 1.0
        self.structural_zones: List[float] = []

        self.db = None
        self.db_status: str = "LOCAL FILE ONLY"
        self.active_trades: List[Dict[str, Any]] = []
        self.audit_archive: List[Dict[str, Any]] = []
        self.backtest_snapshots: List[Dict[str, Any]] = []
        self.audit_reviews: List[Dict[str, Any]] = []
        self.dino_memory: List[Dict[str, Any]] = []
        self._init_turso_db()
        self._load_portfolio_from_turso()

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_background_loop, daemon=True)
        self._thread.start()

    def _init_turso_db(self):
        try:
            turso_sec = st.secrets.get("turso", {})
            raw_url = turso_sec.get("database_url", "")
            turso_token = turso_sec.get("auth_token", "")
            if not raw_url or not turso_token:
                self.db_status = "LOCAL FILE ONLY (NO TURSO SECRETS)"
                return

            turso_url = raw_url.replace("libsql://", "https://").replace("wss://", "https://")
            self.db = create_client_sync(url=turso_url, auth_token=turso_token)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS global_trading_memory (key TEXT PRIMARY KEY, payload TEXT, updated_at REAL);"
            )
            self.db_status = "TURSO + LOCAL BACKUP ACTIVE"
        except Exception as exc:
            logger.error("Turso DB disabled: %s", exc)
            self.db = None
            self.db_status = "LOCAL FILE ONLY (TURSO INIT ERROR)"

    def _save_local_backup(self):
        try:
            data = {
                "active_trades": self.active_trades,
                "audit_archive": self.audit_archive[:500],
                "backtest_snapshots": self.backtest_snapshots[:100],
                "audit_reviews": self.audit_reviews[:200],
                "dino_memory": self.dino_memory[:150],
                "saved_at": time.time(),
            }
            with open(LOCAL_BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as exc:
            logger.error("Local backup error: %s", exc)

    def _save_portfolio_to_turso(self):
        self._save_local_backup()
        if not self.db:
            return
        try:
            now_ts = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (PORTFOLIO_DB_KEY, json.dumps(self.active_trades), now_ts),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (AUDIT_DB_KEY, json.dumps(self.audit_archive[:500]), now_ts),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (BACKTEST_DB_KEY, json.dumps(self.backtest_snapshots[:100]), now_ts),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (AUDIT_NOTES_DB_KEY, json.dumps(self.audit_reviews[:200]), now_ts),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO global_trading_memory (key, payload, updated_at) VALUES (?, ?, ?);",
                (DINO_MEMORY_DB_KEY, json.dumps(self.dino_memory[:150]), now_ts),
            )
            self.db_status = "TURSO + LOCAL BACKUP ACTIVE"
        except Exception as exc:
            logger.error("Failed to persist portfolio to Turso: %s", exc)
            self.db_status = "LOCAL FILE ONLY (TURSO WRITE ERROR)"

    def _load_portfolio_from_turso(self):
        merged_trades: List[Dict[str, Any]] = []
        merged_audit: List[Dict[str, Any]] = []
        loaded_backtests: List[Dict[str, Any]] = []
        loaded_reviews: List[Dict[str, Any]] = []
        loaded_dino: List[Dict[str, Any]] = []
        seen_trade_ids = set()
        seen_audit_ids = set()

        def add_trade(item: Dict[str, Any]):
            tid = item.get("trade_id") or f"{item.get('symbol')}_{item.get('committed_at')}"
            if tid not in seen_trade_ids:
                seen_trade_ids.add(tid)
                merged_trades.append(item)

        def add_audit(item: Dict[str, Any]):
            tid = item.get("trade_id") or f"{item.get('symbol')}_{item.get('committed_at')}"
            if tid not in seen_audit_ids:
                seen_audit_ids.add(tid)
                merged_audit.append(item)

        if self.db:
            try:
                for k in [PORTFOLIO_DB_KEY] + LEGACY_PORTFOLIO_KEYS:
                    res = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (k,))
                    if res.rows and res.rows[0][0]:
                        loaded = json.loads(res.rows[0][0])
                        if isinstance(loaded, list):
                            for row in loaded:
                                add_trade(row)

                for k in [AUDIT_DB_KEY] + LEGACY_AUDIT_KEYS:
                    res_audit = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (k,))
                    if res_audit.rows and res_audit.rows[0][0]:
                        loaded_audit = json.loads(res_audit.rows[0][0])
                        if isinstance(loaded_audit, list):
                            for row in loaded_audit:
                                add_audit(row)

                res_bt = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (BACKTEST_DB_KEY,))
                if res_bt.rows and res_bt.rows[0][0]:
                    data_bt = json.loads(res_bt.rows[0][0])
                    if isinstance(data_bt, list):
                        loaded_backtests = data_bt

                res_rev = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (AUDIT_NOTES_DB_KEY,))
                if res_rev.rows and res_rev.rows[0][0]:
                    data_rev = json.loads(res_rev.rows[0][0])
                    if isinstance(data_rev, list):
                        loaded_reviews = data_rev

                res_dino = self.db.execute("SELECT payload FROM global_trading_memory WHERE key = ?;", (DINO_MEMORY_DB_KEY,))
                if res_dino.rows and res_dino.rows[0][0]:
                    data_dino = json.loads(res_dino.rows[0][0])
                    if isinstance(data_dino, list):
                        loaded_dino = data_dino
            except Exception as exc:
                logger.error("Failed to load portfolio from Turso: %s", exc)

        if os.path.exists(LOCAL_BACKUP_FILE):
            try:
                with open(LOCAL_BACKUP_FILE, "r", encoding="utf-8") as f:
                    bdata = json.load(f)
                for row in bdata.get("active_trades", []):
                    add_trade(row)
                for row in bdata.get("audit_archive", []):
                    add_audit(row)
                if not loaded_backtests:
                    loaded_backtests = bdata.get("backtest_snapshots", [])
                if not loaded_reviews:
                    loaded_reviews = bdata.get("audit_reviews", [])
                if not loaded_dino:
                    loaded_dino = bdata.get("dino_memory", [])
            except Exception as exc:
                logger.error("Failed to load local backup: %s", exc)

        self.active_trades = merged_trades
        self.audit_archive = merged_audit
        self.backtest_snapshots = loaded_backtests
        self.audit_reviews = loaded_reviews
        self.dino_memory = loaded_dino

    def save_dino_entry(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.dino_memory.insert(0, entry)
            self._save_portfolio_to_turso()

    def save_backtest_snapshot(self, title: str, content: str) -> None:
        with self._lock:
            self.backtest_snapshots.insert(
                0,
                {
                    "id": f"bt_{int(time.time() * 1000)}",
                    "title": title.strip() or "Perp Backtest Snapshot",
                    "content": content.strip(),
                    "saved_at": time.time(),
                },
            )
            self._save_portfolio_to_turso()

    def save_audit_review(self, tag: str, missed_accuracy_note: str, audit_payload: str) -> None:
        with self._lock:
            self.audit_reviews.insert(
                0,
                {
                    "id": f"rev_{int(time.time() * 1000)}",
                    "tag": tag,
                    "note": missed_accuracy_note.strip(),
                    "payload": audit_payload.strip(),
                    "saved_at": time.time(),
                },
            )
            self._save_portfolio_to_turso()

    def tag_audit_record(self, trade_id: str, audit_tag: str, audit_note: str) -> None:
        with self._lock:
            for arch in self.audit_archive:
                if arch.get("trade_id") == trade_id:
                    arch["audit_tag"] = audit_tag
                    arch["audit_note"] = audit_note.strip()
                    break
            self._save_portfolio_to_turso()

    @staticmethod
    def _signal_to_record(sig: CommittedSignal, source_mode: str = "BOT_SIGNAL") -> Dict[str, Any]:
        return {
            "trade_id": sig.signal_id,
            "symbol": sig.symbol,
            "direction": sig.direction,
            "timeframe": sig.timeframe,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "tp1": sig.tp1,
            "tp2": sig.tp2,
            "tp3": sig.tp3,
            "leverage": sig.leverage,
            "margin_usd": sig.margin_required_usd,
            "eta_tp1": sig.eta_tp1_str,
            "eta_tp2": sig.eta_tp2_str,
            "eta_tp3": sig.eta_tp3_str,
            "alignment_status": sig.alignment_status,
            "accuracy_score": sig.accuracy_score,
            "squeeze_alert": sig.squeeze_alert,
            "audit_tag": "PENDING_REVIEW",
            "audit_note": "",
            "source_mode": source_mode,
            "committed_at": sig.timestamp,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp3_hit": False,
            "sl_hit": False,
            "status_note": "ACTIVE (UNCOMMITTED SIGNAL)" if source_mode == "BOT_SIGNAL" else "COMMITTED ACTIVE",
        }

    def _auto_log_emitted_signals(self, eng: QuantEngine) -> None:
        new_sigs = eng.drain_emitted_signals()
        if not new_sigs:
            return
        added = False
        existing_ids = {item.get("trade_id") for item in self.audit_archive}
        for sig in new_sigs:
            if sig.signal_id not in existing_ids:
                rec = self._signal_to_record(sig, source_mode="BOT_SIGNAL")
                self.audit_archive.insert(0, rec)
                existing_ids.add(sig.signal_id)
                added = True
        if added:
            self._save_portfolio_to_turso()

    def commit_trade(self, trade_payload: Dict[str, Any]) -> Tuple[bool, str]:
        with self._lock:
            t_id = trade_payload.get("trade_id")
            if len(self.active_trades) >= MAX_ACTIVE_TRADES:
                return False, f"Maximum {MAX_ACTIVE_TRADES} active trades reached. Kill an existing trade first."

            for existing in self.active_trades:
                if existing.get("trade_id") == t_id or (
                    existing.get("symbol") == trade_payload.get("symbol")
                    and existing.get("timeframe") == trade_payload.get("timeframe")
                    and existing.get("direction") == trade_payload.get("direction")
                    and not existing.get("sl_hit")
                    and not existing.get("tp3_hit")
                ):
                    return False, f"Active {trade_payload.get('timeframe')} {trade_payload.get('direction')} trade already locked for {trade_payload.get('symbol')}."

            trade_payload.setdefault(
                "trade_id",
                f"{trade_payload['symbol']}_{trade_payload.get('timeframe', '1H')}_{int(time.time() * 1000)}",
            )
            trade_payload.setdefault("tp1_hit", False)
            trade_payload.setdefault("tp2_hit", False)
            trade_payload.setdefault("tp3_hit", False)
            trade_payload.setdefault("sl_hit", False)
            trade_payload["source_mode"] = "COMMITTED"
            trade_payload["status_note"] = "COMMITTED ACTIVE"

            self.active_trades.append(trade_payload)

            updated_audit = False
            for idx, arch in enumerate(self.audit_archive):
                if arch.get("trade_id") == trade_payload["trade_id"]:
                    merged = dict(arch)
                    merged.update(trade_payload)
                    self.audit_archive[idx] = merged
                    updated_audit = True
                    break
            if not updated_audit:
                self.audit_archive.insert(0, dict(trade_payload))

            self._save_portfolio_to_turso()
            return True, f"Committed [{trade_payload.get('timeframe')}] {trade_payload['symbol']} {trade_payload['direction']} to Active Trades & Audit Logbook."

    def kill_trade(self, trade_id: str) -> None:
        with self._lock:
            self.active_trades = [t for t in self.active_trades if t.get("trade_id") != trade_id]
            for arch in self.audit_archive:
                if arch.get("trade_id") == trade_id and not arch.get("sl_hit") and not arch.get("tp3_hit"):
                    arch["status_note"] = "KILLED / CLOSED BY USER"
            self._save_portfolio_to_turso()

    @staticmethod
    def _step_record_against_price(
        rec: Dict[str, Any], mid_price: float, squeeze_state: str = "NORMAL", squeeze_msg: str = ""
    ) -> bool:
        if mid_price <= 0 or rec.get("sl_hit"):
            return False

        direction = rec.get("direction", "LONG")
        sl = float(rec.get("stop_loss", 0.0))
        tp1 = float(rec.get("tp1", 0.0))
        tp2 = float(rec.get("tp2", 0.0))
        tp3 = float(rec.get("tp3", 0.0))
        changed = False

        if direction == "SHORT" and squeeze_state == "SHORT_SQUEEZE_RISK":
            if rec.get("squeeze_alert") != squeeze_msg:
                rec["squeeze_alert"] = squeeze_msg
                changed = True
        elif direction == "LONG" and squeeze_state == "LONG_SQUEEZE_RISK":
            if rec.get("squeeze_alert") != squeeze_msg:
                rec["squeeze_alert"] = squeeze_msg
                changed = True
        else:
            if rec.get("squeeze_alert"):
                rec["squeeze_alert"] = ""
                changed = True

        if (direction == "LONG" and mid_price <= sl) or (direction == "SHORT" and mid_price >= sl):
            rec["sl_hit"] = True
            rec["status_note"] = "STOP-LOSS HIT"
            rec["closed_at"] = time.time()
            return True

        if direction == "LONG":
            if not rec.get("tp1_hit") and mid_price >= tp1:
                rec["tp1_hit"] = True
                rec["status_note"] = "TP1 HIT"
                changed = True
            if not rec.get("tp2_hit") and mid_price >= tp2:
                rec["tp2_hit"] = True
                rec["status_note"] = "TP2 HIT"
                changed = True
            if not rec.get("tp3_hit") and mid_price >= tp3:
                rec["tp3_hit"] = True
                rec["status_note"] = "ALL TPs HIT"
                rec["closed_at"] = time.time()
                changed = True
        else:
            if not rec.get("tp1_hit") and mid_price <= tp1:
                rec["tp1_hit"] = True
                rec["status_note"] = "TP1 HIT"
                changed = True
            if not rec.get("tp2_hit") and mid_price <= tp2:
                rec["tp2_hit"] = True
                rec["status_note"] = "TP2 HIT"
                changed = True
            if not rec.get("tp3_hit") and mid_price <= tp3:
                rec["tp3_hit"] = True
                rec["status_note"] = "ALL TPs HIT"
                rec["closed_at"] = time.time()
                changed = True

        return changed

    def _update_active_trades_telemetry(
        self, symbol: str, mid_price: float, squeeze_state: str = "NORMAL", squeeze_msg: str = ""
    ) -> None:
        if mid_price <= 0:
            return
        changed = False

        for t in self.active_trades:
            if t.get("symbol") == symbol:
                if self._step_record_against_price(t, mid_price, squeeze_state, squeeze_msg):
                    changed = True

        for arch in self.audit_archive:
            if arch.get("symbol") == symbol:
                if self._step_record_against_price(arch, mid_price, squeeze_state, squeeze_msg):
                    changed = True

        if changed:
            self._save_portfolio_to_turso()

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

            return {
                "symbol": sym,
                "status": self._connection_status,
                "db_status": self.db_status,
                "metrics": latest,
                "liq_stats": latest.get("liq_stats", {}),
                "prioritized_signals": latest.get("prioritized_signals", []),
                "signals_1h": list(eng.signals_by_tf.get("1H", [])),
                "signals_4h": list(eng.signals_by_tf.get("4H", [])),
                "status_1h": eng.status_by_tf.get("1H", "CALIBRATING..."),
                "status_4h": eng.status_by_tf.get("4H", "CALIBRATING..."),
                "tf_bias": dict(eng.tf_bias),
                "tf_rsi": dict(eng.tf_rsi),
                "active_trades": [dict(t) for t in self.active_trades],
                "audit_archive": [dict(a) for a in self.audit_archive],
                "backtest_snapshots": [dict(b) for b in self.backtest_snapshots],
                "audit_reviews": [dict(r) for r in self.audit_reviews],
                "dino_memory": [dict(d) for d in self.dino_memory],
                "coin_tickers": coin_tickers,
                "msg_count": self._msg_count,
            }

    def _run_background_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._supervisor())

    async def _supervisor(self) -> None:
        async def seed_and_htf_loop():
            async with aiohttp.ClientSession() as session:
                while not self._stop_event.is_set():
                    for sym in WATCHLIST_SYMBOLS:
                        inst_id = OKX_MAP[sym]

                        for bar_tf, rest_tf in (("4H", "4H"), ("1H", "1H"), ("1D", "1D")):
                            try:
                                async with session.get(
                                    self.OKX_REST_CANDLES,
                                    params={"instId": inst_id, "bar": rest_tf, "limit": "100"},
                                    timeout=5,
                                ) as resp:
                                    if resp.status == 200:
                                        payload = await resp.json()
                                        rows = payload.get("data", [])
                                        ohlcv = [
                                            (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                                            for r in reversed(rows)
                                        ]
                                        with self._lock:
                                            self.engines[sym].update_timeframe_structure(bar_tf, ohlcv)
                            except Exception:
                                pass

                        try:
                            async with session.get(
                                self.OKX_REST_CANDLES,
                                params={"instId": inst_id, "bar": "1H", "limit": "150"},
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
                                                self._auto_log_emitted_signals(eng)
                                                self._update_active_trades_telemetry(
                                                    sym,
                                                    metrics["mid_price"],
                                                    metrics.get("squeeze_state", "NORMAL"),
                                                    metrics.get("squeeze_msg", ""),
                                                )
                        except Exception:
                            pass

                        await asyncio.sleep(0.15)
                    await asyncio.sleep(120)

        async def run_okx_ws() -> bool:
            async with websockets.connect(
                self.OKX_WS_PUBLIC, ping_interval=15, ping_timeout=10, open_timeout=8
            ) as ws:
                args = []
                for inst_id in OKX_MAP.values():
                    args.append({"channel": "books5", "instId": inst_id})
                    args.append({"channel": "trades", "instId": inst_id})
                await ws.send(json.dumps({"op": "subscribe", "args": args}))

                with self._lock:
                    self._connection_status = "LIVE OKX PERP"

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
                                    self._auto_log_emitted_signals(eng)
                                    self._update_active_trades_telemetry(
                                        sym,
                                        metrics["mid_price"],
                                        metrics.get("squeeze_state", "NORMAL"),
                                        metrics.get("squeeze_msg", ""),
                                    )
            return True

        async def ws_loop():
            while not self._stop_event.is_set():
                try:
                    await run_okx_ws()
                except Exception as exc:
                    with self._lock:
                        self._connection_status = "RECONNECTING"
                    logger.error("OKX WS error (%s), retrying in 2s...", exc)
                    await asyncio.sleep(2.0)

        await asyncio.gather(seed_and_htf_loop(), ws_loop())

    def shutdown(self):
        self._stop_event.set()
