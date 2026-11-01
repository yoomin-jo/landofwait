"""코인 슬리브(전체 10%) — Carver식 추세신호 목표비중 계산 (DESIGN.md Sleeve 3).

대상: BTC 기본비중 100% (2026-10-30 이전 BTC·ETH 각 50%). 신호 데이터는 업비트 KRW-BTC 일봉(공개 시세),
매매는 코인원(2026-11-01~, live_pipeline_coin.py). 목표비중 = 기본비중 × min(1, 예측값/10), 롱 전용.
상수는 올웨더 ETF(olweather_etf_sleeve.py)와 같은 pysystemtrade 기본값·Carver 상수(튜닝 없음),
코인은 365일 거래라 장기 변동성 창만 3650일. FDM 1.09(백테스트 BTC+ETH 풀링 추정).
변동성 타게팅은 미적용(성장 우선 — 2026-10-05 결정). 백테스트: C:\\quant\\scripts\\coin_trend_compare.py
(2018-06~2026-10 단순보유 CAGR 34%/MDD -74% vs 추세신호 CAGR 30%/MDD -48%).
"""
from __future__ import annotations

import pandas as pd

from upbit_api import get_daily_closes

MARKETS = {"KRW-BTC": "BTC"}  # 2026-11-01: ETH 제외(업비트 ETH 는 먼지만 남아 매매 대상 아님)
# 2026-10-30: BTC 100% (ETH 0%, 남은 ETH 는 목표 0% 로 정리). 반감기 기준 백테스트(학습 2018-05~2022-05 / 검증 2022-05~2026-04,
#   비용 0.2%): MDD BTC 100% −44/−26% vs 50:50 −50/−30%, 미사용 구간(2026-04~10) −6% vs −9% — MDD 순서가 세 구간 모두 같음.
#   CAGR 은 학습 40% vs 46%(−6%p), 검증 14% vs 10%. 비율별 PBO 84~88%(수익 기준 비율 선택은 예측 불가) → 사용자 우선순위 1위 MDD 로 결정
BASE_WEIGHT = {"KRW-BTC": 1.0}
RULES = [(16, 64, 3.75), (32, 128, 2.65), (64, 256, 1.87)]  # (fast, slow, forecast scalar)
FDM = 1.09
FORECAST_CAP = 20.0
BUFFER = 0.10  # 목표와 현재 비중 차이가 기본비중의 10% 미만이면 매매 안 함
MIN_DAYS = 300


def _forecast(close: pd.Series) -> float:
    """마지막 완료 일봉 기준 결합 예측값(0~20, 롱 전용)."""
    ret = close.pct_change()
    fast = ret.ewm(span=35, min_periods=10).std()
    slow = fast.rolling(3650, min_periods=365).mean()
    price_vol = (0.7 * fast + 0.3 * slow).fillna(fast) * close
    fcs = [((close.ewm(span=f).mean() - close.ewm(span=s).mean()) / price_vol * k).clip(-FORECAST_CAP, FORECAST_CAP)
           for f, s, k in RULES]
    combined = sum(fcs) / len(fcs) * FDM
    return float(min(FORECAST_CAP, max(0.0, combined.iloc[-1])))


def compute_forecasts() -> dict:
    forecasts = {}
    for market in MARKETS:
        closes = get_daily_closes(market)
        if len(closes) < MIN_DAYS:
            raise RuntimeError(f"{market} 추세신호 계산에 필요한 데이터 부족: {len(closes)}일")
        forecasts[market] = _forecast(closes)
    return forecasts


def compute_target_weights() -> tuple[dict, dict]:
    """반환: (목표비중 {market: 0~0.5}, 예측값 {market: 0~20}). 나머지는 원화 대기."""
    forecasts = compute_forecasts()
    weights = {m: BASE_WEIGHT[m] * min(1.0, forecasts[m] / 10) for m in MARKETS}
    return weights, forecasts


if __name__ == "__main__":
    import json
    w, f = compute_target_weights()
    print(json.dumps({"forecasts": f, "weights": w}, ensure_ascii=False, indent=2))
