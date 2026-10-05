"""HAA(Hybrid Asset Allocation) 슬리브 — 실거래용 목표비중 계산.
`C:\\quant\\scripts\\haa_backtest.py`의 13612U 모멘텀 로직을 "특정 시점 기준 1회 계산"용으로
재구성한 버전 (백테스트 대신 실시간 yfinance 조회). DESIGN.md Sleeve 2 스펙 그대로, 변형 없음.
원자재 자산은 DBC가 아니라 PDBC 사용 — DBC는 K-1 발급 LP 구조라 KIS 해외계좌에서 매수 자체가
안 됨(2026-08 확인). 백테스트(C:\\quant\\scripts\\haa_backtest.py)는 DBC 기준이라 실거래용인
이 파일과 티커가 다르다는 점 유의."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

ATTACK = ["SPY", "IWM", "VEA", "VWO", "PDBC", "VNQ", "IEF", "TLT"]
CANARY = "TIP"
DEFENSIVE = ["BIL", "IEF"]
ALL_TICKERS = sorted(set(ATTACK + [CANARY] + DEFENSIVE))

# KIS 거래소 코드 실측 확인 결과: iShares 발행(TLT/IEF)는 NASD, 나머지(State Street/Vanguard/
# Invesco 발행)는 NYSE Arca 상장이라 KIS 분류상 AMEX.
EXCHANGE = {t: ("NASD" if t in ("TLT", "IEF") else "AMEX") for t in ALL_TICKERS}


def fetch_monthly_prices(as_of: str | None = None) -> pd.DataFrame:
    """as_of 이전 데이터만 사용해 월말 종가 시리즈 반환 (12개월 모멘텀 계산에 필요한 버퍼 포함).
    배당조정(총수익률) 종가 사용 — Keller & Keuning 원조 스펙 및 이전 백테스트
    (C:\\quant\\scripts\\haa_backtest.py, CAGR 11.45%/12.99%KRW 검증치)와 동일 컨벤션.
    2026-08-31~10-05에는 구글시트(GOOGLEFINANCE) 대조용으로 미조정 종가를 썼으나, TIP/BIL/IEF처럼
    분배금 큰 채권 ETF의 모멘텀이 약 1.5%p 비관적으로 계산돼 카나리아 신호가 뒤집히는 달이 생겨
    (예: 2026-03/06/07 미조정=방어, 조정=공격) 원복함(2026-10-05)."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp.today()
    start = end - pd.DateOffset(months=15)
    df = yf.download(ALL_TICKERS, start=start.strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                      auto_adjust=True, progress=False)["Close"]
    df = df[ALL_TICKERS].ffill()
    return df.resample("ME").last()


def _compute_momentum_row(as_of: str | None) -> tuple[pd.Timestamp, pd.Series]:
    monthly = fetch_monthly_prices(as_of)
    r1, r3, r6, r12 = monthly.pct_change(1), monthly.pct_change(3), monthly.pct_change(6), monthly.pct_change(12)
    momentum = (r1 + r3 + r6 + r12) / 4
    momentum = momentum.dropna()
    if momentum.empty:
        raise RuntimeError("모멘텀 계산에 필요한 12개월치 데이터가 부족합니다")
    latest = momentum.index[-1]
    return latest, momentum.loc[latest]


def compute_target_weights(as_of: str | None = None) -> dict:
    """as_of(YYYY-MM-DD, 생략 시 오늘) 기준 가장 최근 완료된 월말 모멘텀으로 목표비중 계산.
    반환: {ticker: weight}. 매수 대상 아닌 티커는 포함하지 않음.

    yfinance 멀티티커 배치 다운로드가 간헐적으로 불완전한 데이터를 반환해 카나리아(TIP)
    신호가 잘못 계산되는 사례가 실제로 있었음(2026-08-31) — 실계좌 자동매매에 그대로 쓰이면
    잘못된 매매로 이어지므로, 독립적으로 2회 재조회해 카나리아 신호(공격/방어) 방향이
    일치하는지 검증한다. 불일치하면 매매하지 않고 예외를 던진다."""
    latest_a, mom_a = _compute_momentum_row(as_of)
    latest_b, mom_b = _compute_momentum_row(as_of)

    signal_a = mom_a[CANARY] > 0
    signal_b = mom_b[CANARY] > 0
    if latest_a != latest_b or signal_a != signal_b:
        raise RuntimeError(
            f"카나리아 신호 재조회 불일치(데이터 오류 의심) — "
            f"1차: {latest_a.date()} TIP={mom_a[CANARY]:.4f} / 2차: {latest_b.date()} TIP={mom_b[CANARY]:.4f} "
            f"— 매매 중단, 수동 확인 필요"
        )

    latest, mom_t = latest_a, mom_a
    if mom_t[CANARY] > 0:
        top4 = mom_t[ATTACK].nlargest(4).index
        return {"as_of_month_end": str(latest.date()), "mode": "공격", "weights": {t: 0.25 for t in top4}}
    else:
        best_def = mom_t[DEFENSIVE].idxmax()
        return {"as_of_month_end": str(latest.date()), "mode": "방어", "weights": {best_def: 1.0}}


if __name__ == "__main__":
    import json
    print(json.dumps(compute_target_weights(), ensure_ascii=False, indent=2))
