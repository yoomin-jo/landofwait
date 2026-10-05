"""올웨더 슬리브 채권/금 ETF 3종 월간 리밸런싱 자동매매 (DESIGN.md Sleeve 1, Carver식 연속 추세신호).
ISA 계좌 전용(소형성장주 KR/US는 소형주퀀트 계좌 소관, 별도 파이프라인).

신호 미보유분 대기자금은 RP로 전환하는데, RP는 KIS Open API로 매수 불가(API 미지원 확인됨)라
자동매매 대상에서 제외 — 그냥 현금으로 남겨두고 텔레그램으로 수동 RP 전환 알림만 보낸다.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from pandas.tseries.offsets import BMonthEnd

from olweather_etf_sleeve import BUFFER, TICKERS, WEIGHT_IN_ISA, compute_target_weights
from kis_domestic import get_access_token, get_balance, get_current_price, get_total_assets, buy_market, sell_market

load_dotenv()

ALL_NAMES = TICKERS
STATE_PATH = Path(__file__).parent / "data" / "olweather_etf_state.json"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def notify(message: str) -> None:
    logger.info(message)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except requests.RequestException:
        logger.exception("텔레그램 알림 전송 실패")


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_month_end_today() -> bool:
    """국내(KRX) 거래일 기준이라 미국 계산 같은 시차 보정 불필요. 주말만 고려, 공휴일 미반영(근사치)."""
    today = pd.Timestamp.now().normalize()
    return today == BMonthEnd().rollforward(today)


def compute_rebalance_orders(current_qty: dict, prices: dict, total: float, weights: dict) -> list[dict]:
    orders = []
    for ticker in ALL_NAMES:
        # 버퍼: 목표와 현재 비중 차이가 기본비중의 10% 미만이면 매매 안 함 (Carver buffering)
        current_w = current_qty.get(ticker, 0) * prices.get(ticker, 0) / total if total > 0 else 0.0
        if abs(weights.get(ticker, 0.0) - current_w) < BUFFER * WEIGHT_IN_ISA[ticker]:
            continue
        target_value = total * weights.get(ticker, 0.0)
        target_qty = int(target_value / prices[ticker]) if prices.get(ticker) else 0
        diff = target_qty - current_qty.get(ticker, 0)
        if diff == 0:
            continue
        orders.append({"ticker": ticker, "qty": abs(diff), "side": "sell" if diff < 0 else "buy", "price": prices[ticker]})
    return orders


def run() -> None:
    if not is_month_end_today():
        logger.info("오늘은 월말이 아님 — 스킵")
        return

    state = _load_state()
    month_key = datetime.now().strftime("%Y-%m")
    if state.get("last_rebalance_month") == month_key:
        logger.info("이번 달(%s) 이미 리밸런싱 완료 — 스킵", month_key)
        return

    notify("[올웨더 ETF 슬리브 리밸런싱] 시작")

    try:
        weights = compute_target_weights()
    except Exception as e:
        notify(f"목표비중 계산 실패 — 리밸런싱 중단: {e}")
        return
    notify(f"목표비중: {weights}")

    token = get_access_token("ISA")
    try:
        balance = get_balance(token)
        current_qty = {t: balance["qty"].get(t, 0) for t in ALL_NAMES}
        # 주식잔고조회(cash+eval_amt)에는 RP가 빠져 총액이 작게 잡힘 → 보유 ETF를 잘못 매도하게 됨.
        # 비중 기준 총액은 RP 포함 총자산(CTRP6548R)을 쓴다 (2026-10-05).
        total = get_total_assets(token)
        notify(f"ISA(올웨더) 총자산(RP 포함): ₩{total:,.0f} (예수금 ₩{balance['cash']:,.0f})")
    except Exception as e:
        notify(f"잔고 조회 실패: {e}")
        return

    prices = {}
    for ticker in ALL_NAMES:
        try:
            prices[ticker] = get_current_price(token, ticker)
        except Exception as e:
            notify(f"{ticker} 현재가 조회 실패: {e}")
            return

    orders = compute_rebalance_orders(current_qty, prices, total, weights)
    if not orders:
        notify("리밸런싱 불필요 (목표비중과 이미 일치)")
        state["last_rebalance_month"] = month_key
        _save_state(state)
        return

    sells = [o for o in orders if o["side"] == "sell"]
    buys = [o for o in orders if o["side"] == "buy"]

    has_failure = False
    for order in sells:
        ticker, qty = order["ticker"], order["qty"]
        try:
            sell_market(token, ticker, qty)
            notify(f"sell {ALL_NAMES[ticker]}({ticker}) {qty}주 @ 시장가")
        except Exception:
            logger.exception("매도 실패: %s", order)
            notify(f"매도 실패: {ALL_NAMES[ticker]}({ticker}) {qty}주")
            has_failure = True

    shortfall = 0.0
    if buys and not has_failure:
        notify("매도 체결 대기 30초...")
        import time
        time.sleep(30)
        try:
            cash_left = get_balance(token)["cash"]
        except Exception as e:
            notify(f"매수 전 예수금 조회 실패 — 매수 중단: {e}")
            cash_left, has_failure = 0.0, True
            buys = []
        for order in buys:
            ticker = order["ticker"]
            # 대기자금이 RP에 있으면 예수금이 모자람 — 살 수 있는 만큼만 사고 부족분은 RP 인출 알림
            unit_cost = order["price"] * 1.005
            qty = min(order["qty"], int(cash_left / unit_cost))
            shortfall += (order["qty"] - qty) * unit_cost
            if qty <= 0:
                continue
            cash_left -= qty * unit_cost
            try:
                buy_market(token, ticker, qty)
                notify(f"buy {ALL_NAMES[ticker]}({ticker}) {qty}주 @ 시장가")
            except Exception:
                logger.exception("매수 실패: %s", order)
                notify(f"매수 실패: {ALL_NAMES[ticker]}({ticker}) {qty}주")
                has_failure = True

    if shortfall > 0:
        has_failure = True
        notify(f"예수금 부족 — RP에서 약 ₩{shortfall:,.0f} 인출(매도) 후 오늘 안에 /run_olweather 재실행하세요")

    idle_ratio = 1.0 - sum(weights.values())
    if idle_ratio > 0 and not has_failure:
        notify(f"대기자금 목표 약 ₩{total * idle_ratio:,.0f} (RP 포함) — 예수금으로 남은 몫은 RP로 수동 전환하세요")

    if has_failure:
        notify("일부 주문 실패 — 다음 실행 시 재시도")
    else:
        state["last_rebalance_month"] = month_key
    _save_state(state)
    notify("[올웨더 ETF 슬리브 리밸런싱] 완료")


if __name__ == "__main__":
    run()
