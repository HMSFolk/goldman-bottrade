<<<<<<< HEAD
# data/collect_news.py
import requests
import pandas as pd
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log      = logging.getLogger("data")
analyzer = SentimentIntensityAnalyzer()

def collect_news_sentiment() -> pd.DataFrame:
    """
    ดึงข่าวจาก GDELT → คำนวณ sentiment score
    บันทึกลง data/raw/news_sentiment.parquet
    """
    Path("data/raw").mkdir(exist_ok=True)

    # ดึงข่าวที่เกี่ยวกับทองและ Forex
    queries = [
        "gold federal reserve inflation dollar",
        "forex dollar euro pound",
        "interest rate inflation CPI NFP",
    ]

    all_articles = []

    for query in queries:
        try:
            resp = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query"      : query,
                    "mode"       : "artlist",
                    "maxrecords" : 100,
                    "format"     : "json",
                    "timespan"   : "3d",   # ข่าว 3 วันล่าสุด
                },
                timeout=15,
            )

            articles = resp.json().get("articles", [])
            all_articles.extend(articles)
            log.info(f"  GDELT '{query[:30]}...': {len(articles)} articles")

        except Exception as e:
            log.warning(f"GDELT query ล้มเหลว: {e}")

    if not all_articles:
        log.warning("ไม่ได้รับข้อมูลข่าวจาก GDELT")
        return pd.DataFrame()

    df = pd.DataFrame(all_articles)

    # คำนวณ sentiment
    df['sentiment'] = df['title'].apply(
        lambda t: analyzer.polarity_scores(str(t))['compound']
    )

    # แปลง seendate
    df['date'] = pd.to_datetime(df['seendate'], format="%Y%m%dT%H%M%SZ", utc=True)
    df = df[['date', 'title', 'sentiment', 'url']].dropna()

    # aggregate sentiment รายวัน
    daily_sentiment = (
        df.set_index('date')['sentiment']
        .resample('D').mean()
        .rename('avg_sentiment')
        .reset_index()
    )

    out = "data/raw/news_sentiment.parquet"
    daily_sentiment.to_parquet(out, index=False)
    log.info(f"  News sentiment: {len(daily_sentiment)} วัน → {out}")

=======
# data/collect_news.py
import requests
import pandas as pd
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

log      = logging.getLogger("data")
analyzer = SentimentIntensityAnalyzer()

def collect_news_sentiment() -> pd.DataFrame:
    """
    ดึงข่าวจาก GDELT → คำนวณ sentiment score
    บันทึกลง data/raw/news_sentiment.parquet
    """
    Path("data/raw").mkdir(exist_ok=True)

    # ดึงข่าวที่เกี่ยวกับทองและ Forex
    queries = [
        "gold federal reserve inflation dollar",
        "forex dollar euro pound",
        "interest rate inflation CPI NFP",
    ]

    all_articles = []

    for query in queries:
        try:
            resp = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query"      : query,
                    "mode"       : "artlist",
                    "maxrecords" : 100,
                    "format"     : "json",
                    "timespan"   : "3d",   # ข่าว 3 วันล่าสุด
                },
                timeout=15,
            )

            articles = resp.json().get("articles", [])
            all_articles.extend(articles)
            log.info(f"  GDELT '{query[:30]}...': {len(articles)} articles")

        except Exception as e:
            log.warning(f"GDELT query ล้มเหลว: {e}")

    if not all_articles:
        log.warning("ไม่ได้รับข้อมูลข่าวจาก GDELT")
        return pd.DataFrame()

    df = pd.DataFrame(all_articles)

    # คำนวณ sentiment
    df['sentiment'] = df['title'].apply(
        lambda t: analyzer.polarity_scores(str(t))['compound']
    )

    # แปลง seendate
    df['date'] = pd.to_datetime(df['seendate'], format="%Y%m%dT%H%M%SZ", utc=True)
    df = df[['date', 'title', 'sentiment', 'url']].dropna()

    # aggregate sentiment รายวัน
    daily_sentiment = (
        df.set_index('date')['sentiment']
        .resample('D').mean()
        .rename('avg_sentiment')
        .reset_index()
    )

    out = "data/raw/news_sentiment.parquet"
    daily_sentiment.to_parquet(out, index=False)
    log.info(f"  News sentiment: {len(daily_sentiment)} วัน → {out}")

>>>>>>> 98ac82b18ee8d71be376450f278ba77dde0c1c3e
    return daily_sentiment