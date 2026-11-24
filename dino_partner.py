import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DINO_SYSTEM_PROMPT = """
You are Dino, an experienced perpetual futures trading partner and big brother to the trader.
Never interfere with or override the trader's core signal framework (entries, stop-losses, take-profits, 1H/4H/1D structure, L2 imbalance, 3m CVD, GARCH volatility).
Normal losses are part of trading; help the trader stay disciplined, spot real conflicts or momentum fades, and protect capital.

Whenever you run, you update ALL relevant targets in a single pass and correlate with prior Dino summaries and audit logs so you silently improve your own memory and judgment.

Formatting rules:
1. For every ACTIVE SIGNAL: give a sharp ONE-LINER partner read (action/watch + win/loss edge in one sentence).
2. For every COMMITTED TRADE: give a BRIEF SUMMARY + LAYMAN SUMMARY (2-3 clear sentences covering what to do/watch, how this trade can win, and how it can go into a loss).
3. Include a concise market & correlation summary plus an internal self-improvement note based on prior Dino summaries and logbook outcomes.

Return ONLY valid JSON with this exact schema:
{
  "market_summary": "2 lines on market state, warnings, and correlation with past runs",
  "active_signal_oneliners": [
    {
      "target_id": "signal_id",
      "label": "SYMBOL [TF] DIR",
      "stance": "FAVORABLE | WATCH_CLOSELY | HIGH_CAUTION",
      "win_prob_est": "e.g. 56%",
      "oneliner": "Single crisp sentence: what to do, how it wins, and how it can lose"
    }
  ],
  "committed_trade_reads": [
    {
      "target_id": "trade_id",
      "label": "SYMBOL [TF] DIR",
      "stance": "FAVORABLE | WATCH_CLOSELY | HIGH_CAUTION",
      "win_prob_est": "e.g. 60%",
      "brief_summary": "1 concise line on current position vs SL/TP1-TP3 and flow",
      "layman_summary": "2 plain-English sentences: what to do now, how it can win, and how it can take a loss"
    }
  ],
  "self_improvement_note": "1-2 lines on how Dino correlated prior summaries & logs to refine its memory"
}
""".strip()


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
            if sec.get("api_key"):
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

        warn_txt = " | ".join([w for w in [sq_msg, mom_msg] if w]) or "Market calm / no squeeze or momentum-fade warning."
        market_summary = (
            f"{coin} at ${mid_px:,.2f} (1H/4H/1D: {tf_bias.get('1H','—')}/{tf_bias.get('4H','—')}/{tf_bias.get('1D','—')}, "
            f"RSI {tf_rsi.get('1H',50.0):.0f}, Imb {imb:+.2f}, CVD {cvd:+.1f}) — {warn_txt}"
        )

        sig_oneliners: List[Dict[str, Any]] = []
        for sig in prior_signals:
            sig_oneliners.append({
                "target_id": sig.signal_id,
                "label": f"{sig.symbol} [{sig.timeframe}] {sig.direction}",
                "stance": "WATCH_CLOSELY" if (sq_msg or mom_msg) else "FAVORABLE",
                "win_prob_est": win_est,
                "oneliner": (
                    f"Follow framework entry near ${sig.entry_price:,.2f} (SL ${sig.stop_loss:,.2f}, TP1${sig.tp1:,.2f}): "
                    f"wins if {sig.direction} flow holds, loses if momentum flips or price breaks SL."
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
                "brief_summary": f"Move {move_pct:+.2f}% | Live ${live_p:,.2f} vs Entry${t_entry:,.2f} | SL ${t_sl:,.2f} \vert{} TP1${t_tp1:,.2f}",
                "layman_summary": (
                    f"Stick to the framework levels and avoid emotional interference. "
                    f"This trade can win if price pushes cleanly toward TP1 (${t_tp1:,.2f}) and higher targets with supportive flow, "
                    f"and it can go into a loss if momentum fades against you or price sweeps past SL (${t_sl:,.2f})."
                ),
            })

        prev_note = dino_memory[0].get("self_improvement_note", "") if dino_memory else ""
        imp_note = (
            f"Correlated {len(audit_archive)} logbook records and {len(dino_memory)} prior Dino runs "
            f"({tp_hits} TP+ / {sl_hits} SL)."
            f"{' Prior refinement: ' + prev_note if prev_note else ''}"
            f"{' [' + reason + ']' if reason else ''}"
        )

        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
        bot_msg = (
            f"🦖 DINO ({now_pkt}) · {mode_label}\n{market_summary}\n"
            + "\n".join([f"• Signal {s['label']}: {s['oneliner']}" for s in sig_oneliners])
            + ("\n" if sig_oneliners and trade_reads else "")
            + "\n".join([f"• Trade {t['label']}: {t['brief_summary']} — {t['layman_summary']}" for t in trade_reads])
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
            return cls._build_local_fallback_brief(
                snap, mode_label, reason="No OpenRouter key detected—using local logbook/memory correlation"
            )

        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        tf_bias = snap.get("tf_bias", {})
        tf_rsi = snap.get("tf_rsi", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        audit_reviews = snap.get("audit_reviews", [])
        dino_memory = snap.get("dino_memory", [])

        signals_payload = [
            {
                "id": s.signal_id,
                "symbol": s.symbol,
                "tf": s.timeframe,
                "dir": s.direction,
                "entry": s.entry_price,
                "sl": s.stop_loss,
                "tp1": s.tp1,
                "tp2": s.tp2,
                "tp3": s.tp3,
                "score": s.accuracy_score,
                "squeeze_alert": s.squeeze_alert,
                "early_warning": s.early_warning,
            }
            for s in prior_signals
        ]

        trades_payload = [
            {
                "id": t.get("trade_id"),
                "symbol": t.get("symbol"),
                "tf": t.get("timeframe"),
                "dir": t.get("direction"),
                "entry": t.get("entry_price"),
                "sl": t.get("stop_loss"),
                "tp1": t.get("tp1"),
                "tp2": t.get("tp2"),
                "tp3": t.get("tp3"),
                "live_px": tickers.get(t.get("symbol", ""), {}).get("price", 0.0),
                "tp1_hit": t.get("tp1_hit", False),
                "tp2_hit": t.get("tp2_hit", False),
                "tp3_hit": t.get("tp3_hit", False),
                "sl_hit": t.get("sl_hit", False),
                "squeeze_alert": t.get("squeeze_alert", ""),
            }
            for t in active_trades
        ]

        user_context = {
            "mode_label": mode_label,
            "market": {
                "symbol": coin,
                "mid_price": metrics.get("mid_price", tickers.get(coin, {}).get("price", 0.0)),
                "tf_bias": tf_bias,
                "tf_rsi": tf_rsi,
                "imbalance": metrics.get("imbalance_t", 0.0),
                "cvd_3m": metrics.get("cvd_15s", 0.0),
                "vol_pct": round(float(metrics.get("garch_sigma", 0.0045)) * 100.0, 2),
                "squeeze_msg": metrics.get("squeeze_msg", ""),
                "momentum_msg": metrics.get("momentum_msg", ""),
            },
            "all_active_signals": signals_payload,
            "all_committed_trades": trades_payload,
            "recent_audit_logs": [
                {
                    "symbol": a.get("symbol"),
                    "tf": a.get("timeframe"),
                    "dir": a.get("direction"),
                    "status": a.get("status_note"),
                    "tag": a.get("audit_tag"),
                    "note": a.get("audit_note"),
                }
                for a in audit_archive[:25]
            ],
            "audit_reviews": audit_reviews[:8],
            "previous_dino_memory": [
                {
                    "pkt": d.get("pkt_str"),
                    "market": d.get("market_summary"),
                    "improvement": d.get("self_improvement_note"),
                }
                for d in dino_memory[:6]
            ],
        }

        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {clean_key}",
                    "Content-Type": "application/json",
                    "X-OpenRouter-Title": "Godzilla Dino Partner",
                },
                json={
                    "model": model.strip() or "openrouter/free",
                    "temperature": 0.35,
                    "messages": [
                        {"role": "system", "content": DINO_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(user_context)},
                    ],
                },
                timeout=25,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"].strip()
            if raw_content.startswith("```"):
                raw_content = raw_content.strip("`")
                if raw_content.lower().startswith("json"):
                    raw_content = raw_content[4:].strip()
            parsed = json.loads(raw_content)

            now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
            market_summary = str(parsed.get("market_summary", "")).strip()
            sig_oneliners = parsed.get("active_signal_oneliners", [])
            if not isinstance(sig_oneliners, list):
                sig_oneliners = []
            trade_reads = parsed.get("committed_trade_reads", [])
            if not isinstance(trade_reads, list):
                trade_reads = []
            imp_note = str(parsed.get("self_improvement_note", "")).strip()

            bot_msg = (
                f"🦖 DINO ({now_pkt}) · {mode_label}\n{market_summary}\n"
                + "\n".join([f"• Signal {s.get('label','')}: {s.get('oneliner','')}" for s in sig_oneliners])
                + ("\n" if sig_oneliners and trade_reads else "")
                + "\n".join([
                    f"• Trade {t.get('label','')}: {t.get('brief_summary','')} — {t.get('layman_summary','')}"
                    for t in trade_reads
                ])
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
        except Exception as exc:
            return cls._build_local_fallback_brief(
                snap, mode_label, reason=f"OpenRouter fallback ({exc})"
            )
