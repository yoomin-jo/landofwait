"""코인 슬리브 알트(ZEC·HYPE) — 추세신호 알림 전용 (DESIGN.md Sleeve 3). 주문 기능 없음.
ZEC는 바이낸스, HYPE는 코인원에 보유 — 거래소 API 키 없이 공개 시세만 사용하고 매매는 사용자가 직접.
신호는 coin_sleeve와 같은 계산(_forecast). 권장 보유비율 = min(1, 예측값/10), 나머지는 USDT/원화 대기.

신호 데이터: ZEC = 바이낸스 ZECUSDT 일봉, HYPE = 하이퍼리퀴드 HYPE 일봉(코인원은 2026-06 상장이라 이력 부족).
둘 다 UTC 00:00(KST 09:00) 마감 — 진행 중인 봉은 제외.
보유수량은 .env ALT_HOLDINGS(예: "ZEC=0.35800079,HYPE=5.05720823") — 매매하면 갱신 필요.
"""
from __future__ import annotations

import os
import time

import pandas as pd
import requests

from coin_sleeve import MIN_DAYS, _forecast

ALTS = {"ZEC": "바이낸스", "HYPE": "코인원"}


def holdings() -> dict:
    raw = os.environ.get("ALT_HOLDINGS", "")
    return {k.strip(): float(v) for k, v in (p.split("=") for p in raw.split(",") if "=" in p)}


def _binance_closes(symbol: str) -> pd.Series:
    rows, start = [], 0
    while True:
        resp = requests.get("https://api.binance.com/api/v3/klines",
                            params={"symbol": symbol, "interval": "1d", "startTime": start, "limit": 1000}, timeout=10)
        resp.raise_for_status()
        batch = resp.json()
        rows += batch
        if len(batch) < 1000:
            break
        start = batch[-1][0] + 1
        time.sleep(0.2)
    return pd.Series({pd.Timestamp(k[0], unit="ms"): float(k[4]) for k in rows}).sort_index()


def _hyperliquid_closes(coin: str) -> pd.Series:
    end = int(time.time() * 1000)
    resp = requests.post("https://api.hyperliquid.xyz/info", timeout=15, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d", "startTime": end - 5000 * 86_400_000, "endTime": end}})
    resp.raise_for_status()
    return pd.Series({pd.Timestamp(int(c["t"]), unit="ms"): float(c["c"]) for c in resp.json()}).sort_index()


def _completed(closes: pd.Series) -> pd.Series:
    today_utc = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    return closes[closes.index < today_utc]


def compute_forecasts() -> dict:
    series = {"ZEC": _binance_closes("ZECUSDT"), "HYPE": _hyperliquid_closes("HYPE")}
    out = {}
    for coin, s in series.items():
        s = _completed(s)
        if len(s) < MIN_DAYS:
            raise RuntimeError(f"{coin} 추세신호 계산에 필요한 데이터 부족: {len(s)}일")
        out[coin] = _forecast(s)
    return out


def values_krw(usdkrw: float) -> dict:
    """보유수량 × 현재가(원화). ZEC는 바이낸스 USDT 가격 × 환율, HYPE는 코인원 원화 가격."""
    qty = holdings()
    zec = float(requests.get("https://api.binance.com/api/v3/ticker/price",
                             params={"symbol": "ZECUSDT"}, timeout=10).json()["price"]) * usdkrw
    hype = float(requests.get("https://api.coinone.co.kr/public/v2/ticker_new/KRW/HYPE",
                              timeout=10).json()["tickers"][0]["last"])
    return {"ZEC": qty.get("ZEC", 0.0) * zec, "HYPE": qty.get("HYPE", 0.0) * hype}
