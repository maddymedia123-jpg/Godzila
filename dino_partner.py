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
    "Speak in polite, plain, natural English—no indicator jargon, no bloated lectures, no headings.\n"
    "Never override the framework's entries, stops, or take-profits.\n\n"
    "On every run:\n"
    "1. Correlate directly with previous_dino_memory so you build on what you said last time and point out what changed.\n"
    "2. Analyze each coin on its own live levels (entry, SL, TP1-TP3, and liquidation cluster).\n"
    "3. For every ACTIVE SIGNAL: give a ONE-LINER with the approaching key level, probability hint, and whether it is GOOD TO GO or STAND DOWN.\n"
    "4. For every COMMITTED TRADE: give a short conversational paragraph (no headings) stating what the live market is doing around key support/resistance, whether a spike is likely just a liquidation wick (so don't panic-close on a wick), when to recheck in PKT, and whether the trade is still GOOD TO GO or SHOULD BE STOPPED.\n\n"
    "Return ONLY valid JSON matching this schema:\n"
    "{\n"
    '  "market_summary": "2 polite conversational sentences on what changed since last check and what the market is doing now",\n'
    '  "active_signal_oneliners": [\n'
    "    {\n"
    '      "target_id": "signal_id",\n'
    '      "label": "SYMBOL [TF] DIR",\n'
    '      "call": "GOOD TO GO | STAND DOWN",\n'
    '      "oneliner": "One clear sentence with key approaching level, hit probability, and whether it is good to go"\n'
    "    }\n"
    "  ],\n"
    '  "committed_trade_reads": [\n'
    "    {\n"
    '      "target_id": "trade_id",\n'
    '      "label": "SYMBOL [TF] DIR",\n'
    '      "call": "GOOD TO GO | STOP / CUT TRADE",\n'
    '      "paragraph": "One concise, polite conversational paragraph (no headings) building on the previous check: approaching levels, liquidation wick warning, PKT recheck time, and clear good-to-go or stop call."\n'
    "    }\n"
    "  ],\n"
    '  "self_improvement_note": "1 short line on what Dino learned from correlating past checks and log outcomes"\n'
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
    def _build_local_desk_brief(
        snap: Dict[str, Any],
        mode_label: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        liq = snap.get("liq_stats", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        dino_memory = snap.get("dino_memory", [])

        mid_px = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))
        sq_msg = metrics.get("squeeze_msg", "")
        mom_msg = metrics.get("momentum_msg", "")
        long_liq = float(liq.get("long_cluster", 0.0))
        short_liq = float(liq.get("short_cluster", 0.0))

        tp_hits = sum(1 for a in audit_archive if a.get("tp1_hit") or a.get("tp2_hit") or a.get("tp3_hit"))
        sl_hits = sum(1 for a in audit_archive if a.get("sl_hit"))
        decided = tp_hits + sl_hits
        prob_str = f"{(tp_hits / decided * 100.0):.0f}%" if decided > 0 else "58%"

        prev_brief = dino_memory[0] if dino_memory else None
        prev_time = prev_brief.get("pkt_str", "") if prev_brief else ""
        prev_px = float(prev_brief.get("anchor_price", 0.0)) if prev_brief else 0.0

        if prev_brief and prev_px > 0 and mid_px > 0:
            delta_pct = ((mid_px - prev_px) / prev_px) * 100.0
            corr_prefix = f"Following up on our {prev_time} check (was {_fmt(prev_px)}, now {_fmt(mid_px)}, {delta_pct:+.2f}%): "
        else:
            corr_prefix = f"Looking at {coin} live around {_fmt(mid_px)}: "

        if sq_msg or mom_msg:
            market_summary = (
                f"{corr_prefix}pressure is building and we need to stay sharp around the nearby liquidation pockets "
                f"({_fmt(long_liq)} below and {_fmt(short_liq)} above)."
            )
        else:
            market_summary = (
                f"{corr_prefix}price is moving steadily with nearby liquidation magnets sitting around "
                f"{_fmt(long_liq)} on the downside and {_fmt(short_liq)} on the upside."
            )

        sig_oneliners: List[Dict[str, Any]] = []
        for sig in prior_signals:
            call_txt = "GOOD TO GO" if not sq_msg else "STAND DOWN"
            sig_oneliners.append({
                "target_id": sig.signal_id,
                "label": f"{sig.symbol} [{sig.timeframe}] {sig.direction}",
                "call": call_txt,
                "oneliner": (
                    f"{call_txt}: {sig.symbol} is approaching {_fmt(sig.entry_price)} toward {_fmt(sig.tp1)} "
                    f"(~{prob_str} hit odds, stop at {_fmt(sig.stop_loss)})—recheck around {sig.eta_tp1_str}."
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
            t_tp2 = float(tr.get("tp2", 0.0))
            eta1 = tr.get("eta_tp1", "next candle close")
            sl_hit = bool(tr.get("sl_hit", False))
            live_p = float(tickers.get(t_sym, {}).get("price", 0.0) or mid_px or t_entry)
            wick_lvl = long_liq if (t_dir == "LONG" and long_liq > 0) else (short_liq if short_liq > 0 else t_sl)

            prev_tr_call = ""
            if prev_brief:
                for old_r in prev_brief.get("committed_trade_reads", []):
                    if old_r.get("target_id") == tr.get("trade_id") or old_r.get("label") == f"{t_sym} [{t_tf}] {t_dir}":
                        prev_tr_call = old_r.get("call", "")
                        break

            if sl_hit or tr.get("squeeze_alert"):
                call_txt = "STOP / CUT TRADE"
                para = (
                    f"Since our last check{f' ({prev_tr_call})' if prev_tr_call else ''}, {t_sym} shifted against us "
                    f"around {_fmt(live_p)} and breached our safety boundary near {_fmt(t_sl)}, so this trade should be stopped. "
                    f"Step aside cleanly and wait for the next qualified setup rather than forcing a recovery."
                )
            else:
                call_txt = "GOOD TO GO"
                change_note = f"Continuing from our {prev_time} check, " if prev_time else ""
                para = (
                    f"{change_note}{t_sym} is trading at {_fmt(live_p)} against our {_fmt(t_entry)} entry and is currently good to go "
                    f"as it works toward {_fmt(t_tp1)} (and {_fmt(t_tp2)} next). Keep an eye on the liquidation level near {_fmt(wick_lvl)}—"
                    f"if price spikes there quickly, treat it as a liquidity wick hunting stops and do not close early unless it holds past {_fmt(t_sl)}; "
                    f"let's recheck around {eta1}."
                )

            trade_reads.append({
                "target_id": tr.get("trade_id", ""),
                "label": f"{t_sym} [{t_tf}] {t_dir}",
                "call": call_txt,
                "paragraph": para,
            })

        imp_note = (
            f"Synced {len(dino_memory)} prior notes and {len(audit_archive)} logged signals ({tp_hits} TP+ / {sl_hits} SL)."
            + (f" [{reason}]" if reason else "")
        )

        now_pkt = datetime.fromtimestamp(time.time() + 18000, tz=timezone.utc).strftime("%a %H:%M PKT").upper()
        bot_msg = (
            f"DINO ({now_pkt})\n{market_summary}\n"
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
            return cls._build_local_desk_brief(snap, mode_label, "Local desk mode")

        coin = snap.get("symbol", "BTCUSDT")
        metrics = snap.get("metrics", {})
        liq = snap.get("liq_stats", {})
        tickers = snap.get("coin_tickers", {})
        active_trades = snap.get("active_trades", [])
        prior_signals = snap.get("prioritized_signals", [])
        audit_archive = snap.get("audit_archive", [])
        dino_memory = snap.get("dino_memory", [])
        mid_px = float(metrics.get("mid_price", 0.0) or tickers.get(coin, {}).get("price", 0.0))

        user_context = {
            "coin": coin,
            "live_price": mid_px,
            "liquidation_levels": {
                "downside_pool": liq.get("long_cluster", 0.0),
                "upside_pool": liq.get("short_cluster", 0.0),
            },
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
                    "summary": d.get("market_summary"),
                    "trades": d.get("committed_trade_reads", []),
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
                    "temperature": 0.3,
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
                f"DINO ({now_pkt})\n{market_summary}\n"
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
                "market_summary": market_summary,
                "active_signal_oneliners": sig_oneliners,
                "committed_trade_reads": trade_reads,
                "self_improvement_note": imp_note,
                "bot_message": bot_msg,
            }
        except Exception as exc:
            return cls._build_local_desk_brief(snap, mode_label, f"Fallback ({exc})")
