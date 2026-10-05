"""코인 슬리브 일간 리밸런싱 자동매매 (DESIGN.md Sleeve 3, 업비트 BTC·ETH 추세신호).
매일 09:05 KST(업비트 일봉 09:00 마감 직후) 트리거. 전일 완료 일봉으로 신호를 계산하고, 목표와 현재 비중
차이가 버퍼(기본비중의 10%)를 넘을 때만 매매한다. 매도 먼저, 그다음 원화 한도 내 매수.

업비트 계좌 전체(원화 + BTC + ETH)를 코인 슬리브 예산으로 본다 — 슬리브 간 자금 이동은 수동.
알트(ZEC 바이낸스, HYPE 코인원)는 같은 신호로 권장 보유비율만 계산해 바뀔 때 알림(coin_alts.py, 매매는 수동).
COIN_LIVE=1 이 아니면 모의 실행(주문 안 내고 텔레그램으로 예정 주문만 알림).
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import coin_alts
from coin_sleeve import BASE_WEIGHT, BUFFER, MARKETS, compute_target_weights
from upbit_api import MIN_ORDER_KRW, buy_market_krw, get_balances, get_prices, sell_market_volume

load_dotenv()

STATE_PATH = Path(__file__).parent / "data" / "coin_state.json"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LIVE = os.environ.get("COIN_LIVE") == "1"
FEE_BUFFER = 0.995  # 매수 시 수수료(0.05%)·호가 여유

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


def compute_orders(balances: dict, prices: dict, weights: dict) -> tuple[list[dict], float]:
    """반환: (주문 목록, 슬리브 총액 원화). 주문: {"market", "side", "krw", "volume"}."""
    total = balances.get("KRW", 0.0) + sum(balances.get(MARKETS[m], 0.0) * prices[m] for m in MARKETS)
    orders = []
    for m, coin in MARKETS.items():
        qty = balances.get(coin, 0.0)
        current_w = qty * prices[m] / total if total > 0 else 0.0
        target_w = weights[m]
        if abs(target_w - current_w) < BUFFER * BASE_WEIGHT[m]:
            continue
        diff_krw = (target_w - current_w) * total
        if abs(diff_krw) < MIN_ORDER_KRW:
            continue
        if diff_krw < 0:
            volume = qty if target_w == 0 else min(qty, -diff_krw / prices[m])
            orders.append({"market": m, "side": "sell", "krw": volume * prices[m], "volume": volume})
        else:
            orders.append({"market": m, "side": "buy", "krw": diff_krw, "volume": diff_krw / prices[m]})
    return orders, total


def check_alts(state: dict) -> None:
    """ZEC·HYPE 권장 보유비율이 직전 알림 대비 BUFFER(10%p) 이상 바뀌면 텔레그램 알림만(매매는 수동).
    첫 실행은 현재 권장값을 기준으로 저장만 한다(현재 전량 보유 중이라는 전제)."""
    forecasts = coin_alts.compute_forecasts()
    qty = coin_alts.holdings()
    last = state.setdefault("alt_exposure", {})
    state["alt_forecasts"] = forecasts
    for coin, venue in coin_alts.ALTS.items():
        exposure = min(1.0, forecasts[coin] / 10)
        prev = last.get(coin)
        if prev is not None and abs(exposure - prev) >= BUFFER:
            keep = qty.get(coin, 0.0) * exposure
            action = "늘리세요" if exposure > prev else "줄이세요"
            notify(f"[코인 알트] {coin}({venue}) 예측 {forecasts[coin]:.1f} → 권장 보유 {exposure:.0%} "
                   f"(직전 {prev:.0%}) — {action}. 기준 수량 {qty.get(coin, 0.0):.4f}개 중 약 {keep:.4f}개 보유, "
                   f"매매 후 .env ALT_HOLDINGS 수량 갱신 필요")
        if prev is None or abs(exposure - prev) >= BUFFER:
            last[coin] = exposure


def run() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("last_run_date") == today:
        logger.info("오늘(%s) 이미 실행 완료 — 스킵", today)
        return

    try:
        check_alts(state)
    except Exception as e:
        notify(f"[코인 알트] 신호 계산 실패: {e}")

    try:
        weights, forecasts = compute_target_weights()
        balances = get_balances()
        prices = get_prices(list(MARKETS))
    except Exception as e:
        notify(f"[코인] 신호/잔고 조회 실패 — 매매 중단: {e}")
        return

    orders, total = compute_orders(balances, prices, weights)
    summary = ", ".join(f"{MARKETS[m]} 예측 {forecasts[m]:.1f}→목표 {weights[m]:.0%}" for m in MARKETS)
    state.update({"last_run_date": today, "forecasts": forecasts, "target_weights": weights,
                  "total_krw": round(total), "live": LIVE})

    if not orders:
        logger.info("매매 불필요 (%s, 총액 ₩%s)", summary, f"{total:,.0f}")
        _save_state(state)
        return

    tag = "" if LIVE else "[모의] "
    notify(f"{tag}[코인 리밸런싱] 총액 ₩{total:,.0f} / {summary}")
    has_failure = False
    for o in [o for o in orders if o["side"] == "sell"]:
        try:
            if LIVE:
                sell_market_volume(o["market"], o["volume"])
            notify(f"{tag}sell {MARKETS[o['market']]} {o['volume']:.6f} (약 ₩{o['krw']:,.0f}) @ 시장가")
        except Exception as e:
            logger.exception("매도 실패: %s", o)
            notify(f"[코인] 매도 실패: {MARKETS[o['market']]} — {e}")
            has_failure = True

    buys = [o for o in orders if o["side"] == "buy"]
    if buys and not has_failure:
        if LIVE:
            time.sleep(3)
            try:
                krw = get_balances().get("KRW", 0.0) * FEE_BUFFER
            except Exception as e:
                notify(f"[코인] 매수 전 원화 조회 실패 — 매수 중단: {e}")
                buys, has_failure = [], True
        else:
            krw = (balances.get("KRW", 0.0) + sum(o["krw"] for o in orders if o["side"] == "sell")) * FEE_BUFFER
        for o in buys:
            amount = min(o["krw"], krw)
            if amount < MIN_ORDER_KRW:
                continue
            try:
                if LIVE:
                    buy_market_krw(o["market"], amount)
                krw -= amount
                notify(f"{tag}buy {MARKETS[o['market']]} ₩{amount:,.0f} @ 시장가")
            except Exception as e:
                logger.exception("매수 실패: %s", o)
                notify(f"[코인] 매수 실패: {MARKETS[o['market']]} — {e}")
                has_failure = True

    if has_failure:
        state.pop("last_run_date")  # 같은 날 /run_coin 재실행 가능하게
        notify("[코인] 일부 주문 실패 — /run_coin 재실행 또는 내일 09:05 자동 재시도")
    _save_state(state)


if __name__ == "__main__":
    run()
