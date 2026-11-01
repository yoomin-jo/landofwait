"""코인 슬리브 일간 리밸런싱 자동매매 (DESIGN.md Sleeve 3, BTC 추세신호).
매일 07:00 KST(미국장 마감 후 — 성과 기록 07:00과 같은 기준) 트리거. 마지막 완료 일봉(전날 09:00 마감)으로 신호를 계산하고, 목표와 현재 비중
차이가 버퍼(10%)를 넘을 때만 매매한다. 매도 먼저, 그다음 대기 현금 한도 내 매수.

2026-11-01: 업비트 전액을 코인원으로 이체 → 업비트 매매 갈래 제거, BTC 는 코인원(API 수수료 0원)에서 운용.
거래소 계좌 전체(코인원: 원화 + BTC + HYPE, 실험 봇 장부 몫 제외 / 바이낸스: USDT + BTC + ZEC)를 그 거래소의 예산으로 본다.
알트(HYPE·ZEC)는 신규 매수 중단(2026-10-30) — 보유분만 추세신호대로 줄이고, 나머지 예산은 BTC 신호로 BTC 운용.
신호 데이터는 업비트 KRW-BTC 일봉(공개 시세, 2018~ 이력). 슬리브 간 자금 이동은 수동. API 키가 없는 거래소는 알림만.
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
import pause
from coin_sleeve import BUFFER, compute_target_weights

load_dotenv()

STATE_PATH = Path(__file__).parent / "data" / "coin_state.json"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
LIVE = os.environ.get("COIN_LIVE") == "1"
FEE_BUFFER = 0.995  # 매수 시 시장가 호가 여유(코인원 API 수수료 0원, 바이낸스 0.1%)

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


def run_alts(state: dict, btc_forecast: float | None) -> bool:
    """거래소별(코인원 HYPE, 바이낸스 ZEC) — 2026-10-30 부터 알트 신규 매수 중단(백테스트: 2022 이후 알트 추세·모멘텀 전략 전부
    검증 CAGR 마이너스). 알트는 보유분만 추세신호대로 줄이고(목표 = min(현재 비중, min(1, 예측값/10)), 버퍼 10%),
    그 거래소의 나머지 예산(대기 현금)은 BTC 신호로 BTC 운용(목표 = min(min(1, BTC 예측/10), 1 − 알트 목표)).
    매도 먼저, 그다음 매수. API 키가 없는 거래소는 알트 권장 비율이 직전 대비 10%p 이상 바뀔 때 알림만 보낸다.
    반환: 주문 실패가 있었는지(있으면 같은 날 /run_coin 재실행 가능하게)."""
    failed = False
    forecasts = coin_alts.compute_forecasts()
    state["alt_forecasts"] = forecasts
    last = state.setdefault("alt_exposure", {})
    tag = "" if LIVE else "[모의] "
    btc_exp = min(1.0, btc_forecast / 10) if btc_forecast is not None else None
    for coin, cfg in coin_alts.ALTS.items():
        exposure = min(1.0, forecasts[coin] / 10)
        qty, cash = coin_alts.balance(coin)
        if not coin_alts.has_keys(coin):
            prev = last.get(coin)
            if prev is not None and abs(exposure - prev) >= BUFFER:
                notify(f"[코인 알트] {coin}({cfg['venue']}) 예측 {forecasts[coin]:.1f} → 권장 보유 {exposure:.0%} "
                       f"(직전 {prev:.0%}) — 수동 매매 후 .env ALT_HOLDINGS 수량 갱신 필요")
            if prev is None or abs(exposure - prev) >= BUFFER:
                last[coin] = exposure
            continue

        q = cfg["quote"]
        try:
            px = coin_alts.price(coin)
            bq, bpx = coin_alts.venue_btc(coin)
            total = qty * px + bq * bpx + cash
            if total <= 0:
                continue
            current = qty * px / total
            target = min(current, exposure)          # 알트는 늘리지 않음
            last[coin] = target
            diff = (target - current) * total
            if current - target >= BUFFER and -diff >= cfg["min_order"]:
                sell_qty = qty if exposure == 0 else min(qty, -diff / px)
                notify(f"{tag}[코인 알트] {coin}({cfg['venue']}) 예측 {forecasts[coin]:.1f} → 목표 {target:.0%} "
                       f"(현재 {current:.0%}, 예산 {total:,.2f} {q}, 신규 매수 없음)")
                if LIVE:
                    coin_alts.place_order(coin, "sell", qty=sell_qty)
                    time.sleep(3)
                notify(f"{tag}sell {coin} {sell_qty:.4f} (약 {sell_qty * px:,.2f} {q}) @ 시장가")
                if LIVE:
                    qty, cash = coin_alts.balance(coin)
                else:
                    qty, cash = qty - sell_qty, cash + sell_qty * px
                current = qty * px / total
                target = current
            if btc_exp is None:
                continue
            btc_cur = bq * bpx / total
            btc_tgt = min(btc_exp, max(0.0, 1 - target))
            bdiff = (btc_tgt - btc_cur) * total
            if abs(btc_tgt - btc_cur) < BUFFER or abs(bdiff) < cfg["min_order"]:
                continue
            notify(f"{tag}[코인 BTC@{cfg['venue']}] BTC 예측 {btc_forecast:.1f} → 목표 {btc_tgt:.0%} "
                   f"(현재 {btc_cur:.0%}, {cfg['venue']} 예산 {total:,.2f} {q})")
            if bdiff < 0:
                sell_qty = bq if btc_tgt == 0 else min(bq, -bdiff / bpx)
                if LIVE:
                    coin_alts.place_btc_order(coin, "sell", qty=sell_qty)
                notify(f"{tag}sell BTC@{cfg['venue']} {sell_qty:.6f} (약 {sell_qty * bpx:,.2f} {q}) @ 시장가")
            else:
                amount = min(bdiff, cash * FEE_BUFFER)
                if amount < cfg["min_order"]:
                    continue
                if LIVE:
                    coin_alts.place_btc_order(coin, "buy", quote_amount=amount)
                notify(f"{tag}buy BTC@{cfg['venue']} {amount:,.2f} {q} @ 시장가")
        except Exception as e:
            logger.exception("알트 거래소 주문 실패: %s", coin)
            notify(f"[코인 알트] {coin}({cfg['venue']}) 주문 실패: {e}")
            failed = True
    return failed


def run() -> None:
    if pause.is_paused("coin"):
        logger.info("긴급 정지 중(/resume coin으로 해제) — 스킵")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("last_run_date") == today:
        logger.info("오늘(%s) 이미 실행 완료 — 스킵", today)
        return

    try:
        weights, forecasts = compute_target_weights()
    except Exception as e:
        notify(f"[코인] BTC 신호 계산 실패 — BTC 매매 생략(알트 줄이기만 진행): {e}")
        weights, forecasts = None, {}

    try:
        has_failure = run_alts(state, forecasts.get("KRW-BTC"))
    except Exception as e:
        notify(f"[코인] 신호/잔고 조회 실패: {e}")
        has_failure = True

    if weights is not None:
        state.update({"forecasts": forecasts, "target_weights": weights})
    state["live"] = LIVE
    if weights is not None and not has_failure:
        state["last_run_date"] = today
    else:
        state.pop("last_run_date", None)  # 같은 날 /run_coin 재실행 가능하게
        notify("[코인] 신호 계산 또는 주문 실패 — /run_coin 재실행 또는 내일 07:00 자동 재시도")
    _save_state(state)


if __name__ == "__main__":
    run()
