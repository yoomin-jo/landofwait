"""코인 실험 봇 — 미국장 장외 시간에만 보유(코인원 KRW 마켓, 코인별 예산 1만 원, 2026-10-07 시작).
대상 BTC·ETH·HYPE(2026-10-07 사용자 결정). 근거(30분봉 장중/장외/보유 CAGR):
  BTC 2018~ -2.4% / +25.0% / +22.0%, 손익분기 편도비용 0.005%, 코인원 실측 편도 약 0.004%
  ETH 2018~ -17.4% / +35.6% / +12.0%, 손익분기 0.038%, 코인원 실측 약 0.014%
  HYPE(1시간봉 2026-03~, 7개월뿐) -26.2% / +499% / +342%, 손익분기 0.060%, 코인원 실측 약 0.41%(호가 얇음)
2022·2026년엔 패턴이 반대였던 해도 있어 본 자산 대신 소액 실험으로 실제 비용·성과를 잰다. 코인원 API 수수료 0%.

매수: 뉴욕 평일 15:55(장 마감 5분 전) / 매도: 뉴욕 평일 09:25(장 시작 5분 전) — systemd 타이머가 뉴욕 시간대로 예약.
주문: 최우선 호가 지정가(post_only, 매수=최우선 매수호가·매도=최우선 매도호가) → 1분마다 체결 확인, 남으면 취소 후
새 최우선 호가로 재주문(최대 4회) → BTC·ETH는 남은 양을 시장가로 마무리, HYPE는 호가가 얇아 시장가 없이 그날 건너뜀.
세 코인은 병렬 처리(장 시작 전에 끝나도록), 체결은 주문별 상세(executed_qty·average_executed_price)로 집계.

실험 자금은 코인별 장부(data/coin_exp_state.json)로 관리 — 코인원 원화·HYPE는 HYPE 봇과 같은 계좌라
coin_alts가 장부의 원화 합계와 HYPE 수량을 HYPE 봇 예산에서 뺀다. 최소 주문 5,000원 미만 잔량은 다음 회차로 이월.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import coin_alts as a
import pause

load_dotenv()

STATE_PATH = Path(__file__).parent / "data" / "coin_exp_state.json"
TRADES_PATH = Path(__file__).parent / "data" / "coin_exp_trades.csv"
COINS = {"BTC": {"budget": 10_000, "market_fallback": True},
         "ETH": {"budget": 10_000, "market_fallback": True},
         "HYPE": {"budget": 10_000, "market_fallback": False}}
MIN_ORDER = 5_000
LIVE = os.environ.get("COIN_LIVE") == "1"
ATTEMPTS, WAIT = 4, 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _new_ledger(budget: float) -> dict:
    return {"krw": float(budget), "qty": 0.0, "start_price": None, "trades": 0, "cost_bp_sum": 0.0}


def load_state() -> dict:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    if "coins" not in st:  # 2026-10-07 BTC 단일 장부 → 코인별 장부로 변환
        old = st
        st = {"start": old.get("start", datetime.now().strftime("%Y-%m-%d %H:%M")), "coins": {}}
        if old:
            st["coins"]["BTC"] = {"krw": old["krw"], "qty": old["btc"], "start_price": old["start_btc_price"],
                                  "trades": old["trades"], "cost_bp_sum": old["cost_bp_sum"]}
    for coin, cfg in COINS.items():
        st["coins"].setdefault(coin, _new_ledger(cfg["budget"]))
    return st


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def orderbook(coin: str) -> tuple[float, float]:
    d = requests.get(f"https://api.coinone.co.kr/public/v2/orderbook/KRW/{coin}", params={"size": 5}, timeout=10).json()
    return float(d["bids"][0]["price"]), float(d["asks"][0]["price"])


def _limit(coin: str, side: str, price: float, qty: float) -> str:
    body = {"side": side.upper(), "quote_currency": "KRW", "target_currency": coin, "type": "LIMIT",
            "price": f"{price:f}".rstrip("0").rstrip("."), "qty": f"{math.floor(qty * 1e8) / 1e8:.8f}", "post_only": True}
    return a._coinone_post("/v2.1/order", body)["order_id"]


def _cancel(coin: str, order_id: str) -> None:
    try:
        a._coinone_post("/v2.1/order/cancel", {"order_id": order_id, "quote_currency": "KRW", "target_currency": coin})
    except RuntimeError as e:  # 이미 전량 체결이면 취소할 게 없어 오류 — 상세 조회로 판단
        logger.info("%s 취소 응답: %s", coin, e)


def _detail(coin: str, order_id: str) -> dict:
    d = a._coinone_post("/v2.1/order/detail", {"order_id": order_id, "quote_currency": "KRW", "target_currency": coin})
    return d.get("order") or d


def _filled(coin: str, order_id: str) -> tuple[float, float]:
    """(체결 수량, 체결 금액 원화) — 주문 상세 기준."""
    o = _detail(coin, order_id)
    qty = float(o.get("executed_qty") or 0)
    return qty, qty * float(o.get("average_executed_price") or 0)


def run_coin(coin: str, side: str, led: dict) -> dict | None:
    """한 코인의 매수/매도. 반환: 기록용 dict(매매 없으면 None). led는 호출자가 저장."""
    bid, ask = orderbook(coin)
    mid = (bid + ask) / 2
    if side == "buy" and led["krw"] < MIN_ORDER:
        logger.info("%s 장부 원화 %.0f < 최소주문 — 매수 생략", coin, led["krw"]); return None
    if side == "sell" and led["qty"] * bid < MIN_ORDER:
        logger.info("%s 장부 평가 %.0f < 최소주문 — 매도 생략", coin, led["qty"] * bid); return None

    got_qty = got_krw = 0.0  # 매수: +수량/−원화, 매도: −수량/+원화의 절댓값 누적
    how = "지정가"

    def left() -> float:  # 매수: 남은 원화, 매도: 남은 수량
        return led["krw"] - got_krw if side == "buy" else led["qty"] - got_qty

    if LIVE:
        for _ in range(ATTEMPTS):
            b, k = orderbook(coin)
            px = b if side == "buy" else k
            if (left() if side == "buy" else left() * px) < MIN_ORDER:
                break
            oid = _limit(coin, side, px, left() / px if side == "buy" else left())
            time.sleep(WAIT)
            if _detail(coin, oid).get("status") != "FILLED":
                _cancel(coin, oid)
                time.sleep(2)
            q, k_ = _filled(coin, oid)
            got_qty += q; got_krw += k_
        b, k = orderbook(coin)
        rest = left() if side == "buy" else left() * b
        if rest >= MIN_ORDER and COINS[coin]["market_fallback"]:
            how = "지정가+시장가 마무리"
            r = a._coinone_order(coin, side, qty=None if side == "buy" else left(),
                                 quote_amount=left() if side == "buy" else None)
            time.sleep(3)
            q, k_ = _filled(coin, r["order_id"])
            got_qty += q; got_krw += k_
        elif rest >= MIN_ORDER:
            how = "지정가 일부 미체결 — 이월" if got_qty else "지정가 미체결 — 이번 회차 건너뜀"
    else:  # 모의: 중간가 전량 체결 가정
        got_qty = led["krw"] / mid if side == "buy" else led["qty"]
        got_krw = led["krw"] if side == "buy" else led["qty"] * mid

    if got_qty <= 0:
        logger.info("%s %s: 체결 없음 (%s)", coin, side, how)
        return {"coin": coin, "side": side, "how": how, "qty": 0, "krw": 0, "fill": 0, "mid": round(mid), "cost_bp": 0}
    sign = 1 if side == "buy" else -1
    led["qty"] += sign * got_qty
    led["krw"] -= sign * got_krw
    fill = got_krw / got_qty
    cost_bp = ((fill - mid) / mid if side == "buy" else (mid - fill) / mid) * 1e4  # +면 비용, −면 이득
    led["trades"] += 1
    led["cost_bp_sum"] += cost_bp
    if led["start_price"] is None:
        led["start_price"] = mid
    logger.info("%s %s %s: 수량 %+.8f, 원화 %+.0f, 체결가 %.0f (중간가 대비 %.2fbp)",
                coin, side, how, sign * got_qty, -sign * got_krw, fill, cost_bp)
    return {"coin": coin, "side": side, "how": how, "qty": round(sign * got_qty, 8), "krw": round(-sign * got_krw),
            "fill": round(fill), "mid": round(mid), "cost_bp": round(cost_bp, 2)}


def summary() -> str:
    """텔레그램 /status용."""
    st = load_state()
    lines = ["[코인 실험 — 장외 시간 보유, 코인원]"]
    for coin, led in st["coins"].items():
        budget = COINS.get(coin, {}).get("budget", 10_000)
        if led["start_price"] is None:
            lines.append(f"  {coin}: 대기 중 (예산 ₩{budget:,})"); continue
        bid, ask = orderbook(coin)
        px = (bid + ask) / 2
        value = led["krw"] + led["qty"] * px
        hold = budget * px / led["start_price"]
        avg = led["cost_bp_sum"] / led["trades"] if led["trades"] else 0.0
        lines.append(f"  {coin}: ₩{value:,.0f} ({value / budget - 1:+.1%}) vs 그냥 보유 {hold / budget - 1:+.1%} "
                     f"/ {led['trades']}회, 평균 비용 {avg:.1f}bp")
    return "\n".join(lines)


def main(side: str) -> None:
    if pause.is_paused("coinexp"):
        logger.info("긴급 정지 중(/resume coinexp) — 스킵"); return
    st = load_state()
    _, krw = a._coinone_balance("BTC")
    if side == "buy":
        need = sum(led["krw"] for led in st["coins"].values())
        if krw + 1 < need:
            raise RuntimeError(f"코인원 원화 {krw:,.0f} < 실험 장부 합계 {need:,.0f} — 입금 확인 필요")
    errors = []
    with ThreadPoolExecutor(max_workers=len(st["coins"])) as ex:
        futures = {coin: ex.submit(run_coin, coin, side, led) for coin, led in st["coins"].items()}
        rows = {}
        for coin, f in futures.items():
            try:
                rows[coin] = f.result()
            except Exception as e:  # 한 코인 오류가 다른 코인 장부 저장을 막지 않게
                logger.exception("%s 오류", coin)
                errors.append(f"{coin}: {e}")
                rows[coin] = None
    save_state(st)
    new = not TRADES_PATH.exists()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        fields = ["time", "coin", "side", "how", "qty", "krw", "fill", "mid", "cost_bp", "ledger_krw", "ledger_qty", "live"]
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for coin, row in rows.items():
            if row:
                led = st["coins"][coin]
                w.writerow({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), **row,
                            "ledger_krw": round(led["krw"]), "ledger_qty": round(led["qty"], 8), "live": LIVE})
    if errors:  # 장부 저장 후 실패로 끝내서 서비스 실패 알림(OnFailure)이 가게
        raise RuntimeError("코인 실험 일부 실패 — " + " / ".join(errors))


if __name__ == "__main__":
    main(sys.argv[1])
