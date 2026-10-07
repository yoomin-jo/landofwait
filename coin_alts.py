"""코인 슬리브 알트(ZEC 바이낸스, HYPE 코인원) — 추세신호 자동매매 (DESIGN.md Sleeve 3).
각 거래소 잔고(코인 + 대기 현금: 바이낸스 USDT, 코인원 원화)를 그 알트의 예산으로 본다.
권장 보유비율 = min(1, 예측값/10), 나머지는 대기 현금. 신호는 coin_sleeve와 같은 계산(_forecast).

신호 데이터: ZEC = 바이낸스 ZECUSDT 일봉, HYPE = 하이퍼리퀴드 HYPE 일봉(코인원은 2026-06 상장이라 이력 부족).
둘 다 UTC 00:00(KST 09:00) 마감 — 진행 중인 봉은 제외.

API 키가 없는 거래소는 잔고를 .env ALT_HOLDINGS(예: "ZEC=0.358")로 보고 알림만 보낸다(has_keys).
환경변수: COINONE_ACCESS_KEY/SECRET_KEY, BINANCE_API_KEY/SECRET_KEY (Pi 공인 IP 허용 등록, 출금 권한 없음).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
import uuid
from urllib.parse import urlencode

import pandas as pd
import requests

from coin_sleeve import MIN_DAYS, _forecast

ALTS = {
    "ZEC": {"venue": "바이낸스", "quote": "USDT", "min_order": 6.0},    # 바이낸스 최소 주문 5 USDT + 여유
    "HYPE": {"venue": "코인원", "quote": "KRW", "min_order": 5000.0},
}


def has_keys(coin: str) -> bool:
    return bool(os.environ.get("BINANCE_API_KEY" if coin == "ZEC" else "COINONE_ACCESS_KEY"))


def _env_holdings() -> dict:
    raw = os.environ.get("ALT_HOLDINGS", "")
    return {k.strip(): float(v) for k, v in (p.split("=") for p in raw.split(",") if "=" in p)}


# ── 시세 (공개 API) ────────────────────────────────────────────────

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


def price(coin: str) -> float:
    """거래 통화 기준 현재가 — ZEC는 USDT, HYPE는 원화."""
    if coin == "ZEC":
        return float(requests.get("https://api.binance.com/api/v3/ticker/price",
                                  params={"symbol": "ZECUSDT"}, timeout=10).json()["price"])
    return float(requests.get("https://api.coinone.co.kr/public/v2/ticker_new/KRW/HYPE",
                              timeout=10).json()["tickers"][0]["last"])


# ── 코인원 (v2.1) ──────────────────────────────────────────────────

def _coinone_post(path: str, body: dict) -> dict:
    payload = {"access_token": os.environ["COINONE_ACCESS_KEY"], "nonce": str(uuid.uuid4()), **body}
    encoded = base64.b64encode(json.dumps(payload).encode())
    signature = hmac.new(os.environ["COINONE_SECRET_KEY"].encode(), encoded, hashlib.sha512).hexdigest()
    resp = requests.post(f"https://api.coinone.co.kr{path}", data=encoded, timeout=10, headers={
        "Content-Type": "application/json", "X-COINONE-PAYLOAD": encoded.decode(), "X-COINONE-SIGNATURE": signature})
    data = resp.json()
    if data.get("result") != "success":
        raise RuntimeError(f"코인원 {path} 실패: {data.get('error_code')} {data.get('error_msg', '')}")
    return data


def _coinone_balance(coin: str) -> tuple[float, float]:
    """(코인 수량, 원화) — 주문 대기(limit) 포함."""
    bal = {b["currency"]: float(b["available"]) + float(b["limit"])
           for b in _coinone_post("/v2.1/account/balance/all", {})["balances"]}
    return bal.get(coin, 0.0), bal.get("KRW", 0.0)


def _coinone_order(coin: str, side: str, qty: float | None = None, quote_amount: float | None = None) -> dict:
    body = {"side": "BUY" if side == "buy" else "SELL", "quote_currency": "KRW", "target_currency": coin,
            "type": "MARKET"}
    if side == "buy":
        body["amount"] = str(int(quote_amount))
    else:
        body["qty"] = f"{math.floor(qty * 1e8) / 1e8:.8f}"  # 코인원 qty_unit 0.00000001(BTC·HYPE 공통)
    return _coinone_post("/v2.1/order", body)


# ── 바이낸스 (현물) ────────────────────────────────────────────────

def _binance_signed(method: str, path: str, params: dict) -> dict:
    params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
    query = urlencode(params)
    sig = hmac.new(os.environ["BINANCE_SECRET_KEY"].encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.request(method, f"https://api.binance.com{path}?{query}&signature={sig}",
                            headers={"X-MBX-APIKEY": os.environ["BINANCE_API_KEY"]}, timeout=10)
    if resp.status_code >= 400:
        raise RuntimeError(f"바이낸스 {path} 실패 {resp.status_code}: {resp.text}")
    return resp.json()


def _binance_balance(coin: str) -> tuple[float, float]:
    """(코인 수량, USDT) — 주문 대기(locked) 포함."""
    bal = {b["asset"]: float(b["free"]) + float(b["locked"])
           for b in _binance_signed("GET", "/api/v3/account", {})["balances"]}
    return bal.get(coin, 0.0), bal.get("USDT", 0.0)


def _binance_step(symbol: str) -> float:
    info = requests.get("https://api.binance.com/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=10).json()
    lot = next(f for f in info["symbols"][0]["filters"] if f["filterType"] == "LOT_SIZE")
    return float(lot["stepSize"])


def _binance_order(coin: str, side: str, qty: float | None = None, quote_amount: float | None = None) -> dict:
    symbol = f"{coin}USDT"
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET"}
    if side == "buy":
        params["quoteOrderQty"] = f"{quote_amount:.2f}"
    else:
        step = _binance_step(symbol)
        decimals = max(0, -int(math.floor(math.log10(step))))
        params["quantity"] = f"{math.floor(qty / step) * step:.{decimals}f}"
    return _binance_signed("POST", "/api/v3/order", params)


# ── 공통 ───────────────────────────────────────────────────────────

def balance(coin: str) -> tuple[float, float]:
    """(코인 수량, 대기 현금[USDT 또는 원화]). 키가 없으면 .env ALT_HOLDINGS 수량, 현금 0."""
    if not has_keys(coin):
        return _env_holdings().get(coin, 0.0), 0.0
    if coin == "ZEC":
        return _binance_balance(coin)
    qty, krw = _coinone_balance(coin)
    return qty, max(0.0, krw - _exp_ledger()["krw"])  # 코인원 원화 중 실험 봇(coin_exp) 몫은 HYPE 예산에서 제외


def _exp_ledger() -> dict:
    """코인 실험 봇 장부(coin_exp.py) — 없으면 0."""
    path = os.path.join(os.path.dirname(__file__), "data", "coin_exp_state.json")
    if not os.path.exists(path):
        return {"krw": 0.0, "btc": 0.0}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def place_order(coin: str, side: str, qty: float | None = None, quote_amount: float | None = None) -> dict:
    if coin == "ZEC":
        return _binance_order(coin, side, qty, quote_amount)
    return _coinone_order(coin, side, qty, quote_amount)


def values_krw(usdkrw: float) -> dict:
    """알트별 예산(코인 평가액 + 그 거래소 대기 현금)의 원화 환산."""
    out = {}
    for coin, cfg in ALTS.items():
        qty, cash = balance(coin)
        value = qty * price(coin) + cash
        out[coin] = value * usdkrw if cfg["quote"] == "USDT" else value
    exp = _exp_ledger()
    if exp["krw"] or exp["btc"]:  # 코인 실험 봇(코인원 BTC) 장부 평가액도 코인 슬리브에 포함
        btc_px = float(requests.get("https://api.coinone.co.kr/public/v2/ticker_new/KRW/BTC",
                                    timeout=10).json()["tickers"][0]["last"])
        out["EXP"] = exp["krw"] + exp["btc"] * btc_px
    return out
