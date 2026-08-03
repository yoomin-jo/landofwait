"""올웨더 슬리브의 채권/금 ETF 3종 — 60SMA 월말체크 오버레이 목표비중 계산 (DESIGN.md Sleeve 1).
ISA 계좌에 이 3개 ETF를 전용으로 담는다(소형성장주 KR/US는 OVERSEAS 계좌 소관, 별도 파이프라인).

비중 기준: 전체 포트폴리오 절대비중(국고채30 15%, 미국채30 15%, 금현물 20% — 합 50%가 ISA 몫)을
ISA 계좌 총평가액 대비 상대비중으로 환산: 15/50=30%, 15/50=30%, 20/50=40%.

60SMA 이탈 시 대기자금은 RP로 수동 전환한다 — RP는 KIS Open API로 매수 불가(API 미지원 확인됨),
CD ETF 자동전환 방식은 폐기(체결 문제 + 세액공제 실효성 없어 연금저축 자체를 안 쓰기로 함).
비중이 0이 된 자산은 그냥 현금으로 남고, live_pipeline_olweather.py가 텔레그램으로 RP 수동전환 알림.

가격 데이터는 yfinance 사용(".KS" 접미사) — pykrx/KRX 서버는 접속 과다로 IP 차단 이력 있어 회피.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

TICKERS = {"439870": "국고채30", "476760": "미국채30", "411060": "금현물"}
WEIGHT_IN_ISA = {"439870": 15 / 50, "476760": 15 / 50, "411060": 20 / 50}
SMA_DAYS = 60


def compute_target_weights(as_of: str | None = None) -> dict:
    """as_of(YYYY-MM-DD, 생략 시 오늘) 기준 각 ETF의 60거래일 SMA 신호로 목표비중 계산.
    반환: {ticker: weight_in_isa} — 3개 자산 전부 포함.
    신호 OFF인 자산의 비중은 0(현금, RP 수동전환 대상)."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()
    start = end - pd.Timedelta(days=SMA_DAYS * 3)

    weights = {}
    for ticker in TICKERS:
        df = yf.download(f"{ticker}.KS", start=start.strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False)
        close = df["Close"][f"{ticker}.KS"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        close = close.dropna()
        if len(close) < SMA_DAYS:
            raise RuntimeError(f"{ticker}({TICKERS[ticker]}) 60SMA 계산에 필요한 데이터 부족: {len(close)}일")
        sma = close.rolling(SMA_DAYS).mean()
        signal_on = float(close.iloc[-1]) > float(sma.iloc[-1])
        weights[ticker] = WEIGHT_IN_ISA[ticker] if signal_on else 0.0

    return weights


if __name__ == "__main__":
    import json
    print(json.dumps(compute_target_weights(), ensure_ascii=False, indent=2))
