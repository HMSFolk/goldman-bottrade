# data/collect_news.py
"""
ดึงข่าวจาก GDELT → คำนวณ sentiment score ด้วย VADER
บันทึกลง data/raw/news_sentiment.parquet
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import pandas as pd
import requests

from config import get_config
_CFG = get_config()

log = logging.getLogger("data")

# ✅ FIX BUG-10: lazy import — ถ้า vaderSentiment ไม่ install จะ error ที่ function
#    ไม่ใช่ตอน import module (ซึ่งจะ crash ทุกไฟล์ที่ import collect_news)
_analyzer = None

def _get_analyzer():
    """Lazy-load VADER ครั้งเดียว"""
    global _analyzer
    if _analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            log.error(
                "vaderSentiment ไม่ได้ติดตั้ง — "
                "รัน: pip install vaderSentiment"
            )
            raise
    return _analyzer


GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# queries เกี่ยวกับ Gold + Forex
_QUERIES = [
    "gold price federal reserve inflation dollar",
    "XAUUSD gold trading forex dollar",
    "interest rate CPI NFP jobs report",
]


def collect_news_sentiment() -> pd.DataFrame:
    """
    ดึงข่าวจาก GDELT → คำนวณ sentiment score
    คืน DataFrame รายวัน: [date, avg_sentiment]
    บันทึกลง data/raw/news_sentiment.parquet
    """
    # ✅ FIX BUG-6: absolute path จาก config
    raw_dir = _ROOT / _CFG['paths']['data_raw']
    raw_dir.mkdir(parents=True, exist_ok=True)

    analyzer     = _get_analyzer()
    all_articles = []

    for query in _QUERIES:
        try:
            resp = requests.get(
                GDELT_URL,
                params={
                    "query"      : query,
                    "mode"       : "artlist",
                    "maxrecords" : 100,
                    "format"     : "json",
                    "timespan"   : "3d",
                },
                timeout = 15,
            )

            # ✅ FIX BUG-7: ตรวจ HTTP status ก่อน parse JSON
            if resp.status_code != 200:
                log.warning(
                    f"GDELT {resp.status_code} สำหรับ query "
                    f"'{query[:30]}': {resp.text[:100]}"
                )
                continue

            # ✅ FIX BUG-7: ป้องกัน JSONDecodeError
            try:
                data = resp.json()
            except ValueError:
                log.warning(f"GDELT คืน non-JSON: {resp.text[:100]}")
                continue

            articles = data.get("articles", [])
            all_articles.extend(articles)
            log.info(f"  GDELT '{query[:30]}': {len(articles)} articles")

        except requests.exceptions.Timeout:
            log.warning(f"GDELT timeout query '{query[:30]}'")
        except requests.exceptions.ConnectionError:
            log.warning(f"GDELT connection error query '{query[:30]}'")
        except Exception as e:
            log.warning(f"GDELT query ล้มเหลว: {e}")

    if not all_articles:
        log.warning("ไม่ได้รับข้อมูลข่าวจาก GDELT — คืน DataFrame ว่าง")
        return pd.DataFrame(columns=['date', 'avg_sentiment'])

    df = pd.DataFrame(all_articles)

    # ✅ FIX BUG-8: ใช้ .get() pattern แทน hard column access
    # title อาจเป็น None — ใช้ str() ครอบเสมอ
    if 'title' not in df.columns:
        log.warning("GDELT: ไม่มีคอลัมน์ 'title' ใน response")
        return pd.DataFrame(columns=['date', 'avg_sentiment'])

    df['sentiment'] = df['title'].apply(
        lambda t: analyzer.polarity_scores(str(t))['compound']
        if pd.notna(t) else 0.0
    )

    # ✅ FIX BUG-9: errors='coerce' ป้องกัน format ผิด crash
    if 'seendate' not in df.columns:
        log.warning("GDELT: ไม่มีคอลัมน์ 'seendate'")
        return pd.DataFrame(columns=['date', 'avg_sentiment'])

    df['date'] = pd.to_datetime(
        df['seendate'],
        format = "%Y%m%dT%H%M%SZ",
        utc    = True,
        errors = "coerce",     # ✅ วันที่ format ผิด → NaT แทน crash
    )

    # ✅ FIX BUG-8: 'url' อาจไม่มี — เลือกเฉพาะ column ที่มีจริง
    keep_cols = ['date', 'title', 'sentiment']
    if 'url' in df.columns:
        keep_cols.append('url')

    df = df[keep_cols].dropna(subset=['date', 'sentiment'])

    if df.empty:
        log.warning("ข้อมูลข่าวว่างหลัง dropna")
        return pd.DataFrame(columns=['date', 'avg_sentiment'])

    # aggregate sentiment รายวัน
    daily_sentiment = (
        df.set_index('date')['sentiment']
        .resample('D').mean()
        .rename('avg_sentiment')
        .reset_index()
    )

    # ✅ FIX BUG-6: absolute path
    out = raw_dir / "news_sentiment.parquet"
    daily_sentiment.to_parquet(out, index=False)
    log.info(
        f"  News sentiment: {len(daily_sentiment)} วัน "
        f"({len(df)} articles) → {out}"
    )

    return daily_sentiment