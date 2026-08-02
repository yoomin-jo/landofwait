"""HAA 슬리브 월간 리밸런싱 자동매매 (DESIGN.md Sleeve 2, 원조 스펙 그대로).
평일 매일 트리거되지만, "오늘이 이번 달 마지막 미국 거래일(근사)"이고 이번 달에 아직
리밸런싱 안 했을 때만 실제로 동작한다 (d:\\tqqq\\live_pipeline.py 구조를 본뜸).

매월 항상 리밸런싱(편입종목 불변이어도 목표비중으로 재정렬) — HAA 원조 스펙, buy-only 아님.
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
from pandas.tseries.offsets import BMonthEnd

from haa_sleeve import ALL_TICKERS, EXCHANGE, compute_target_weights
from kis_overseas import get_access_token, get_current_price, get_us_balance, place_us_order

load_dotenv()

STATE_PATH = Path(__file__).parent / "data" / "haa_state.json"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

LIMIT_BUFFER = 0.005

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
    """이 파이프라인은 미국 장마감 이후(KST 익일 새벽 06:30, deploy/haa-rebalance.timer 참조)에
    돌아가므로, KST 기준 "오늘"이 아니라 방금 마감한 미국 거래일(KST 기준 어제)이 그 달의
    마지막 영업일인지 확인해야 함. 주말만 고려, 미국 공휴일은 미반영 — 근사치. 실제 휴장일과
    어긋나면 최대 며칠 늦게 실행될 수 있음(월간 전략이라 허용 가능한 오차)."""
    us_trading_date = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    return us_trading_date == BMonthEnd().rollforward(us_trading_date)


def compute_rebalance_orders(current_qty: dict, prices: dict, cash: float, weights: dict) -> list[dict]:
    tickers = set(current_qty) | set(weights)
    current_values = {t: current_qty.get(t, 0) * prices.get(t, 0) for t in tickers}
    total_value = sum(current_values.values()) + cash
    if total_value <= 0:
        return []

    sells, buys = [], []
    for ticker in tickers:
        target_value = total_value * weights.get(ticker, 0.0)
        target_qty = int(target_value / prices[ticker]) if prices.get(ticker) else 0
        diff = target_qty - current_qty.get(ticker, 0)
        if diff == 0:
            continue
        order = {"ticker": ticker, "qty": abs(diff), "price": prices[ticker]}
        if diff < 0:
            order["side"] = "sell"
            sells.append(order)
        else:
            order["side"] = "buy"
            buys.append(order)

    available = cash * 0.98
    for o in sells:
        available += o["qty"] * o["price"] * (1 - LIMIT_BUFFER)
    for o in buys:
        buy_price = o["price"] * (1 + LIMIT_BUFFER)
        cost = o["qty"] * buy_price
        if cost > available:
            o["qty"] = int(available / buy_price)
        available -= o["qty"] * buy_price

    return sells + [o for o in buys if o["qty"] > 0]


def run() -> None:
    if not is_month_end_today():
        logger.info("오늘은 월말이 아님 — 스킵")
        return

    month_key = datetime.now().strftime("%Y-%m")
    state = _load_state()
    if state.get("last_rebalance_month") == month_key:
        logger.info("이번 달(%s) 이미 리밸런싱 완료 — 스킵", month_key)
        return

    notify("[HAA 리밸런싱] 시작")

    try:
        result = compute_target_weights()
        weights = result["weights"]
        mode = result["mode"]
    except Exception as e:
        notify(f"HAA 목표비중 계산 실패 — 리밸런싱 중단: {e}")
        return
    notify(f"모드: {mode} / 목표비중: {weights}")

    token = get_access_token("HAA")
    try:
        balance = get_us_balance(token)
        current_qty = {t: int(q) for t, q in balance["qty"].items()}
        cash = balance["cash_usd"]
        notify(f"잔고: 가용현금 ${cash:,.2f}, 보유 {current_qty}")
    except Exception as e:
        notify(f"잔고 조회 실패: {e}")
        return

    tickers_needed = set(current_qty) | set(weights)
    prices = {}
    for ticker in tickers_needed:
        try:
            time.sleep(1)
            prices[ticker] = get_current_price(token, ticker, EXCHANGE)
        except Exception as e:
            notify(f"{ticker} 현재가 조회 실패: {e}")
            return

    orders = compute_rebalance_orders(current_qty, prices, cash, weights)
    if not orders:
        notify("리밸런싱 불필요 (목표비중과 이미 일치)")
        state.update({"last_rebalance_month": month_key, "target_weights": weights, "mode": mode})
        _save_state(state)
        return

    sells = [o for o in orders if o["side"] == "sell"]
    buys_planned = [o for o in orders if o["side"] == "buy"]

    has_failure = False
    for i, order in enumerate(sells):
        if i > 0:
            time.sleep(1)
        ticker, qty = order["ticker"], order["qty"]
        limit_price = order["price"] * (1 - LIMIT_BUFFER)
        try:
            place_us_order(token, ticker, EXCHANGE, "sell", qty, limit_price)
            notify(f"sell {ticker} {qty}주 @ ${limit_price:.2f}")
        except Exception:
            logger.exception("매도 실패: %s", order)
            notify(f"매도 실패: sell {ticker} {qty}주")
            has_failure = True

    if buys_planned and not has_failure:
        notify("매도 체결 대기 60초...")
        time.sleep(60)

        try:
            time.sleep(1)
            balance = get_us_balance(token)
            cash = balance["cash_usd"]
            current_qty = {t: int(q) for t, q in balance["qty"].items()}
            notify(f"매도 후 가용현금: ${cash:,.2f}")
        except Exception:
            logger.exception("잔고 재조회 실패")

        for ticker in tickers_needed:
            try:
                time.sleep(1)
                prices[ticker] = get_current_price(token, ticker, EXCHANGE)
            except Exception:
                pass

        buys = [o for o in compute_rebalance_orders(current_qty, prices, cash, weights) if o["side"] == "buy"]

        for i, order in enumerate(buys):
            if i > 0:
                time.sleep(1)
            ticker, qty = order["ticker"], order["qty"]
            limit_price = order["price"] * (1 + LIMIT_BUFFER)
            try:
                place_us_order(token, ticker, EXCHANGE, "buy", qty, limit_price)
                notify(f"buy {ticker} {qty}주 @ ${limit_price:.2f}")
            except Exception:
                logger.exception("매수 실패: %s", order)
                notify(f"매수 실패: buy {ticker} {qty}주")
                has_failure = True

    if has_failure:
        notify("일부 주문 실패 — 다음 실행 시 재시도")
    else:
        state.update({"last_rebalance_month": month_key, "target_weights": weights, "mode": mode})
        _save_state(state)
    notify("[HAA 리밸런싱] 완료")


if __name__ == "__main__":
    run()
