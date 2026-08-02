"""KIS(한국투자증권) 해외주식 REST API 래퍼. d:\\tqqq\\kis_api.py 패턴을 그대로 이식.
모의투자 없음 — 무조건 실전(prod) 계좌. 해외계좌가 2개(소형성장주 US용, HAA전용)로 분리되어 있어
get_access_token(prefix)로 어느 계좌를 쓸지 선택한다.

환경변수 (prefix별로 3개 세트):
  KIS_OVERSEAS_APP_KEY / KIS_OVERSEAS_APP_SECRET / KIS_OVERSEAS_ACCOUNT (소형성장주 US 등 일반 해외주식)
  KIS_HAA_APP_KEY / KIS_HAA_APP_SECRET / KIS_HAA_ACCOUNT (HAA 슬리브 전용)

국내(ISA) 계좌는 kis_domestic.py 참조.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

_BASE_URL = "https://openapi.koreainvestment.com:9443"

_TR_ORDER_BUY = "TTTT1002U"
_TR_ORDER_SELL = "TTTT1006U"
_TR_OVERSEAS_BALANCE = "TTTS3012R"
_TR_PSAMOUNT = "TTTS3007R"

_QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}

_TOKEN_CACHE_DIR = Path(__file__).parent / "data"


class KisToken:
    def __init__(self, app_key: str, app_sec: str, account: str):
        self.app_key = app_key
        self.app_sec = app_sec
        self.account = account
        self._token = ""
        self._token_exp = 0.0
        self._cache_file = _TOKEN_CACHE_DIR / f"kis_overseas_token_{account}.json"

    def _load_cached(self) -> str | None:
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if data.get("access_token") and time.time() < data.get("expires_at", 0):
                self._token = data["access_token"]
                self._token_exp = data["expires_at"]
                return self._token
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return None

    def _save_cached(self) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(
            json.dumps({"access_token": self._token, "expires_at": self._token_exp}, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        if self._load_cached():
            return self._token
        resp = requests.post(
            f"{_BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_sec},
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        self._token_exp = time.time() + 23 * 3600
        self._save_cached()
        return self._token

    def headers(self, tr_id: str) -> dict:
        return {
            "authorization": f"Bearer {self.get()}",
            "appkey": self.app_key,
            "appsecret": self.app_sec,
            "tr_id": tr_id,
            "Content-Type": "application/json; charset=utf-8",
        }


def get_access_token(prefix: str = "OVERSEAS") -> KisToken:
    """prefix: "OVERSEAS"(소형성장주 US 등 일반 해외주식) 또는 "HAA"(HAA 전용 계좌)."""
    return KisToken(
        os.environ[f"KIS_{prefix}_APP_KEY"],
        os.environ[f"KIS_{prefix}_APP_SECRET"],
        os.environ[f"KIS_{prefix}_ACCOUNT"],
    )


def get_us_balance(token: KisToken) -> dict:
    """{"equity_usd": float, "cash_usd": float, "qty": {ticker: 보유수량}}"""
    acct_no, prod_cd = token.account[:8], token.account[8:]

    resp = requests.get(
        f"{_BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount",
        headers=token.headers(_TR_PSAMOUNT),
        params={"CANO": acct_no, "ACNT_PRDT_CD": prod_cd, "OVRS_EXCG_CD": "NASD",
                "OVRS_ORD_UNPR": "1", "ITEM_CD": "AAPL"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"매수가능금액 조회 실패: {data.get('msg1', data)}")
    usd_cash = float(data.get("output", {}).get("ord_psbl_frcr_amt", 0) or 0)

    time.sleep(1)

    resp = requests.get(
        f"{_BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
        headers=token.headers(_TR_OVERSEAS_BALANCE),
        params={"CANO": acct_no, "ACNT_PRDT_CD": prod_cd, "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"해외주식 잔고 조회 실패: {data.get('msg1', data)}")

    qty: dict[str, int] = {}
    eval_amt_usd = 0.0
    for item in data.get("output1", []):
        q = int(item.get("ovrs_cblc_qty", 0) or 0)
        if q > 0:
            qty[item["ovrs_pdno"]] = q
            eval_amt_usd += float(item.get("ovrs_stck_evlu_amt", 0) or 0)

    return {"equity_usd": usd_cash + eval_amt_usd, "cash_usd": usd_cash, "qty": qty}


def get_current_price(token: KisToken, isu_no: str, ord_mkt_code: str) -> float:
    resp = requests.get(
        f"{_BASE_URL}/uapi/overseas-price/v1/quotations/price",
        headers=token.headers("HHDFS00000300"),
        params={"AUTH": "", "EXCD": _QUOTE_EXCD[ord_mkt_code], "SYMB": isu_no},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"현재가 조회 실패: {data.get('msg1', data)}")
    return float(data["output"]["last"])


def place_us_order(token: KisToken, ticker: str, exchange: str, side: str, qty: int, price: float) -> dict:
    """side: 'buy' or 'sell'. price: 지정가."""
    tr_id = _TR_ORDER_BUY if side == "buy" else _TR_ORDER_SELL
    body = {
        "CANO": token.account[:8],
        "ACNT_PRDT_CD": token.account[8:],
        "OVRS_EXCG_CD": exchange,
        "PDNO": ticker,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",
    }
    if side == "sell":
        body["SLL_TYPE"] = "00"

    resp = requests.post(
        f"{_BASE_URL}/uapi/overseas-stock/v1/trading/order",
        headers=token.headers(tr_id), json=body, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"주문 거부: {data.get('msg1', data)}")
    return data
