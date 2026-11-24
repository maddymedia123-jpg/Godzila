import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
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
    squeeze_alert: str = ""
    early_warning: str = ""


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
        self.tf_trend_strength: Dict[str, float] = {"1H": 0.0, "4H": 0.0, "1D": 0.0}
        self.tf_closes: Dict[str, List[float]] = {"1H": [], "4H": [], "1D": []}

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
        self.tf_closes[tf_label] = closes[-40:].tolist()

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

        ema_spread_pct = abs(ema_fast - ema_slow) / max(last_close, 1e-8) * 100.0
        self.tf_trend_strength[tf_label] = ema_spread_pct

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
            logger.error("GARCH error: %s", exc)
        return self.garch_sigma

    def ingest_trade(self, timestamp_sec: float, price: float, qty: float, is_buyer_maker: bool) -> None:
        self.trade_buffer.append((timestamp_sec, price, qty, is_buyer_maker))

    def compute_rolling_cvd(self, now: float, window_sec: float = 180.0) -> float:
        cvd = 0.0
        cutoff = now - window_sec
        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff:
                break
            cvd += -qty if is_buyer_maker else qty
        return cvd

    def detect_momentum_warning(self, imb: float, cvd_3m: float) -> Tuple[str, str]:
        h1_bias = self.tf_bias.get("1H", "CALIBRATING")
        h4_bias = self.tf_bias.get("4H", "CALIBRATING")
        rsi_1h = self.tf_rsi.get("1H", 50.0)
        h1_str = self.tf_trend_strength.get("1H", 0.0)

        if h1_bias == "BULLISH" or h4_bias == "BULLISH":
            if (rsi_1h < 52.0 and imb < 0.05) or (h1_str < 0.22 and cvd_3m < 0):
                return (
                    "BULLISH_MOMENTUM_FADE",
                    f"EARLY WARNING — BULLISH MOMENTUM EASING (1H RSI {rsi_1h:.1f}, Imb {imb:+.2f}, CVD {cvd_3m:+.1f})",
                )

        if h1_bias == "BEARISH" or h4_bias == "BEARISH":
            if (rsi_1h > 48.0 and imb > -0.05) or (h1_str < 0.22 and cvd_3m > 0):
                return (
                    "BEARISH_MOMENTUM_FADE",
                    f"EARLY WARNING — BEARISH MOMENTUM EASING (1H RSI {rsi_1h:.1f}, Imb {imb:+.2f}, CVD {cvd_3m:+.1f})",
                )

        return ("STEADY", "")

    def detect_squeeze_state(self, now: float, mid_price: float, imb: float, cvd_3m: float) -> Tuple[str, str]:
        h4_bias = self.tf_bias.get("4H", "CALIBRATING")
        h1_bias = self.tf_bias.get("1H", "CALIBRATING")

        closes_1h = self.tf_closes.get("1H", [])
        bounce_1h_pct = 0.0
        drop_1h_pct = 0.0
        net_move_pct = 0.0

        if len(closes_1h) >= 4:
            recent_slice = closes_1h[-4:]
            recent_min = min(recent_slice)
            recent_max = max(recent_slice)
            anchor_px = recent_slice[0]
            bounce_1h_pct = ((mid_price - recent_min) / max(recent_min, 1e-8)) * 100.0
            drop_1h_pct = ((recent_max - mid_price) / max(recent_max, 1e-8)) * 100.0
            net_move_pct = ((mid_price - anchor_px) / max(anchor_px, 1e-8)) * 100.0

        if (
            net_move_pct > 0.70
            and bounce_1h_pct > 1.30
            and imb > 0.35
            and cvd_3m > 0
            and (h4_bias == "BEARISH" or h1_bias == "BEARISH")
        ):
            return (
                "SHORT_SQUEEZE_RISK",
                f"SHORT SQUEEZE ALERT: +{bounce_1h_pct:.2f}% upward push, Imb {imb:+.2f}, CVD {cvd_3m:+.1f}",
            )

        if (
            net_move_pct < -0.70
            and drop_1h_pct > 1.30
            and imb < -0.35
            and cvd_3m < 0
            and (h4_bias == "BULLISH" or h1_bias == "BULLISH")
        ):
            return (
                "LONG_SQUEEZE_RISK",
                f"LONG SQUEEZE ALERT: -{drop_1h_pct:.2f}% downward move, Imb {imb:+.2f}, CVD {cvd_3m:+.1f}",
            )

        return ("NORMAL", "")

    def _refresh_signal_entry_gate(
        self,
        sig: CommittedSignal,
        mid_price: float,
        squeeze_state: str,
        squeeze_msg: str,
        momentum_state: str,
        momentum_msg: str,
    ) -> None:
        tp1_dist = abs(sig.tp1 - sig.entry_price)
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        if tp1_dist <= 1e-9 or sl_dist <= 1e-9:
            return

        if sig.direction == "SHORT" and squeeze_state == "SHORT_SQUEEZE_RISK":
            sig.squeeze_alert = squeeze_msg
        elif sig.direction == "LONG" and squeeze_state == "LONG_SQUEEZE_RISK":
            sig.squeeze_alert = squeeze_msg
        else:
            sig.squeeze_alert = ""

        if sig.direction == "LONG" and momentum_state == "BULLISH_MOMENTUM_FADE":
            sig.early_warning = momentum_msg
        elif sig.direction == "SHORT" and momentum_state == "BEARISH_MOMENTUM_FADE":
            sig.early_warning = momentum_msg
        else:
            sig.early_warning = ""

        if sig.direction == "LONG":
            drift = mid_price - sig.entry_price
            if mid_price >= sig.tp1:
                sig.entry_status = "TP1 REACHED — TRACKING REMAINING TARGETS"
                sig.can_enter = False
                sig.status = "TP1_HIT"
            elif drift < -sl_dist:
                sig.entry_status = "STOP-LOSS ZONE REACHED"
                sig.can_enter = False
                sig.status = "SL_HIT"
            elif drift > tp1_dist * 0.65:
                sig.entry_status = "EXTENDED MOVE — WATCH PULLBACK OR NEXT SETUP"
                sig.can_enter = True
                sig.status = "ACTIVE_TRACKING"
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True
                sig.status = "ACTIVE_TRACKING"
        else:
            drift = sig.entry_price - mid_price
            if mid_price <= sig.tp1:
                sig.entry_status = "TP1 REACHED — TRACKING REMAINING TARGETS"
                sig.can_enter = False
                sig.status = "TP1_HIT"
            elif drift < -sl_dist:
                sig.entry_status = "STOP-LOSS ZONE REACHED"
                sig.can_enter = False
                sig.status = "SL_HIT"
            elif drift > tp1_dist * 0.65:
                sig.entry_status = "EXTENDED MOVE — WATCH BOUNCE OR NEXT SETUP"
                sig.can_enter = True
                sig.status = "ACTIVE_TRACKING"
            else:
                sig.entry_status = "EXECUTE NOW — OPTIMAL ENTRY ZONE"
                sig.can_enter = True
                sig.status = "ACTIVE_TRACKING"

    def get_prioritized_signals(self) -> List[CommittedSignal]:
        latest_4h = [self.signals_by_tf["4H"][-1]] if self.signals_by_tf.get("4H") else []
        latest_1h = [self.signals_by_tf["1H"][-1]] if self.signals_by_tf.get("1H") else []

        cand_4h = [s for s in latest_4h if s.status == "ACTIVE_TRACKING"]
        cand_1h = [s for s in latest_1h if s.status == "ACTIVE_TRACKING"]

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
        squeeze_state, squeeze_msg = self.detect_squeeze_state(timestamp_sec, mid_price, imbalance_t, cvd_3m)
        momentum_state, momentum_msg = self.detect_momentum_warning(imbalance_t, cvd_3m)

        for tf_key in ("4H", "1H"):
            for existing_sig in self.signals_by_tf[tf_key]:
                self._refresh_signal_entry_gate(
                    existing_sig, mid_price, squeeze_state, squeeze_msg, momentum_state, momentum_msg
                )

        if scanner_active:
            for tf_key in ("4H", "1H"):
                self._evaluate_tf_signal(
                    tf_key,
                    timestamp_sec,
                    symbol,
                    mid_price,
                    best_bid,
                    best_ask,
                    phi_t,
                    imbalance_t,
                    cvd_3m,
                    student_balance,
                    squeeze_state,
                    squeeze_msg,
                    momentum_msg,
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
            "squeeze_state": squeeze_state,
            "squeeze_msg": squeeze_msg,
            "momentum_state": momentum_state,
            "momentum_msg": momentum_msg,
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
            time_str = pkt_dt.strftime("%a %H:%M PKT").upper()
        else:
            time_str = pkt_dt.strftime("%H:%M PKT")
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
        squeeze_state: str,
        squeeze_msg: str,
        momentum_msg: str,
    ) -> None:
        bias = self.tf_bias.get(tf_label, "CALIBRATING")
        h4_bias = self.tf_bias.get("4H", "CALIBRATING")
        d1_bias = self.tf_bias.get("1D", "CALIBRATING")
        rsi_val = self.tf_rsi.get(tf_label, 50.0)
        atr_val = self.tf_atr.get(tf_label, 0.0)

        if bias in ("CALIBRATING", "NEUTRAL") or h4_bias == "CALIBRATING":
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} STRUCTURE NEUTRAL OR CALIBRATING"
            return

        vol_pct = self.garch_sigma * 100.0
        if tf_label == "1H" and self.garch_sigma > 0.011:
            self.status_by_tf[tf_label] = f"NO SIGNAL — 1H STAND-DOWN (VOLATILITY {vol_pct:.2f}%)"
            return

        direction = "LONG" if bias == "BULLISH" else "SHORT"
        three_tf_aligned = h4_bias == bias and d1_bias == bias

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
            alignment_str = "4H + 1D FULL ALIGNMENT" if d1_bias == bias else "4H QUALIFIED (1D NON-OPPOSING)"

        cooldown_sec = 1800.0 if tf_label == "1H" else 7200.0
        if (now - self._last_gen_by_tf[tf_label]) < cooldown_sec:
            existing = self.signals_by_tf[tf_label]
            if existing and existing[-1].status == "ACTIVE_TRACKING" and existing[-1].direction == direction:
                self.status_by_tf[tf_label] = f"ACTIVE {tf_label} {direction} SIGNAL (HTF + DUAL ORDER FLOW CONFIRMED)"
                return
            mins_left = int((cooldown_sec - (now - self._last_gen_by_tf[tf_label])) / 60)
            self.status_by_tf[tf_label] = f"NO NEW SIGNAL — {tf_label} COOLDOWN ({mins_left}m left)"
            return

        if direction == "LONG" and rsi_val > 72.0:
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} RSI OVERBOUGHT ({rsi_val:.1f})"
            return
        if direction == "SHORT" and rsi_val < 28.0:
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} RSI OVERSOLD ({rsi_val:.1f})"
            return

        if phi_t > self.max_spread_phi:
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} SPREAD TOO WIDE ({phi_t:.5f})"
            return

        req_imb = max(self.imbalance_threshold, 0.22) if tf_label == "1H" else max(self.imbalance_threshold * 0.85, 0.18)
        if direction == "LONG" and (imb < req_imb or cvd_3m <= 0):
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} LONG AWAITING DUAL FLOW (Imb {imb:+.2f}, CVD {cvd_3m:+.1f})"
            return
        if direction == "SHORT" and (imb > -req_imb or cvd_3m >= 0):
            self.status_by_tf[tf_label] = f"NO SIGNAL — {tf_label} SHORT AWAITING DUAL FLOW (Imb {imb:+.2f}, CVD {cvd_3m:+.1f})"
            return

        self.status_by_tf[tf_label] = f"ACTIVE {tf_label} {direction} SIGNAL (HTF + DUAL ORDER FLOW CONFIRMED)"

        entry = b_bid if direction == "LONG" else b_ask
        lev = 20 if tf_label == "1H" else 12
        sl_dist, units, notional, margin = self.compute_tf_position_sizing(tf_label, entry, balance, 1.0, lev)

        if direction == "LONG":
            sl = entry - sl_dist
            tp1 = entry + sl_dist * 1.50
            tp2 = entry + sl_dist * 2.50
            tp3 = entry + sl_dist * 4.00
        else:
            sl = entry + sl_dist
            tp1 = entry - sl_dist * 1.50
            tp2 = entry - sl_dist * 2.50
            tp3 = entry - sl_dist * 4.00

        if tf_label == "1H":
            vel = max(atr_val * 0.50, mid * self.garch_sigma * 2.4, mid * 0.0030)
            eta_tp1 = self._format_pkt_eta(now, abs(tp1 - entry) / vel, 1.0, 6.0)
            eta_tp2 = self._format_pkt_eta(now, abs(tp2 - entry) / vel, 2.5, 12.0)
            eta_tp3 = self._format_pkt_eta(now, abs(tp3 - entry) / vel, 4.5, 20.0)
            tier = "1H HIGH-ACCURACY SETUP"
        else:
            vel = max((atr_val / 4.0) * 0.55, mid * self.garch_sigma * 2.0, mid * 0.0025)
            eta_tp1 = self._format_pkt_eta(now, abs(tp1 - entry) / vel, 4.0, 16.0)
            eta_tp2 = self._format_pkt_eta(now, abs(tp2 - entry) / vel, 8.0, 28.0)
            eta_tp3 = self._format_pkt_eta(now, abs(tp3 - entry) / vel, 16.0, 48.0)
            tier = "4H HIGH-ACCURACY SETUP"

        tf_weight = 25.0 if tf_label == "4H" else 15.0
        align_weight = 20.0 if three_tf_aligned else 10.0
        flow_weight = min(25.0, abs(imb) * 30.0)
        accuracy_score = round(tf_weight + align_weight + flow_weight, 1)

        sl_pct = (sl_dist / entry) * 100.0
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
            eta_tp1_str=eta_tp1,
            eta_tp2_str=eta_tp2,
            eta_tp3_str=eta_tp3,
            htf_summary=f"{tf_label} {bias} (RSI {rsi_val:.0f}) · Imb {imb:+.2f} · CVD {cvd_3m:+.1f}",
            alignment_status=alignment_str,
            risk_warning=f"{tf_label} Isolated Margin · ATR SL ({sl_pct:.2f}%) · Tracked to TP3 or SL",
            accuracy_score=accuracy_score,
            entry_status="EXECUTE NOW — OPTIMAL ENTRY ZONE",
            can_enter=True,
            status="ACTIVE_TRACKING",
            squeeze_alert=squeeze_msg if squeeze_state != "NORMAL" else "",
            early_warning=momentum_msg,
        )

        self.signals_by_tf[tf_label] = [sig]
        self._locked_dir_by_tf[tf_label] = direction
        self._lock_ts_by_tf[tf_label] = now
        self._last_gen_by_tf[tf_label] = now
        self.emitted_signals_queue.append(sig)
        utc_now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.audit_logbook.appendleft(
            f"[{utc_now} UTC] [{tf_label}] {symbol} {direction} @ {entry:.4f} | Score {accuracy_score} | {alignment_str}"
        )
