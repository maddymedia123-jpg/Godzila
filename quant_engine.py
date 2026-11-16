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
    event_type: str  # "SPOOF_BID", "SPOOF_ASK", "ABSORPTION_BULL", "ABSORPTION_BEAR"
    price: float
    size: float
    details: str


@dataclass
class TradeSetup:
    timestamp: float
    symbol: str
    direction: str  # "LONG" or "SHORT"
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
class StudentAutoSignal:
    timestamp: float
    symbol: str
    timeframe_bias: str  # e.g. "15m + 1H + 4H BULLISH"
    risk_tier: str       # "LOW RISK", "MEDIUM RISK", "HIGH RISK"
    direction: str       # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    position_size_units: float
    position_notional_usd: float
    safe_leverage: float
    win_probability: int
    risk_warning: str
    structure_level: float
    imbalance: float
    garch_vol: float


class QuantEngine:
    """
    Institutional-grade quantitative microstructure & volatility engine.
    Computes distance-weighted L2 imbalance, spoof filters, CVD tape absorption,
    multi-timeframe (15m/1H/4H) auto-structural levels, Student-t GARCH(1,1)
    volatility scaling, and automated Low/Medium/High risk student setups.
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

        # Fixed ring buffers for constant low RAM usage
        self.trade_buffer: deque = deque(maxlen=15000)
        self.ob_history: deque = deque(maxlen=50)
        self.imbalance_history: deque = deque(maxlen=600)
        self.events_log: deque = deque(maxlen=100)
        self.student_signal_feed: deque = deque(maxlen=40)

        # State tracking
        self.last_spoof_ts: float = 0.0
        self.spoofed_price_levels: Dict[float, float] = {}
        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0
        self.garch_regime: str = "CALIBRATING"
        self.latest_signal: Optional[TradeSetup] = None

        # Multi-timeframe auto-structural state (15m, 1H, 4H)
        self.auto_sr_levels: Dict[str, List[float]] = {"15m": [], "1H": [], "4H": []}
        self.tf_bias: Dict[str, str] = {"15m": "NEUTRAL", "1H": "NEUTRAL", "4H": "NEUTRAL"}
        self.latest_student_profiles: Dict[str, Optional[StudentAutoSignal]] = {
            "LOW RISK": None,
            "MEDIUM RISK": None,
            "HIGH RISK": None,
        }
        self._last_student_emit_ts: float = 0.0

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

    # -------------------------------------------------------------------------
    # 1. Multi-Timeframe Auto-Structural S/R & Trend Detection (15m / 1H / 4H)
    # -------------------------------------------------------------------------
    def update_timeframe_structure(self, tf_label: str, ohlcv_rows: List[Tuple[float, float, float, float]]) -> None:
        """
        Extracts swing highs/lows and EMA trend direction from (open, high, low, close) candles
        for 15m, 1H, and 4H timeframes automatically.
        """
        if len(ohlcv_rows) < 25:
            return

        highs = np.array([r[1] for r in ohlcv_rows], dtype=np.float64)
        lows = np.array([r[2] for r in ohlcv_rows], dtype=np.float64)
        closes = np.array([r[3] for r in ohlcv_rows], dtype=np.float64)

        # Swing pivot detection (window = 4 bars left/right)
        pivots = []
        w = 4
        for i in range(w, len(highs) - w):
            if highs[i] == np.max(highs[i - w : i + w + 1]):
                pivots.append(float(highs[i]))
            if lows[i] == np.min(lows[i - w : i + w + 1]):
                pivots.append(float(lows[i]))

        # Keep most recent distinct structural levels (deduplicated within 0.15%)
        unique_levels: List[float] = []
        for lvl in reversed(pivots):
            if all(abs(lvl - existing) / max(existing, 1e-8) > 0.0015 for existing in unique_levels):
                unique_levels.append(lvl)
            if len(unique_levels) >= 6:
                break

        self.auto_sr_levels[tf_label] = sorted(unique_levels)

        # Fast vs Slow EMA bias for timeframe alignment
        ema_fast = float(np.mean(closes[-8:]))
        ema_slow = float(np.mean(closes[-21:]))
        if ema_fast > ema_slow * 1.0008:
            self.tf_bias[tf_label] = "BULLISH"
        elif ema_fast < ema_slow * 0.9992:
            self.tf_bias[tf_label] = "BEARISH"
        else:
            self.tf_bias[tf_label] = "NEUTRAL"

    def get_all_auto_zones(self) -> List[Tuple[str, float]]:
        combined = []
        for tf in ("4H", "1H", "15m"):
            for lvl in self.auto_sr_levels.get(tf, []):
                combined.append((tf, lvl))
        return combined

    # -------------------------------------------------------------------------
    # 2. GARCH(1,1) Student-t Volatility Model
    # -------------------------------------------------------------------------
    def fit_garch(self, close_prices: List[float]) -> float:
        if len(close_prices) < 100:
            return self.garch_sigma

        try:
            prices = np.asarray(close_prices, dtype=np.float64)
            log_returns = 100.0 * np.diff(np.log(prices))
            log_returns = log_returns[np.isfinite(log_returns)]

            model = arch_model(
                log_returns,
                mean="Zero",
                vol="GARCH",
                p=1,
                q=1,
                dist="t",
                rescale=False,
            )
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

    # -------------------------------------------------------------------------
    # 3. Trade Tape Ingestion & Rolling CVD
    # -------------------------------------------------------------------------
    def ingest_trade(self, timestamp_sec: float, price: float, qty: float, is_buyer_maker: bool) -> None:
        self.trade_buffer.append((timestamp_sec, price, qty, is_buyer_maker))

    def compute_rolling_cvd(self, now: float) -> Tuple[float, float, float, float, float]:
        cvd_15s = 0.0
        cvd_1m = 0.0
        cvd_5m = 0.0
        exec_buy_500ms = 0.0
        exec_sell_500ms = 0.0

        cutoff_5m = now - 300.0
        cutoff_1m = now - 60.0
        cutoff_15s = now - 15.0
        cutoff_500ms = now - 0.50

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

    # -------------------------------------------------------------------------
    # 4. Level-2 Depth Processing, Spoof Detection & Dual-Mode Signal Generation
    # -------------------------------------------------------------------------
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
        student_balance: float = 1000.0,
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

        self.spoofed_price_levels = {
            p: exp for p, exp in self.spoofed_price_levels.items() if exp > timestamp_sec
        }

        cvd_15s, cvd_1m, cvd_5m, exec_buy_500ms, exec_sell_500ms = self.compute_rolling_cvd(timestamp_sec)
        self._detect_spoofs(timestamp_sec, bids, asks, exec_buy_500ms, exec_sell_500ms)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)

        filt_bid_vols = np.array(
            [0.0 if b[0] in self.spoofed_price_levels else b[1] for b in bids[:n_b]],
            dtype=np.float64,
        )
        filt_ask_vols = np.array(
            [0.0 if a[0] in self.spoofed_price_levels else a[1] for a in asks[:n_a]],
            dtype=np.float64,
        )

        w_bid = float(np.dot(self.weights[:n_b], filt_bid_vols))
        w_ask = float(np.dot(self.weights[:n_a], filt_ask_vols))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        bids_map = {p: q for p, q in bids}
        asks_map = {p: q for p, q in asks}
        self.ob_history.append((timestamp_sec, bids_map, asks_map, mid_price))
        self.imbalance_history.append(
            (timestamp_sec, mid_price, imbalance_t, phi_t, cvd_15s, cvd_1m, cvd_5m)
        )

        absorption_state = self._detect_absorption(
            timestamp_sec, mid_price, imbalance_t, cvd_15s, w_bid, w_ask
        )

        spoof_active = (timestamp_sec - self.last_spoof_ts) < self.spoof_suppress_sec
        spread_ok = phi_t <= self.max_spread_phi

        # Always keep live 3-tier Student Presets refreshed with current price/GARCH levels
        self._refresh_live_student_presets(
            timestamp_sec=timestamp_sec,
            symbol=symbol,
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            imbalance_t=imbalance_t,
            student_balance=student_balance,
        )

        # Only fire new triggers when Scanner Toggle is ON, spread is clean, and no spoof suppression is active
        if scanner_active and spread_ok and not spoof_active:
            if absorption_state is not None:
                self._evaluate_pro_trade_setup(
                    timestamp_sec=timestamp_sec,
                    symbol=symbol,
                    mid_price=mid_price,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    imbalance_t=imbalance_t,
                    absorption_state=absorption_state,
                    account_balance=account_balance,
                    risk_pct=risk_pct,
                    structural_zones=structural_zones,
                )

            self._evaluate_student_auto_signal(
                timestamp_sec=timestamp_sec,
                symbol=symbol,
                mid_price=mid_price,
                best_bid=best_bid,
                best_ask=best_ask,
                imbalance_t=imbalance_t,
                absorption_state=absorption_state,
                student_balance=student_balance,
            )

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
            "cvd_1m": cvd_1m,
            "cvd_5m": cvd_5m,
            "spoof_active": spoof_active,
            "spread_ok": spread_ok,
            "garch_sigma": self.garch_sigma,
            "garch_regime": self.garch_regime,
            "raw_bids": bids,
            "raw_asks": asks,
            "filtered_bid_vols": filt_bid_vols.tolist(),
            "filtered_ask_vols": filt_ask_vols.tolist(),
            "tf_bias": dict(self.tf_bias),
            "auto_sr_levels": {k: list(v) for k, v in self.auto_sr_levels.items()},
        }

    def _detect_spoofs(
        self,
        now: float,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        exec_buy_500ms: float,
        exec_sell_500ms: float,
    ) -> None:
        if len(self.ob_history) < 4:
            return

        target_ts = now - 0.45
        past_snap = None
        for snap in reversed(self.ob_history):
            if snap[0] <= target_ts:
                past_snap = snap
                break
        if past_snap is None:
            return

        _, past_bids, past_asks, _ = past_snap
        curr_bids = dict(bids)
        curr_asks = dict(asks)

        avg_bid_size = float(np.mean([q for _, q in bids])) if bids else 0.0
        avg_ask_size = float(np.mean([q for _, q in asks])) if asks else 0.0

        for price, old_qty in past_bids.items():
            if old_qty > avg_bid_size * self.spoof_wall_multiplier:
                new_qty = curr_bids.get(price, 0.0)
                vanished = old_qty - new_qty
                if vanished > old_qty * 0.75 and exec_sell_500ms < vanished * 0.25:
                    self._register_spoof(now, "SPOOF_BID", price, vanished)

        for price, old_qty in past_asks.items():
            if old_qty > avg_ask_size * self.spoof_wall_multiplier:
                new_qty = curr_asks.get(price, 0.0)
                vanished = old_qty - new_qty
                if vanished > old_qty * 0.75 and exec_buy_500ms < vanished * 0.25:
                    self._register_spoof(now, "SPOOF_ASK", price, vanished)

    def _register_spoof(self, now: float, event_type: str, price: float, vanished_qty: float) -> None:
        if price in self.spoofed_price_levels:
            return
        self.last_spoof_ts = now
        self.spoofed_price_levels[price] = now + self.spoof_suppress_sec
        self.events_log.appendleft(
            MicrostructureEvent(
                timestamp=now,
                event_type=event_type,
                price=price,
                size=vanished_qty,
                details=f"Wall vanished ({vanished_qty:.2f}) w/o tape fill. Triggers suppressed {self.spoof_suppress_sec:.0f}s.",
            )
        )

    def _detect_absorption(
        self,
        now: float,
        mid_price: float,
        imbalance_t: float,
        cvd_15s: float,
        w_bid: float,
        w_ask: float,
    ) -> Optional[str]:
        if len(self.imbalance_history) < 15:
            return None

        past_mid = None
        for ts, m_p, _, _, _, _, _ in reversed(self.imbalance_history):
            if now - ts >= 3.0:
                past_mid = m_p
                break
        if past_mid is None:
            return None

        price_change_pct = (mid_price - past_mid) / past_mid
        vol_spike_thresh = max((w_bid + w_ask) * 0.35, 1e-6)

        if self.events_log and (now - self.events_log[0].timestamp) < 5.0:
            if "ABSORPTION" in self.events_log[0].event_type:
                return self.events_log[0].event_type

        if (
            cvd_15s < -vol_spike_thresh
            and imbalance_t >= self.imbalance_threshold
            and price_change_pct >= -0.0001
        ):
            self.events_log.appendleft(
                MicrostructureEvent(
                    timestamp=now,
                    event_type="ABSORPTION_BULL",
                    price=mid_price,
                    size=abs(cvd_15s),
                    details=f"Aggressive sells ({cvd_15s:.2f}) absorbed by bid stack (I_t={imbalance_t:+.2f}).",
                )
            )
            return "ABSORPTION_BULL"

        if (
            cvd_15s > vol_spike_thresh
            and imbalance_t <= -self.imbalance_threshold
            and price_change_pct <= 0.0001
        ):
            self.events_log.appendleft(
                MicrostructureEvent(
                    timestamp=now,
                    event_type="ABSORPTION_BEAR",
                    price=mid_price,
                    size=abs(cvd_15s),
                    details=f"Aggressive buys (+{cvd_15s:.2f}) absorbed by ask stack (I_t={imbalance_t:+.2f}).",
                )
            )
            return "ABSORPTION_BEAR"

        return None

    # -------------------------------------------------------------------------
    # 5. Sizing & Pro Setup Evaluation
    # -------------------------------------------------------------------------
    def compute_position_sizing(
        self,
        entry_price: float,
        account_balance: float,
        risk_pct: float,
        k_mult: Optional[float] = None,
        max_lev_cap: float = 50.0,
    ) -> Tuple[float, float, float, float]:
        mult = k_mult if k_mult is not None else self.garch_k
        sl_dist = max(mult * self.garch_sigma * entry_price, entry_price * 0.0008)

        risk_usd = account_balance * (risk_pct / 100.0)
        position_units = risk_usd / sl_dist
        notional_usd = position_units * entry_price

        stop_pct = sl_dist / entry_price
        vol_derived_max_lev = 0.50 / max(stop_pct, 1e-4)
        required_lev = notional_usd / max(account_balance, 1.0)
        safe_leverage = float(
            np.clip(min(vol_derived_max_lev, max(required_lev * 1.25, 1.0)), 1.0, max_lev_cap)
        )

        if required_lev > vol_derived_max_lev:
            notional_usd = account_balance * vol_derived_max_lev
            position_units = notional_usd / entry_price
            safe_leverage = float(np.clip(vol_derived_max_lev, 1.0, max_lev_cap))

        return sl_dist, position_units, notional_usd, safe_leverage

    def _evaluate_pro_trade_setup(
        self,
        timestamp_sec: float,
        symbol: str,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        imbalance_t: float,
        absorption_state: str,
        account_balance: float,
        risk_pct: float,
        structural_zones: List[float],
    ) -> None:
        zone_tolerance = max(self.garch_sigma * mid_price * 1.25, mid_price * 0.0015)
        near_zone = True
        matched_zone = None
        if structural_zones:
            near_zone = False
            for z in structural_zones:
                if abs(mid_price - z) <= zone_tolerance:
                    near_zone = True
                    matched_zone = z
                    break

        if not near_zone:
            return

        if self.latest_signal and (timestamp_sec - self.latest_signal.timestamp) < 15.0:
            return

        if absorption_state == "ABSORPTION_BULL" and imbalance_t >= self.imbalance_threshold:
            entry = best_bid
            sl_dist, units, notional, lev = self.compute_position_sizing(
                entry, account_balance, risk_pct
            )
            zone_txt = f" near S/R {matched_zone:,.4f}" if matched_zone else ""
            self.latest_signal = TradeSetup(
                timestamp=timestamp_sec,
                symbol=symbol,
                direction="LONG",
                entry_price=entry,
                stop_loss=entry - sl_dist,
                take_profit=entry + (sl_dist * self.rr_ratio),
                position_size_units=units,
                position_notional_usd=notional,
                safe_leverage=lev,
                imbalance=imbalance_t,
                garch_vol=self.garch_sigma,
                reason=f"Bullish Tape Absorption + Bid Stack (I_t={imbalance_t:+.2f}){zone_txt}",
            )

        elif absorption_state == "ABSORPTION_BEAR" and imbalance_t <= -self.imbalance_threshold:
            entry = best_ask
            sl_dist, units, notional, lev = self.compute_position_sizing(
                entry, account_balance, risk_pct
            )
            zone_txt = f" near S/R {matched_zone:,.4f}" if matched_zone else ""
            self.latest_signal = TradeSetup(
                timestamp=timestamp_sec,
                symbol=symbol,
                direction="SHORT",
                entry_price=entry,
                stop_loss=entry + sl_dist,
                take_profit=entry - (sl_dist * self.rr_ratio),
                position_size_units=units,
                position_notional_usd=notional,
                safe_leverage=lev,
                imbalance=imbalance_t,
                garch_vol=self.garch_sigma,
                reason=f"Bearish Tape Absorption + Ask Stack (I_t={imbalance_t:+.2f}){zone_txt}",
            )

    # -------------------------------------------------------------------------
    # 6. Automated Layman Student Profiles (Low / Medium / High Risk)
    # -------------------------------------------------------------------------
    def _build_student_preset(
        self,
        timestamp_sec: float,
        symbol: str,
        risk_tier: str,
        direction: str,
        entry_price: float,
        imbalance_t: float,
        student_balance: float,
        matched_tf: str,
        matched_level: float,
        confluence_score: int,
    ) -> StudentAutoSignal:
        # Tier configurations: (risk_pct, garch_k_multiplier, max_leverage_cap)
        tier_cfg = {
            "LOW RISK": (0.5, 2.2, 5.0),
            "MEDIUM RISK": (1.0, 1.5, 12.0),
            "HIGH RISK": (2.0, 1.1, 25.0),
        }
        r_pct, k_mult, max_lev = tier_cfg[risk_tier]
        sl_dist, units, notional, safe_lev = self.compute_position_sizing(
            entry_price, student_balance, r_pct, k_mult=k_mult, max_lev_cap=max_lev
        )

        if direction == "LONG":
            stop_loss = entry_price - sl_dist
            tp1 = entry_price + (sl_dist * 1.5)
            tp2 = entry_price + (sl_dist * 2.5)
            tp3 = entry_price + (sl_dist * 4.0)
        else:
            stop_loss = entry_price + sl_dist
            tp1 = entry_price - (sl_dist * 1.5)
            tp2 = entry_price - (sl_dist * 2.5)
            tp3 = entry_price - (sl_dist * 4.0)

        # Probability & Risk Warning synthesis
        is_high_vol = "HIGH" in self.garch_regime
        base_prob = 58 + confluence_score * 6
        if risk_tier == "LOW RISK":
            win_prob = min(base_prob + 6, 84)
        elif risk_tier == "MEDIUM RISK":
            win_prob = min(base_prob, 78)
        else:
            win_prob = max(base_prob - 8, 52)

        if is_high_vol:
            win_prob = max(win_prob - 7, 50)
            warning = "ELEVATED VOLATILITY — STRICT STOP LOSS REQUIRED"
        elif risk_tier == "HIGH RISK":
            warning = "AGGRESSIVE SCALP — FAST EXECUTION & TIGHT STOP"
        elif risk_tier == "MEDIUM RISK":
            warning = "BALANCED INSTITUTIONAL SETUP — MOVE SL TO ENTRY AT TP1"
        else:
            warning = "CONSERVATIVE HIGH-CONFLUENCE SETUP — LOW RISK"

        bias_summary = f"{matched_tf} S/R ({self.tf_bias.get('15m','NEUTRAL')}/{self.tf_bias.get('1H','NEUTRAL')}/{self.tf_bias.get('4H','NEUTRAL')})"

        return StudentAutoSignal(
            timestamp=timestamp_sec,
            symbol=symbol,
            timeframe_bias=bias_summary,
            risk_tier=risk_tier,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            position_size_units=units,
            position_notional_usd=notional,
            safe_leverage=safe_lev,
            win_probability=win_prob,
            risk_warning=warning,
            structure_level=matched_level,
            imbalance=imbalance_t,
            garch_vol=self.garch_sigma,
        )

    def _refresh_live_student_presets(
        self,
        timestamp_sec: float,
        symbol: str,
        mid_price: float,
        best_bid: float,
        best_ask: float,
        imbalance_t: float,
        student_balance: float,
    ) -> None:
        """
        Continuously maintains live-calibrated Low, Medium, and High Risk setup cards
        anchored to the nearest auto-detected 15m/1H/4H structural level and live book skew.
        """
        direction = "LONG" if imbalance_t >= 0 else "SHORT"
        entry = best_bid if direction == "LONG" else best_ask

        # Find nearest auto-detected structural level across 4H, 1H, 15m
        all_zones = self.get_all_auto_zones()
        matched_tf = "15m/1H"
        matched_level = mid_price
        if all_zones:
            best_zone = min(all_zones, key=lambda item: abs(item[1] - mid_price))
            matched_tf, matched_level = best_zone

        # Calculate confluence score (0 to 4)
        score = 0
        if abs(imbalance_t) >= 0.35:
            score += 1
        if abs(imbalance_t) >= 0.50:
            score += 1
        if self.tf_bias.get("1H") == ("BULLISH" if direction == "LONG" else "BEARISH"):
            score += 1
        if self.tf_bias.get("4H") == ("BULLISH" if direction == "LONG" else "BEARISH"):
            score += 1

        for tier in ("LOW RISK", "MEDIUM RISK", "HIGH RISK"):
            self.latest_student_profiles[tier] = self._build_student_preset(
                timestamp_sec=timestamp_sec,
                symbol=symbol,
                risk_tier=tier,
                direction=direction,
                entry_price=entry,
                imbalance_t=imbalance_t,
                student_balance=student_balance,
                matched_tf=matched_tf,
                matched_level=matched_level,
                confluence_score=score,
            )

    def _evaluate_student_auto_signal(
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
        Emits verified automated signals into the live multi-coin student feed
        whenever order book skew + structural proximity + absorption align.
        """
        if (timestamp_sec - self._last_student_emit_ts) < 30.0:
            return

        # Trigger condition: either confirmed absorption OR extreme order book imbalance (>0.48) near auto-structure
        bull_trigger = (absorption_state == "ABSORPTION_BULL" and imbalance_t >= 0.35) or (imbalance_t >= 0.52)
        bear_trigger = (absorption_state == "ABSORPTION_BEAR" and imbalance_t <= -0.35) or (imbalance_t <= -0.52)

        if not (bull_trigger or bear_trigger):
            return

        direction = "LONG" if bull_trigger else "SHORT"
        entry = best_bid if direction == "LONG" else best_ask

        # Check proximity to auto-detected 15m/1H/4H levels (within 1.5 * GARCH sigma)
        all_zones = self.get_all_auto_zones()
        matched_tf = "15m"
        matched_level = mid_price
        near_structure = False
        tol = max(self.garch_sigma * mid_price * 1.5, mid_price * 0.002)

        for tf, lvl in all_zones:
            if abs(mid_price - lvl) <= tol:
                near_structure = True
                matched_tf = tf
                matched_level = lvl
                break

        if all_zones and not near_structure:
            return

        # Choose risk tier classification based on signal strength
        if abs(imbalance_t) >= 0.55 and absorption_state is not None:
            tier = "LOW RISK"
            score = 4
        elif abs(imbalance_t) >= 0.42:
            tier = "MEDIUM RISK"
            score = 3
        else:
            tier = "HIGH RISK"
            score = 2

        sig = self._build_student_preset(
            timestamp_sec=timestamp_sec,
            symbol=symbol,
            risk_tier=tier,
            direction=direction,
            entry_price=entry,
            imbalance_t=imbalance_t,
            student_balance=student_balance,
            matched_tf=matched_tf,
            matched_level=matched_level,
            confluence_score=score,
        )
        self.student_signal_feed.appendleft(sig)
        self._last_student_emit_ts = timestamp_sec
