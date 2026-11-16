import datetime
import time
from typing import List
import pandas as pd
import streamlit as st
from stream_manager import BinanceFuturesStreamManager, WATCHLIST_SYMBOLS

PKT_TZ = datetime.timezone(datetime.timedelta(hours=5), name="PKT")
UTC_TZ = datetime.timezone.utc


def format_dual_time(ts: float) -> str:
    dt_pkt = datetime.datetime.fromtimestamp(ts, tz=PKT_TZ).strftime("%H:%M:%S PKT")
    dt_utc = datetime.datetime.fromtimestamp(ts, tz=UTC_TZ).strftime("%H:%M:%S UTC")
    return f"{dt_pkt} ({dt_utc})"


st.set_page_config(page_title="Student Committed Signals & Coinglass Terminal", page_icon="⚡", layout="wide")

st.markdown(
    """
<style>
.stApp { background-color: #F1F5F9; color: #0F172A; }
.neutral-card { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 14px 16px; color: #0F172A; }
.preset-card { background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 10px; padding: 18px; color: #0F172A; }
.price-box { background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 0.88rem; margin: 10px 0; }
.price-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #E2E8F0; }
.badge-long { background-color: #DCFCE7; color: #166534; border: 1px solid #86EFAC; padding: 3px 10px; border-radius: 5px; font-weight: 800; font-family: monospace; }
.badge-short { background-color: #FEE2E2; color: #991B1B; border: 1px solid #FCA5A5; padding: 3px 10px; border-radius: 5px; font-weight: 800; font-family: monospace; }
.coinglass-banner { background: linear-gradient(135deg, #0F172A, #1E293B); color: #F8FAFC; border-radius: 10px; padding: 14px 18px; font-family: monospace; font-size: 0.85rem; margin-bottom: 16px; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")


manager = get_stream_manager()


def fmt_price(val: float) -> str:
    if val >= 100: return f"${val:,.2f}"
    elif val >= 1: return f"${val:,.4f}"
    return f"${val:,.5f}"


# Session state for committed trades so they stay locked in memory without flickering
if "committed_trade" not in st.session_state:
    st.session_state.committed_trade = None
if "student_coin" not in st.session_state:
    st.session_state.student_coin = "BTCUSDT"
if "student_balance" not in st.session_state:
    st.session_state.student_balance = 100.0

now_ts = time.time()
st.markdown("### ⚡ Student Committed Signals & Coinglass Liquidation Terminal")
st.caption(f"🕒 Server Clock: **{format_dual_time(now_ts)}**")

s_ctrl1, s_ctrl2 = st.columns([2.2, 1.0])
with s_ctrl1:
    selected_coin = st.segmented_control("Select Asset", options=WATCHLIST_SYMBOLS, default=st.session_state.student_coin)
    if selected_coin: st.session_state.student_coin = selected_coin
with s_ctrl2:
    st.session_state.student_balance = st.number_input("My Account Balance ($)", 50.0, 10_000_000.0, float(st.session_state.student_balance), 50.0)

manager.update_config(
    active_symbol=st.session_state.student_coin, scanner_active=True, account_balance=25000.0,
    student_balance=st.session_state.student_balance, risk_pct=1.0, decay_lambda=0.35,
    imbalance_threshold=0.40, max_spread_phi=0.0005, garch_k=1.5, structural_zones=[]
)


@st.fragment(run_every=1.0)
def render_live_board():
    snap = manager.get_snapshot(st.session_state.student_coin)
    liq = snap.get("liq_stats", {})
    candidates = snap.get("candidate_signals", [])

    # Coinglass Banner
    st.markdown(
        f"<div class='coinglass-banner'>"
        f"<b>📊 COINGLASS DERIVATIVES PULSE ({snap['symbol']}):</b><br>"
        f"📉 Long Liq Cluster: <b style='color:#F87171;'>{fmt_price(liq.get('long_cluster',0))}</b> (${liq.get('long_usd',0):,.1f}M) &nbsp;|&nbsp; "
        f"📈 Short Liq Cluster: <b style='color:#34D399;'>{fmt_price(liq.get('short_cluster',0))}</b> (${liq.get('short_usd',0):,.1f}M)<br>"
        f"⚡ OI Bias: <b>{liq.get('oi_bias','Neutral')}</b>"
        f"</div>",
        unsafe_allow_html=True,
    )

    col_comm, col_cand = st.columns(2)

    # Column 1: COMMITTED TRADE (Pinned in memory, no flickering)
    with col_comm:
        st.markdown("#### 📌 Pinned Committed Trade")
        if st.session_state.committed_trade is not None:
            t = st.session_state.committed_trade
            badge = "badge-long" if t.direction == "LONG" else "badge-short"
            accent = "#15803D" if t.direction == "LONG" else "#B91C1C"

            st.markdown(
                f"<div class='preset-card' style='border-top: 4px solid {accent};'>"
                f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<span style='font-weight:800; color:{accent};'>{t.symbol} · {t.timeframe}</span>"
                f"<span class='{badge}'>{t.direction}</span>"
                f"</div>"
                f"<div style='font-size:0.75rem; color:#475569; font-family:monospace; margin-top:4px;'>Target ETA: {t.eta_pkt_utc}</div>"
                f"<div class='price-box'>"
                f"<div class='price-row'><span>ENTRY:</span><b>{fmt_price(t.entry_price)}</b></div>"
                f"<div class='price-row'><span>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(t.stop_loss)}</b></div>"
                f"<div class='price-row'><span>TP 1:</span><b style='color:#15803D;'>{fmt_price(t.tp1)}</b></div>"
                f"<div class='price-row'><span>TP 2:</span><b style='color:#15803D;'>{fmt_price(t.tp2)}</b></div>"
                f"<div class='price-row'><span>TP 3:</span><b style='color:#15803D;'>{fmt_price(t.tp3)}</b></div>"
                f"</div>"
                f"<b>Margin Required ({t.leverage}x):</b> ${t.margin_required_usd:,.2f}<br>",
                unsafe_allow_html=True,
            )
            if st.button("❌ Close / Clear Committed Trade"):
                st.session_state.committed_trade = None
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("No trade committed yet. Review candidates on the right and click 'Commit' to lock one in.")

    # Column 2: READY CANDIDATES TO COMMIT
    with col_cand:
        st.markdown("#### 🔍 Ready Signal Candidate")
        if candidates:
            c = candidates[0]
            badge = "badge-long" if c.direction == "LONG" else "badge-short"
            accent = "#15803D" if c.direction == "LONG" else "#B91C1C"

            st.markdown(
                f"<div class='preset-card' style='border-top: 4px solid {accent};'>"
                f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
                f"<span style='font-weight:800; color:{accent};'>{c.symbol} · {c.timeframe}</span>"
                f"<span class='{badge}'>{c.direction}</span>"
                f"</div>"
                f"<div class='price-box'>"
                f"<div class='price-row'><span>ENTRY:</span><b>{fmt_price(c.entry_price)}</b></div>"
                f"<div class='price-row'><span>STOP LOSS:</span><b style='color:#B91C1C;'>{fmt_price(c.stop_loss)}</b></div>"
                f"<div class='price-row'><span>TP 1:</span><b style='color:#15803D;'>{fmt_price(c.tp1)}</b></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if st.button("📌 Commit to This Setup"):
                st.session_state.committed_trade = c
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("Scanning order books... A stable candidate will appear here shortly.")


render_live_board()
