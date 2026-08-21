"""
Authoritative Currency Exchange Rate Service.
Retrieves live market exchange rates via European Central Bank (Frankfurter API).
Tracks exact provenance, retrieval timestamps, cache status, and Decimal rates.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from typing import Any, Dict, Optional, Tuple
import urllib.request
import urllib.error


@dataclass
class LiveRateResult:
    base: str
    quote: str
    rate: Decimal
    rate_date: str
    timestamp: str
    provider: str
    status: str  # "LIVE", "CACHED", "BASE_CURRENCY", "FALLBACK"
    is_live: bool
    is_cached: bool
    is_fallback: bool
    formatted_label: str
    error: Optional[str] = None

    @property
    def source(self) -> str:
        return self.provider

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base": self.base,
            "quote": self.quote,
            "rate": str(self.rate),
            "rate_date": self.rate_date,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "status": self.status,
            "is_live": self.is_live,
            "is_cached": self.is_cached,
            "is_fallback": self.is_fallback,
            "formatted_label": self.formatted_label,
            "error": self.error
        }


class CurrencyRateService:
    """Enterprise service to fetch, cache, and track authoritative exchange rates."""

    # cache: (from_curr, to_curr) -> (rate, rate_date, iso_timestamp, unix_timestamp)
    _cache: Dict[Tuple[str, str], Tuple[Decimal, str, str, float]] = {}
    CACHE_TTL_SECONDS = 3600  # 1 hour cache

    @classmethod
    def get_exchange_rate(
        cls,
        from_currency: str,
        to_currency: str,
        force_refresh: bool = False
    ) -> LiveRateResult:
        """
        Retrieves the authoritative exchange rate to convert 1 unit of `from_currency` into `to_currency`.
        All financial rate values are returned as Decimal objects.
        """
        from_curr = from_currency.upper().strip()
        to_curr = to_currency.upper().strip()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # 1. Identity rate
        if from_curr == to_curr:
            return LiveRateResult(
                base=from_curr,
                quote=to_curr,
                rate=Decimal("1.0"),
                rate_date="Current",
                timestamp=now_utc,
                provider="Base Currency",
                status="BASE_CURRENCY",
                is_live=True,
                is_cached=False,
                is_fallback=False,
                formatted_label=f"{from_curr} = {to_curr} (1:1 Base Sourcing Currency)"
            )

        cache_key = (from_curr, to_curr)
        now_time = time.time()

        # 2. Check In-Memory Cache (if not force refresh)
        if not force_refresh and cache_key in cls._cache:
            rate_val, r_date, cached_ts, cached_unix = cls._cache[cache_key]
            if now_time - cached_unix < cls.CACHE_TTL_SECONDS:
                symbol = "INR " if to_curr == "INR" else ("USD " if to_curr == "USD" else ("EUR " if to_curr == "EUR" else f"{to_curr} "))
                label = f"{from_curr} -> {to_curr}: {symbol}{rate_val} | Source: European Central Bank (via Frankfurter) | Cached Rate (Cached at: {cached_ts})"
                return LiveRateResult(
                    base=from_curr,
                    quote=to_curr,
                    rate=rate_val,
                    rate_date=r_date,
                    timestamp=cached_ts,
                    provider="European Central Bank (via Frankfurter)",
                    status="CACHED",
                    is_live=False,
                    is_cached=True,
                    is_fallback=False,
                    formatted_label=label
                )

        # 3. Live External API Fetch from Frankfurter
        try:
            url = f"https://api.frankfurter.dev/v2/rate/{from_curr}/{to_curr}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "QuoteIntelligence/1.0 (Enterprise Procurement Engine)"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                rate_val = Decimal(str(data["rate"]))
                rate_date = str(data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")))
                
                cls._cache[cache_key] = (rate_val, rate_date, now_utc, now_time)

                symbol = "INR " if to_curr == "INR" else ("USD " if to_curr == "USD" else ("EUR " if to_curr == "EUR" else f"{to_curr} "))
                label = f"{from_curr} -> {to_curr}: {symbol}{rate_val} | Source: European Central Bank (via Frankfurter) | Live Rate (Updated: {now_utc})"
                return LiveRateResult(
                    base=from_curr,
                    quote=to_curr,
                    rate=rate_val,
                    rate_date=rate_date,
                    timestamp=now_utc,
                    provider="European Central Bank (via Frankfurter)",
                    status="LIVE",
                    is_live=True,
                    is_cached=False,
                    is_fallback=False,
                    formatted_label=label
                )
        except Exception as exc:
            # 4. Fallback if external API is unreachable
            fallback_rates = {
                ("USD", "INR"): Decimal("86.50"),
                ("EUR", "INR"): Decimal("92.50"),
                ("GBP", "INR"): Decimal("108.00"),
                ("INR", "USD"): Decimal("0.0116"),
                ("INR", "EUR"): Decimal("0.0108"),
            }
            fb_rate = fallback_rates.get((from_curr, to_curr), Decimal("1.0"))
            symbol = "INR " if to_curr == "INR" else ("USD " if to_curr == "USD" else ("EUR " if to_curr == "EUR" else f"{to_curr} "))
            label = f"{from_curr} -> {to_curr}: {symbol}{fb_rate} | Source: Offline Fallback | FX Rate Unavailable / API Failed (Updated: {now_utc})"
            return LiveRateResult(
                base=from_curr,
                quote=to_curr,
                rate=fb_rate,
                rate_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                timestamp=now_utc,
                provider="Offline Fallback",
                status="FALLBACK",
                is_live=False,
                is_cached=False,
                is_fallback=True,
                formatted_label=label,
                error=f"External FX API unreachable: {str(exc)}"
            )
