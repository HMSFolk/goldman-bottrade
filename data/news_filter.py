# data/news_filter.py
"""
News Filter — บล็อกเทรดช่วง high-impact economic news
=======================================================
ป้องกันบอทเทรดตอนตลาดผันผวนจากข่าวใหญ่:
  NFP, FOMC, CPI, GDP, Interest Rate Decision ฯลฯ

Sources (ลองตามลำดับ):
  1. Finnhub Economic Calendar  (ต้องใส่ FINNHUB_API_KEY ใน .env)
  2. ForexFactory JSON           (ไม่ต้องมี key — unofficial)
  3. Local cache                 (ใช้ข้อมูลเก่าถ้า API ล้มเหลว)
  4. Fail-open / Fail-closed     (ตั้งค่าใน config.yaml)

ใช้งาน:
  from data.news_filter import NewsFilter
  nf = NewsFilter()
  if nf.is_news_window(symbol='XAUUSDm'):
      log.info("News window — skip")
      return
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import get_config

log = logging.getLogger("data.news_filter")

CFG = get_config()

# ── Paths ──────────────────────────────────────────────────────
CACHE_PATH = _ROOT / "data" / "news_cache.json"

# ── Symbol → Currencies ที่ต้องสนใจ ───────────────────────────
# ทอง sensitive กับ USD มากที่สุด เพราะ XAU/USD quote ด้วย USD
SYMBOL_CURRENCIES: dict[str, list[str]] = {
    "XAUUSDm" : ["USD"],
    "XAUUSD"  : ["USD"],
    "EURUSDm" : ["EUR", "USD"],
    "EURUSD"  : ["EUR", "USD"],
    "GBPUSDm" : ["GBP", "USD"],
    "GBPUSD"  : ["GBP", "USD"],
    "USDJPYm" : ["USD", "JPY"],
    "USDJPY"  : ["USD", "JPY"],
}

# ── Keywords ของ high-impact events ──────────────────────────
# ตรวจ case-insensitive ใน event title
HIGH_IMPACT_KEYWORDS = [
    "nfp", "non-farm", "nonfarm", "payroll",
    "fomc", "federal funds", "fed rate",
    "interest rate decision", "rate decision",
    "cpi", "consumer price index", "inflation",
    "ppi", "producer price",
    "gdp", "gross domestic",
    "pce",
    "retail sales",
    "employment change", "unemployment",
    "ism manufacturing", "ism services",
    "ecb", "european central bank",
    "boe", "bank of england",
    "boj", "bank of japan",
    "press conference",
    "monetary policy statement",
    "speech",                   # Fed Chair / ECB Chair speech
]


# ══════════════════════════════════════════════════════════════
# NewsFilter
# ══════════════════════════════════════════════════════════════
class NewsFilter:
    """
    ตรวจว่าตอนนี้อยู่ใน news blackout window หรือไม่
    """

    def __init__(self):
        cfg_nf = CFG.get("news_filter", {})
        self.enabled        = cfg_nf.get("enabled",          True)
        self.buffer_min     = cfg_nf.get("buffer_minutes",   30)
        self.high_only      = cfg_nf.get("high_impact_only", True)
        self.cache_hours    = cfg_nf.get("cache_hours",      6)
        self.fail_open      = cfg_nf.get("fail_open",        True)   # True=ให้เทรดถ้า API ล้มเหลว
        self.extra_keywords = [k.lower() for k in cfg_nf.get("keywords", [])]
        self.finnhub_key    = CFG.get("api_keys", {}).get("finnhub", "")

        # รวม keywords จาก config เข้ากับ default list
        self._keywords = HIGH_IMPACT_KEYWORDS + self.extra_keywords

        # cache in-memory
        self._cache_events: list  = []
        self._cache_fetched: Optional[datetime] = None

    # ── Public API ─────────────────────────────────────────────

    def is_news_window(
        self,
        symbol:         Optional[str] = None,
        buffer_minutes: Optional[int] = None,
    ) -> bool:
        """
        คืน True ถ้าตอนนี้อยู่ใน news blackout window
        → ควร skip การเทรด

        Args:
            symbol:         เช่น 'XAUUSDm' (ถ้า None = เช็คทุก currency)
            buffer_minutes: override ค่าจาก config (default 30)
        """
        if not self.enabled:
            return False

        buf = buffer_minutes or self.buffer_min

        try:
            events = self._get_events(symbol=symbol)
            now    = datetime.now(timezone.utc)

            for ev in events:
                ev_time = ev.get("time_utc")
                if ev_time is None:
                    continue

                delta = abs((now - ev_time).total_seconds() / 60)
                if delta <= buf:
                    log.warning(
                        f"🚫 News window: [{ev.get('impact','?').upper()}] "
                        f"{ev.get('title','?')} "
                        f"({ev.get('country','?')}) "
                        f"@ {ev_time.strftime('%H:%M UTC')} "
                        f"— {delta:.0f} min {'until' if ev_time > now else 'since'}"
                    )
                    return True

        except Exception as e:
            log.error(f"NewsFilter.is_news_window error: {e}", exc_info=True)
            # fail_open=True → ให้เทรดต่อเพื่อไม่ miss โอกาส
            # fail_open=False → บล็อกเพื่อความปลอดภัย
            return not self.fail_open

        return False

    def get_upcoming_events(
        self,
        hours_ahead:    int = 4,
        symbol:         Optional[str] = None,
    ) -> list:
        """
        คืน list ของ events ที่จะเกิดใน X ชั่วโมงข้างหน้า
        ใช้สำหรับแสดงใน Telegram bot หรือ dashboard
        """
        try:
            events = self._get_events(symbol=symbol)
            now    = datetime.now(timezone.utc)
            cutoff = now + timedelta(hours=hours_ahead)

            upcoming = [
                ev for ev in events
                if ev.get("time_utc") and now <= ev["time_utc"] <= cutoff
            ]
            return sorted(upcoming, key=lambda x: x["time_utc"])
        except Exception as e:
            log.error(f"get_upcoming_events error: {e}")
            return []

    # ── Internal: event loading + caching ──────────────────────

    def _get_events(self, symbol: Optional[str] = None) -> list:
        """
        โหลด events จาก cache หรือ fetch ใหม่ถ้าหมดอายุ
        แล้ว filter ตาม symbol ที่ระบุ
        """
        self._refresh_cache_if_needed()
        events = self._cache_events

        # filter ตาม symbol
        if symbol:
            currencies = SYMBOL_CURRENCIES.get(symbol, ["USD"])
            events = [
                ev for ev in events
                if ev.get("country", "").upper() in [c.upper() for c in currencies]
            ]

        # filter เฉพาะ high impact
        if self.high_only:
            events = [
                ev for ev in events
                if ev.get("impact", "").lower() in ("high", "h", "3")
            ]

        return events

    def _refresh_cache_if_needed(self):
        """Refresh in-memory cache ถ้าหมดอายุหรือยังไม่มี"""
        now = datetime.now(timezone.utc)

        # ถ้ายังใหม่อยู่ → ใช้ cache
        if (
            self._cache_fetched is not None
            and (now - self._cache_fetched).total_seconds() < self.cache_hours * 3600
        ):
            return

        log.info("NewsFilter: refreshing event cache...")
        events = self._fetch_with_fallback()

        if events:
            self._cache_events  = events
            self._cache_fetched = now
            self._save_disk_cache(events)
            log.info(f"NewsFilter: loaded {len(events)} events")
        else:
            # ลอง disk cache เก่า
            disk = self._load_disk_cache()
            if disk:
                self._cache_events  = disk
                self._cache_fetched = now   # อัปเดตเวลาเพื่อไม่ fetch ซ้ำทันที
                log.warning(f"NewsFilter: using stale disk cache ({len(disk)} events)")
            else:
                log.warning("NewsFilter: no event data available — using fail_open policy")

    # ── Fetching ───────────────────────────────────────────────

    def _fetch_with_fallback(self) -> list:
        """ลอง fetch จาก source ต่างๆ ตามลำดับ"""
        # Source 1: Finnhub
        if self.finnhub_key:
            events = self._fetch_finnhub()
            if events:
                log.info(f"NewsFilter: fetched {len(events)} events from Finnhub")
                return events
            log.warning("NewsFilter: Finnhub failed, trying ForexFactory...")

        # Source 2: ForexFactory (unofficial JSON)
        events = self._fetch_forexfactory()
        if events:
            log.info(f"NewsFilter: fetched {len(events)} events from ForexFactory")
            return events

        log.warning("NewsFilter: all sources failed")
        return []

    def _fetch_finnhub(self) -> list:
        """
        Finnhub Economic Calendar API
        https://finnhub.io/docs/api/economic-calendar
        """
        try:
            now       = datetime.now(timezone.utc)
            date_from = now.strftime("%Y-%m-%d")
            date_to   = (now + timedelta(days=7)).strftime("%Y-%m-%d")

            resp = requests.get(
                "https://finnhub.io/api/v1/calendar/economic",
                params={
                    "from"  : date_from,
                    "to"    : date_to,
                    "token" : self.finnhub_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            raw    = data.get("economicCalendar", [])
            events = [self._normalize_finnhub(e) for e in raw]
            return [e for e in events if e is not None]

        except Exception as e:
            log.debug(f"Finnhub fetch error: {e}")
            return []

    def _fetch_forexfactory(self) -> list:
        """
        ForexFactory unofficial JSON endpoint
        คืน events ของสัปดาห์นี้
        """
        try:
            resp = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            raw    = resp.json()
            events = [self._normalize_forexfactory(e) for e in raw]
            return [e for e in events if e is not None]

        except Exception as e:
            log.debug(f"ForexFactory fetch error: {e}")
            return []

    # ── Normalization ──────────────────────────────────────────

    def _normalize_finnhub(self, raw: dict) -> Optional[dict]:
        """แปลง Finnhub event → standard format"""
        try:
            # Finnhub time: "2024-01-05 13:30:00" (UTC)
            time_str = raw.get("time", "")
            if not time_str:
                return None

            ev_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            ev_time = ev_time.replace(tzinfo=timezone.utc)

            impact = raw.get("impact", "low").lower()
            title  = raw.get("event", "")

            # ตรวจ keywords ถ้า impact ไม่ได้ระบุว่า high
            if impact != "high" and self._is_keyword_match(title):
                impact = "high"

            return {
                "title"    : title,
                "country"  : raw.get("country", "").upper(),
                "impact"   : impact,
                "time_utc" : ev_time,
                "source"   : "finnhub",
            }
        except Exception:
            return None

    def _normalize_forexfactory(self, raw: dict) -> Optional[dict]:
        """
        แปลง ForexFactory event → standard format
        ForexFactory ส่งเวลาเป็น US/Eastern timezone
        """
        try:
            # FF format: "2024-01-05T08:30:00-05:00"
            time_str = raw.get("date", "")
            if not time_str:
                return None

            # parse ISO format (with timezone offset)
            from datetime import timezone as tz
            # Python 3.7+ รองรับ %z กับ offset เช่น -05:00
            # แต่ต้องลบ ':' ออกก่อนใน Python < 3.11
            time_str_fixed = time_str
            if len(time_str) > 19 and time_str[19] in ('+', '-'):
                # ensure no colon in offset for strptime
                offset_part = time_str[19:]
                if ':' in offset_part:
                    time_str_fixed = time_str[:19] + offset_part.replace(':', '', 1)

            try:
                ev_time = datetime.strptime(time_str_fixed, "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                # fallback: try without time component
                ev_time = datetime.strptime(time_str[:10], "%Y-%m-%d")
                ev_time = ev_time.replace(tzinfo=timezone.utc)

            # Convert to UTC
            ev_time_utc = ev_time.astimezone(timezone.utc)

            impact_raw = raw.get("impact", "Low")
            impact_map = {"High": "high", "Medium": "medium", "Low": "low"}
            impact     = impact_map.get(impact_raw, impact_raw.lower())

            title   = raw.get("title", "")
            country = raw.get("country", "").upper()

            # ForexFactory ใช้ "USD" ไม่ใช่ "US"
            # ถ้า country เป็น 2 ตัวอักษรมันคือ currency code แล้ว

            # ตรวจ keywords
            if impact != "high" and self._is_keyword_match(title):
                impact = "high"

            return {
                "title"    : title,
                "country"  : country,   # "USD", "EUR", "GBP" etc.
                "impact"   : impact,
                "time_utc" : ev_time_utc,
                "source"   : "forexfactory",
            }
        except Exception as e:
            log.debug(f"ForexFactory normalize error: {e} | raw={raw}")
            return None

    def _is_keyword_match(self, title: str) -> bool:
        """ตรวจว่า title ตรงกับ high-impact keyword หรือไม่"""
        title_lower = title.lower()
        return any(kw in title_lower for kw in self._keywords)

    # ── Disk cache ─────────────────────────────────────────────

    def _save_disk_cache(self, events: list):
        """บันทึก events ลง JSON file"""
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_utc" : datetime.now(timezone.utc).isoformat(),
                "events"      : [
                    {
                        **ev,
                        "time_utc": ev["time_utc"].isoformat(),
                    }
                    for ev in events
                ],
            }
            CACHE_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"NewsFilter: cannot save cache: {e}")

    def _load_disk_cache(self) -> list:
        """โหลด events จาก disk cache"""
        try:
            if not CACHE_PATH.exists():
                return []

            payload  = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            events   = payload.get("events", [])
            restored = []
            for ev in events:
                ev["time_utc"] = datetime.fromisoformat(ev["time_utc"])
                # ตรวจ tzinfo
                if ev["time_utc"].tzinfo is None:
                    ev["time_utc"] = ev["time_utc"].replace(tzinfo=timezone.utc)
                restored.append(ev)
            return restored
        except Exception as e:
            log.warning(f"NewsFilter: cannot load cache: {e}")
            return []


# ══════════════════════════════════════════════════════════════
# Standalone test
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt = "%H:%M:%S",
    )

    nf = NewsFilter()

    print("\n── News window check ──────────────────────")
    for sym in ["XAUUSDm", "EURUSDm", "GBPUSDm"]:
        blocked = nf.is_news_window(symbol=sym)
        status  = "🚫 BLOCKED" if blocked else "✅ OK to trade"
        print(f"  {sym:<12}: {status}")

    print("\n── Upcoming events (next 24h) ─────────────")
    events = nf.get_upcoming_events(hours_ahead=24)
    if not events:
        print("  (ไม่มีข้อมูล หรือไม่มีข่าวใน 24ชม. ข้างหน้า)")
    for ev in events:
        t = ev["time_utc"].strftime("%Y-%m-%d %H:%M UTC")
        print(
            f"  [{ev['impact'].upper():<6}] "
            f"{ev['country']:<4} "
            f"{ev['title']:<35} "
            f"@ {t}"
        )