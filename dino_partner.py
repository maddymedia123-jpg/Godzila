import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
import streamlit as st

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class DinoPartner:
    @staticmethod
    def resolve_api_key(ui_key: str = "") -> str:
        if ui_key and ui_key.strip():
            return ui_key.strip()
        env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if env_key:
            return env_key
        try:
            sec = st.secrets.get("openrouter", {})
            if sec and sec.get("api_key"):
                return str(sec.get("api_key")).strip()
        except Exception:
            pass
        try:
            sec_top = st.secrets.get("OPENROUTER_API_KEY", "")
            if sec_top:
                return str(sec_top).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _build_local_fallback_brief(
        snap: Dict[str, Any],
        mode_label: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        tf_bias = snap.get("tf_bias", {})
        tf_rsi = snap.get("tf_rsi", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        dino_memory = snap.get("dino_memory", [])

        mid_px = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))
        imb = float(metrics.get("imbalance_t", 0.0))
        cvd = float(metrics.get("cvd_15s", 0.0))
        sq_msg = metrics.get("squeeze_msg", "")
        mom_msg = metrics.get("momentum_msg", "")

        tp_hits = sum(1 for a in audit_archive if a.get("tp1_hit") or a.get("tp2_hit") or a.get("tp3_hit"))
        sl_hits = sum(1 for a in audit_archive if a.get("sl_hit"))
        decided = tp_hits + sl_hits
        win_est = f"{(tp_hits / decided * 100.0):.0f}%" if decided > 0 else "50%"

        warn_txt = " | ".join([w for w in [sq_msg, mom_msg] if w]) or "Market calm."
        market_summary = (
            f"{coin} @ ${mid_px:,.2f} (1H/4H/1D: {tf_bias.get('1H', '-')}/{tf_bias.get('4H', '-')}/{tf_bias.get('1D', '-')}, "
            f"RSI {tf_rsi.get('1H', 50.0):.0f}, Imb {imb:+.2f}, CVD {cvd:+.1f}) — {warn_txt}"
        )

        sig_oneliners: List[Dict[str, Any]] = []
        for sig in prior_signals:
            sig_oneliners.append({
                "target_id": sig.signal_id,
                "label": f"{sig.symbol} [{sig.timeframe}] {sig.direction}",
                "stance": "WATCH_CLOSELY" if (sq_msg or mom_msg) else "FAVORABLE",
                "win_prob_est": win_est,
                "oneliner": (
                    f"Entry ${sig.entry_price:,.2f}, SL ${sig.stop_loss:,.2f}, TP1 ${sig.tp1:,.2f}: "
                    f"wins if {sig.direction} flow holds, loses on reversal or SL break."
                ),
            })

        trade_reads: List[Dict[str, Any]] = []
        for tr in active_trades:
            t_sym = tr.get("symbol", coin)
            t_tf = tr.get("timeframe", "1H")
            t_dir = tr.get("direction", "LONG")
            t_entry = float(tr.get("entry_price", 0.0))
            t_sl = float(tr.get("stop_loss", 0.0))
            t_tp1 = float(tr.get("tp1", 0.0))
            live_p = float(tickers.get(t_sym, {}).get("price", 0.0) or mid_px or t_entry)
            move_pct = (((live_p - t_entry) / max(t_entry, 1e-8)) * 100.0) * (1.0 if t_dir == "LONG" else -1.0)

            trade_reads.append({
                "target_id": tr.get("trade_id", ""),
                "label": f"{t_sym} [{t_tf}] {t_dir}",
                "stance": "WATCH_CLOSELY" if (sq_msg or mom_msg or move_pct < 0) else "FAVORABLE",
                "win_prob_est": win_est,
                "brief_summary": (
                    f"Move {move_pct:+.2f}% | Live ${live_p:,.2f} vs Entry ${t_entry:,.2f} | "
                    f"SL ${t_sl:,.2f} | TP1 ${t_tp1:,.2f}"
                ),
                "layman_summary": (
                    f"Follow the plan without overreacting to normal noise. "
                    f"It wins if price reaches TP1 (${t_tp1:,.2f}) and targets, and loses if momentum flips or price hits SL (${t_sl:,.2f})."
                ),
            })

        prev_note = dino_memory[0].get("self_improvement_note", "") if dino_memory else ""
        imp_note = (
            f"Correlated {len(audit_archive)} logs and {len(dino_memory)} Dino runs ({tp_hits} TP+ / {sl_hits} SL)."
            + (f" Prior: {prev_note}" if prev_note else "")
            + (f" [{reason}]" if reason else "")
        )

        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
        bot_msg = (
            f"DINO ({now_pkt}) · {mode_label}\n{market_summary}\n"
            + "\n".join([f"- {s['label']}: {s['oneliner']}" for s in sig_oneliners])
            + ("\n" if sig_oneliners and trade_reads else "")
            + "\n".join([f"- {t['label']}: {t['brief_summary']} — {t['layman_summary']}" for t in trade_reads])
        )

        return {
            "id": f"dino_{int(time.time() * 1000)}",
            "timestamp": time.time(),
            "pkt_str": now_pkt,
            "symbol": coin,
            "mode_label": mode_label,
            "market_summary": market_summary,
            "active_signal_oneliners": sig_oneliners,
            "committed_trade_reads": trade_reads,
            "self_improvement_note": imp_note,
            "bot_message": bot_msg,
        }

    @classmethod
    def run_all_on_demand(
        cls,
        snap: Dict[str, Any],
        api_key: str,
        model: str = "openrouter/free",
        mode_label: str = "FULL ON-DEMAND SYNC",
    ) -> Dict[str, Any]:
        clean_key = cls.resolve_api_key(api_key)
        if not clean_key:
            return cls._build_local_fallback_brief(snap, mode_label, "No OpenRouter key")

        prompt = (
            "You are Dino, a disciplined perp trading partner. Never override framework levels. "
            "Give a one-liner for each active signal and a brief + layman summary (action, how it wins, how it can lose) "
            "for each committed trade, plus a self-improvement note correlating past runs and logs. "
            "Reply in plain clear text.\n"
            f"Context: symbol={snap.get('symbol')}, trades={len(snap.get('active_trades', []))}, "
            f"signals={len(snap.get('prioritized_signals', []))}, logs={len(snap.get('audit_archive', []))}"
        )

        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {clean_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": (model or "openrouter/free").strip(),
                    "temperature": 0.3,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            ai_txt = data["choices"][0]["message"]["content"].strip()
            base = cls._build_local_fallback_brief(snap, mode_label, "OpenRouter synced")
            if ai_txt:
                base["market_summary"] = f"{base['market_summary']} | Dino: {ai_txt[:300]}"
                base["bot_message"] = f"{base['bot_message']}\n\nOpenRouter Read:\n{ai_txt}"
            return base
        except Exception as exc:
            return cls._build_local_fallback_brief(snap, mode_label, f"Fallback: {exc}")
