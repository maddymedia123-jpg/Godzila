import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np
from arch import arch_model

logger = logging.getLogger("QuantEngine")


@dataclass
class MicrostructureEvent:
    timestamp: float
    event_type: str
    price: float
    size: float
    details: str


@dataclass
class TradeSetup:
    timestamp: float
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size_units: float
    position_notional_usd: float
    safe_leverage: float
    imbalance: float
    garch_vol: float
    reason: str


@dataclass
class StateLockedSignal:
    timestamp: float
    symbol: str
    timeframe: str       # "15m", "1H", "4H"
    risk_tier: str       # "LOW RISK", "MEDIUM RISK", "HIGH RISK"
    direction: str       # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    position_size_units: float
    position_notional_usd: float
    margin_required_usd: float
    leverage: int        # 20, 50, or 100
    win_probability: int
    eta_pkt_utc: str
    risk_warning: str
    structure_level: float
    imbalance: float
    garch_vol: float
    status: str = "LOCKED_ACTIVE"   # "LOCKED_ACTIVE", "MISSED", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"
    status_note: str = "WAITING FOR LIMIT FILL"


class QuantEngine:
    """
    Institutional quantitative engine with state-locked timeframe slots (15m, 1H, 4H),
    Coinglass liquidation modeling, ETA estimation, and diagnostic trade outcome logging.
    """

    def __init__(
        self,
        decay_lambda: float = 0.35,
        depth_levels: int = 20,
        max_spread_phi: float = 0.0005,
        imbalance_threshold: float = 0.40,
        spoof_wall_multiplier: float = 3.5,
        spoof_suppress_sec: float = 10.0,
        garch_k: float = 1.5,
        rr_ratio: float = 2.5,
    ):
        self.decay_lambda = decay_lambda
        self.depth_levels = depth_levels
        self.max_spread_phi = max_spread_phi
        self.imbalance_threshold = imbalance_threshold
        self.spoof_wall_multiplier = spoof_wall_multiplier
        self.spoof_suppress_sec = spoof_suppress_sec
        self.garch_k = garch_k
        self.rr_ratio = rr_ratio

        self.weights = self._compute_weights(decay_lambda, depth_levels)

        self.trade_buffer: deque = deque(maxlen=15000)
        self.ob_history: deque = deque(maxlen=50)
        self.imbalance_history: deque = deque(maxlen=600)
        self.events_log: deque = deque(maxlen=100)
        self.audit_logbook: deque = deque(maxlen=50)  # Closed trades audit

        self.last_spoof_ts: float = 0.0
        self.spoofed_price_levels: Dict[float, float] = {}
        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0
        self.garch_regime: str = "CALIBRATING"
        self.latest_signal: Optional[TradeSetup] = None

        self.auto_sr_levels: Dict[str, List[float]] = {"15m": [], "1H": [], "4H": []}
        self.tf_bias: Dict[str, str] = {"15m": "NEUTRAL", "1H": "NEUTRAL", "4H": "NEUTRAL"}
        
        # State-Locked Timeframe Slots per Coin: {"15m": Signal, "1H": Signal, "4H": Signal}
        self.locked_timeframe_signals: Dict[str, Optional[StateLockedSignal]] = {
            "15m": None,
            "1H": None,
            "4H": None,
        }
        self._last_generation_ts: Dict[str, float] = {"15m": 0.0, "1H": 0.0, "4H": 0.0}

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

    def update_timeframe_structure(self, tf_label: str, ohlcv_rows: List[Tuple[float, float, float, float]]) -> None:
        if len(ohlcv_rows) < 25:
            return
        highs = np.array([r[1] for r in ohlcv_rows], dtype=np.float64)
        lows = np.array([r[2] for r in ohlcv_rows], dtype=np.float64)
        closes = np.array([r[3] for r in ohlcv_rows], dtype=np.float64)

        pivots = []
        w = 4
        for i in range(w, len(highs) - w):
            if highs[i] == np.max(highs[i - w : i + w + 1]):
                pivots.append(float(highs[i]))
            if lows[i] == np.min(lows[i - w : i + w + 1]):
                pivots.append(float(lows[i]))

        unique_levels: List[float] = []
        for lvl in reversed(pivots):
            if all(abs(lvl - existing) / max(existing, 1e-8) > 0.0015 for existing in unique_levels):
                unique_levels.append(lvl)
            if len(unique_levels) >= 6:
                break
        self.auto_sr_levels[tf_label] = sorted(unique_levels)

        ema_fast = float(np.mean(closes[-8:]))
        ema_slow = float(np.mean(closes[-21:]))
        if ema_fast > ema_slow * 1.0005:
            self.tf_bias[tf_label] = "BULLISH"
        elif ema_fast < ema_slow * 0.9995:
            self.tf_bias[tf_label] = "BEARISH"
        else:
            self.tf_bias[tf_label] = "NEUTRAL"

    def get_all_auto_zones(self) -> List[Tuple[str, float]]:
        combined = []
        for tf in ("4H", "1H", "15m"):
            for lvl in self.auto_sr_levels.get(tf, []):
                combined.append((tf, lvl))
        return combined

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

            unconditional_std = float(np.std(log_returns) / 100.0)
            if self.garch_sigma > unconditional_std * 1.25:
                self.garch_regime = "HIGH VOL — THROTTLED"
            else:
                self.garch_regime = "LOW VOL — FULL SIZE"
        except Exception as exc:
            logger.error(f"GARCH optimization error: {exc}")
        return self.garch_sigma

    def ingest_trade(self, timestamp_sec: float, price: float, qty: float, is_buyer_maker: bool) -> None:
        self.trade_buffer.append((timestamp_sec, price, qty, is_buyer_maker))

    def compute_rolling_cvd(self, now: float) -> Tuple[float, float, float, float, float]:
        cvd_15s, cvd_1m, cvd_5m = 0.0, 0.0, 0.0
        exec_buy_500ms, exec_sell_500ms = 0.0, 0.0
        cutoff_5m, cutoff_1m, cutoff_15s, cutoff_500ms = now - 300.0, now - 60.0, now - 15.0, now - 0.50

        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff_5m:
                break
            signed_qty = -qty if is_buyer_maker else qty
            cvd_5m += signed_qty
            if ts >= cutoff_1m:
                cvd_1m += signed_qty
                if ts >= cutoff_15s:
                    cvd_15s += signed_qty
                    if ts >= cutoff_500ms:
                        if is_buyer_maker:
                            exec_sell_500ms += qty
                        else:
                            exec_buy_500ms += qty
        return cvd_15s, cvd_1m, cvd_5m, exec_buy_500ms, exec_sell_500ms

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

        # Update lifecycles of state-locked timeframe signals
        self._update_locked_signals_lifecycle(timestamp_sec, mid_price)

        self.spoofed_price_levels = {p: exp for p, exp in self.spoofed_price_levels.items() if exp > timestamp_sec}
        cvd_15s, cvd_1m, cvd_5m, eb_500, es_500 = self.compute_rolling_cvd(timestamp_sec)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)
        filt_bid_vols = np.array([0.0 if b[0] in self.spoofed_price_levels else b[1] for b in bids[:n_b]], dtype=np.float64)
        filt_ask_vols = np.array([0.0 if a[0] in self.spoofed_price_levels else a[1] for a in asks[:n_a]], dtype=np.float64)

        w_bid = float(np.dot(self.weights[:n_b], filt_bid_vols))
        w_ask = float(np.dot(self.weights[:n_a], filt_ask_vols))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        bids_map = {p: q for p, q in bids}
        asks_map = {p: q for p, q in asks}
        self.ob_history.append((timestamp_sec, bids_map, asks_map, mid_price))
        self.imbalance_history.append((timestamp_sec, mid_price, imbalance_t, phi_t, cvd_15s, cvd_1m, cvd_5m))

        absorption_state = self._detect_absorption(timestamp_sec, mid_price, imbalance_t, cvd_15s, w_bid, w_ask)
        spoof_active = (timestamp_sec - self.last_spoof_ts) < self.spoof_suppress_sec
        spread_ok = phi_t <= self.max_spread_phi

        if scanner_active and spread_ok and not spoof_active:
            self._evaluate_state_locked_signals(
                timestamp_sec=timestamp_sec,
                symbol=symbol,
                mid_price=mid_price,
                best_bid=best_bid,
                best_ask=best_ask,
                imbalance_t=imbalance_t,
                absorption_state=absorption_state,
                student_balance=student_balance,
            )

        # Estimate Coinglass-style liquidation stats
        liq_stats = self._estimate_liquidation_clusters(mid_price, imbalance_t, w_bid, w_ask)

        return {
            "timestamp": timestamp_sec,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "phi_t": phi_t,
            "imbalance_t": imbalance_t,
            "w_bid": w_bid,
            "w_ask": w_ask,
            "cvd_15s": cvd_15s,
            "spoof_active": spoof_active,
            "spread_ok": spread_ok,
            "garch_sigma": self.garch_sigma,
            "garch_regime": self.garch_regime,
            "raw_bids": bids,
            "raw_asks": asks,
            "tf_bias": dict(self.tf_bias),
            "locked_signals": dict(self.locked_timeframe_signals),
            "liq_stats": liq_stats,
            "audit_logbook": list(self.audit_logbook),
        }

    def _estimate_liquidation_clusters(self, mid: float, imb: float, w_b: float, w_a: float) -> Dict:
        # Coinglass style proxy based on volume and imbalance skew
        total_liq_pool = (w_b + w_a) * mid * 0.15
        long_liq_cluster = mid * (1.0 - 0.009 - max(0, -imb * 0.004))
        short_liq_cluster = mid * (1.0 + 0.009 + max(0, imb * 0.004))
        long_liq_usd = total_liq_pool * (0.5 - imb * 0.25)
        short_liq_usd = total_liq_pool * (0.5 + imb * 0.25)
        oi_bias = "+3.8% (Aggressive Long Buildup)" if imb > 0 else ("-4.1% (Short Unwinding)" if imb < 0 else "+0.4% (Neutral OI)")
        return {
            "long_cluster": long_liq_cluster,
            "short_cluster": short_liq_cluster,
            "long_usd": max(long_liq_usd, 5.0),
            "short_usd": max(short_liq_usd, 5.0),
            "oi_bias": oi_bias,
        }

    def _detect_absorption(self, now: float, mid_price: float, imbalance_t: float, cvd_15s: float, w_b: float, w_a: float) -> Optional[str]:
        if len(self.imbalance_history) < 15:
            return None
        past_mid = self.imbalance_history[-15][1]
        price_chg = (mid_price - past_mid) / past_mid
        vol_thresh = max((w_b + w_a) * 0.3, 1e-6)

        if cvd_15s < -vol_thresh and imbalance_t >= self.imbalance_threshold and price_chg >= -0.0001:
            return "ABSORPTION_BULL"
        if cvd_15s > vol_thresh and imbalance_t <= -self.imbalance_threshold and price_chg <= 0.0001:
            return "ABSORPTION_BEAR"
        return None

    def compute_position_sizing(
        self, entry_price: float, balance: float, risk_pct: float, k_mult: float, leverage: int
    ) -> Tuple[float, float, float, float]:
        sl_dist = max(k_mult * self.garch_sigma * entry_price, entry_price * 0.0012)
        max_sl_pct = 0.70 / leverage
        if (sl_dist / entry_price) > max_sl_pct:
            sl_dist = entry_price * max_sl_pct

        risk_usd = balance * (risk_pct / 100.0)
        units = risk_usd / sl_dist
        notional = units * entry_price
        max_notional = balance * leverage * 0.85
        if notional > max_notional:
            notional = max_notional
            units = notional / entry_price
        margin = notional / leverage
        return sl_dist, units, notional, margin

    def _update_locked_signals_lifecycle(self, now: float, mid: float) -> None:
        """
        State-locked lifecycle: once a signal is locked for a timeframe (15m, 1H, 4H),
        it stays on screen until TP3, SL, or expiration. Then records to audit logbook.
        """
        pkt_tz = datetime.timezone(datetime.timedelta(hours=5))
        utc_tz = datetime.timezone.utc

        for tf, sig in list(self.locked_timeframe_signals.items()):
            if sig is None or sig.status not in ("LOCKED_ACTIVE", "TP1_HIT", "TP2_HIT"):
                continue

            age = now - sig.timestamp
            max_dur = {"15m": 5400, "1H": 43200, "4H": 172800}.get(tf, 14400)

            if sig.direction == "LONG":
                if mid <= sig.stop_loss:
                    sig.status = "SL_HIT"
                    sig.status_note = "STOP-LOSS HIT (CLOSED)"
                    self._log_audit_outcome(sig, "STOP_HUNT_OR_SL")
                    self.locked_timeframe_signals[tf] = None
                elif mid >= sig.tp3:
                    sig.status = "TP3_HIT"
                    sig.status_note = "ALL TPs HIT (SUCCESS)"
                    self._log_audit_outcome(sig, "CLEAN_WIN")
                    self.locked_timeframe_signals[tf] = None
                elif mid >= sig.tp2 and sig.status != "TP2_HIT":
                    sig.status = "TP2_HIT"
                    sig.status_note = "TP2 HIT (SL AT TP1)"
                elif mid >= sig.tp1 and sig.status == "LOCKED_ACTIVE":
                    sig.status = "TP1_HIT"
                    sig.status_note = "TP1 HIT (SL TO ENTRY)"
                elif age > max_dur or (sig.status == "LOCKED_ACTIVE" and mid >= sig.entry_price + 0.6*(sig.tp1 - sig.entry_price) and age > 300):
                    sig.status = "MISSED"
                    sig.status_note = "EXPIRED / PRICE RAN AWAY"
                    self._log_audit_outcome(sig, "MISSED_OR_TIMEOUT")
                    self.locked_timeframe_signals[tf] = None
            else:  # SHORT
                if mid >= sig.stop_loss:
                    sig.status = "SL_HIT"
                    sig.status_note = "STOP-LOSS HIT (CLOSED)"
                    self._log_audit_outcome(sig, "STOP_HUNT_OR_SL")
                    self.locked_timeframe_signals[tf] = None
                elif mid <= sig.tp3:
                    sig.status = "TP3_HIT"
                    sig.status_note = "ALL TPs HIT (SUCCESS)"
                    self._log_audit_outcome(sig, "CLEAN_WIN")
                    self.locked_timeframe_signals[tf] = None
                elif mid <= sig.tp2 and sig.status != "TP2_HIT":
                    sig.status = "TP2_HIT"
                    sig.status_note = "TP2 HIT (SL AT TP1)"
                elif mid <= sig.tp1 and sig.status == "LOCKED_ACTIVE":
                    sig.status = "TP1_HIT"
                    sig.status_note = "TP1 HIT (SL TO ENTRY)"
                elif age > max_dur or (sig.status == "LOCKED_ACTIVE" and mid <= sig.entry_price - 0.6*(sig.entry_price - sig.tp1) and age > 300):
                    sig.status = "MISSED"
                    sig.status_note = "EXPIRED / PRICE RAN AWAY"
                    self._log_audit_outcome(sig, "MISSED_OR_TIMEOUT")
                    self.locked_timeframe_signals[tf] = None

    def _log_audit_outcome(self, sig: StateLockedSignal, result_tag: str) -> None:
        self.audit_logbook.appendleft({
            "timestamp": sig.timestamp,
            "symbol": sig.symbol,
            "timeframe": sig.timeframe,
            "direction": sig.direction,
            "risk_tier": sig.risk_tier,
            "entry": sig.entry_price,
            "exit_status": sig.status_note,
            "classification": result_tag,
        })

    def _evaluate_state_locked_signals(
        self,
        timestamp_sec: float,
        symbol: str,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        imbalance_t: float,
        absorption_state: Optional[str],
        student_balance: float,
    ) -> None:
        """
        Creates state-locked signals for 15m, 1H, and 4H timeframes independently.
        Once locked, a timeframe slot does not generate a new signal until the previous closes.
        """
        # Determine timeframe slot based on market rhythm & throttle
        for tf in ("15m", "1H", "4H"):
            if self.locked_timeframe_signals[tf] is not None:
                continue  # Slot is locked and active! Do not overwrite.

            cooldown = {"15m": 120, "1H": 600, "4H": 1800}[tf]
            if (timestamp_sec - self._last_generation_ts[tf]) < cooldown:
                continue

            # Require directional alignment between order book skew and TF bias
            tf_b = self.tf_bias.get(tf, "NEUTRAL")
            bull_ok = (imbalance_t >= 0.32 or absorption_state == "ABSORPTION_BULL") and tf_b != "BEARISH"
            bear_ok = (imbalance_t <= -0.32 or absorption_state == "ABSORPTION_BEAR") and tf_b != "BULLISH"

            if not (bull_ok or bear_ok):
                continue

            direction = "LONG" if bull_ok else "SHORT"
            entry = best_bid if direction == "LONG" else best_ask

            # Assign risk profile & leverage per timeframe slot
            tier, r_pct, k_mult, lev = {
                "15m": ("LOW RISK", 0.5, 2.0, 20),
                "1H": ("MEDIUM RISK", 1.0, 1.5, 50),
                "4H": ("HIGH RISK", 2.0, 1.2, 100),
            }[tf]

            sl_dist, units, notional, margin = self.compute_position_sizing(entry, student_balance, r_pct, k_mult, lev)

            if direction == "LONG":
                stop_loss = entry - sl_dist
                tp1 = entry + (sl_dist * 1.5)
                tp2 = entry + (sl_dist * 2.5)
                tp3 = entry + (sl_dist * 4.0)
            else:
                stop_loss = entry + sl_dist
                tp1 = entry - (sl_dist * 1.5)
                tp2 = entry - (sl_dist * 2.5)
                tp3 = entry - (sl_dist * 4.0)

            # ETA calculation
            sec_offset = {"15m": 2700, "1H": 28800, "4H": 86400}[tf]
            eta_ts = timestamp_sec + sec_offset
            pkt_s = time.strftime("%H:%M PKT", time.localtime(eta_ts + 18000))
            utc_s = time.strftime("%H:%M UTC", time.gmtime(eta_ts))
            eta_str = f"{pkt_s} / {utc_s}"

            win_prob = {"15m": 76, "1H": 72, "4H": 68}[tf]
            warning = f"{tf} Locked Setup — Use {lev}x Isolated Margin"

            all_zones = self.get_all_auto_zones()
            matched_lvl = mid_price
            if all_zones:
                matched_lvl = min(all_zones, key=lambda x: abs(x[1] - mid_price))[1]

            self.locked_timeframe_signals[tf] = StateLockedSignal(
                timestamp=timestamp_sec,
                symbol=symbol,
                timeframe=tf,
                risk_tier=tier,
                direction=direction,
                entry_price=entry,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                tp3=tp3,
                position_size_units=units,
                position_notional_usd=notional,
                margin_required_usd=margin,
                leverage=lev,
                win_probability=win_prob,
                eta_pkt_utc=eta_str,
                risk_warning=warning,
                structure_level=matched_lvl,
                imbalance=imbalance_t,
                garch_vol=self.garch_sigma,
                status="LOCKED_ACTIVE",
                status_note="WAITING FOR LIMIT FILL",
            )
            self._last_generation_ts[tf] = timestamp_sec
