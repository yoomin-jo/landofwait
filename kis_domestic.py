"""KIS(한국투자증권) 국내주식 REST API 래퍼. kis_overseas.py와 동일한 KisToken/tr_id 패턴,
/uapi/domestic-stock/* 엔드포인트만 다름. quant-platform의 brokers/kis.py를 TR코드/페이로드
형태 레퍼런스로 참고했으나 코드 자체는 새로 작성함 (그 프로젝트는 미사용/미검증 코드라 신뢰 X).
모의투자 없음 — 무조건 실전(prod) 계좌. 계좌마다 별도 앱키 사용, get_access_token(prefix)로 선택.

환경변수 (prefix별로 3개 세트):
  KIS_ISA_APP_KEY / KIS_ISA_APP_SECRET / KIS_ISA_ACCOUNT (올웨더 — 국고채30/미국채30/금현물 ETF 3종, RP는 수동)
  KIS_OVERSEAS_APP_KEY / ... (소형주퀀트 계좌, 국내주식 매매 시 KR퀀트용. kis_overseas.py와 계좌 공유 — 일반 위탁계좌라 국내+해외 겸용)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

_BASE_URL = "https://openapi.koreainvestment.com:9443"

_TR_ORDER_BUY = "TTTC0012U"  # 문서: (매수) TTTC0012U
_TR_ORDER_SELL = "TTTC0011U"  # 문서: (매도) TTTC0011U
_TR_BALANCE = "TTTC8434R"
_TR_PRICE = "FHKST01010100"

_TOKEN_CACHE_DIR = Path(__file__).parent / "data"


class KisToken:
    def __init__(self, app_key: str, app_sec: str, account: str):
        self.app_key = app_key
        self.app_sec = app_sec
        self.account = account
        self._token = ""
        self._token_exp = 0.0
        self._cache_file = _TOKEN_CACHE_DIR / f"kis_domestic_token_{account}.json"

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
            "custtype": "P",
            "Content-Type": "application/json; charset=utf-8",
        }


def get_access_token(prefix: str = "ISA") -> KisToken:
    """prefix: "ISA"(올웨더 채권/금 ETF 3종) 또는 "OVERSEAS"(국내주식 매매 시 KR퀀트용, kis_overseas.py와 계좌 공유)."""
    return KisToken(
        os.environ[f"KIS_{prefix}_APP_KEY"],
        os.environ[f"KIS_{prefix}_APP_SECRET"],
        os.environ[f"KIS_{prefix}_ACCOUNT"],
    )


def get_balance(token: KisToken) -> dict:
    """{"cash": float, "eval_amt": float, "qty": {종목코드: 보유수량}}"""
    acct_no, prod_cd = token.account[:8], token.account[8:]

    qty: dict[str, int] = {}
    eval_amt = 0.0
    fk100, nk100 = "", ""
    while True:
        resp = requests.get(
            f"{_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=token.headers(_TR_BALANCE),
            params={
                "CANO": acct_no, "ACNT_PRDT_CD": prod_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                "CTX_AREA_FK100": fk100, "CTX_AREA_NK100": nk100,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise RuntimeError(f"국내주식 잔고 조회 실패: {data.get('msg1', data)}")

        for item in data.get("output1", []):
            q = int(item.get("hldg_qty", 0) or 0)
            if q > 0:
                qty[item["pdno"]] = q
                eval_amt += float(item.get("evlu_amt", 0) or 0)

        if data.get("tr_cont") not in ("F", "M"):
            break
        fk100 = data.get("ctx_area_fk100", "")
        nk100 = data.get("ctx_area_nk100", "")
        time.sleep(0.3)

    output2 = data.get("output2", [{}])
    cash = float(output2[0].get("prvs_rcdl_excc_amt", 0) or 0) if output2 else 0.0

    return {"cash": cash, "eval_amt": eval_amt, "qty": qty}


def get_total_assets(token: KisToken) -> float:
    """투자계좌자산현황조회(CTRP6548R, HTS [0891] 결제기준) 총자산금액.
    get_balance()의 주식잔고조회에는 RP가 안 잡혀 계좌 총액이 작게 나오므로, RP를 포함한 계좌 전체
    금액이 필요할 때 사용(2026-10-05 ISA 실조회로 RP 행이 포함됨을 확인)."""
    resp = requests.get(
        f"{_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-account-balance",
        headers=token.headers("CTRP6548R"),
        params={"CANO": token.account[:8], "ACNT_PRDT_CD": token.account[8:],
                "INQR_DVSN_1": "", "BSPR_BF_DT_APLY_YN": ""},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"투자계좌자산현황 조회 실패: {data.get('msg1', data)}")
    output2 = data.get("output2")
    output2 = output2[0] if isinstance(output2, list) else output2
    return float(output2["tot_asst_amt"])


def get_current_price(token: KisToken, ticker: str) -> float:
    resp = requests.get(
        f"{_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=token.headers(_TR_PRICE),
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"현재가 조회 실패: {data.get('msg1', data)}")
    return float(data["output"]["stck_prpr"])


def buy_market(token: KisToken, ticker: str, qty: int) -> dict:
    return _order(token, ticker, "buy", qty, ord_dvsn="01", price=0)


def sell_market(token: KisToken, ticker: str, qty: int) -> dict:
    """시장가 매도 거부 이슈 회피용으로 현재가 지정가 주문 사용(quant-platform에서 확인된 KIS 특성)."""
    price = get_current_price(token, ticker)
    return _order(token, ticker, "sell", qty, ord_dvsn="00", price=int(price))


def buy_limit(token: KisToken, ticker: str, qty: int) -> dict:
    """지정가 매수 — 현재가로 주문(2026-10 KR퀀트 리밸런싱용, 관리종목 등 단일가매매 대응)."""
    price = get_current_price(token, ticker)
    return _order(token, ticker, "buy", qty, ord_dvsn="00", price=int(price))


def _order(token: KisToken, ticker: str, side: str, qty: int, ord_dvsn: str, price: int) -> dict:
    tr_id = _TR_ORDER_BUY if side == "buy" else _TR_ORDER_SELL
    body = {
        "CANO": token.account[:8],
        "ACNT_PRDT_CD": token.account[8:],
        "PDNO": ticker,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(price),
    }
    resp = requests.post(
        f"{_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=token.headers(tr_id), json=body, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"주문 거부: {data.get('msg1', data)}")
    return data
