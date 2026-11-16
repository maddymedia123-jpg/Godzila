import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
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


class QuantEngine:
    """
    Institutional-grade quantitative microstructure & volatility engine.
    Computes distance-weighted L2 imbalance, spoof filters, CVD tape absorption,
    and Student-t GARCH(1,1) risk/leverage scaling.
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

        # Precompute exponential decay weights: w_k = exp(-lambda * (k - 1))
        self.weights = self._compute_weights(decay_lambda, depth_levels)

        # Ring buffers for constant memory footprint
        self.trade_buffer: deque = deque(maxlen=15000)       # (ts, price, qty, is_buyer_maker)
        self.ob_history: deque = deque(maxlen=50)            # (ts, bids_dict, asks_dict, mid_price)
        self.imbalance_history: deque = deque(maxlen=600)    # (ts, mid, I_t, phi_t, cvd_15s, cvd_1m, cvd_5m)
        self.events_log: deque = deque(maxlen=100)           # MicrostructureEvent

        # State tracking
        self.last_spoof_ts: float = 0.0
        self.spoofed_price_levels: Dict[float, float] = {}   # price -> expiry_ts
        self.garch_sigma: float = 0.0045                     # Default fallback 15m vol (0.45%)
        self.garch_last_fit: float = 0.0
        self.garch_regime: str = "CALIBRATING"
        self.latest_signal: Optional[TradeSetup] = None

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
    # 1. GARCH(1,1) Student-t Volatility Model
    # -------------------------------------------------------------------------
    def fit_garch(self, close_prices: List[float]) -> float:
        """
        Fits GARCH(1,1) with Student's t-distribution on 15m log returns (scaled x100).
        Returns forecasted 1-step ahead conditional volatility sigma_t (in decimal return units).
        """
        if len(close_prices) < 100:
            logger.warning("Insufficient candles for GARCH fit; retaining prior sigma.")
            return self.garch_sigma

        try:
            prices = np.asarray(close_prices, dtype=np.float64)
            # Percentage log returns for numerical stability in arch optimizer
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

            # Clamp extreme outliers while preserving regime responsiveness
            self.garch_sigma = float(np.clip(sigma_decimal, 0.0005, 0.05))
            self.garch_last_fit = time.time()

            # Classify regime relative to median historical return std
            unconditional_std = float(np.std(log_returns) / 100.0)
            if self.garch_sigma > unconditional_std * 1.25:
                self.garch_regime = "HIGH VOL — THROTTLED"
            else:
                self.garch_regime = "LOW VOL — FULL SIZE"

            logger.info(
                f"GARCH(1,1)-t updated: sigma_t={self.garch_sigma:.5f} | Regime={self.garch_regime}"
            )
        except Exception as exc:
            logger.error(f"GARCH optimization error: {exc}")

        return self.garch_sigma

    # -------------------------------------------------------------------------
    # 2. Trade Tape Ingestion & Rolling CVD
    # -------------------------------------------------------------------------
    def ingest_trade(self, timestamp_sec: float, price: float, qty: float, is_buyer_maker: bool) -> None:
        """
        Ingests aggTrade ticks.
        is_buyer_maker == True  -> Seller was aggressor (Market Sell -> negative delta)
        is_buyer_maker == False -> Buyer was aggressor (Market Buy -> positive delta)
        """
        self.trade_buffer.append((timestamp_sec, price, qty, is_buyer_maker))

    def compute_rolling_cvd(self, now: float) -> Tuple[float, float, float, float, float]:
        """
        Computes rolling CVD over 15s, 60s, and 300s windows, plus 500ms executed
        buy/sell volume for spoof reconciliation.
        """
        cvd_15s = 0.0
        cvd_1m = 0.0
        cvd_5m = 0.0
        exec_buy_500ms = 0.0
        exec_sell_500ms = 0.0

        cutoff_5m = now - 300.0
        cutoff_1m = now - 60.0
        cutoff_15s = now - 15.0
        cutoff_500ms = now - 0.50

        # Iterate backwards from newest ticks
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
    # 3. Level-2 Depth Processing, Spoof Detection & Absorption Confluence
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

        # Clean expired spoofed price levels
        self.spoofed_price_levels = {
            p: exp for p, exp in self.spoofed_price_levels.items() if exp > timestamp_sec
        }

        cvd_15s, cvd_1m, cvd_5m, exec_buy_500ms, exec_sell_500ms = self.compute_rolling_cvd(timestamp_sec)

        # --- Spoof Filter: Compare current L2 snapshot against ~400-500ms prior ---
        self._detect_spoofs(timestamp_sec, bids, asks, exec_buy_500ms, exec_sell_500ms)

        # Compute raw vs spoof-filtered exponential weighted volumes
        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)

        raw_bid_vols = np.array([b[1] for b in bids[:n_b]], dtype=np.float64)
        raw_ask_vols = np.array([a[1] for a in asks[:n_a]], dtype=np.float64)

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

        # Store snapshot for future delta checks
        bids_map = {p: q for p, q in bids}
        asks_map = {p: q for p, q in asks}
        self.ob_history.append((timestamp_sec, bids_map, asks_map, mid_price))
        self.imbalance_history.append(
            (timestamp_sec, mid_price, imbalance_t, phi_t, cvd_15s, cvd_1m, cvd_5m)
        )

        # --- Absorption Detector ---
        absorption_state = self._detect_absorption(
            timestamp_sec, mid_price, imbalance_t, cvd_15s, w_bid, w_ask
        )

        # --- Co-Pilot Signal Synthesis ---
        spoof_active = (timestamp_sec - self.last_spoof_ts) < self.spoof_suppress_sec
        spread_ok = phi_t <= self.max_spread_phi

        if spread_ok and not spoof_active and absorption_state is not None:
            self._evaluate_trade_setup(
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

        # Find snapshot closest to 450ms ago
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

        # Check vanishing bid walls (resting liquidity pulled without market sell fills)
        for price, old_qty in past_bids.items():
            if old_qty > avg_bid_size * self.spoof_wall_multiplier:
                new_qty = curr_bids.get(price, 0.0)
                vanished = old_qty - new_qty
                if vanished > old_qty * 0.75 and exec_sell_500ms < vanished * 0.25:
                    self._register_spoof(now, "SPOOF_BID", price, vanished)

        # Check vanishing ask walls (resting liquidity pulled without market buy fills)
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

        # Compare against price 3 seconds ago
        past_mid = None
        for ts, m_p, _, _, _, _, _ in reversed(self.imbalance_history):
            if now - ts >= 3.0:
                past_mid = m_p
                break
        if past_mid is None:
            return None

        price_change_pct = (mid_price - past_mid) / past_mid
        # Dynamic volume threshold scaled to top-of-book weighted liquidity
        vol_spike_thresh = max((w_bid + w_ask) * 0.35, 1e-6)

        # Prevent duplicate event spam within 5 seconds
        if self.events_log and (now - self.events_log[0].timestamp) < 5.0:
            if "ABSORPTION" in self.events_log[0].event_type:
                return self.events_log[0].event_type

        # Bullish Absorption: Heavy market selling (CVD_15s << 0) absorbed by strong bid stack (I_t > thresh), price >= past_mid
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

        # Bearish Absorption: Heavy market buying (CVD_15s >> 0) absorbed by strong ask stack (I_t < -thresh), price <= past_mid
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
    # 4. GARCH Volatility-Scaled Sizing & Trade Setup Generation
    # -------------------------------------------------------------------------
    def compute_position_sizing(
        self,
        entry_price: float,
        account_balance: float,
        risk_pct: float,
    ) -> Tuple[float, float, float, float]:
        """
        Computes:
        - Stop Loss distance: SL_dist = k * sigma_t * P_t
        - Position size in base units: Risk_USD / SL_dist
        - Notional USD value
        - Safe Max Leverage capped inversely to sigma_t (with 2x safety buffer before liquidation)
        """
        sl_dist = self.garch_k * self.garch_sigma * entry_price
        sl_dist = max(sl_dist, entry_price * 0.0005)  # Minimum 5 bps floor

        risk_usd = account_balance * (risk_pct / 100.0)
        position_units = risk_usd / sl_dist
        notional_usd = position_units * entry_price

        # Inverse volatility leverage cap: ensures a 2x SL move cannot liquidate margin
        stop_pct = sl_dist / entry_price
        vol_derived_max_lev = 0.50 / max(stop_pct, 1e-4)
        required_lev = notional_usd / max(account_balance, 1.0)
        safe_leverage = float(np.clip(min(vol_derived_max_lev, max(required_lev * 1.25, 1.0)), 1.0, 50.0))

        # Cap notional if required leverage exceeds vol-derived safe ceiling
        if required_lev > vol_derived_max_lev:
            notional_usd = account_balance * vol_derived_max_lev
            position_units = notional_usd / entry_price
            safe_leverage = float(np.clip(vol_derived_max_lev, 1.0, 50.0))

        return sl_dist, position_units, notional_usd, safe_leverage

    def _evaluate_trade_setup(
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
        # Check proximity to user-defined structural S/R zones (within 1.0 * GARCH sigma)
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

        # Cooldown of 15 seconds between identical directional signals
        if self.latest_signal and (timestamp_sec - self.latest_signal.timestamp) < 15.0:
            return

        if absorption_state == "ABSORPTION_BULL" and imbalance_t >= self.imbalance_threshold:
            entry = best_bid  # Post-only passive limit at inside bid
            sl_dist, units, notional, lev = self.compute_position_sizing(
                entry, account_balance, risk_pct
            )
            stop_loss = entry - sl_dist
            take_profit = entry + (sl_dist * self.rr_ratio)
            zone_txt = f" near S/R {matched_zone:,.2f}" if matched_zone else ""
            self.latest_signal = TradeSetup(
                timestamp=timestamp_sec,
                symbol=symbol,
                direction="LONG",
                entry_price=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size_units=units,
                position_notional_usd=notional,
                safe_leverage=lev,
                imbalance=imbalance_t,
                garch_vol=self.garch_sigma,
                reason=f"Bullish Tape Absorption + Bid Stack (I_t={imbalance_t:+.2f}){zone_txt}",
            )

        elif absorption_state == "ABSORPTION_BEAR" and imbalance_t <= -self.imbalance_threshold:
            entry = best_ask  # Post-only passive limit at inside ask
            sl_dist, units, notional, lev = self.compute_position_sizing(
                entry, account_balance, risk_pct
            )
            stop_loss = entry + sl_dist
            take_profit = entry - (sl_dist * self.rr_ratio)
            zone_txt = f" near S/R {matched_zone:,.2f}" if matched_zone else ""
            self.latest_signal = TradeSetup(
                timestamp=timestamp_sec,
                symbol=symbol,
                direction="SHORT",
                entry_price=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size_units=units,
                position_notional_usd=notional,
                safe_leverage=lev,
                imbalance=imbalance_t,
                garch_vol=self.garch_sigma,
                reason=f"Bearish Tape Absorption + Ask Stack (I_t={imbalance_t:+.2f}){zone_txt}",
            )
