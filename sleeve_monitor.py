"""슬리브간 60% 밴드 모니터링 (DESIGN.md "슬리브 간 리밸런싱 규칙").
매월 말 올웨더(ISA+소형주퀀트 계좌 총평가액 합산) vs HAA(HAA 계좌 총평가액) 비중을 체크,
60%를 넘는 쪽이 있으면 텔레그램 알림만 보냄 — 환전 필요해서 실제 리밸런싱은 항상 수동.

소형성장주 KR/US는 별도 티커 추적 없이 계좌 총평가액으로만 판단(사용자 확인 방식) —
ISA는 채권/금 ETF 3종 전용, 소형주퀀트 계좌는 KR+US 겸용(계좌 하나로 국내+해외 매매, env prefix는 OVERSEAS 그대로 유지).
KR퀀트 자동매매(Phase 3)는 아직 미구현 — 소형주퀀트 계좌 잔고 조회가 지금은 해외(US) 쪽만 잡히고
국내 보유분은 안 잡히는 상태. Phase 3 구현 시 kis_domestic.get_balance("OVERSEAS")도 합산 필요.
"""
from __future__ import annotations

import logging
import os

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from pandas.tseries.offsets import BMonthEnd

import kis_domestic
import kis_overseas

load_dotenv()

BAND = 0.60
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


def is_month_end_today() -> bool:
    today = pd.Timestamp.now().normalize()
    return today == BMonthEnd().rollforward(today)


def get_usdkrw() -> float:
    df = yf.download("KRW=X", period="5d", progress=False, auto_adjust=True)
    close = df["Close"]["KRW=X"] if hasattr(df["Close"], "columns") else df["Close"]
    return float(close.dropna().iloc[-1])


def compute_sleeve_totals() -> dict:
    """{"olweather_krw":.., "haa_krw":.., "olweather_ratio":.., "haa_ratio":.., "fx":..}"""
    isa_token = kis_domestic.get_access_token("ISA")
    isa_total_krw = kis_domestic.get_total_assets(isa_token)  # RP 포함 (주식잔고조회는 RP 누락)

    overseas_token = kis_overseas.get_access_token("OVERSEAS")
    overseas_balance = kis_overseas.get_us_balance(overseas_token)
    overseas_total_usd = overseas_balance["equity_usd"]

    haa_token = kis_overseas.get_access_token("HAA")
    haa_balance = kis_overseas.get_us_balance(haa_token)
    haa_total_usd = haa_balance["equity_usd"]

    fx = get_usdkrw()

    olweather_krw = isa_total_krw + overseas_total_usd * fx
    haa_krw = haa_total_usd * fx
    combined = olweather_krw + haa_krw

    return {
        "olweather_krw": olweather_krw,
        "haa_krw": haa_krw,
        "olweather_ratio": olweather_krw / combined if combined > 0 else 0.0,
        "haa_ratio": haa_krw / combined if combined > 0 else 0.0,
        "fx": fx,
    }


def check_band() -> str | None:
    """60% 밴드 초과 시 알림 텍스트 반환, 아니면 None (아무것도 안 함 — DESIGN.md 스펙)."""
    totals = compute_sleeve_totals()
    if totals["olweather_ratio"] > BAND or totals["haa_ratio"] > BAND:
        return (
            f"[경고] 슬리브간 60% 밴드 초과!\n"
            f"올웨더: ₩{totals['olweather_krw']:,.0f} ({totals['olweather_ratio']:.1%})\n"
            f"HAA:    ₩{totals['haa_krw']:,.0f} ({totals['haa_ratio']:.1%})\n"
            f"환전 필요 — 수동으로 슬리브간 리밸런싱 하세요"
        )
    return None


def run() -> None:
    """월말에만 체크 — 60% 밴드 초과 시에만 텔레그램 알림, 아니면 아무것도 안 함(DESIGN.md 스펙)."""
    if not is_month_end_today():
        logger.info("오늘은 월말이 아님 — 스킵")
        return
    try:
        alert = check_band()
    except Exception as e:
        notify(f"슬리브간 비중 조회 실패: {e}")
        return
    if alert:
        notify(alert)
    else:
        logger.info("60% 밴드 정상 범위 — 알림 없음")


if __name__ == "__main__":
    run()
