import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DINO_SYSTEM_PROMPT = """
You are Dino, a battle-tested perpetual futures trading partner and big brother to the trader.
Your job is to protect the trader's capital and framework discipline—never act like a hype bot, never invent off-framework signals, and never tell the trader to override their core signal engine's entries, stop-losses, or take-profits.

How you think and advise:
1. Respect the Framework: Treat the trader's 1H/4H/1D structure, order-book imbalance, 3m CVD, GARCH volatility, and ATR stops/targets as the core rules. Normal losses happen in real trading; distinguish a normal framework loss from a real flaw or warning gap.
2. Correlate Memory & Logs: Always inspect past audit outcomes, audit notes/tags, and your own previous Dino summaries so you catch recurring mistakes (like chasing into momentum fade or holding against an active squeeze) and refine how you summarize and warn.
3. Practical, Grounded Partner Tone: Speak like an experienced older-brother trader sitting next to them—calm, sharp, honest, and concise.
4. Strict Format:
   - Give a brief market/memory check in 2-3 crisp lines.
   - For each trade or active signal reviewed, give a tight 2-3 line partner read covering: (a) what we should do/watch right now, (b) how this trade can win, and (c) how it can go into a loss.
   - End with one short "Self-Improvement / Memory Note" (1-2 lines) on how you correlated past summaries/logs for next time.

Return ONLY valid JSON with this exact structure:
{
  "market_partner_note": "2-3 line big-brother market & memory read",
  "trade_reads": [
    {
      "target_id": "id or label",
      "label": "SYMBOL [TF] DIR",
      "stance": "FAVORABLE | WATCH_CLOSELY | HIGH_CAUTION",
      "win_prob_est": "e.g. 58%",
      "partner_take": "2-3 lines: what to do, how it wins, and how it can lose"
    }
  ],
  "memory_improvement_note": "1-2 lines on what was correlated from prior logs/summaries"
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
        return ""

    @staticmethod
    def _build_local_fallback_brief(
        snap: Dict[str, Any],
        mode_label: str,
        target_id: Optional[str] = None,
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
        win_rate = f"{(tp_hits / decided * 100.0):.0f}%" if decided > 0 else "early sample"

        warn_part = " | ".join([w for w in [sq_msg, mom_msg] if w]) or "No active squeeze or momentum-fade warning."
        market_note = (
            f"{coin} at ${mid_px:,.2f} (1H/4H/1D: {tf_bias.get('1H','—')}/{tf_bias.get('4H','—')}/{tf_bias.get('1D','—')}, "
            f"1H RSI {tf_rsi.get('1H',50.0):.0f}, Imb {imb:+.2f}, CVD {cvd:+.1f}). {warn_part} "
            f"Logbook check: {tp_hits} TP+ vs {sl_hits} SL ({win_rate})."
        )

        reads: List[Dict[str, Any]] = []
        for tr in active_trades:
            if target_id and tr.get("trade_id") != target_id:
                continue
            t_sym = tr.get("symbol", coin)
            t_tf = tr.get("timeframe", "1H")
            t_dir = tr.get("direction", "LONG")
            t_entry = float(tr.get("entry_price", 0.0))
            t_sl = float(tr.get("stop_loss", 0.0))
            t_tp1 = float(tr.get("tp1", 0.0))
            live_p = float(tickers.get(t_sym, {}).get("price", 0.0) or mid_px or t_entry)
            diff_pct = (((live_p - t_entry) / max(t_entry, 1e-8)) * 100.0) * (1.0 if t_dir == "LONG" else -1.0)

            reads.append({
                "target_id": tr.get("trade_id", ""),
                "label": f"{t_sym} [{t_tf}] {t_dir}",
                "stance": "WATCH_CLOSELY" if (sq_msg or mom_msg or diff_pct < 0) else "FAVORABLE",
                "win_prob_est": win_rate,
                "partner_take": (
                    f"Stick to the framework plan—current pace is {diff_pct:+.2f}% from entry toward TP1 (${t_tp1:,.2f}). "
                    f"It wins if flow and structure stay aligned into TP1–TP3; it loses if momentum fades or price sweeps past SL (${t_sl:,.2f})."
                ),
            })

        for sig in prior_signals:
            if target_id and sig.signal_id != target_id:
                continue
            reads.append({
                "target_id": sig.signal_id,
                "label": f"{sig.symbol} [{sig.timeframe}] {sig.direction}",
                "stance": "WATCH_CLOSELY" if (sq_msg or mom_msg) else "FAVORABLE",
                "win_prob_est": win_rate,
                "partner_take": (
                    f"Framework setup is active around ${sig.entry_price:,.2f} with SL ${sig.stop_loss:,.2f} and TP1${sig.tp1:,.2f}. "
                    f"It wins on clean continuation with order flow backing; it loses if order flow flips against {sig.direction} or volatility whips through SL."
                ),
            })

        prev_note = dino_memory[0].get("memory_improvement_note", "") if dino_memory else ""
        mem_note = (
            f"Correlated {len(audit_archive)} logged signals and {len(dino_memory)} prior Dino runs"
            f"{' (last note: ' + prev_note + ')' if prev_note else ''}."
            f"{' (' + reason + ')' if reason else ''}"
        )

        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
        bot_msg = f"🦖 DINO ({now_pkt}) · {mode_label}\n{market_note}\n" + "\n".join(
            [f"- {r['label']} [{r['stance']}]: {r['partner_take']}" for r in reads[:4]]
        )

        return {
            "id": f"dino_{int(time.time() * 1000)}",
            "timestamp": time.time(),
            "pkt_str": now_pkt,
            "symbol": coin,
            "mode_label": mode_label,
            "target_id": target_id or "ALL",
            "market_partner_note": market_note,
            "trade_reads": reads,
            "memory_improvement_note": mem_note,
            "bot_message": bot_msg,
        }

    @classmethod
    def run_on_demand(
        cls,
        snap: Dict[str, Any],
        api_key: str,
        model: str = "openrouter/free",
        mode_label: str = "ON-DEMAND PARTNER CHECK",
        target_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_key = cls.resolve_api_key(api_key)
        if not clean_key:
            return cls._build_local_fallback_brief(
                snap, mode_label, target_id, reason="No OpenRouter key set yet—using local logbook/memory summary"
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
            if not target_id or s.signal_id == target_id
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
            if not target_id or t.get("trade_id") == target_id
        ]

        recent_logs = [
            {
                "symbol": a.get("symbol"),
                "tf": a.get("timeframe"),
                "dir": a.get("direction"),
                "status": a.get("status_note"),
                "tag": a.get("audit_tag"),
                "note": a.get("audit_note"),
            }
            for a in audit_archive[:25]
        ]

        prev_dino = [
            {
                "pkt": d.get("pkt_str"),
                "symbol": d.get("symbol"),
                "market": d.get("market_partner_note"),
                "improvement": d.get("memory_improvement_note"),
            }
            for d in dino_memory[:6]
        ]

        user_context = {
            "request_mode": mode_label,
            "target_id": target_id,
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
            "active_signals": signals_payload,
            "committed_trades": trades_payload,
            "recent_audit_logs": recent_logs,
            "audit_reviews": audit_reviews[:8],
            "previous_dino_summaries": prev_dino,
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
            market_note = str(parsed.get("market_partner_note", "")).strip()
            reads = parsed.get("trade_reads", [])
            if not isinstance(reads, list):
                reads = []
            mem_note = str(parsed.get("memory_improvement_note", "")).strip()

            bot_msg = f"🦖 DINO ({now_pkt}) · {mode_label}\n{market_note}\n" + "\n".join(
                [f"- {r.get('label','')} [{r.get('stance','')}]: {r.get('partner_take','')}" for r in reads[:5]]
            )

            return {
                "id": f"dino_{int(time.time() * 1000)}",
                "timestamp": time.time(),
                "pkt_str": now_pkt,
                "symbol": coin,
                "mode_label": mode_label,
                "target_id": target_id or "ALL",
                "market_partner_note": market_note,
                "trade_reads": reads,
                "memory_improvement_note": mem_note,
                "bot_message": bot_msg,
            }
        except Exception as exc:
            return cls._build_local_fallback_brief(
                snap, mode_label, target_id, reason=f"OpenRouter call fallback ({exc})"
            )
