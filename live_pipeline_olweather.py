"""올웨더 슬리브 채권/금 ETF 3종 월간 리밸런싱 자동매매 (DESIGN.md Sleeve 1, 마켓타이밍 오버레이).
ISA 계좌에는 소형성장주 KR도 같이 있으므로, olweather_etf_sleeve.TICKERS(3개)만 건드리고
그 외 보유종목(소형성장주 KR 등)은 절대 매도/매수하지 않는다.

60SMA 이탈로 매도된 자금은 CMA 계좌(4333314821)로 수동 이체해야 함(KIS API로 계좌간 이체 불가) —
매도 후 T+2 영업일(국내주식 결제일)이 지나면 텔레그램으로 이체 가능 알림만 보낸다.
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from pandas.tseries.offsets import BDay, BMonthEnd

from olweather_etf_sleeve import TICKERS, compute_target_weights
from kis_domestic import get_access_token, get_balance, get_current_price, buy_market, sell_market

load_dotenv()

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
    return {"pending_settlements": []}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_month_end_today() -> bool:
    """국내(KRX) 거래일 기준이라 미국 계산 같은 시차 보정 불필요. 주말만 고려, 공휴일 미반영(근사치)."""
    today = pd.Timestamp.now().normalize()
    return today == BMonthEnd().rollforward(today)


def check_settlements(state: dict) -> None:
    """T+2 영업일 지난 매도 건은 CMA 이체 가능 알림(수동 이체 — API 미지원)."""
    today = pd.Timestamp.now().normalize()
    remaining = []
    for entry in state.get("pending_settlements", []):
        settle_date = pd.Timestamp(entry["settle_date"])
        if today >= settle_date:
            notify(
                f"[CMA 이체 가능] {entry['name']}({entry['ticker']}) 매도대금 ₩{entry['amount']:,.0f} "
                f"(매도일 {entry['sell_date']}) — CMA 계좌(4333314821)로 수동 이체하세요"
            )
        else:
            remaining.append(entry)
    state["pending_settlements"] = remaining


def compute_rebalance_orders(current_qty: dict, prices: dict, total: float, weights: dict) -> list[dict]:
    orders = []
    for ticker in TICKERS:
        target_value = total * weights.get(ticker, 0.0)
        target_qty = int(target_value / prices[ticker]) if prices.get(ticker) else 0
        diff = target_qty - current_qty.get(ticker, 0)
        if diff == 0:
            continue
        orders.append({"ticker": ticker, "qty": abs(diff), "side": "sell" if diff < 0 else "buy", "price": prices[ticker]})
    return orders


def run() -> None:
    state = _load_state()
    check_settlements(state)
    _save_state(state)

    if not is_month_end_today():
        logger.info("오늘은 월말이 아님 — 리밸런싱 스킵(정산 알림 체크만 수행)")
        return

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

    token = get_access_token()
    try:
        balance = get_balance(token)
        current_qty = {t: balance["qty"].get(t, 0) for t in TICKERS}
        total = balance["cash"] + balance["eval_amt"]
        notify(f"ISA 총평가액: ₩{total:,.0f} (현금 ₩{balance['cash']:,.0f})")
    except Exception as e:
        notify(f"잔고 조회 실패: {e}")
        return

    prices = {}
    for ticker in TICKERS:
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
    sell_date = pd.Timestamp.now().normalize()
    settle_date = sell_date + BDay(2)
    for order in sells:
        ticker, qty = order["ticker"], order["qty"]
        try:
            sell_market(token, ticker, qty)
            amount = qty * order["price"]
            notify(f"sell {TICKERS[ticker]}({ticker}) {qty}주 @ 시장가 (₩{amount:,.0f})")
            state.setdefault("pending_settlements", []).append({
                "ticker": ticker, "name": TICKERS[ticker], "amount": amount,
                "sell_date": str(sell_date.date()), "settle_date": str(settle_date.date()),
            })
        except Exception:
            logger.exception("매도 실패: %s", order)
            notify(f"매도 실패: {TICKERS[ticker]}({ticker}) {qty}주")
            has_failure = True

    if buys and not has_failure:
        notify("매도 체결 대기 30초...")
        time.sleep(30)
        for order in buys:
            ticker, qty = order["ticker"], order["qty"]
            try:
                buy_market(token, ticker, qty)
                notify(f"buy {TICKERS[ticker]}({ticker}) {qty}주 @ 시장가")
            except Exception:
                logger.exception("매수 실패: %s", order)
                notify(f"매수 실패: {TICKERS[ticker]}({ticker}) {qty}주")
                has_failure = True

    if has_failure:
        notify("일부 주문 실패 — 다음 실행 시 재시도")
    else:
        state["last_rebalance_month"] = month_key
    _save_state(state)
    notify("[올웨더 ETF 슬리브 리밸런싱] 완료")


if __name__ == "__main__":
    run()
