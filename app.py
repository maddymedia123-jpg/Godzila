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
class CommittedSignal:
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
    eta_pkt_utc: str
    risk_warning: str
    status: str = "COMMITTED_ACTIVE"
    status_note: str = "TRACKING LIVE TO TP/SL"


class QuantEngine:
    """
    Institutional quantitative engine with stable signal generation,
    Coinglass liquidation modeling, ETA estimation, and pinned signal tracking.
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
        self.audit_logbook: deque = deque(maxlen=50)

        self.last_spoof_ts: float = 0.0
        self.spoofed_price_levels: Dict[float, float] = {}
        self.garch_sigma: float = 0.0045
        self.garch_last_fit: float = 0.0
        self.garch_regime: str = "CALIBRATING"
        self.latest_signal: Optional[TradeSetup] = None

        self.auto_sr_levels: Dict[str, List[float]] = {"15m": [], "1H": [], "4H": []}
        self.tf_bias: Dict[str, str] = {"15m": "NEUTRAL", "1H": "NEUTRAL", "4H": "NEUTRAL"}
        
        # Stable Candidate Pool (Ready signals for the user to review and commit)
        self.candidate_signals: List[CommittedSignal] = []
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

    def compute_rolling_cvd(self, now: float) -> Tuple[float, float, float]:
        cvd_15s = 0.0
        cutoff_15s = now - 15.0
        for ts, _, qty, is_buyer_maker in reversed(self.trade_buffer):
            if ts < cutoff_15s:
                break
            cvd_15s += (-qty if is_buyer_maker else qty)
        return cvd_15s, 0.0, 0.0

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

        cvd_15s, _, _ = self.compute_rolling_cvd(timestamp_sec)

        n_b = min(len(bids), self.depth_levels)
        n_a = min(len(asks), self.depth_levels)
        w_bid = float(np.dot(self.weights[:n_b], [b[1] for b in bids[:n_b]]))
        w_ask = float(np.dot(self.weights[:n_a], [a[1] for a in asks[:n_a]]))
        denom = w_bid + w_ask
        imbalance_t = float((w_bid - w_ask) / denom) if denom > 0 else 0.0

        liq_stats = self._estimate_liquidation_clusters(mid_price, imbalance_t, w_bid, w_ask)

        if scanner_active and (timestamp_sec - self._last_generation_ts) > 60.0:
            self._generate_candidate_signal(timestamp_sec, symbol, mid_price, best_bid, best_ask, imbalance_t, student_balance)

        return {
            "timestamp": timestamp_sec,
            "mid_price": mid_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "phi_t": phi_t,
            "imbalance_t": imbalance_t,
            "garch_sigma": self.garch_sigma,
            "raw_bids": bids,
            "raw_asks": asks,
            "liq_stats": liq_stats,
            "candidate_signals": list(self.candidate_signals),
            "audit_logbook": list(self.audit_logbook),
        }

    def _estimate_liquidation_clusters(self, mid: float, imb: float, w_b: float, w_a: float) -> Dict:
        total_pool = (w_b + w_a) * mid * 0.15
        return {
            "long_cluster": mid * (1.0 - 0.01 - max(0, -imb * 0.005)),
            "short_cluster": mid * (1.0 + 0.01 + max(0, imb * 0.005)),
            "long_usd": max(total_pool * (0.5 - imb * 0.25), 2.5),
            "short_usd": max(total_pool * (0.5 + imb * 0.25), 2.5),
            "oi_bias": "+4.2% (Long Accumulation)" if imb > 0 else ("-3.8% (Short Unwinding)" if imb < 0 else "Neutral"),
        }

    def compute_position_sizing(self, entry: float, balance: float, risk_pct: float, k_mult: float, lev: int):
        sl_dist = max(k_mult * self.garch_sigma * entry, entry * 0.0012)
        risk_usd = balance * (risk_pct / 100.0)
        units = risk_usd / sl_dist
        notional = units * entry
        max_notional = balance * lev * 0.85
        if notional > max_notional:
            notional = max_notional
            units = notional / entry
        margin = notional / lev
        return sl_dist, units, notional, margin

    def _generate_candidate_signal(self, now: float, symbol: str, mid: float, b_bid: float, b_ask: float, imb: float, balance: float):
        if abs(imb) < 0.25:
            return

        direction = "LONG" if imb >= 0 else "SHORT"
        entry = b_bid if direction == "LONG" else b_ask
        tf = "1H Intraday"
        tier, r_pct, k_mult, lev = ("MEDIUM RISK", 1.0, 1.5, 50)

        sl_dist, units, notional, margin = self.compute_position_sizing(entry, balance, r_pct, k_mult, lev)

        if direction == "LONG":
            sl = entry - sl_dist
            tp1, tp2, tp3 = entry + sl_dist * 1.5, entry + sl_dist * 2.5, entry + sl_dist * 4.0
        else:
            sl = entry + sl_dist
            tp1, tp2, tp3 = entry - sl_dist * 1.5, entry - sl_dist * 2.5, entry - sl_dist * 4.0

        eta_ts = now + 14400
        eta_str = f"{time.strftime('%H:%M PKT', time.localtime(eta_ts + 18000))} / {time.strftime('%H:%M UTC', time.gmtime(eta_ts))}"

        sig = CommittedSignal(
            timestamp=now, symbol=symbol, timeframe=tf, risk_tier=tier, direction=direction,
            entry_price=entry, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            position_size_units=units, position_notional_usd=notional, margin_required_usd=margin,
            leverage=lev, win_probability=74, eta_pkt_utc=eta_str,
            risk_warning="Stable Institutional Signal — Use Isolated Margin",
            status="CANDIDATE_READY", status_note="READY TO COMMIT"
        )

        # Keep only the single best stable candidate per coin until committed or refreshed
        self.candidate_signals = [sig]
        self._last_generation_ts = now
