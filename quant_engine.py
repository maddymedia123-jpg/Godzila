import time
from datetime import datetime, timezone
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
    eta_tp1_str: str
    eta_tp2_str: str
    eta_tp3_str: str
    htf_summary: str
    alignment_status: str
    risk_warning: str
    accuracy_score: float = 0.0
    entry_status: str = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
    can_enter: bool = True
    status: str = "ACTIVE_TRACKING"


class QuantEngine:
    def __init__(
        self,
        decay_lambda: float = 0.35,
        depth_levels: int = 20,
        max_spread_phi: float = 0.0008,
        imbalance_threshold: float = 0.25,
        garch_k: float = 1.8,
    ):
        self.decay_lambda = decay_lambda
        self.depth_levels = depth_levels
        self.max_spread_phi = max_spread_phi
        self.imbalance_threshold = imbalance_threshold
        self.garch_k = garch_k

        self.weights = self._compute_weights(decay_lambda, depth_levels)
        self.trade_buffer: deque = deque(maxlen=20000)
        self.audit_logbook: deque = deque(maxlen=200)
        self.emitted_signals_queue: deque = deque(maxlen=200)

        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0

        self.auto_sr_levels: Dict[str, List[float]] = {"1H": [], "4H": [], "1D": []}
        self.tf_bias: Dict[str, str] = {"1H": "CALIBRATING", "4H": "CALIBRATING", "1D": "CALIBRATING"}
        self.tf_rsi: Dict[str, float] = {"1H": 50.0, "4H": 50.0, "1D": 50.0}
        self.tf_atr: Dict[str, float] = {"1H": 0.0, "4H": 0.0, "1D": 0.0}

        self.signals_by_tf: Dict[str, List[CommittedSignal]] = {"1H": [], "4H": []}
        self.status_by_tf: Dict[str, str] = {
            "1H": "CALIBRATING 1H STRUCTURE...",
            "4H": "CALIBRATING 4H STRUCTURE...",
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
        if tf_label not in ("1H", "4H", "1D") or len(ohlcv_rows) < 25:
            return

        highs = np.array([r[1] for r in ohlcv_rows], dtype=np.float64)
        lows = np.array([r[2] for r in ohlcv_rows], dtype=np.float64)
        closes = np.array([r[3] for r in ohlcv_rows], dtype=np.float64)

        if len(closes) >= 15 and tf_label != "1D":
            prev_closes = closes[:-1]
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - prev_closes), np.abs(lows[1:] - prev_closes)),
            )
            self.tf_atr[tf_label] = float(np.mean(tr[-14:]))

        ema_fast = self._compute_ema(closes[-40:], 9)
        ema_slow = self._compute_ema(closes[-60:], 21)
        ema_trend = self._compute_ema(closes[-100:], 50)
        last_close = float(closes[-1])
        rsi_val = self._compute_rsi(closes, 14)
        self.tf_rsi[tf_label] = rsi_val

        if ema_fast > ema_slow and last_close > ema_slow and last_close >= ema_trend * 0.995:
            self.tf_bias[tf_label] = "BULLISH"
        elif ema_fast < ema_slow and last_close < ema_slow and last_close <= ema_trend * 1.005:
            self.tf_bias[tf_label] = "BEARISH"
        else:
            self.tf_bias[tf_label] = "NEUTRAL"

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

    def compute_rolling_cvd(self, now: float, window_sec: float = 180.0) -> float:
        cvd = 0.0
        cutoff = now - window_sec
        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff:
                break
            cvd += (-qty if is_buyer_maker else qty)
        return cvd

    def _refresh_signal_entry_gate(self, sig: CommittedSignal, mid_price: float) -> None:
        tp1_dist = abs(sig.tp1 - sig.entry_price)
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        if tp1_dist <= 1e-9 or sl_dist <= 1e-9:
            return

        if sig.direction == "LONG":
            drift = mid_price - sig.entry_price
            if mid_price >= sig.tp1 or drift > tp1_dist * 0.30:
                sig.entry_status = "SETUP PASSED ENTRY ZONE — TRACKING TO COMPLETION"
                sig.can_enter = False
            elif drift < -sl_dist * 0.50:
                sig.entry_status = "PRICE PULLED BACK NEAR SL — TRACKING TO COMPLETION"
                sig.can_enter = False
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True
        else:
            drift = sig.entry_price - mid_price
            if mid_price <= sig.tp1 or drift > tp1_dist * 0.30:
                sig.entry_status = "SETUP PASSED ENTRY ZONE — TRACKING TO COMPLETION"
                sig.can_enter = False
            elif drift < -sl_dist * 0.50:
                sig.entry_status = "PRICE BOUNCED NEAR SL — TRACKING TO COMPLETION"
                sig.can_enter = False
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True

    def get_prioritized_signals(self) -> List[CommittedSignal]:
        """
        Return qualifying non-conflicting signals ordered from highest accuracy score first.
        If 4H and 1H oppose each other, suppress the conflicting lower-priority signal.
        """
        cand_4h = [s for s in self.signals_by_tf.get("4H", []) if s.status == "ACTIVE_TRACKING"]
        cand_1h = [s for s in self.signals_by_tf.get("1H", []) if s.status == "ACTIVE_TRACKING"]

        qualified: List[CommittedSignal] = []
        primary_dir: Optional[str] = None

        ranked = sorted(cand_4h + cand_1h, key=lambda s: s.accuracy_score, reverse=True)
        for sig in ranked:
            if primary_dir is None:
                primary_dir = sig.direction
                qualified.append(sig)
            elif sig.direction == primary_dir:
                qualified.append(sig)

        return qualified

    def drain_emitted_signals(self) -> List[CommittedSignal]:
        items = list(self.emitted_signals_queue)
        self.emitted_signals_queue.clear()
        return items

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

        cvd_3m = self.compute_rolling_cvd(timestamp_sec, 180.0)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)
        w_bid = float(np.dot(self.weights[:n_b], [b[1] for b in bids[:n_b]]))
        w_ask = float(np.dot(self.weights[:n_a], [a[1] for a in asks[:n_a]]))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        liq_stats = self._estimate_liquidation_clusters(mid_price, imbalance_t, w_bid, w_ask)

        for tf_key in ("4H", "1H"):
            for existing_sig in self.signals_by_tf[tf_key]:
                self._refresh_signal_entry_gate(existing_sig, mid_price)

        if scanner_active:
            for tf_key in ("4H", "1H"):
                self._evaluate_tf_signal(
                    tf_key, timestamp_sec, symbol, mid_price, best_bid, best_ask, phi_t, imbalance_t, cvd_3m, student_balance
                )

        prioritized_signals = self.get_prioritized_signals()

        return {
            "timestamp": timestamp_sec,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "phi_t": phi_t,
            "imbalance_t": imbalance_t,
            "cvd_15s": cvd_3m,
            "garch_sigma": self.garch_sigma,
            "tf_bias": dict(self.tf_bias),
            "tf_rsi": dict(self.tf_rsi),
            "tf_atr": dict(self.tf_atr),
            "raw_bids": bids,
            "raw_asks": asks,
            "liq_stats": liq_stats,
            "prioritized_signals": prioritized_signals,
            "signals_1h": list(self.signals_by_tf["1H"]),
            "signals_4h": list(self.signals_by_tf["4H"]),
            "status_1h": self.status_by_tf["1H"],
            "status_4h": self.status_by_tf["4H"],
            "audit_logbook": list(self.audit_logbook),
        }

    def _estimate_liquidation_clusters(self, mid: float, imb: float, w_b: float, w_a: float) -> Dict:
        total_pool = (w_b + w_a) * mid * 0.15
        return {
            "long_cluster": mid * (1.0 - 0.015 - max(0, -imb * 0.005)),
            "short_cluster": mid * (1.0 + 0.015 + max(0, imb * 0.005)),
            "long_usd": max(total_pool * (0.5 - imb * 0.25), 2.5),
            "short_usd": max(total_pool * (0.5 + imb * 0.25), 2.5),
            "oi_bias": f"1H: {self.tf_bias['1H']} | 4H: {self.tf_bias['4H']}",
        }

    def compute_tf_position_sizing(self, tf_label: str, entry: float, balance: float, risk_pct: float, lev: int):
        atr_val = self.tf_atr.get(tf_label, 0.0)
        if tf_label == "1H":
            sl_dist = max(atr_val * 1.25, self.garch_k * self.garch_sigma * entry * 1.5, entry * 0.0085)
        else:
            sl_dist = max(atr_val * 1.40, self.garch_k * self.garch_sigma * entry * 2.5, entry * 0.0150)

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

        if target_pkt_day != now_pkt_day or clamped_hours >= 24.0:
            time_str = f"{pkt_dt.strftime('%a %H:%M PKT').upper()}"
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
        cvd_3m: float,
        balance: float,
    ) -> None:
        bias = self.tf_bias.get(tf_label, "CALIBRATING")
        h4_bias = self.tf_bias.get("4H", "CALIBRATING")
        d1_bias = self.tf_bias.get("1D", "CALIBRATING")
        rsi_val = self.tf_rsi.get(tf_label, 50.0)
        atr_val = self.tf_atr.get(tf_label, 0.0)

        if bias in ("CALIBRATING", "NEUTRAL") or h4_bias == "CALIBRATING":
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} STRUCTURE NEUTRAL OR CALIBRATING"
            return

        if tf_label == "1H" and self.garch_sigma > 0.011:
            self.status_by_tf[tf_label] = f"NO SIGNAL — 1H STAND-DOWN (VOLATILITY {self.garch_sigma*100:.2f}%)"
            return

        direction = "LONG" if bias == "BULLISH" else "SHORT"

        three_tf_aligned = (h4_bias == bias and d1_bias == bias)
        if tf_label == "1H":
            if direction == "LONG" and (h4_bias != "BULLISH" or d1_bias == "BEARISH"):
                self.status_by_tf[tf_label] = f"NO SIGNAL — 1H LONG NOT ALIGNED WITH 4H ({h4_bias}) / 1D ({d1_bias})"
                return
            if direction == "SHORT" and (h4_bias != "BEARISH" or d1_bias == "BULLISH"):
                self.status_by_tf[tf_label] = f"NO SIGNAL — 1H SHORT NOT ALIGNED WITH 4H ({h4_bias}) / 1D ({d1_bias})"
                return
            alignment_str = "1H + 4H + 1D FULL ALIGNMENT" if three_tf_aligned else "1H + 4H ALIGNED (1D NON-OPPOSING)"
        else:
            if direction == "LONG" and d1_bias == "BEARISH":
                self.status_by_tf[tf_label] = "NO SIGNAL — 4H LONG AGAINST 1D BEARISH TREND"
                return
            if direction == "SHORT" and d1_bias == "BULLISH":
                self.status_by_tf[tf_label] = "NO SIGNAL — 4H SHORT AGAINST 1D BULLISH TREND"
                return
            alignment_str = "4H + 1D FULL ALIGNMENT" if d1_bias
