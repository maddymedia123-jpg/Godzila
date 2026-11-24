import time
from datetime import datetime, timezone
from typing import Any, Dict, List


class AIPartnerAgent:
    """
    Advisory partner that inspects live market state, committed trades, and
    historical audit logs to give plain-language summaries, timeline checks,
    and learned win-probability estimates without altering core signal logic.
    """

    @staticmethod
    def build_learned_memory(audit_archive: List[Dict[str, Any]], audit_reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
        tp_wins = 0
        sl_losses = 0
        by_tf_dir: Dict[str, Dict[str, int]] = {}
        lessons: List[str] = []

        for item in audit_archive:
            tf = item.get("timeframe", "1H")
            direction = item.get("direction", "LONG")
            key = f"{tf}_{direction}"
            by_tf_dir.setdefault(key, {"wins": 0, "losses": 0})

            if item.get("sl_hit"):
                sl_losses += 1
                by_tf_dir[key]["losses"] += 1
            elif item.get("tp1_hit") or item.get("tp2_hit") or item.get("tp3_hit"):
                tp_wins += 1
                by_tf_dir[key]["wins"] += 1

        for rev in audit_reviews[:10]:
            note = (rev.get("note") or "").strip()
            tag = rev.get("tag", "")
            if note:
                lessons.append(f"[{tag}] {note}")

        decided = tp_wins + sl_losses
        base_win_rate = (tp_wins / decided * 100.0) if decided > 0 else 50.0

        return {
            "decided_trades": decided,
            "tp_wins": tp_wins,
            "sl_losses": sl_losses,
            "learned_win_rate_pct": round(base_win_rate, 1),
            "by_tf_dir": by_tf_dir,
            "learned_lessons": lessons,
        }

    @classmethod
    def estimate_win_probability(
        cls,
        direction: str,
        timeframe: str,
        imb: float,
        cvd_3m: float,
        squeeze_state: str,
        momentum_state: str,
        memory: Dict[str, Any],
    ) -> float:
        score = float(memory.get("learned_win_rate_pct", 50.0))

        # Adjust gently by live flow & early warnings (advisory only)
        if direction == "LONG":
            score += 6.0 if (imb > 0.20 and cvd_3m > 0) else -6.0
            if squeeze_state == "LONG_SQUEEZE_RISK":
                score -= 12.0
            if momentum_state == "BULLISH_MOMENTUM_FADE":
                score -= 8.0
        else:
            score += 6.0 if (imb < -0.20 and cvd_3m < 0) else -6.0
            if squeeze_state == "SHORT_SQUEEZE_RISK":
                score -= 12.0
            if momentum_state == "BEARISH_MOMENTUM_FADE":
                score -= 8.0

        if timeframe == "4H":
            score += 4.0

        return round(max(15.0, min(88.0, score)), 1)

    @classmethod
    def generate_partner_brief(cls, snap: Dict[str, Any]) -> Dict[str, Any]:
        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        tf_bias = snap.get("tf_bias", {})
        tf_rsi = snap.get("tf_rsi", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        audit_archive = snap.get("audit_archive", [])
        audit_reviews = snap.get("audit_reviews", [])

        mid_price = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))
        imb = float(metrics.get("imbalance_t", 0.0))
        cvd_3m = float(metrics.get("cvd_15s", 0.0))
        vol_pct = float(metrics.get("garch_sigma", 0.0045)) * 100.0
        squeeze_msg = metrics.get("squeeze_msg", "")
        momentum_msg = metrics.get("momentum_msg", "")
        squeeze_state = metrics.get("squeeze_state", "NORMAL")
        momentum_state = metrics.get("momentum_state", "STEADY")

        memory = cls.build_learned_memory(audit_archive, audit_reviews)
        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()

        # Market overview in plain language
        market_summary = (
            f"{coin} is trading near ${mid_price:,.2f} with 1H/4H/1D bias at "
            f"{tf_bias.get('1H', '—')}/{tf_bias.get('4H', '—')}/{tf_bias.get('1D', '—')}, "
            f"1H RSI at {tf_rsi.get('1H', 50.0):.1f}, order-book imbalance at {imb:+.2f}, "
            f"3m CVD at {cvd_3m:+.1f}, and expected volatility around {vol_pct:.2f}%."
        )

        warnings: List[str] = []
        if squeeze_msg:
            warnings.append(squeeze_msg)
        if momentum_msg:
            warnings.append(momentum_msg)
        if not warnings:
            warnings.append("No active squeeze or momentum-fade warning right now; conditions look relatively calm.")

        trade_notes: List[Dict[str, Any]] = []
        for tr in active_trades:
            t_sym = tr.get("symbol", "BTCUSDT")
            t_dir = tr.get("direction", "LONG")
            t_tf = tr.get("timeframe", "1H")
            t_entry = float(tr.get("entry_price", 0.0))
            t_sl = float(tr.get("stop_loss", 0.0))
            t_tp1 = float(tr.get("tp1", 0.0))
            live_px = float(tickers.get(t_sym, {}).get("price", 0.0) or t_entry)

            win_prob = cls.estimate_win_probability(
                t_dir, t_tf, imb, cvd_3m, squeeze_state, momentum_state, memory
            )

            if t_dir == "LONG":
                pnl_pct = ((live_px - t_entry) / max(t_entry, 1e-8)) * 100.0
                sl_dist_pct = ((live_px - t_sl) / max(t_entry, 1e-8)) * 100.0
            else:
                pnl_pct = ((t_entry - live_px) / max(t_entry, 1e-8)) * 100.0
                sl_dist_pct = ((t_sl - live_px) / max(t_entry, 1e-8)) * 100.0

            if tr.get("sl_hit"):
                layman = f"{t_sym} [{t_tf}] {t_dir} hit its stop-loss. Framework risk protection capped the downside."
            elif tr.get("tp3_hit"):
                layman = f"{t_sym} [{t_tf}] {t_dir} reached TP3—full target sequence completed."
            elif pnl_pct >= 0:
                layman = (
                    f"{t_sym} [{t_tf}] {t_dir} is currently +{pnl_pct:.2f}% in your favor toward TP1 (${t_tp1:,.2f}). "
                    f"Buffer to SL is ~{abs(sl_dist_pct):.2f}%. Expected TP1 window: {tr.get('eta_tp1', '~')}."
                )
            else:
                layman = (
                    f"{t_sym} [{t_tf}] {t_dir} is currently {pnl_pct:+.2f}% against entry; buffer to SL (${t_sl:,.2f}) "
                    f"is ~{abs(sl_dist_pct):.2f}%. Watch momentum and TP1 ETA ({tr.get('eta_tp1', '~')}) before acting outside the plan."
                )

            trade_notes.append({
                "trade_id": tr.get("trade_id"),
                "label": f"{t_sym} [{t_tf}] {t_dir}",
                "win_prob_pct": win_prob,
                "timeline": f"TP1: {tr.get('eta_tp1', '—')} | TP2: {tr.get('eta_tp2', '—')} | TP3: {tr.get('eta_tp3', '—')}",
                "layman": layman,
            })

        bot_text = (
            f"🦖 GODZILLA AI PARTNER ({now_pkt})\n"
            f"Market: {market_summary}\n"
            f"Alerts: {' | '.join(warnings)}\n"
            f"Memory Win Rate: {memory['learned_win_rate_pct']}% ({memory['decided_trades']} decided logged trades)"
        )

        return {
            "timestamp_pkt": now_pkt,
            "market_summary": market_summary,
            "warnings": warnings,
            "memory": memory,
            "trade_notes": trade_notes,
            "bot_text": bot_text,
        }
