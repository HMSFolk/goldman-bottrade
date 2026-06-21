# dashboard/app_live_compare.py
"""
Live vs Backtest Comparison ("Reality Check")
════════════════════════════════════════════════════════════
เทียบผลเทรดจริง (จาก db/trades.db) กับผล walk-forward backtest
(จาก scripts/walk_forward.py → reports/walk_forward_{symbol}.csv)

ทำไมต้องมี tab นี้:
  Backtest ที่ดูดีมาก แต่ของจริงแย่กว่าเยอะ มักเป็นสัญญาณว่า backtest
  มี bias บางอย่าง (overfitting, look-ahead leak ในฟีเจอร์, ฯลฯ)
  tab นี้เปรียบเทียบให้เห็นชัดๆ ว่า "ของจริง" กับ "ที่คาดไว้" ห่างกันแค่ไหน

เรียกใช้จาก dashboard/app.py:
  from dashboard.app_live_compare import show_live_vs_backtest
  show_live_vs_backtest(trades)
════════════════════════════════════════════════════════════
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

from config import get_config
CFG = get_config()

REPORTS_DIR = _ROOT / CFG['paths']['reports']


# ══════════════════════════════════════════════════════════════
# Helpers (แยกจาก app.py — กัน circular import เพราะ app.py
# import ไฟล์นี้อยู่แล้ว)
# ══════════════════════════════════════════════════════════════
def _plotly_layout(title: str = "", height: int = 300) -> dict:
    """Dark theme layout — สไตล์เดียวกับ app.py"""
    return dict(
        title         = dict(text=title, font=dict(color="#f0b429")),
        paper_bgcolor = "#151820",
        plot_bgcolor  = "#0f1115",
        font          = dict(color="#94a3b8", family="monospace"),
        xaxis         = dict(gridcolor="#1c2030", showgrid=True, linecolor="#1c2030"),
        yaxis         = dict(gridcolor="#1c2030", showgrid=True, linecolor="#1c2030"),
        height        = height,
        margin        = dict(l=0, r=0, t=40, b=0),
        showlegend    = True,
    )


@st.cache_data(ttl=60)
def _load_backtest_results(symbol: str) -> pd.DataFrame:
    """โหลดผล walk-forward backtest ของ symbol นี้ (ทุก fold)"""
    path = REPORTS_DIR / f"walk_forward_{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _backtest_summary(symbol: str) -> dict | None:
    """สรุปผล backtest เป็นค่าเฉลี่ยทุก fold"""
    df = _load_backtest_results(symbol)
    if df.empty or 'win_rate' not in df.columns:
        return None

    pf = df['profit_factor'].replace([np.inf, -np.inf], np.nan)

    return {
        'win_rate'     : df['win_rate'].mean(),
        'profit_factor': pf.mean(),
        'return_pct'   : df['return_pct'].mean() if 'return_pct' in df.columns else None,
        'sharpe'       : df['sharpe'].mean() if 'sharpe' in df.columns else None,
        'max_dd'       : df['max_dd'].mean() if 'max_dd' in df.columns else None,
        'total_trades' : int(df['total_trades'].sum()) if 'total_trades' in df.columns else 0,
        'n_folds'      : len(df),
    }


def _live_summary(trades: pd.DataFrame, symbol: str) -> dict | None:
    """สรุปผล live trading จริงของ symbol นี้ (เฉพาะ trade ที่ปิดแล้ว)"""
    if trades.empty or 'symbol' not in trades.columns:
        return None

    df = trades[trades['symbol'] == symbol]
    if 'profit' in df.columns:
        df = df[df['profit'].notna()]

    if df.empty:
        return None

    wins   = df[df['profit'] > 0]
    losses = df[df['profit'] <= 0]
    gw     = wins['profit'].sum()
    gl     = abs(losses['profit'].sum())

    return {
        'win_rate'     : len(wins) / len(df) * 100,
        'profit_factor': (gw / gl) if gl > 0 else np.inf,
        'avg_trade'    : df['profit'].mean(),
        'total_pnl'    : df['profit'].sum(),
        'total_trades' : len(df),
    }


def _gap_badge(live_val, bt_val, threshold_pp: float = 15.0) -> str:
    """
    ป้ายเตือน gap ระหว่าง live กับ backtest (สำหรับ win_rate, หน่วย percentage point)
    🟢 ของจริงดีกว่าหรือเท่า backtest
    🟡 แย่กว่าเล็กน้อย (ยังไม่น่ากังวล — sample size เล็ก/variance ปกติ)
    🔴 แย่กว่ามาก (> threshold) — น่าสงสัยว่า backtest มี bias
    """
    if live_val is None or bt_val is None:
        return "⚪ N/A"

    gap = live_val - bt_val
    if gap >= 0:
        return f"🟢 +{gap:.1f}pp"
    elif gap >= -threshold_pp:
        return f"🟡 {gap:.1f}pp"
    else:
        return f"🔴 {gap:.1f}pp (reality gap!)"


def _fmt_pf(val) -> str:
    """Format profit factor — รองรับ inf/NaN/None"""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "N/A"
    if val == np.inf:
        return "∞"
    return f"{val:.2f}"


# ══════════════════════════════════════════════════════════════
# Entry Point — เรียกจาก app.py
# ══════════════════════════════════════════════════════════════
def show_live_vs_backtest(trades: pd.DataFrame, symbols: list = None):
    """
    แสดงตารางเทียบ + กราฟ + คำเตือน reality gap ระหว่างผลเทรดจริง
    กับผล walk-forward backtest

    Args:
        trades : DataFrame จาก load_trades() ใน app.py (closed trades)
        symbols: list symbol ที่จะเทียบ (default: ทุกตัวใน config.symbols.active)
    """
    symbols = symbols or CFG.get('symbols', {}).get('active', [])

    st.markdown("#### 🔬 Live vs Backtest — Reality Check")
    st.caption(
        "เทียบผลเทรดจริงกับผล walk-forward backtest "
        "(สร้างจาก `python scripts/walk_forward.py`) — "
        "ถ้าของจริงแย่กว่า backtest มาก อาจมี bias ใน backtest "
        "(overfitting, look-ahead leak ในฟีเจอร์) ควรตรวจ feature pipeline ซ้ำ"
    )

    rows = []
    has_any_backtest = False

    for sym in symbols:
        bt   = _backtest_summary(sym)
        live = _live_summary(trades, sym)

        if bt is not None:
            has_any_backtest = True

        rows.append({'symbol': sym, 'bt': bt, 'live': live})

    if not has_any_backtest:
        st.info(
            "ยังไม่มีผล backtest ให้เทียบ — รัน "
            "`python scripts/walk_forward.py` ก่อน "
            "เพื่อสร้างไฟล์ `reports/walk_forward_{symbol}.csv`"
        )
        if trades.empty:
            return

    # ── การ์ดเทียบรายตัว ────────────────────────────────────────
    for r in rows:
        sym, bt, live = r['symbol'], r['bt'], r['live']

        st.markdown(
            f"<div style='background:#151820;border:1px solid "
            f"rgba(240,180,41,0.15);border-radius:8px;"
            f"padding:14px 16px;margin:8px 0'>"
            f"<span style='color:#ffd166;font-weight:700;font-size:16px'>"
            f"{sym}</span></div>",
            unsafe_allow_html=True,
        )

        col1, col2, col3 = st.columns(3)

        with col1:
            if live and bt:
                badge = _gap_badge(live['win_rate'], bt['win_rate'])
                st.metric(
                    "Win Rate (Live)",
                    f"{live['win_rate']:.1f}%",
                    delta=f"{live['win_rate'] - bt['win_rate']:+.1f}pp vs BT",
                )
                st.caption(f"Backtest: {bt['win_rate']:.1f}%  {badge}")
            elif live:
                st.metric("Win Rate (Live)", f"{live['win_rate']:.1f}%")
                st.caption("_ไม่มีผล backtest ให้เทียบ_")
            elif bt:
                st.metric("Win Rate (Backtest)", f"{bt['win_rate']:.1f}%")
                st.caption("_ยังไม่มี live trade_")
            else:
                st.caption("_ไม่มีข้อมูล_")

        with col2:
            live_pf = live['profit_factor'] if live else None
            bt_pf   = bt['profit_factor'] if bt else None
            st.metric("Profit Factor (Live)", _fmt_pf(live_pf))
            st.caption(f"Backtest: {_fmt_pf(bt_pf)}")

        with col3:
            n_live = live['total_trades'] if live else 0
            n_bt   = bt['total_trades'] if bt else 0
            st.metric("Trades (Live)", f"{n_live}")
            st.caption(f"Backtest sample: {n_bt} (จาก {bt['n_folds'] if bt else 0} folds)")

    # ── กราฟเปรียบเทียบ Win Rate ────────────────────────────────
    plot_rows = [r for r in rows if r['live'] and r['bt']]
    if plot_rows:
        st.divider()
        plot_syms = [r['symbol'] for r in plot_rows]
        live_vals = [r['live']['win_rate'] for r in plot_rows]
        bt_vals   = [r['bt']['win_rate']   for r in plot_rows]

        fig = go.Figure()
        fig.add_trace(go.Bar(
            name='Live', x=plot_syms, y=live_vals,
            marker_color='#06d6a0',
        ))
        fig.add_trace(go.Bar(
            name='Backtest', x=plot_syms, y=bt_vals,
            marker_color='#f0b429',
        ))
        layout = _plotly_layout("Win Rate: Live vs Backtest", height=280)
        layout['barmode'] = 'group'
        layout['yaxis']['ticksuffix'] = '%'
        fig.update_layout(**layout)
        st.plotly_chart(fig, use_container_width=True)

        # ── คำเตือน reality gap ────────────────────────────────
        big_gaps = [
            r['symbol'] for r in plot_rows
            if (r['live']['win_rate'] - r['bt']['win_rate']) < -15
        ]
        if big_gaps:
            st.warning(
                f"⚠️ **Reality Gap พบใน: {', '.join(big_gaps)}**\n\n"
                f"Win rate จริงต่ำกว่า backtest เกิน 15 percentage points — "
                f"สัญญาณว่า backtest อาจ overfit หรือมี look-ahead bias "
                f"ในขั้นตอนสร้างฟีเจอร์ ควรตรวจ feature pipeline อีกครั้ง"
            )
        else:
            st.success("✅ ผลจริงใกล้เคียงกับ backtest — ไม่พบ reality gap ที่น่ากังวล")