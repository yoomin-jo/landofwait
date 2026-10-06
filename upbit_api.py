"""업비트 REST API 래퍼 (코인 슬리브, DESIGN.md Sleeve 3).
환경변수: UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY (bot/.env, Pi 공인 IP를 업비트 허용 IP로 등록해야 함).
키 권한은 자산조회·주문조회·주문하기만 사용 — 출금 권한 불필요.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from urllib.parse import urlencode

import jwt
import pandas as pd
import requests

_BASE_URL = "https://api.upbit.com"
MIN_ORDER_KRW = 5000


def _auth_headers(params: dict | None = None) -> dict:
    payload = {"access_key": os.environ["UPBIT_ACCESS_KEY"], "nonce": str(uuid.uuid4())}
    if params:
        payload["query_hash"] = hashlib.sha512(urlencode(params).encode()).hexdigest()
        payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, os.environ["UPBIT_SECRET_KEY"], algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def get_balances() -> dict:
    """{"KRW": float, "BTC": float, ...} — 주문 대기(locked) 포함 보유수량."""
    resp = requests.get(f"{_BASE_URL}/v1/accounts", headers=_auth_headers(), timeout=10)
    resp.raise_for_status()
    return {a["currency"]: float(a["balance"]) + float(a["locked"]) for a in resp.json()}


def get_prices(markets: list[str]) -> dict:
    """{"KRW-BTC": 현재가, ...}"""
    resp = requests.get(f"{_BASE_URL}/v1/ticker", params={"markets": ",".join(markets)}, timeout=10)
    resp.raise_for_status()
    return {t["market"]: float(t["trade_price"]) for t in resp.json()}


def get_daily_closes(market: str, days: int = 1500) -> pd.Series:
    """완료된 일봉 종가(KST 09:00 시작~다음 날 09:00 마감). 시작 후 24시간이 안 지난 진행 중인 봉은 제외 —
    07:00 실행 때 어제 09:00에 시작한 봉이 날짜만 보고 완성 봉으로 섞이지 않게(2026-10-07)."""
    rows, to = [], None
    while len(rows) < days:
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to
        resp = requests.get(f"{_BASE_URL}/v1/candles/days", params=params, timeout=10)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows += batch
        to = batch[-1]["candle_date_time_utc"].replace("T", " ")
        time.sleep(0.15)  # 시세 API 초당 요청 제한
    now_kst = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    done = {pd.Timestamp(c["candle_date_time_kst"][:10]): float(c["trade_price"]) for c in rows
            if pd.Timestamp(c["candle_date_time_kst"]) + pd.Timedelta(days=1) <= now_kst}
    return pd.Series(done).sort_index()


def _order(body: dict) -> dict:
    resp = requests.post(f"{_BASE_URL}/v1/orders", json=body, headers=_auth_headers(body), timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"업비트 주문 실패 {resp.status_code}: {resp.text}")
    return resp.json()


def buy_market_krw(market: str, krw: float) -> dict:
    """시장가 매수 — 원화 금액 지정."""
    return _order({"market": market, "side": "bid", "ord_type": "price", "price": str(int(krw))})


def sell_market_volume(market: str, volume: float) -> dict:
    """시장가 매도 — 수량 지정."""
    return _order({"market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"})
