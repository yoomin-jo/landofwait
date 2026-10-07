"""코인 실험 봇 — 미국장 장외 시간에만 BTC 보유(코인원 KRW-BTC, 예산 1만 원, 2026-10-07 시작).
근거: 2018~2026 BTC 30분봉에서 미국장 중 CAGR -2.4%, 장외 +25.0%(그냥 보유 +22.0%). 다만 매일 2회 매매라
손익분기 편도 비용이 0.005%뿐이고, 2026년엔 패턴이 반대(장중 +7%, 장외 -11%) — 그래서 소액 실험으로 실제
비용과 성과를 잰다. 코인원 API 수수료 0%(trade_fee 조회 확인), BTC 스프레드 실측 약 0.0175%.

매수: 뉴욕 평일 15:55(장 마감 5분 전) / 매도: 뉴욕 평일 09:25(장 시작 5분 전) — systemd 타이머가 뉴욕 시간대로 예약.
주문: 최우선 호가 지정가(매수=최우선 매수호가, 매도=최우선 매도호가) → 1분마다 체결 확인, 안 걸리면 취소 후
새 최우선 호가로 재주문(최대 4회) → 남으면 시장가로 마무리(최소 주문 5,000원 미만 잔량은 다음 회차로 이월).

실험 자금은 장부(data/coin_exp_state.json)로 따로 관리 — 코인원 원화는 HYPE 봇의 대기 현금이기도 해서
coin_alts가 이 장부의 원화를 HYPE 예산에서 뺀다. 체결은 매 단계 코인원 잔고 변화로 계산한다.
사용: python coin_exp.py buy | sell
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import coin_alts as a
import pause

load_dotenv()

STATE_PATH = Path(__file__).parent / "data" / "coin_exp_state.json"
TRADES_PATH = Path(__file__).parent / "data" / "coin_exp_trades.csv"
BUDGET = 10_000
MIN_ORDER = 5_000
LIVE = os.environ.get("COIN_LIVE") == "1"
ATTEMPTS, WAIT = 4, 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"krw": float(BUDGET), "btc": 0.0, "start": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "start_btc_price": None, "trades": 0, "cost_bp_sum": 0.0}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def orderbook() -> tuple[float, float]:
    d = requests.get("https://api.coinone.co.kr/public/v2/orderbook/KRW/BTC", params={"size": 5}, timeout=10).json()
    return float(d["bids"][0]["price"]), float(d["asks"][0]["price"])


def _balances() -> tuple[float, float]:
    return a._coinone_balance("BTC")  # (BTC 수량, 원화)


def _limit(side: str, price: float, qty: float) -> str:
    body = {"side": side.upper(), "quote_currency": "KRW", "target_currency": "BTC", "type": "LIMIT",
            "price": str(int(price)), "qty": f"{math.floor(qty * 1e8) / 1e8:.8f}", "post_only": True}
    return a._coinone_post("/v2.1/order", body)["order_id"]


def _cancel(order_id: str) -> None:
    try:
        a._coinone_post("/v2.1/order/cancel", {"order_id": order_id, "quote_currency": "KRW", "target_currency": "BTC"})
    except RuntimeError as e:  # 이미 전량 체결돼 취소할 게 없으면 오류 — 무시하고 잔고로 판단
        logger.info("취소 응답: %s", e)


def _status(order_id: str) -> str:
    d = a._coinone_post("/v2.1/order/detail", {"order_id": order_id, "quote_currency": "KRW", "target_currency": "BTC"})
    return (d.get("order") or d).get("status", "")


def trade(side: str, st: dict) -> None:
    btc0, krw0 = _balances()
    bid, ask = orderbook()
    mid = (bid + ask) / 2
    if side == "buy":
        if st["krw"] < MIN_ORDER:
            logger.info("장부 원화 %.0f < 최소주문 — 매수 생략", st["krw"]); return
        if krw0 < st["krw"]:
            raise RuntimeError(f"코인원 원화 {krw0:,.0f} < 실험 장부 {st['krw']:,.0f} — 확인 필요")
    else:
        if st["btc"] * bid < MIN_ORDER:
            logger.info("장부 BTC 평가 %.0f < 최소주문 — 매도 생략", st["btc"] * bid); return
        if btc0 + 1e-9 < st["btc"]:
            raise RuntimeError(f"코인원 BTC {btc0} < 실험 장부 {st['btc']} — 확인 필요")

    def remaining() -> float:  # 매수: 남은 원화, 매도: 남은 BTC
        btc, krw = _balances()
        return st["krw"] - (krw0 - krw) if side == "buy" else st["btc"] - (btc0 - btc)

    how = "지정가"
    if LIVE:
        for _ in range(ATTEMPTS):
            left = remaining()
            b, k = orderbook()
            px = b if side == "buy" else k
            if (left if side == "buy" else left * px) < MIN_ORDER:
                break
            qty = left / px if side == "buy" else left
            oid = _limit(side, px, qty)
            time.sleep(WAIT)
            if _status(oid) != "FILLED":
                _cancel(oid)
                time.sleep(2)
        left = remaining()
        b, k = orderbook()
        if (left if side == "buy" else left * b) >= MIN_ORDER:
            how = "지정가+시장가 마무리"
            a._coinone_order("BTC", side, qty=None if side == "buy" else left,
                             quote_amount=left if side == "buy" else None)
            time.sleep(3)

    btc1, krw1 = _balances() if LIVE else (btc0, krw0)
    d_btc, d_krw = btc1 - btc0, krw1 - krw0
    if not LIVE:  # 모의: 중간가 전량 체결 가정
        d_btc = st["krw"] / mid if side == "buy" else -st["btc"]
        d_krw = -st["krw"] if side == "buy" else st["btc"] * mid
    st["btc"] += d_btc
    st["krw"] += d_krw
    fill = abs(d_krw / d_btc) if d_btc else mid
    cost_bp = ((fill - mid) / mid if side == "buy" else (mid - fill) / mid) * 1e4  # +면 비용, −면 이득
    st["trades"] += 1
    st["cost_bp_sum"] += cost_bp
    if st["start_btc_price"] is None:
        st["start_btc_price"] = mid
    row = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "side": side, "how": how, "btc": round(d_btc, 8),
           "krw": round(d_krw), "fill": round(fill), "mid": round(mid), "cost_bp": round(cost_bp, 2),
           "ledger_krw": round(st["krw"]), "ledger_btc": round(st["btc"], 8), "live": LIVE}
    new = not TRADES_PATH.exists()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    logger.info("%s %s: BTC %+.8f, 원화 %+.0f, 체결가 %.0f (중간가 대비 %.2fbp)", side, how, d_btc, d_krw, fill, cost_bp)


def summary() -> str:
    """텔레그램 /status·하트비트용 한 줄."""
    st = load_state()
    if st["start_btc_price"] is None:
        return f"[코인 실험] 대기 중 (예산 ₩{BUDGET:,})"
    bid, ask = orderbook()
    px = (bid + ask) / 2
    value = st["krw"] + st["btc"] * px
    hold = BUDGET * px / st["start_btc_price"]
    avg = st["cost_bp_sum"] / st["trades"] if st["trades"] else 0.0
    return (f"[코인 실험] ₩{value:,.0f} ({value / BUDGET - 1:+.1%}) vs 그냥 보유 ₩{hold:,.0f} ({hold / BUDGET - 1:+.1%}) "
            f"/ {st['trades']}회 매매, 평균 비용 {avg:.2f}bp")


def main(side: str) -> None:
    if pause.is_paused("coinexp"):
        logger.info("긴급 정지 중(/resume coinexp) — 스킵"); return
    st = load_state()
    trade(side, st)
    save_state(st)


if __name__ == "__main__":
    main(sys.argv[1])
