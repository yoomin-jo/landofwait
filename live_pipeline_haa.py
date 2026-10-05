"""HAA 슬리브 월간 리밸런싱 자동매매 (DESIGN.md Sleeve 2, 원조 스펙 그대로).
매일 23:45 KST(미국 정규장 중 — 서머타임 22:30 개장/겨울 23:30 개장 모두 커버) 트리거되지만,
월초 매매창(이번 달 첫 5영업일) 안이고 이번 달 리밸런싱이 아직 완료 안 됐을 때만 동작한다.
신호는 전월 말 종가 기준(월말 신호 → 익월 첫 거래일 매매, DESIGN.md 확정 스펙). 주문 실패·
외화RP 인출 필요·휴장 등으로 미완료면 다음 날 같은 시각에 자동 재시도.

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

import pause
from haa_sleeve import ALL_TICKERS, EXCHANGE, compute_target_weights
from kis_domestic import get_total_assets
from kis_overseas import get_access_token, get_current_price, get_us_balance, place_us_order
from sleeve_monitor import get_usdkrw

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


REBALANCE_WINDOW_BDAYS = 5


def in_rebalance_window() -> bool:
    """월초 매매창: 이번 달 첫 영업일 ~ 5영업일째. 23:45 KST 실행이라 한국 날짜 = 미국 거래일 날짜.
    주말만 고려 — 미국 휴장일엔 주문이 실패해 다음 날 재시도로 흡수된다."""
    today = pd.Timestamp.now().normalize()
    if today.weekday() >= 5:
        return False
    return len(pd.bdate_range(today.replace(day=1), today)) <= REBALANCE_WINDOW_BDAYS


def signal_as_of() -> str:
    """신호 기준일 = 전월 마지막 날(달력). haa_sleeve가 그 달을 완료된 달로 보고 전월 말 종가로 계산."""
    return (pd.Timestamp.now().normalize().replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def compute_rebalance_orders(current_qty: dict, prices: dict, cash: float, weights: dict,
                             outside_usd: float = 0.0) -> list[dict]:
    """outside_usd: 잔고조회에 안 잡히는 계좌 자금(외화RP 등). 목표금액 계산에는 포함하되
    바로 쓸 수 있는 돈은 아니므로 매수 가능금액(available)에는 넣지 않는다."""
    tickers = set(current_qty) | set(weights)
    current_values = {t: current_qty.get(t, 0) * prices.get(t, 0) for t in tickers}
    total_value = sum(current_values.values()) + cash + outside_usd
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
    if pause.is_paused("haa"):
        logger.info("긴급 정지 중(/resume haa로 해제) — 스킵")
        return
    if not in_rebalance_window():
        logger.info("월초 매매창(첫 %d영업일) 아님 — 스킵", REBALANCE_WINDOW_BDAYS)
        return

    month_key = datetime.now().strftime("%Y-%m")
    state = _load_state()
    if state.get("last_rebalance_month") == month_key:
        logger.info("이번 달(%s) 이미 리밸런싱 완료 — 스킵", month_key)
        return

    notify("[HAA 리밸런싱] 시작")

    try:
        result = compute_target_weights(signal_as_of())
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
            prices[ticker] = get_current_price(token, ticker, EXCHANGE[ticker])
        except Exception as e:
            notify(f"{ticker} 현재가 조회 실패: {e}")
            return

    # 외화RP 등 잔고조회에 안 잡히는 자금 — 계좌 총자산(원화)과의 차이로 추정
    try:
        fx = get_usdkrw()
        visible_usd = cash + sum(current_qty.get(t, 0) * prices.get(t, 0) for t in tickers_needed)
        outside_usd = max(0.0, get_total_assets(token) / fx - visible_usd)
    except Exception as e:
        notify(f"총자산(외화RP 포함) 조회 실패 — 리밸런싱 중단: {e}")
        return
    if outside_usd > 1:
        notify(f"잔고조회 밖 자금(외화RP 등) 약 ${outside_usd:,.0f}")

    orders = compute_rebalance_orders(current_qty, prices, cash, weights, outside_usd)
    # 외화RP에 묶여 못 사는 금액. 방어모드 BIL 100%는 외화RP로 대체 운용 중이라 예외
    wanted = compute_rebalance_orders(current_qty, prices, cash + outside_usd, weights)
    buy_value = lambda os_: sum(o["qty"] * o["price"] for o in os_ if o["side"] == "buy")
    shortfall_usd = 0.0 if weights == {"BIL": 1.0} else buy_value(wanted) - buy_value(orders)
    if shortfall_usd > 0.01 * (visible_usd + outside_usd):
        notify(f"외화RP에서 약 ${shortfall_usd:,.0f} 매도(인출) 필요 — 매도 후 /run_haa 또는 "
               f"내일 23:45 자동 재시도(월초 {REBALANCE_WINDOW_BDAYS}영업일 내)")
    else:
        shortfall_usd = 0.0

    if not orders and not shortfall_usd:
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
            place_us_order(token, ticker, EXCHANGE[ticker], "sell", qty, limit_price)
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
                prices[ticker] = get_current_price(token, ticker, EXCHANGE[ticker])
            except Exception:
                pass

        buys = [o for o in compute_rebalance_orders(current_qty, prices, cash, weights, outside_usd)
                if o["side"] == "buy"]

        for i, order in enumerate(buys):
            if i > 0:
                time.sleep(1)
            ticker, qty = order["ticker"], order["qty"]
            limit_price = order["price"] * (1 + LIMIT_BUFFER)
            try:
                place_us_order(token, ticker, EXCHANGE[ticker], "buy", qty, limit_price)
                notify(f"buy {ticker} {qty}주 @ ${limit_price:.2f}")
            except Exception:
                logger.exception("매수 실패: %s", order)
                notify(f"매수 실패: buy {ticker} {qty}주")
                has_failure = True

    if shortfall_usd:
        has_failure = True
    if has_failure:
        notify(f"일부 주문 실패/미완료 — 내일 23:45 자동 재시도(월초 {REBALANCE_WINDOW_BDAYS}영업일 내)")
    else:
        state.update({"last_rebalance_month": month_key, "target_weights": weights, "mode": mode})
        _save_state(state)
    notify("[HAA 리밸런싱] 완료")


if __name__ == "__main__":
    run()
