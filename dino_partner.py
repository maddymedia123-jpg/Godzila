import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
import streamlit as st

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DINO_SYSTEM_PROMPT = (
    "You are Dino, a calm, experienced trading partner sitting right next to the trader at the desk.\n"
    "Speak in simple, plain, interactive everyday English—no indicator jargon, no heavy data dumps, "
    "no stiff headings. Talk like a real trader pointing at the screen: 'watch here', 'go here'.\n"
    "Never override the framework's entry, stop-loss, or take-profit levels, and never prescribe an exact "
    "dollar amount—only guide whether the market feels quiet or risky and how cautious to be with committing.\n\n"
    "Every time you run:\n"
    "1. Check previous_dino_memory, reconcile what happened since the last summary, note any misses or "
    "shifts, and update the roadmap.\n"
    "2. Give an overall layman market summary: bear or bullish lean, main confluences, roughly how long "
    "or until what level that trend holds, whether the market is quiet or risky, and whether signals look "
    "healthy or risky.\n"
    "3. Give each coin its OWN coin-specific summary and guidance (never copy BTC's levels onto TAO or alts).\n"
    "4. Give a ONE-LINER for every active signal and a short conversational paragraph for every committed trade "
    "(including wick vs real break guidance and when to recheck in PKT).\n\n"
    "Return ONLY valid JSON matching this exact schema:\n"
    "{\n"
    '  "reconciliation_note": "1-2 plain sentences comparing this run with the previous summary and updating the roadmap",\n'
    '  "market_summary": "A plain layman paragraph covering bear/bullish lean, confluences, trend horizon/invalidation level, quiet vs risky mood, and overall signal quality",\n'
    '  "coin_summaries": {\n'
    '    "BTCUSDT": "Plain coin-specific note: where price is, what to watch, wick/level to respect",\n'
    '    "ETHUSDT": "Plain coin-specific note",\n'
    '    "SOLUSDT": "Plain coin-specific note",\n'
    '    "XRPUSDT": "Plain coin-specific note",\n'
    '    "DOGEUSDT": "Plain coin-specific note",\n'
    '    "TAOUSDT": "Plain TAO-specific note using TAO price & levels only",\n'
    '    "AAVEUSDT": "Plain AAVE-specific note using AAVE price & levels only"\n'
    "  },\n"
    '  "active_signal_oneliners": [\n'
    "    {\n"
    '      "target_id": "signal_id",\n'
    '      "label": "SYMBOL [TF] DIR",\n'
    '      "call": "GOOD TO GO | RISKY / STAND DOWN",\n'
    '      "oneliner": "One clear sentence: watch here, go here, signal quality, and when to recheck"\n'
    "    }\n"
    "  ],\n"
    '  "committed_trade_reads": [\n'
    "    {\n"
    '      "target_id": "trade_id",\n'
    '      "label": "SYMBOL [TF] DIR",\n'
    '      "call": "GOOD TO GO | STOP / CUT TRADE",\n'
    '      "paragraph": "Short conversational paragraph: what changed, watch here/go here, wick vs real break, PKT recheck, and good-to-go or stop call"\n'
    "    }\n"
    "  ],\n"
    '  "self_improvement_note": "1 short line on how Dino refined its future roadmap from past summaries and logs"\n'
    "}"
)


def _fmt(val: float) -> str:
    if val <= 0:
        return "$0.00"
    if val < 1.0:
        return f"${val:,.4f}"
    if val < 100.0:
        return f"${val:,.3f}"
    return f"${val:,.2f}"


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
    def _coin_liq_pair(px: float, vol_pct: float = 1.5) -> tuple[float, float]:
        if px <= 0:
            return 0.0, 0.0
        band = max(0.012, min(0.08, vol_pct / 100.0 * 1.4))
        return round(px * (1.0 - band), 6), round(px * (1.0 + band), 6)

    @classmethod
    def _build_local_desk_brief(
        cls,
        snap: Dict[str, Any],
        mode_label: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        dino_memory = snap.get("dino_memory", [])

        mid_px = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))
        sq_msg = metrics.get("squeeze_msg", "")
        mom_msg = metrics.get("momentum_msg", "")
        vol_pct = float(metrics.get("garch_sigma", 0.0045)) * 100.0

        liq = snap.get("liq_stats", {})
        long_liq = float(liq.get("long_cluster", 0.0) or 0.0)
        short_liq = float(liq.get("short_cluster", 0.0) or 0.0)
        if mid_px > 0 and (long_liq <= 0 or abs(long_liq - mid_px) / mid_px > 0.35):
            long_liq, short_liq = cls._coin_liq_pair(mid_px, vol_pct)

        prev_brief = dino_memory[0] if dino_memory else None
        prev_time = prev_brief.get("pkt_str", "") if prev_brief else ""
        prev_px = float(prev_brief.get("anchor_price", 0.0)) if prev_brief else 0.0
        prev_coin = prev_brief.get("symbol", coin) if prev_brief else coin

        if prev_brief and prev_coin == coin and prev_px > 0 and mid_px > 0:
            delta_pct = ((mid_px - prev_px) / prev_px) * 100.0
            reconciliation_note = (
                f"Checking back against our {prev_time} note on {coin} ({_fmt(prev_px)} then vs {_fmt(mid_px)} now, {delta_pct:+.2f}%): "
                f"we keep the same roadmap and watch how price reacts around {_fmt(long_liq)} and {_fmt(short_liq)} before committing more."
            )
        elif prev_brief:
            reconciliation_note = (
                f"Reconciling with our last check at {prev_time}: we're carrying forward our earlier roadmap and updating "
                f"what to watch right now on {coin} around {_fmt(mid_px)}."
            )
        else:
            reconciliation_note = (
                f"Starting a fresh roadmap for {coin} around {_fmt(mid_px)}—every rerun from here will compare against this note "
                f"and tighten what we watch next."
            )

        mood_txt = "risky and jumpy—keep position sizing light and be picky with signals" if (sq_msg or mom_msg) else (
            "relatively calm and orderly—good setups can play out if you wait for clean levels"
        )
        market_summary = (
            f"Right now the overall tape for {coin} around {_fmt(mid_px)} feels {mood_txt}. "
            f"Watch {_fmt(long_liq)} below and {_fmt(short_liq)} above—if price sweeps into a liquidation pocket and snaps back, "
            f"don't panic-close on a wick; only treat a clean hold past that zone as a real trend shift."
        )

        coin_summaries: Dict[str, str] = {}
        for sym, t_info in tickers.items():
            s_px = float(t_info.get("price", 0.0) or 0.0)
            s_low, s_high = cls._coin_liq_pair(s_px, vol_pct)
            if s_px > 0:
                coin_summaries[sym] = (
                    f"{sym} is sitting near {_fmt(s_px)}—watch {_fmt(s_low)} below and {_fmt(s_high)} above, "
                    f"and treat quick pokes into those zones as possible wick sweeps unless price holds there."
                )
            else:
                coin_summaries[sym] = f"{sym} is waiting on fresh price ticks—watch its own key support/resistance before committing."

        sig_oneliners: List[Dict[str, Any]] = []
        for sig in prior_signals:
            call_txt = "RISKY / STAND DOWN" if sq_msg else "GOOD TO GO"
            sig_oneliners.append({
                "target_id": sig.signal_id,
                "label": f"{sig.symbol} [{sig.timeframe}] {sig.direction}",
                "call": call_txt,
                "oneliner": (
                    f"{call_txt}: watch {_fmt(sig.entry_price)} to go {sig.direction} toward {_fmt(sig.tp1)} "
                    f"(stop {_fmt(sig.stop_loss)}), and recheck around {sig.eta_tp1_str}."
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
            eta1 = tr.get("eta_tp1", "next candle close")
            sl_hit = bool(tr.get("sl_hit", False))
            live_p = float(tickers.get(t_sym, {}).get("price", 0.0) or (mid_px if t_sym == coin else t_entry))
            t_wick_low, t_wick_high = cls._coin_liq_pair(live_p, vol_pct)
            wick_watch = t_wick_low if t_dir == "LONG" else t_wick_high

            if sl_hit or tr.get("squeeze_alert"):
                call_txt = "STOP / CUT TRADE"
                para = (
                    f"Hey, looking at {t_sym} around {_fmt(live_p)}: price has pushed past our safety line near {_fmt(t_sl)}, "
                    f"so this trade should be stopped. Take the cut cleanly and wait for a fresh setup rather than hoping a wick saves it."
                )
            else:
                call_txt = "GOOD TO GO"
                para = (
                    f"Hey, {t_sym} is trading around {_fmt(live_p)} from our {_fmt(t_entry)} entry and is still good to go toward {_fmt(t_tp1)}. "
                    f"Watch {_fmt(wick_watch)} closely—if price spikes there fast, that can be a liquidation wick hunting stops, so don't close "
                    f"on a wick unless price stays past {_fmt(t_sl)}; let's recheck around {eta1}."
                )

            trade_reads.append({
                "target_id": tr.get("trade_id", ""),
                "label": f"{t_sym} [{t_tf}] {t_dir}",
                "call": call_txt,
                "paragraph": para,
            })

        tp_hits = sum(1 for a in audit_archive if a.get("tp1_hit") or a.get("tp2_hit") or a.get("tp3_hit"))
        sl_hits = sum(1 for a in audit_archive if a.get("sl_hit"))
        imp_note = (
            f"Memory updated ({len(dino_memory) + 1} runs · {len(audit_archive)} logged signals: {tp_hits} TP+ / {sl_hits} SL)."
            + (f" [{reason}]" if reason else "")
        )

        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
        bot_msg = (
            f"DINO ({now_pkt})\n{market_summary}\nRoadmap: {reconciliation_note}\n"
            + "\n".join([f"- {s['label']}: {s['oneliner']}" for s in sig_oneliners])
            + ("\n" if sig_oneliners and trade_reads else "")
            + "\n".join([f"- {t['label']} [{t['call']}]: {t['paragraph']}" for t in trade_reads])
        )

        return {
            "id": f"dino_{int(time.time() * 1000)}",
            "timestamp": time.time(),
            "pkt_str": now_pkt,
            "symbol": coin,
            "anchor_price": mid_px,
            "mode_label": mode_label,
            "reconciliation_note": reconciliation_note,
            "market_summary": market_summary,
            "coin_summaries": coin_summaries,
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
            return cls._build_local_desk_brief(snap, mode_label, "Local desk partner mode")

        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        dino_memory = snap.get("dino_memory", [])
        mid_px = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))

        user_context = {
            "active_coin": coin,
            "live_price": mid_px,
            "all_watchlist_prices": {k: v.get("price", 0.0) for k, v in tickers.items()},
            "warnings": {
                "squeeze": metrics.get("squeeze_msg", ""),
                "momentum": metrics.get("momentum_msg", ""),
            },
            "active_signals": [
                {
                    "id": s.signal_id,
                    "label": f"{s.symbol} [{s.timeframe}] {s.direction}",
                    "entry": s.entry_price,
                    "sl": s.stop_loss,
                    "tp1": s.tp1,
                    "tp2": s.tp2,
                    "recheck_eta": s.eta_tp1_str,
                }
                for s in prior_signals
            ],
            "committed_trades": [
                {
                    "id": t.get("trade_id"),
                    "label": f"{t.get('symbol')} [{t.get('timeframe')}] {t.get('direction')}",
                    "live_px": tickers.get(t.get("symbol", ""), {}).get("price", 0.0),
                    "entry": t.get("entry_price"),
                    "sl": t.get("stop_loss"),
                    "tp1": t.get("tp1"),
                    "tp2": t.get("tp2"),
                    "recheck_eta": t.get("eta_tp1", ""),
                    "sl_hit": t.get("sl_hit", False),
                    "squeeze_alert": t.get("squeeze_alert", ""),
                }
                for t in active_trades
            ],
            "previous_dino_memory": [
                {
                    "pkt": d.get("pkt_str"),
                    "coin": d.get("symbol"),
                    "anchor_price": d.get("anchor_price"),
                    "reconciliation": d.get("reconciliation_note"),
                    "summary": d.get("market_summary"),
                }
                for d in dino_memory[:4]
            ],
            "audit_stats": {
                "total_logged": len(audit_archive),
                "tp_hits": sum(1 for a in audit_archive if a.get("tp1_hit") or a.get("tp2_hit") or a.get("tp3_hit")),
                "sl_hits": sum(1 for a in audit_archive if a.get("sl_hit")),
            },
        }

        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {clean_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": (model or "openrouter/free").strip(),
                    "temperature": 0.35,
                    "messages": [
                        {"role": "system", "content": DINO_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(user_context)},
                    ],
                },
                timeout=22,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"].strip()
            if raw_content.startswith("```"):
                raw_content = raw_content.strip("`")
                if raw_content.lower().startswith("json"):
                    raw_content = raw_content[4:].strip()
            parsed = json.loads(raw_content)

            fallback = cls._build_local_desk_brief(snap, mode_label, "OpenRouter synced")
            now_pkt = fallback["pkt_str"]
            reconciliation_note = str(parsed.get("reconciliation_note", "")).strip() or fallback["reconciliation_note"]
            market_summary = str(parsed.get("market_summary", "")).strip() or fallback["market_summary"]
            coin_summaries = parsed.get("coin_summaries", {})
            if not isinstance(coin_summaries, dict):
                coin_summaries = fallback["coin_summaries"]
            else:
                merged_cs = dict(fallback["coin_summaries"])
                for k, v in coin_summaries.items():
                    if isinstance(v, str) and v.strip():
                        merged_cs[str(k).upper()] = v.strip()
                coin_summaries = merged_cs

            sig_oneliners = parsed.get("active_signal_oneliners", [])
            if not isinstance(sig_oneliners, list):
                sig_oneliners = fallback["active_signal_oneliners"]
            trade_reads = parsed.get("committed_trade_reads", [])
            if not isinstance(trade_reads, list):
                trade_reads = fallback["committed_trade_reads"]
            imp_note = str(parsed.get("self_improvement_note", "")).strip() or fallback["self_improvement_note"]

            bot_msg = (
                f"DINO ({now_pkt})\n{market_summary}\nRoadmap: {reconciliation_note}\n"
                + "\n".join([f"- {s.get('label', '')}: {s.get('oneliner', '')}" for s in sig_oneliners])
                + ("\n" if sig_oneliners and trade_reads else "")
                + "\n".join([f"- {t.get('label', '')} [{t.get('call', '')}]: {t.get('paragraph', '')}" for t in trade_reads])
            )

            return {
                "id": f"dino_{int(time.time() * 1000)}",
                "timestamp": time.time(),
                "pkt_str": now_pkt,
                "symbol": coin,
                "anchor_price": mid_px,
                "mode_label": mode_label,
                "reconciliation_note": reconciliation_note,
                "market_summary": market_summary,
                "coin_summaries": coin_summaries,
                "active_signal_oneliners": sig_oneliners,
                "committed_trade_reads": trade_reads,
                "self_improvement_note": imp_note,
                "bot_message": bot_msg,
            }
        except Exception as exc:
            return cls._build_local_desk_brief(snap, mode_label, f"Fallback ({exc})")
