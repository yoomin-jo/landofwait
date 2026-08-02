"""HAA(Hybrid Asset Allocation) 슬리브 — 실거래용 목표비중 계산.
`C:\\quant\\scripts\\haa_backtest.py`의 13612U 모멘텀 로직을 "특정 시점 기준 1회 계산"용으로
재구성한 버전 (백테스트 대신 실시간 yfinance 조회). DESIGN.md Sleeve 2 스펙 그대로, 변형 없음."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

ATTACK = ["SPY", "IWM", "VEA", "VWO", "DBC", "VNQ", "IEF", "TLT"]
CANARY = "TIP"
DEFENSIVE = ["BIL", "IEF"]
ALL_TICKERS = sorted(set(ATTACK + [CANARY] + DEFENSIVE))

# KIS 거래소 코드 실측 확인 결과: iShares 발행(TLT/IEF)는 NASD, 나머지(State Street/Vanguard/
# Invesco 발행)는 NYSE Arca 상장이라 KIS 분류상 AMEX.
EXCHANGE = {t: ("NASD" if t in ("TLT", "IEF") else "AMEX") for t in ALL_TICKERS}


def fetch_monthly_prices(as_of: str | None = None) -> pd.DataFrame:
    """as_of 이전 데이터만 사용해 월말 종가 시리즈 반환 (12개월 모멘텀 계산에 필요한 버퍼 포함)."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp.today()
    start = end - pd.DateOffset(months=15)
    df = yf.download(ALL_TICKERS, start=start.strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False)["Close"]
    df = df[ALL_TICKERS].ffill()
    return df.resample("ME").last()


def compute_target_weights(as_of: str | None = None) -> dict:
    """as_of(YYYY-MM-DD, 생략 시 오늘) 기준 가장 최근 완료된 월말 모멘텀으로 목표비중 계산.
    반환: {ticker: weight}. 매수 대상 아닌 티커는 포함하지 않음."""
    monthly = fetch_monthly_prices(as_of)

    r1, r3, r6, r12 = monthly.pct_change(1), monthly.pct_change(3), monthly.pct_change(6), monthly.pct_change(12)
    momentum = (r1 + r3 + r6 + r12) / 4
    momentum = momentum.dropna()
    if momentum.empty:
        raise RuntimeError("모멘텀 계산에 필요한 12개월치 데이터가 부족합니다")

    latest = momentum.index[-1]
    mom_t = momentum.loc[latest]

    if mom_t[CANARY] > 0:
        top4 = mom_t[ATTACK].nlargest(4).index
        return {"as_of_month_end": str(latest.date()), "mode": "공격", "weights": {t: 0.25 for t in top4}}
    else:
        best_def = mom_t[DEFENSIVE].idxmax()
        return {"as_of_month_end": str(latest.date()), "mode": "방어", "weights": {best_def: 1.0}}


if __name__ == "__main__":
    import json
    print(json.dumps(compute_target_weights(), ensure_ascii=False, indent=2))
