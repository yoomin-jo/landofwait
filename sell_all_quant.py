"""소형주퀀트 중단(2026-11) — 소형주퀀트 계좌(env prefix OVERSEAS)의 KR 또는 US 보유 종목 전량 현재가 지정가 매도.
매도 대금은 ISA로 옮겨 올웨더 주식 지수 ETF(KOSPI200·S&P500)를 사는 데 쓴다(매수·이체는 수동).
일회성 스크립트 — 로컬 Windows 작업 스케줄러로 실행, Pi 배포 안 함. 미체결분은 다시 실행하면 남은 수량만 재주문.

사용: python sell_all_quant.py kr|us [--dry-run]"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import kis_domestic  # noqa: E402  (.env 로드 후 import)
import kis_overseas  # noqa: E402

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


def sell_kr(dry_run: bool) -> None:
    token = kis_domestic.get_access_token("OVERSEAS")
    qty = kis_domestic.get_balance(token)["qty"]
    notify(f"[소형주퀀트 KR 전량 매도{' (모의)' if dry_run else ''}] {len(qty)}종목")
    for ticker, q in qty.items():
        try:
            if dry_run:
                notify(f"  (모의) sell {ticker} {q}주 @ ₩{kis_domestic.get_current_price(token, ticker):,.0f}")
            else:
                kis_domestic.sell_market(token, ticker, q)  # 이름과 달리 현재가 지정가
                notify(f"  sell {ticker} {q}주 @ 현재가")
        except Exception as e:
            notify(f"  매도 실패: {ticker} {q}주 — {e}")
        time.sleep(1)  # 순차 주문 — 몰아넣으면 일부 누락될 수 있음


def sell_us(dry_run: bool) -> None:
    token = kis_overseas.get_access_token("OVERSEAS")
    balance = kis_overseas.get_us_balance(token)
    notify(f"[소형주퀀트 US 전량 매도{' (모의)' if dry_run else ''}] {len(balance['qty'])}종목")
    for ticker, q in balance["qty"].items():
        exchange = balance["exchange"][ticker]
        try:
            price = kis_overseas.get_current_price(token, ticker, exchange)
            if not dry_run:
                kis_overseas.place_us_order(token, ticker, exchange, "sell", q, price)
            notify(f"  {'(모의) ' if dry_run else ''}sell {ticker}({exchange}) {q}주 @ ${price:.2f}")
        except Exception as e:
            notify(f"  매도 실패: {ticker}({exchange}) {q}주 — {e}")
        time.sleep(1)


if __name__ == "__main__":
    market, dry = sys.argv[1], "--dry-run" in sys.argv
    (sell_kr if market == "kr" else sell_us)(dry)
