# dashboard/app.py
"""
Streamlit Dashboard — Trading Bot Monitor
รันด้วย: streamlit run dashboard/app.py --server.port 8501
เปิดจาก: http://VPS_IP:8501

อ่านข้อมูลจาก:
  - db/trades.db      (SQLite)
  - logs/account.json (real-time snapshot)
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import json
import yaml
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Page Config ────────────────────────────────────────────────
st.set_page_config(
    page_title = "AURUM BOT Dashboard",
    page_icon  = "📊",
    layout     = "wide",
    initial_sidebar_state = "collapsed",
)

# ── Load Config ────────────────────────────────────────────────
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

DB_PATH      = Path(CFG['paths']['db'])
ACCOUNT_JSON = Path(CFG['paths']['logs']) / "account.json"
REFRESH_SEC  = CFG['dashboard']['refresh_sec']

# ── Dark Theme CSS ─────────────────────────────────────────────
st.markdown("""
<style>
  /* Dark background */
  .stApp { background:#0a0b0d; color:#e2e8f0; }
  [data-testid="stSidebar"] { background:#0f1115; }

  /* Metric cards */
  [data-testid="metric-container"] {
    background    : #151820;
    border        : 1px solid rgba(240,180,41,0.15);
    border-radius : 8px;
    padding       : 12px;
  }
  [data-testid="stMetricValue"] {
    color       : #f0b429;
    font-family : monospace;
    font-size   : 24px !important;
  }
  [data-testid="stMetricDelta"] { font-family: monospace; }

  /* DataFrames */
  .dataframe {
    background  : #151820 !important;
    font-family : monospace;
    font-size   : 12px;
  }

  /* Headers */
  h1,h2,h3 {
    font-family : sans-serif;
    color       : #f0b429 !important;
  }

  /* Divider */
  hr { border-color: rgba(240,180,41,0.2); }

  /* Plotly dark */
  .js-plotly-plot .plotly { background: #0f1115; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# Data Loading (cached)
# ══════════════════════════════════════════════════════════════
@st.cache_data(ttl=REFRESH_SEC)
def load_trades(days_back: int = 90) -> pd.DataFrame:
    """โหลด closed trades จาก SQLite"""
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("""
        SELECT *
        FROM   trades
        WHERE  close_time IS NOT NULL
          AND  open_time >= datetime('now', ?)
        ORDER  BY close_time DESC
    """, conn, params=[f"-{days_back} days"])
    conn.close()

    if df.empty:
        return df

    for col in ['open_time','close_time']:
        df[col] = pd.to_datetime(df[col], utc=True, errors='coerce')

    df['pnl_cumsum'] = (
        df.sort_values('close_time')['profit'].cumsum()
    )
    df['date'] = df['close_time'].dt.date

    return df


@st.cache_data(ttl=REFRESH_SEC)
def load_equity(days_back: int = 90) -> pd.DataFrame:
    """โหลด account snapshots"""
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("""
        SELECT ts, balance, equity, profit, open_trades
        FROM   account_snapshots
        WHERE  ts >= datetime('now', ?)
        ORDER  BY ts ASC
    """, conn, params=[f"-{days_back} days"])
    conn.close()

    if not df.empty:
        df['ts'] = pd.to_datetime(df['ts'], utc=True, errors='coerce')

    return df


@st.cache_data(ttl=30)
def load_account() -> dict:
    """โหลด account snapshot ล่าสุด (refresh บ่อย)"""
    if not ACCOUNT_JSON.exists():
        return {
            'balance': 0, 'equity': 0, 'profit': 0,
            'free_margin': 0, 'open_trades': 0,
            'updated_at': 'N/A',
        }
    try:
        return json.loads(ACCOUNT_JSON.read_text())
    except Exception:
        return {}


@st.cache_data(ttl=REFRESH_SEC)
def load_signals(days_back: int = 7) -> pd.DataFrame:
    """โหลด ML signals"""
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("""
        SELECT ts, symbol, signal, confidence, regime, session
        FROM   signals
        WHERE  ts >= datetime('now', ?)
        ORDER  BY ts DESC
        LIMIT  500
    """, conn, params=[f"-{days_back} days"])
    conn.close()

    if not df.empty:
        df['ts'] = pd.to_datetime(df['ts'], utc=True, errors='coerce')

    return df


@st.cache_data(ttl=REFRESH_SEC)
def load_daily_summary() -> pd.DataFrame:
    """โหลด daily summary"""
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("""
        SELECT *
        FROM   daily_summary
        ORDER  BY date DESC
        LIMIT  30
    """, conn)
    conn.close()
    return df


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════
def _color_pnl(val: float) -> str:
    if val > 0:  return "#06d6a0"
    if val < 0:  return "#ef476f"
    return "#94a3b8"

def _plotly_layout(title: str = "", height: int = 300) -> dict:
    """Dark theme layout สำหรับ Plotly"""
    return dict(
        title           = dict(text=title, font=dict(color="#f0b429")),
        paper_bgcolor   = "#151820",
        plot_bgcolor    = "#0f1115",
        font            = dict(color="#94a3b8", family="monospace"),
        xaxis           = dict(gridcolor="#1c2030", showgrid=True,
                               linecolor="#1c2030"),
        yaxis           = dict(gridcolor="#1c2030", showgrid=True,
                               linecolor="#1c2030"),
        height          = height,
        margin          = dict(l=0, r=0, t=40, b=0),
        showlegend      = False,
    )

def _fmt_pnl(val: float) -> str:
    return f"${val:+,.2f}"

def _delta_color(val: float) -> str:
    return "normal" if val >= 0 else "inverse"


# ══════════════════════════════════════════════════════════════
# Main Dashboard
# ══════════════════════════════════════════════════════════════
def main():
    # ── Header ────────────────────────────────────────────────
    col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
    with col_h1:
        st.markdown("## 🤖 AURUM BOT — Trading Dashboard")
    with col_h2:
        now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        st.markdown(
            f"<div style='text-align:right;color:#94a3b8;"
            f"font-family:monospace;margin-top:8px'>"
            f"⏱ {now_utc}</div>",
            unsafe_allow_html=True,
        )
    with col_h3:
        if st.button("🔄 Refresh"):
            st.cache_data.clear()
            st.rerun()

    st.divider()

    # ── Load Data ─────────────────────────────────────────────
    acc    = load_account()
    trades = load_trades(days_back=90)
    equity = load_equity(days_back=90)

    # ── Tabs ──────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 Overview",
        "📋 Trades",
        "🧠 Signals",
        "📊 Analysis",
    ])

    # ══════════════════════════════════════════════════════════
    # Tab 1: Overview
    # ══════════════════════════════════════════════════════════
    with tab1:
        _render_overview(acc, trades, equity)

    # ══════════════════════════════════════════════════════════
    # Tab 2: Trades
    # ══════════════════════════════════════════════════════════
    with tab2:
        _render_trades(trades)

    # ══════════════════════════════════════════════════════════
    # Tab 3: Signals
    # ══════════════════════════════════════════════════════════
    with tab3:
        _render_signals()

    # ══════════════════════════════════════════════════════════
    # Tab 4: Analysis
    # ══════════════════════════════════════════════════════════
    with tab4:
        _render_analysis(trades)

    # ── Auto Refresh ──────────────────────────────────────────
    st.markdown(
        f"<div style='text-align:center;color:#4a5568;"
        f"font-size:11px;margin-top:20px'>"
        f"Auto refresh ทุก {REFRESH_SEC} วินาที</div>",
        unsafe_allow_html=True,
    )

    import time
    time.sleep(REFRESH_SEC)
    st.rerun()


# ══════════════════════════════════════════════════════════════
# Tab 1 — Overview
# ══════════════════════════════════════════════════════════════
def _render_overview(acc, trades, equity):
    # ── Metric Cards ──────────────────────────────────────────
    wins      = trades[trades['profit'] > 0] if not trades.empty else pd.DataFrame()
    total_pnl = trades['profit'].sum() if not trades.empty else 0
    win_rate  = len(wins) / max(len(trades), 1) * 100
    gw        = wins['profit'].sum() if not wins.empty else 0
    gl        = abs(trades[trades['profit'] < 0]['profit'].sum()) if not trades.empty else 0
    pf        = gw / gl if gl > 0 else 0

    m1,m2,m3,m4,m5,m6 = st.columns(6)
    m1.metric(
        "Balance",
        f"${acc.get('balance',0):,.2f}",
        delta=f"{acc.get('profit',0):+.2f}",
        delta_color=_delta_color(acc.get('profit',0)),
    )
    m2.metric(
        "Equity",
        f"${acc.get('equity',0):,.2f}",
    )
    m3.metric(
        "Total P&L (90d)",
        _fmt_pnl(total_pnl),
        delta_color=_delta_color(total_pnl),
    )
    m4.metric(
        "Win Rate",
        f"{win_rate:.1f}%",
        f"{len(wins)}W/{len(trades)-len(wins)}L",
    )
    m5.metric(
        "Profit Factor",
        f"{pf:.2f}",
    )
    m6.metric(
        "Open Trades",
        acc.get('open_trades', 0),
    )

    st.divider()

    # ── Equity Curve + Daily PnL ───────────────────────────────
    col_left, col_right = st.columns([3, 2])

    with col_left:
        _chart_equity_curve(equity)

    with col_right:
        _chart_daily_pnl(trades)

    # ── Symbol Performance ─────────────────────────────────────
    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        _chart_symbol_performance(trades)

    with col_b:
        _chart_win_loss_donut(trades)


# ══════════════════════════════════════════════════════════════
# Charts
# ══════════════════════════════════════════════════════════════
def _chart_equity_curve(equity: pd.DataFrame):
    st.markdown("#### 📈 Equity Curve")

    if equity.empty:
        st.info("ยังไม่มีข้อมูล")
        return

    fig = go.Figure()

    # Equity line
    fig.add_trace(go.Scatter(
        x    = equity['ts'],
        y    = equity['equity'],
        mode = 'lines',
        name = 'Equity',
        line = dict(color='#f0b429', width=1.5),
        fill = 'tozeroy',
        fillcolor = 'rgba(240,180,41,0.06)',
    ))

    # Balance line
    fig.add_trace(go.Scatter(
        x    = equity['ts'],
        y    = equity['balance'],
        mode = 'lines',
        name = 'Balance',
        line = dict(color='#378ADD', width=1, dash='dot'),
        showlegend = True,
    ))

    # Initial balance reference
    init = equity['balance'].iloc[0] if not equity.empty else 0
    fig.add_hline(
        y           = init,
        line_dash   = "dash",
        line_color  = "rgba(255,255,255,0.15)",
        annotation_text = f"Start ${init:,.0f}",
        annotation_font_color = "#94a3b8",
    )

    layout = _plotly_layout("", height=260)
    layout['showlegend'] = True
    layout['legend'] = dict(
        font=dict(color="#94a3b8"),
        bgcolor="rgba(0,0,0,0)",
    )
    layout['yaxis']['tickprefix'] = '$'
    fig.update_layout(**layout)

    st.plotly_chart(fig, use_container_width=True)


def _chart_daily_pnl(trades: pd.DataFrame):
    st.markdown("#### 📊 Daily P&L (30 วัน)")

    if trades.empty:
        st.info("ยังไม่มีข้อมูล")
        return

    daily = (
        trades.set_index('close_time')['profit']
        .resample('D')
        .sum()
        .tail(30)
        .reset_index()
    )
    daily.columns = ['date','pnl']

    fig = go.Figure(go.Bar(
        x            = daily['date'],
        y            = daily['pnl'],
        marker_color = [
            '#06d6a0' if v >= 0 else '#ef476f'
            for v in daily['pnl']
        ],
        marker_opacity = 0.85,
        text         = [f"${v:+.0f}" for v in daily['pnl']],
        textposition = 'outside',
        textfont     = dict(size=9, color='#94a3b8'),
    ))

    layout = _plotly_layout("", height=260)
    layout['yaxis']['tickprefix'] = '$'
    fig.update_layout(**layout)

    st.plotly_chart(fig, use_container_width=True)


def _chart_symbol_performance(trades: pd.DataFrame):
    st.markdown("#### 🥇 P&L by Symbol")

    if trades.empty:
        st.info("ยังไม่มีข้อมูล")
        return

    sym = (
        trades.groupby('symbol')['profit']
        .sum()
        .sort_values()
        .reset_index()
    )

    fig = go.Figure(go.Bar(
        x            = sym['profit'],
        y            = sym['symbol'],
        orientation  = 'h',
        marker_color = [
            '#06d6a0' if v >= 0 else '#ef476f'
            for v in sym['profit']
        ],
        marker_opacity = 0.85,
    ))

    layout = _plotly_layout("", height=220)
    layout['xaxis']['tickprefix'] = '$'
    fig.update_layout(**layout)

    st.plotly_chart(fig, use_container_width=True)


def _chart_win_loss_donut(trades: pd.DataFrame):
    st.markdown("#### 🎯 Win/Loss Distribution")

    if trades.empty:
        st.info("ยังไม่มีข้อมูล")
        return

    wins   = (trades['profit'] > 0).sum()
    losses = (trades['profit'] <= 0).sum()

    fig = go.Figure(go.Pie(
        labels = ['Win', 'Loss'],
        values = [wins, losses],
        hole   = 0.6,
        marker = dict(colors=['#06d6a0','#ef476f']),
        textinfo   = 'percent+label',
        textfont   = dict(color='#94a3b8', size=12),
        showlegend = False,
    ))

    fig.add_annotation(
        text      = f"{wins/(wins+losses)*100:.0f}%<br>Win",
        x=0.5, y=0.5,
        font      = dict(color='#f0b429', size=18),
        showarrow = False,
    )

    layout = _plotly_layout("", height=220)
    fig.update_layout(**layout)

    st.plotly_chart(fig, use_container_width=True)


def _chart_drawdown(equity: pd.DataFrame):
    if equity.empty:
        return

    eq = equity['equity'].values
    running_max = np.maximum.accumulate(eq)
    dd_pct      = (eq - running_max) / (running_max + 1e-9) * 100

    fig = go.Figure(go.Scatter(
        x         = equity['ts'],
        y         = dd_pct,
        mode      = 'lines',
        fill      = 'tozeroy',
        line      = dict(color='#ef476f', width=1),
        fillcolor = 'rgba(239,71,111,0.1)',
    ))

    layout = _plotly_layout("Drawdown %", height=200)
    layout['yaxis']['ticksuffix'] = '%'
    fig.update_layout(**layout)

    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════
# Tab 2 — Trades
# ══════════════════════════════════════════════════════════════
def _render_trades(trades: pd.DataFrame):
    if trades.empty:
        st.info("ยังไม่มี trade")
        return

    # ── Filters ───────────────────────────────────────────────
    col_f1, col_f2, col_f3 = st.columns([2,2,1])
    with col_f1:
        symbols = ["ทั้งหมด"] + sorted(trades['symbol'].unique().tolist())
        sym     = st.selectbox("Symbol", symbols)
    with col_f2:
        dirs    = ["ทั้งหมด","BUY","SELL"]
        dir_sel = st.selectbox("Direction", dirs)
    with col_f3:
        n_rows  = st.selectbox("แสดง", [20,50,100,200], index=0)

    # กรองข้อมูล
    df = trades.copy()
    if sym != "ทั้งหมด":
        df = df[df['symbol'] == sym]
    if dir_sel != "ทั้งหมด":
        df = df[df['direction'] == dir_sel]

    df = df.head(n_rows)

    # ── Cumulative P&L chart ───────────────────────────────────
    df_sorted = df.sort_values('close_time')
    df_sorted['cum_pnl'] = df_sorted['profit'].cumsum()

    fig = go.Figure(go.Scatter(
        x    = df_sorted['close_time'],
        y    = df_sorted['cum_pnl'],
        mode = 'lines+markers',
        line = dict(color='#f0b429', width=2),
        marker = dict(
            size  = 6,
            color = ['#06d6a0' if p >= 0 else '#ef476f'
                     for p in df_sorted['profit']],
        ),
    ))
    layout = _plotly_layout("Cumulative P&L", height=220)
    layout['yaxis']['tickprefix'] = '$'
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)

    # ── Trade Table ────────────────────────────────────────────
    display_cols = [
        'close_time','symbol','direction','volume',
        'open_price','close_price','profit','confidence',
    ]
    available = [c for c in display_cols if c in df.columns]
    df_show   = df[available].copy()

    # Format columns
    if 'close_time' in df_show.columns:
        df_show['close_time'] = df_show['close_time'].dt.strftime(
            "%m-%d %H:%M"
        )
    for col in ['open_price','close_price']:
        if col in df_show.columns:
            df_show[col] = df_show[col].map("{:.5f}".format)
    if 'volume' in df_show.columns:
        df_show['volume'] = df_show['volume'].map("{:.2f}".format)
    if 'confidence' in df_show.columns:
        df_show['confidence'] = df_show['confidence'].map("{:.0%}".format)

    def _highlight_profit(row):
        color = (
            "color: #06d6a0" if row.get('profit', 0) > 0
            else "color: #ef476f" if row.get('profit', 0) < 0
            else "color: #94a3b8"
        )
        return [
            color if col == 'profit' else ""
            for col in row.index
        ]

    styled = (
        df_show.style
        .apply(_highlight_profit, axis=1)
        .format({'profit': '{:+.2f}'})
        .set_properties(**{
            'background-color': '#151820',
            'color'           : '#e2e8f0',
            'font-family'     : 'monospace',
            'font-size'       : '12px',
        })
    )

    st.dataframe(styled, use_container_width=True, height=400)

    # ── Export ────────────────────────────────────────────────
    csv = df[available].to_csv(index=False).encode('utf-8')
    st.download_button(
        "⬇️ Export CSV",
        data     = csv,
        file_name= f"trades_{datetime.now().strftime('%Y%m%d')}.csv",
        mime     = "text/csv",
    )


# ══════════════════════════════════════════════════════════════
# Tab 3 — Signals
# ══════════════════════════════════════════════════════════════
def _render_signals():
    signals = load_signals(days_back=7)

    if signals.empty:
        st.info("ยังไม่มี signal data")
        return

    # ── Signal distribution ────────────────────────────────────
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📡 Latest Signals")
        for sym in signals['symbol'].unique():
            latest = signals[signals['symbol'] == sym].iloc[0]
            sig    = latest['signal']
            conf   = latest['confidence']
            arrow  = "↑" if sig == 1 else "↓" if sig == -1 else "→"
            color  = (
                "#06d6a0" if sig == 1
                else "#ef476f" if sig == -1
                else "#94a3b8"
            )
            st.markdown(
                f"<div style='background:#151820;border:1px solid "
                f"rgba(240,180,41,0.15);border-radius:6px;"
                f"padding:10px 14px;margin:4px 0;"
                f"display:flex;justify-content:space-between'>"
                f"<span style='color:#ffd166;font-weight:600'>"
                f"{sym}</span>"
                f"<span style='color:{color};font-size:18px'>"
                f"{arrow}</span>"
                f"<span style='color:#94a3b8'>"
                f"conf={conf:.0%}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    with col2:
        st.markdown("#### 📊 Signal Distribution")
        sig_counts = signals['signal'].value_counts()
        fig = go.Figure(go.Bar(
            x = ['SELL','HOLD','BUY'],
            y = [
                sig_counts.get(-1, 0),
                sig_counts.get( 0, 0),
                sig_counts.get( 1, 0),
            ],
            marker_color = ['#ef476f','#94a3b8','#06d6a0'],
            marker_opacity = 0.85,
        ))
        fig.update_layout(**_plotly_layout("", height=200))
        st.plotly_chart(fig, use_container_width=True)

    # ── Confidence over time ───────────────────────────────────
    st.markdown("#### 📉 Confidence Over Time")
    for sym in signals['symbol'].unique():
        df_sym = signals[signals['symbol'] == sym].sort_values('ts')
        fig    = go.Figure(go.Scatter(
            x    = df_sym['ts'],
            y    = df_sym['confidence'],
            mode = 'lines',
            name = sym,
            line = dict(width=1.5),
        ))
        fig.add_hline(
            y          = CFG['signal']['min_confidence'],
            line_dash  = "dash",
            line_color = "#f0b429",
            annotation_text = f"min_conf={CFG['signal']['min_confidence']}",
        )
        layout = _plotly_layout(sym, height=180)
        layout['yaxis'].update(range=[0,1], tickformat='.0%')
        fig.update_layout(**layout)
        st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════
# Tab 4 — Analysis
# ══════════════════════════════════════════════════════════════
def _render_analysis(trades: pd.DataFrame):
    equity = load_equity(days_back=90)
    daily  = load_daily_summary()

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 📉 Drawdown")
        _chart_drawdown(equity)

    with col2:
        st.markdown("#### 📊 Risk Metrics")
        if not trades.empty:
            wins   = trades[trades['profit'] > 0]['profit']
            losses = trades[trades['profit'] <= 0]['profit']
            gw     = wins.sum()
            gl     = abs(losses.sum())

            metrics = {
                "Total Trades"  : len(trades),
                "Win Rate"      : f"{len(wins)/len(trades)*100:.1f}%",
                "Profit Factor" : f"{gw/gl:.2f}" if gl > 0 else "∞",
                "Avg Win"       : f"${wins.mean():.2f}" if not wins.empty else "$0",
                "Avg Loss"      : f"${losses.mean():.2f}" if not losses.empty else "$0",
                "Best Trade"    : f"${trades['profit'].max():+.2f}",
                "Worst Trade"   : f"${trades['profit'].min():+.2f}",
                "Total P&L"     : f"${trades['profit'].sum():+.2f}",
            }
            for k, v in metrics.items():
                col_k, col_v = st.columns([3,2])
                col_k.markdown(
                    f"<span style='color:#94a3b8;font-family:monospace'>"
                    f"{k}</span>", unsafe_allow_html=True
                )
                col_v.markdown(
                    f"<span style='color:#f0b429;font-family:monospace'>"
                    f"**{v}**</span>", unsafe_allow_html=True
                )
        else:
            st.info("ยังไม่มีข้อมูล")

    # ── Daily Summary Table ────────────────────────────────────
    if not daily.empty:
        st.markdown("#### 📅 Daily Summary")
        st.dataframe(
            daily.style.set_properties(**{
                'background-color': '#151820',
                'color'           : '#e2e8f0',
                'font-family'     : 'monospace',
                'font-size'       : '12px',
            }),
            use_container_width=True,
            height=300,
        )


# ── Run ────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()