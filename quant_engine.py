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
    win_probability: int
    eta_tp1_str: str
    eta_tp2_str: str
    eta_tp3_str: str
    htf_summary: str
    risk_warning: str
    entry_status: str = "ENTER NOW (IN ZONE)"  # ENTER NOW (IN ZONE) | TRADE PASSED — WAIT
    can_enter: bool = True
    status: str = "CANDIDATE_READY"


class QuantEngine:
    """
    Godzilla Institutional Quantitative Engine:
    - Strict 4H + 1H HTF trend & RSI confluence (eliminates 15m LONG/SHORT flip-flopping)
    - L2 Orderbook Exponential Imbalance + Rolling CVD confirmation
    - Dynamic ATR/GARCH velocity ETAs individually calculated for TP1, TP2, and TP3
    - Live Entry-Zone Drift Guard (blocks stale/chased entries once price passes entry)
    """

    def __init__(
        self,
        decay_lambda: float = 0.35,
        depth_levels: int = 20,
        max_spread_phi: float = 0.0008,
        imbalance_threshold: float = 0.25,
        garch_k: float = 1.5,
    ):
        self.decay_lambda = decay_lambda
        self.depth_levels = depth_levels
        self.max_spread_phi = max_spread_phi
        self.imbalance_threshold = imbalance_threshold
        self.garch_k = garch_k

        self.weights = self._compute_weights(decay_lambda, depth_levels)

        self.trade_buffer: deque = deque(maxlen=15000)
        self.audit_logbook: deque = deque(maxlen=50)

        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0

        # Higher-Timeframe Structure (1H & 4H ONLY — 15m excluded from signal triggers)
        self.auto_sr_levels: Dict[str, List[float]] = {"1H": [], "4H": []}
        self.tf_bias: Dict[str, str] = {"1H": "CALIBRATING", "4H": "CALIBRATING"}
        self.tf_rsi: Dict[str, float] = {"1H": 50.0, "4H": 50.0}
        self.atr_1h: float = 0.0

        self.candidate_signals: List[CommittedSignal] = []
        self.regime_status: str = "CALIBRATING HTF STRUCTURE..."
        self._locked_direction: Optional[str] = None
        self._direction_lock_ts: float = 0.0
        self._last_generation_ts: float = 0.0

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
        """
        Ingests 1H and 4H candles [(open, high, low, close), ...] to establish
        institutional trend regime, ATR velocity, RSI filter, and swing S/R zones.
        """
        if tf_label not in ("1H", "4H") or len(ohlcv_rows) < 25:
            return

        highs = np.array([r[1] for r in ohlcv_rows], dtype=np.float64)
        lows = np.array([r[2] for r in ohlcv_rows], dtype=np.float64)
        closes = np.array([r[3] for r in ohlcv_rows], dtype=np.float64)

        # True Range & ATR calculation on 1H for realistic TP ETA modeling
        if tf_label == "1H" and len(closes) >= 15:
            prev_closes = closes[:-1]
            tr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - prev_closes), np.abs(lows[1:] - prev_closes)),
            )
            self.atr_1h = float(np.mean(tr[-14:]))

        # Swing Pivot Detection
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

        # Require both EMA alignment and price confirmation relative to slow EMA
        if ema_fast > ema_slow * 1.0008 and last_close >= ema_slow:
            self.tf_bias[tf_label] = "BULLISH"
        elif ema_fast < ema_slow * 0.9992 and last_close <= ema_slow:
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

    def compute_rolling_cvd(self, now: float) -> float:
        cvd_15s = 0.0
        cutoff_15s = now - 15.0
        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff_15s:
                break
            cvd_15s += (-qty if is_buyer_maker else qty)
        return cvd_15s

    def _refresh_signal_entry_gate(self, sig: CommittedSignal, mid_price: float) -> None:
        """
        Evaluates live price relative to the signal's Entry Price and TP1.
        If price has already moved >35% toward TP1 (chased/missed entry), blocks new entry.
        """
        tp1_dist = abs(sig.tp1 - sig.entry_price)
        if tp1_dist <= 1e-9:
            return

        if sig.direction == "LONG":
            drift = mid_price - sig.entry_price
            if mid_price >= sig.tp1 or drift > tp1_dist * 0.35:
                sig.entry_status = "TRADE PASSED — DO NOT CHASE (WAIT FOR NEXT SIGNAL)"
                sig.can_enter = False
            elif drift < -abs(sig.entry_price - sig.stop_loss) * 0.50:
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
            elif drift < -abs(sig.stop_loss - sig.entry_price) * 0.50:
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

        cvd_15s = self.compute_rolling_cvd(timestamp_sec)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)
        w_bid = float(np.dot(self.weights[:n_b], [b[1] for b in bids[:n_b]]))
        w_ask = float(np.dot(self.weights[:n_a], [a[1] for a in asks[:n_a]]))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        liq_stats = self._estimate_liquidation_clusters(mid_price, imbalance_t, w_bid, w_ask)

        # Continuously update live entry validity on any existing signal
        for existing_sig in self.candidate_signals:
            self._refresh_signal_entry_gate(existing_sig, mid_price)

        if scanner_active:
            self._evaluate_institutional_signal(
                timestamp_sec, symbol, mid_price, best_bid, best_ask, phi_t, imbalance_t, cvd_15s, student_balance
            )

        return {
            "timestamp": timestamp_sec,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "phi_t": phi_t,
            "imbalance_t": imbalance_t,
            "cvd_15s": cvd_15s,
            "garch_sigma": self.garch_sigma,
            "atr_1h": self.atr_1h,
            "tf_bias": dict(self.tf_bias),
            "tf_rsi": dict(self.tf_rsi),
            "regime_status": self.regime_status,
            "raw_bids": bids,
            "raw_asks": asks,
            "liq_stats": liq_stats,
            "candidate_signals": list(self.candidate_signals),
            "audit_logbook": list(self.audit_logbook),
        }

    def _estimate_liquidation_clusters(self, mid: float, imb: float, w_b: float, w_a: float) -> Dict:
        total_pool = (w_b + w_a) * mid * 0.15
        bias_4h = self.tf_bias.get("4H", "NEUTRAL")
        bias_1h = self.tf_bias.get("1H", "NEUTRAL")
        return {
            "long_cluster": mid * (1.0 - 0.012 - max(0, -imb * 0.005)),
            "short_cluster": mid * (1.0 + 0.012 + max(0, imb * 0.005)),
            "long_usd": max(total_pool * (0.5 - imb * 0.25), 2.5),
            "short_usd": max(total_pool * (0.5 + imb * 0.25), 2.5),
            "oi_bias": f"4H: {bias_4h} | 1H: {bias_1h}",
        }

    def compute_position_sizing(self, entry: float, balance: float, risk_pct: float, k_mult: float, lev: int):
        # Use 1H ATR if available, otherwise GARCH sigma floor
        vol_unit = max(self.atr_1h * 0.65, k_mult * self.garch_sigma * entry, entry * 0.0035)
        sl_dist = vol_unit
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
    def _format_eta(now_ts: float, hours_ahead: float) -> str:
        clamped_hours = float(np.clip(hours_ahead, 0.35, 36.0))
        target_ts = now_ts + (clamped_hours * 3600.0)
        pkt_dt = datetime.fromtimestamp(target_ts + 18000, tz=timezone.utc)
        utc_dt = datetime.fromtimestamp(target_ts, tz=timezone.utc)
        return f"~{clamped_hours:.1f}h ({pkt_dt.strftime('%H:%M PKT')} / {utc_dt.strftime('%H:%M UTC')})"

    def _evaluate_institutional_signal(
        self,
        now: float,
        symbol: str,
        mid: float,
        b_bid: float,
        b_ask: float,
        phi_t: float,
        imb: float,
        cvd_15s: float,
        balance: float,
    ) -> None:
        bias_4h = self.tf_bias.get("4H", "CALIBRATING")
        bias_1h = self.tf_bias.get("1H", "CALIBRATING")
        rsi_1h = self.tf_rsi.get("1H", 50.0)

        if bias_4h == "CALIBRATING" or bias_1h == "CALIBRATING":
            self.regime_status = "CALIBRATING 1H & 4H CANDLE STRUCTURE..."
            return

        # 1. Strict HTF Alignment Gate (4H + 1H must agree — zero 15m noise)
        if bias_4h != bias_1h or bias_4h == "NEUTRAL":
            self.regime_status = f"HTF REGIME CONFLICT (4H: {bias_4h} vs 1H: {bias_1h}) — STANDING ASIDE"
            return

        htf_dir = "LONG" if bias_4h == "BULLISH" else "SHORT"

        # 2. 45-Minute Directional Hysteresis Lock (prevents flip-flopping)
        if (
            self._locked_direction is not None
            and self._locked_direction != htf_dir
            and (now - self._direction_lock_ts) < 2700.0
        ):
            mins_left = int((2700.0 - (now - self._direction_lock_ts)) / 60)
            self.regime_status = f"DIRECTION LOCKED {self._locked_direction} ({mins_left}m cooldown before reversal)"
            return

        # 3. Exhaustion RSI Guard (do not buy extreme overbought or short extreme oversold)
        if htf_dir == "LONG" and rsi_1h > 72.0:
            self.regime_status = f"4H/1H BULLISH, BUT 1H RSI OVERBOUGHT ({rsi_1h:.1f}) — WAITING FOR PULLBACK"
            return
        if htf_dir == "SHORT" and rsi_1h < 28.0:
            self.regime_status = f"4H/1H BEARISH, BUT 1H RSI OVERSOLD ({rsi_1h:.1f}) — WAITING FOR BOUNCE"
            return

        # 4. Orderbook Imbalance + Spread + CVD Microstructure Confirmation
        if phi_t > self.max_spread_phi:
            self.regime_status = f"SPREAD TOO WIDE ({phi_t:.5f}) — FILTERED"
            return

        thresh = max(self.imbalance_threshold, 0.22)
        if htf_dir == "LONG" and (imb < thresh or cvd_15s < 0):
            self.regime_status = f"4H/1H BULLISH ({htf_dir}) — WAITING FOR BID ABSORPTION (Imb: {imb:+.2f})"
            return
        if htf_dir == "SHORT" and (imb > -thresh or cvd_15s > 0):
            self.regime_status = f"4H/1H BEARISH ({htf_dir}) — WAITING FOR ASK DISTRIBUTION (Imb: {imb:+.2f})"
            return

        self.regime_status = f"CONFIRMED INSTITUTIONAL {htf_dir} REGIME (4H + 1H + L2 FLOW ALIGNED)"

        # Avoid regenerating identical setup within 10 minutes unless prior signal passed
        if self.candidate_signals and (now - self._last_generation_ts) < 600.0:
            if self.candidate_signals[0].direction == htf_dir and self.candidate_signals[0].can_enter:
                return

        entry = b_bid if htf_dir == "LONG" else b_ask
        sl_dist, units, notional, margin = self.compute_position_sizing(entry, balance, 1.0, self.garch_k, 25)

        if htf_dir == "LONG":
            sl = entry - sl_dist
            tp1 = entry + sl_dist * 1.4
            tp2 = entry + sl_dist * 2.4
            tp3 = entry + sl_dist * 3.8
        else:
            sl = entry + sl_dist
            tp1 = entry - sl_dist * 1.4
            tp2 = entry - sl_dist * 2.4
            tp3 = entry - sl_dist * 3.8

        # Realistic per-TP ETA using 1H ATR velocity
        hourly_velocity = max(self.atr_1h * 0.45, mid * self.garch_sigma * 2.2, mid * 0.0025)
        eta_tp1 = self._format_eta(now, abs(tp1 - entry) / hourly_velocity)
        eta_tp2 = self._format_eta(now, abs(tp2 - entry) / hourly_velocity)
        eta_tp3 = self._format_eta(now, abs(tp3 - entry) / hourly_velocity)

        win_prob = min(84, int(68 + abs(imb) * 20))
        sig_id = f"{symbol}_{htf_dir}_{int(now)}"

        sig = CommittedSignal(
            signal_id=sig_id,
            timestamp=now,
            symbol=symbol,
            timeframe="4H Macro + 1H Structure",
            risk_tier="INSTITUTIONAL CONFLUENCE",
            direction=htf_dir,
            entry_price=entry,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            position_size_units=units,
            position_notional_usd=notional,
            margin_required_usd=margin,
            leverage=25,
            win_probability=win_prob,
            eta_tp1_str=eta_tp1,
            eta_tp2_str=eta_tp2,
            eta_tp3_str=eta_tp3,
            htf_summary=f"4H {bias_4h} · 1H {bias_1h} (RSI {rsi_1h:.0f}) · L2 Imb {imb:+.2f}",
            risk_warning="Isolated Margin · Shared SL · Move SL to Breakeven at TP1",
            entry_status="ENTER TRADE NOW (IN OPTIMAL ZONE)",
            can_enter=True,
            status="CANDIDATE_READY",
        )

        self.candidate_signals = [sig]
        self._locked_direction = htf_dir
        self._direction_lock_ts = now
        self._last_generation_ts = now
        self.audit_logbook.appendleft(
            f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {symbol} {htf_dir} confirmed @ {entry:.4f} | 4H={bias_4h}, 1H={bias_1h}"
        )
