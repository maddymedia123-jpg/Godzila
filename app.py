import streamlit as st
import time
from stream_manager import BinanceFuturesStreamManager

# 1. Page Configuration (Must be the very first Streamlit command)
st.set_page_config(
    page_title="Institutional Perpetuals Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling for Institutional Dark Terminal Look
st.markdown("""
    <style>
    .main { background-color: #0e1117; color: #c9d1d9; }
    .stMetric { background-color: #161b22; padding: 15px; border-radius: 6px; border: 1px solid #30363d; }
    </style>
""", unsafe_allow_html=True)

# 2. Initialize Stream Manager in Session State (with Turso DB persistence)
@st.cache_resource
def get_stream_manager():
    return BinanceFuturesStreamManager(active_symbol="BTCUSDT")

manager = get_stream_manager()

# 3. Sidebar Configuration Controls
st.sidebar.title("⚡ Quant Engine Controls")
active_symbol = st.sidebar.selectbox("Asset Universe", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"], index=0)
student_balance = st.sidebar.number_input("Account Capital ($)", value=100.0, step=10.0)
scanner_active = st.sidebar.toggle("Live Signal Scanner", value=True)

st.sidebar.markdown("---")
st.sidebar.subheader("Model Hyperparameters")
decay_lambda = st.sidebar.slider("Decay Lambda ($\lambda$)", 0.05, 1.0, 0.35, 0.05)
imbalance_threshold = st.sidebar.slider("Imbalance Threshold", 0.1, 0.8, 0.40, 0.05)
max_spread_phi = st.sidebar.number_input("Max Spread $\phi$", value=0.0005, format="%.5f")
garch_k = st.sidebar.slider("GARCH Multiplier ($k$)", 1.0, 3.0, 1.5, 0.1)

# Push configuration updates back to the manager engine
manager.update_config(
    active_symbol=active_symbol,
    scanner_active=scanner_active,
    account_balance=25000.0,
    student_balance=student_balance,
    risk_pct=1.0,
    decay_lambda=decay_lambda,
    imbalance_threshold=imbalance_threshold,
    max_spread_phi=max_spread_phi,
    garch_k=garch_k,
    structural_zones=[]
)

# 4. Main Terminal Layout
st.title(f"Institutional Derivatives & Microstructure Terminal — {active_symbol}")

# Fetch live snapshot data from background stream & engine
snapshot = manager.get_snapshot(active_symbol)
metrics = snapshot.get("metrics", {})
liq_stats = snapshot.get("liq_stats", {})

# Top Metrics Row
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Mid Price", f"${metrics.get('mid_price', 0.0):,.2f}")
with col2:
    st.metric("Orderbook Imbalance", f"{metrics.get('imbalance_t', 0.0):+.3f}")
with col3:
    st.metric("GARCH Volatility ($\sigma$)", f"{metrics.get('garch_sigma', 0.0045)*100:.3f}%")
with col4:
    st.metric("WS Message Count", f"{snapshot.get('msg_count', 0):,}")

st.markdown("---")

# 5. Live Signals & Persistence Integration (Turso DB)
st.subheader("📡 Live Candidate & Pinned Trade Signals")
candidate_signals = snapshot.get("candidate_signals", [])

if candidate_signals:
    for idx, sig in enumerate(candidate_signals):
        with st.container():
            c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
            c1.markdown(f"**Symbol:** {sig.symbol} | **{sig.direction}**")
            c2.markdown(f"**Entry:** ${sig.entry_price:,.2f}")
            c3.markdown(f"**Stop Loss:** ${sig.stop_loss:,.2f}")
            c4.markdown(f"**Target 2:** ${sig.tp2:,.2f}")
            
            # Turso persistence button action
            if st.button(f"Commit & Pin Signal #{idx+1}", key=f"commit_{sig.symbol}_{idx}"):
                signal_data = {
                    "symbol": sig.symbol,
                    "direction": sig.direction,
                    "entry": sig.entry_price,
                    "sl": sig.stop_loss,
                    "tp1": sig.tp1,
                    "tp2": sig.tp2,
                    "tp3": sig.tp3,
                    "timestamp": time.time()
                }
                manager.save_state_to_turso(f"pinned_trade_{sig.symbol}", signal_data)
                st.success(f"Successfully committed and synced {sig.symbol} trade to Turso global memory!")
else:
    st.info("Scanning orderbook microstructure for high-probability institutional imbalances...")

# Display persistent saved trades from Turso database
st.markdown("---")
st.subheader("💾 Persistent Cloud State (Turso DB)")
saved_trade = manager.load_state_from_turso(f"pinned_trade_{active_symbol}")
if saved_trade:
    st.json(saved_trade)
else:
    st.caption("No pinned active trades stored in Turso for this asset yet.")

# Auto-refresh mechanism to keep websocket metrics streaming live on screen
time.sleep(1)
st.rerun()
