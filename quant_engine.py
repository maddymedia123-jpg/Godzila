import time
from datetime import datetime, timezone, timedelta
import logging
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np
from arch import arch_model

logger = logging.getLogger("QuantEngine")


@dataclass
class CommittedSignal:
    signal_id: str
    timestamp: float
    symbol: str
    timeframe: str
    risk_tier: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    position_size_units: float
    position_notional_usd: float
    margin_required_usd: float
    leverage: int
    win_probability: int
    eta_tp1_str: str
    eta_tp2_str: str
    eta_tp3_str: str
    htf_summary: str
    risk_warning: str
    entry_status: str = "ENTER TRADE NOW (IN OPTIMAL ZONE)"
    can_enter: bool = True
    status: str = "CANDIDATE_READY"


class QuantEngine:
    def __init__(
        self,
        decay_lambda: float = 0.35,
        depth_levels: int = 20,
        max_spread_phi: float = 0.0008,
        imbalance_threshold: float = 0.22,
        garch_k: float = 1.5,
    ):
        self.decay_lambda = decay_lambda
        self.depth_levels = depth_levels
        self.max_spread_phi = max_spread_phi
        self.imbalance_threshold = imbalance_threshold
        self.garch_k = garch_k

        self.weights = self._compute_weights(decay_lambda, depth_levels)
        self.trade_buffer: deque = deque(maxlen=20000)
        self.audit_logbook: deque = deque(maxlen=50)

        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0

        self.auto_sr_levels: Dict[str, List[float]] = {"1H": [], "4H": []}
        self.tf_bias: Dict[str, str] = {"1H": "CALIBRATING", "4H": "CALIBRATING"}
        self.tf_rsi: Dict[str, float] = {"1H": 50.0, "4H": 50.0}
        self.tf_atr: Dict[str, float] = {"1H": 0.0, "4H": 0.0}

        self.signals_by_tf: Dict[str, List[CommittedSignal]] = {"1H": [], "4H": []}
        self.status_by_tf: Dict[str, str] = {
            "1H": "CALIBRATING 1H CANDLES...",
            "4H": "CALIBRATING 4H CANDLES...",
        }
        self._locked_dir_by_tf: Dict[str, Optional[str]] = {"1H": None, "4H": None}
        self._lock_ts_by_tf: Dict[str, float] = {"1H": 0.0, "4H": 0.0}
        self._last_gen_by_tf: Dict[str, float] = {"1H": 0.0, "4H": 0.0}

    @staticmethod
    def _compute_weights(decay_lambda: float, levels: int) -> np.ndarray:
        k = np.arange(1, levels + 1, dtype=np.float64)
        return np.exp(-decay_lambda * (k - 1))

    def update_params(
        self,
        decay_lambda: float,
        max_spread_phi: float,
        imbalance_threshold: float,
        garch_k: float,
    ) -> None:
        if abs(decay_lambda - self.decay_lambda) > 1e-6:
            self.decay_lambda = decay_lambda
            self.weights = self._compute_weights(decay_lambda, self.depth_levels)
        self.max_spread_phi = max_spread_phi
        self.imbalance_threshold = imbalance_threshold
        self.garch_k = garch_k

    @staticmethod
    def _compute_ema(arr: np.ndarray, period: int) -> float:
        if len(arr) < period:
            return float(np.mean(arr))
        alpha = 2.0 / (period + 1.0)
        ema = float(arr[0])
        for val in arr[1:]:
            ema = alpha * float(val) + (1.0 - alpha) * ema
        return ema

    @staticmethod
    def _compute_rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes[-(period + 15):])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[-period:]))
        avg_loss = float(np.mean(losses[-period:]))
        if avg_loss < 1e-12:
            return 85.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    def update_timeframe_structure(self, tf_label: str, ohlcv_rows: List[Tuple[float, float, float, float]]) -> None:
        if tf_label not in ("1H", "4H") or len(ohlcv_rows) < 25:
            return

        highs = np.array([r[1] for r in ohlcv_rows], dtype=np.float64)
        lows = np.array([r[2] for r in ohlcv_rows], dtype=np.float64)
        closes = np.array([r[3] for r in ohlcv_rows], dtype=np.float64)

        if len(closes) >= 15:
            prev_closes = closes[:-1]
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - prev_closes), np.abs(lows[1:] - prev_closes)),
            )
            self.tf_atr[tf_label] = float(np.mean(tr[-14:]))

        pivots = []
        w = 3
        for i in range(w, len(highs) - w):
            if highs[i] == np.max(highs[i - w : i + w + 1]):
                pivots.append(float(highs[i]))
            if lows[i] == np.min(lows[i - w : i + w + 1]):
                pivots.append(float(lows[i]))

        unique_levels: List[float] = []
        for lvl in reversed(pivots):
            if all(abs(lvl - existing) / max(existing, 1e-8) > 0.002 for existing in unique_levels):
                unique_levels.append(lvl)
            if len(unique_levels) >= 6:
                break
        self.auto_sr_levels[tf_label] = sorted(unique_levels)

        ema_fast = self._compute_ema(closes[-40:], 8)
        ema_slow = self._compute_ema(closes[-60:], 21)
        last_close = float(closes[-1])
        rsi_val = self._compute_rsi(closes, 14)
        self.tf_rsi[tf_label] = rsi_val

        if ema_fast > ema_slow and last_close >= ema_slow * 0.9985:
            self.tf_bias[tf_label] = "BULLISH"
        elif ema_fast < ema_slow and last_close <= ema_slow * 1.0015:
            self.tf_bias[tf_label] = "BEARISH"
        else:
            self.tf_bias[tf_label] = "BULLISH" if last_close >= ema_fast else "BEARISH"

    def fit_garch(self, close_prices: List[float]) -> float:
        if len(close_prices) < 100:
            return self.garch_sigma
        try:
            prices = np.asarray(close_prices, dtype=np.float64)
            log_returns = 100.0 * np.diff(np.log(prices))
            log_returns = log_returns[np.isfinite(log_returns)]

            model = arch_model(log_returns, mean="Zero", vol="GARCH", p=1, q=1, dist="t", rescale=False)
            res = model.fit(disp="off", show_warning=False)
            forecast = res.forecast(horizon=1, reindex=False)
            variance_pct = float(forecast.variance.values[-1, 0])
            sigma_decimal = np.sqrt(max(variance_pct, 1e-8)) / 100.0

            self.garch_sigma = float(np.clip(sigma_decimal, 0.0005, 0.05))
            self.garch_last_fit = time.time()
        except Exception as exc:
            logger.error(f"GARCH error: {exc}")
        return self.garch_sigma

    def ingest_trade(self, timestamp_sec: float, price: float, qty: float, is_buyer_maker: bool) -> None:
        self.trade_buffer.append((timestamp_sec, price, qty, is_buyer_maker))

    def compute_rolling_cvd(self, now: float, window_sec: float = 60.0) -> float:
        cvd = 0.0
        cutoff = now - window_sec
        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff:
                break
            cvd += (-qty if is_buyer_maker else qty)
        return cvd

    def _refresh_signal_entry_gate(self, sig: CommittedSignal, mid_price: float) -> None:
        tp1_dist = abs(sig.tp1 - sig.entry_price)
        if tp1_dist <= 1e-9:
            return

        if sig.direction == "LONG":
            drift = mid_price - sig.entry_price
            if mid_price >= sig.tp1 or drift > tp1_dist * 0.35:
                sig.entry_status = "TRADE PASSED — DO NOT CHASE (WAIT FOR NEXT SIGNAL)"
                sig.can_enter = False
            elif drift < -abs(sig.entry_price - sig.stop_loss) * 0.55:
                sig.entry_status = "PULLBACK NEAR SL — WAIT FOR RECLAIM"
                sig.can_enter = False
            else:
                sig.entry_status = "ENTER TRADE NOW (IN OPTIMAL ZONE)"
                sig.can_enter = True
        else:
            drift = sig.entry_price - mid_price
            if mid_price <= sig.tp1 or drift > tp1_dist * 0.35:
                sig.entry_status = "TRADE PASSED — DO NOT CHASE (WAIT FOR NEXT SIGNAL)"
                sig.can_enter = False
            elif drift < -abs(sig.stop_loss - sig.entry_price) * 0.55:
                sig.entry_status = "BOUNCE NEAR SL — WAIT FOR REJECTION"
                sig.can_enter = False
            else:
                sig.entry_status = "ENTER TRADE NOW (IN OPTIMAL ZONE)"
                sig.can_enter = True

    def process_orderbook(
        self,
        timestamp_sec: float,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        symbol: str,
        account_balance: float,
        risk_pct: float,
        structural_zones: List[float],
        scanner_active: bool = True,
        student_balance: float = 100.0,
    ) -> Dict:
        if not bids or not asks:
            return {}

        bids = bids[: self.depth_levels]
        asks = asks[: self.depth_levels]
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = 0.5 * (best_bid + best_ask)
        spread = max(best_ask - best_bid, 0.0)
        phi_t = spread / mid_price if mid_price > 0 else 1.0

        cvd_60s = self.compute_rolling_cvd(timestamp_sec, 60.0)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)
        w_bid = float(np.dot(self.weights[:n_b], [b[1] for b in bids[:n_b]]))
        w_ask = float(np.dot(self.weights[:n_a], [a[1] for a in asks[:n_a]]))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        liq_stats = self._estimate_liquidation_clusters(mid_price, imbalance_t, w_bid, w_ask)

        for tf_key in ("1H", "4H"):
            for existing_sig in self.signals_by_tf[tf_key]:
                self._refresh_signal_entry_gate(existing_sig, mid_price)

        if scanner_active:
            for tf_key in ("1H", "4H"):
                self._evaluate_tf_signal(
                    tf_key, timestamp_sec, symbol, mid_price, best_bid, best_ask, phi_t, imbalance_t, cvd_60s, student_balance
                )

        return {
            "timestamp": timestamp_sec,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "phi_t": phi_t,
            "imbalance_t": imbalance_t,
            "cvd_15s": cvd_60s,
            "garch_sigma": self.garch_sigma,
            "tf_bias": dict(self.tf_bias),
            "tf_rsi": dict(self.tf_rsi),
            "tf_atr": dict(self.tf_atr),
            "raw_bids": bids,
            "raw_asks": asks,
            "liq_stats": liq_stats,
            "signals_1h": list(self.signals_by_tf["1H"]),
            "signals_4h": list(self.signals_by_tf["4H"]),
            "status_1h": self.status_by_tf["1H"],
            "status_4h": self.status_by_tf["4H"],
            "audit_logbook": list(self.audit_logbook),
        }

    def _estimate_liquidation_clusters(self, mid: float, imb: float, w_b: float, w_a: float) -> Dict:
        total_pool = (w_b + w_a) * mid * 0.15
        return {
            "long_cluster": mid * (1.0 - 0.012 - max(0, -imb * 0.005)),
            "short_cluster": mid * (1.0 + 0.012 + max(0, imb * 0.005)),
            "long_usd": max(total_pool * (0.5 - imb * 0.25), 2.5),
            "short_usd": max(total_pool * (0.5 + imb * 0.25), 2.5),
            "oi_bias": f"1H: {self.tf_bias['1H']} | 4H: {self.tf_bias['4H']}",
        }

    def compute_tf_position_sizing(self, tf_label: str, entry: float, balance: float, risk_pct: float, lev: int):
        atr_val = self.tf_atr.get(tf_label, 0.0)
        if tf_label == "1H":
            sl_dist = max(atr_val * 0.70, self.garch_k * self.garch_sigma * entry, entry * 0.0035)
        else:
            sl_dist = max(atr_val * 0.85, self.garch_k * self.garch_sigma * entry * 1.8, entry * 0.0075)

        risk_usd = balance * (risk_pct / 100.0)
        units = risk_usd / sl_dist
        notional = units * entry
        max_notional = balance * lev * 0.80
        if notional > max_notional:
            notional = max_notional
            units = notional / entry
        margin = notional / lev
        return sl_dist, units, notional, margin

    @staticmethod
    def _format_pkt_eta(now_ts: float, hours_ahead: float, min_h: float, max_h: float) -> str:
        clamped_hours = float(np.clip(hours_ahead, min_h, max_h))
        target_ts = now_ts + (clamped_hours * 3600.0)
        pkt_dt = datetime.fromtimestamp(target_ts + 18000, tz=timezone.utc)
        now_pkt_day = datetime.fromtimestamp(now_ts + 18000, tz=timezone.utc).day
        target_pkt_day = pkt_dt.day

        if target_pkt_day != now_pkt_day:
            time_str = f"{pkt_dt.strftime('%a %H:%M PKT')}"
        else:
            time_str = f"{pkt_dt.strftime('%H:%M PKT')}"
        return f"~{clamped_hours:.1f}h ({time_str})"

    def _evaluate_tf_signal(
        self,
        tf_label: str,
        now: float,
        symbol: str,
        mid: float,
        b_bid: float,
        b_ask: float,
        phi_t: float,
        imb: float,
        cvd_60s: float,
        balance: float,
    ) -> None:
        bias = self.tf_bias.get(tf_label, "CALIBRATING")
        rsi_val = self.tf_rsi.get(tf_label, 50.0)
        atr_val = self.tf_atr.get(tf_label, 0.0)

        if bias == "CALIBRATING":
            self.status_by_tf[tf_label] = f"CALIBRATING {tf_label} CANDLE STRUCTURE..."
            return

        direction = "LONG" if bias == "BULLISH" else "SHORT"
        cooldown_sec = 1500.0 if tf_label == "1H" else 5400.0

        locked_dir = self._locked_dir_by_tf[tf_label]
        lock_ts = self._lock_ts_by_tf[tf_label]
        if locked_dir is not None and locked_dir != direction and (now - lock_ts) < cooldown_sec:
            mins_left = int((cooldown_sec - (now - lock_ts)) / 60)
            self.status_by_tf[tf_label] = f"{tf_label} LOCKED {locked_dir} ({mins_left}m hysteresis cooldown)"
            return

        if direction == "LONG" and rsi_val > 74.0:
            self.status_by_tf[tf_label] = f"{tf_label} BULLISH, BUT RSI OVERBOUGHT ({rsi_val:.1f}) — WAITING FOR PULLBACK"
            return
        if direction == "SHORT" and rsi_val < 26.0:
            self.status_by_tf[tf_label] = f"{tf_label} BEARISH, BUT RSI OVERSOLD ({rsi_val:.1f}) — WAITING FOR BOUNCE"
            return

        if phi_t > self.max_spread_phi:
            self.status_by_tf[tf_label] = f"{tf_label} SPREAD WIDE ({phi_t:.5f}) — FILTERED"
            return

        req_imb = max(self.imbalance_threshold, 0.18) if tf_label == "1H" else max(self.imbalance_threshold * 0.85, 0.15)
        if direction == "LONG" and (imb < req_imb and cvd_60s <= 0):
            self.status_by_tf[tf_label] = f"{tf_label} BULLISH BIAS — WAITING FOR BUY FLOW (L2 Imb: {imb:+.2f})"
            return
        if direction == "SHORT" and (imb > -req_imb and cvd_60s >= 0):
            self.status_by_tf[tf_label] = f"{tf_label} BEARISH BIAS — WAITING FOR SELL FLOW (L2 Imb: {imb:+.2f})"
            return

        self.status_by_tf[tf_label] = f"ACTIVE {tf_label} {direction} SIGNAL ({tf_label} STRUCTURE + FLOW ALIGNED)"

        regen_window = 600.0 if tf_label == "1H" else 1800.0
        existing = self.signals_by_tf[tf_label]
        if existing and (now - self._last_gen_by_tf[tf_label]) < regen_window:
            if existing[0].direction == direction and existing[0].can_enter:
                return

        entry = b_bid if direction == "LONG" else b_ask
        lev = 30 if tf_label == "1H" else 15
        sl_dist, units, notional, margin = self.compute_tf_position_sizing(tf_label, entry, balance, 1.0, lev)

        if direction == "LONG":
            sl = entry - sl_dist
            tp1 = entry + sl_dist * 1.35
            tp2 = entry + sl_dist * 2.30
            tp3 = entry + sl_dist * 3.60
        else:
            sl = entry + sl_dist
            tp1 = entry - sl_dist * 1.35
            tp2 = entry - sl_dist * 2.30
            tp3 = entry - sl_dist * 3.60

        if tf_label == "1H":
            vel = max(atr_val * 0.50, mid * self.garch_sigma * 2.4, mid * 0.0025)
            eta_tp1 = self._format_pkt_eta(now, abs(tp1 - entry) / vel, 0.5, 4.0)
            eta_tp2 = self._format_pkt_eta(now, abs(tp2 - entry) / vel, 1.5, 8.0)
            eta_tp3 = self._format_pkt_eta(now, abs(tp3 - entry) / vel, 3.0, 14.0)
            tier = "1H INTRADAY MOMENTUM"
        else:
            vel = max((atr_val / 4.0) * 0.55, mid * self.garch_sigma * 2.0, mid * 0.0020)
            eta_tp1 = self._format_pkt_eta(now, abs(tp1 - entry) / vel, 2.5, 12.0)
            eta_tp2 = self._format_pkt_eta(now, abs(tp2 - entry) / vel, 6.0, 24.0)
            eta_tp3 = self._format_pkt_eta(now, abs(tp3 - entry) / vel, 12.0, 42.0)
            tier = "4H SWING STRUCTURE"

        win_prob = min(84, int(67 + abs(imb) * 22))
        sig_id = f"{symbol}_{tf_label}_{direction}_{int(now)}"

        sig = CommittedSignal(
            signal_id=sig_id,
            timestamp=now,
            symbol=symbol,
            timeframe=tf_label,
            risk_tier=tier,
            direction=direction,
            entry_price=entry,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            position_size_units=units,
            position_notional_usd=notional,
            margin_required_usd=margin,
            leverage=lev,
            win_probability=win_prob,
            eta_tp1_str=eta_tp1,
            eta_tp2_str=eta_tp2,
            eta_tp3_str=eta_tp3,
            htf_summary=f"{tf_label} {bias} (RSI {rsi_val:.0f}) · L2 Imb {imb:+.2f} · 60s CVD {cvd_60s:+.1f}",
            risk_warning=f"{tf_label} Isolated Margin · Shared SL · Trail SL to Entry at TP1",
            entry_status="ENTER TRADE NOW (IN OPTIMAL ZONE)",
            can_enter=True,
            status="CANDIDATE_READY",
        )

        self.signals_by_tf[tf_label] = [sig]
        self._locked_dir_by_tf[tf_label] = direction
        self._lock_ts_by_tf[tf_label] = now
        self._last_gen_by_tf[tf_label] = now
        self.audit_logbook.appendleft(
            f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] [{tf_label}] {symbol} {direction} @ {entry:.4f} | RSI={rsi_val:.1f}, Imb={imb:+.2f}"
        )
