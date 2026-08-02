"""올웨더 슬리브의 채권/금 ETF 3종 — 60SMA 월말체크 오버레이 목표비중 계산 (DESIGN.md Sleeve 1).
ISA 계좌에는 소형성장주 KR도 같이 있으므로, 이 모듈은 3개 ETF 티커만 알고 그 외 보유종목은
건드리지 않는다(ISA 계좌 총평가액을 기준으로 하되, 소형성장주 KR 몫(25/75)은 다른 파이프라인 소관).

비중 기준: 전체 포트폴리오 절대비중(국고채30 15%, 미국채30 15%, 금현물 20%, 소형성장주KR 25% —
합 75%가 ISA 계좌 몫)을 ISA 계좌 총평가액 대비 상대비중으로 환산: 15/75=20%, 15/75=20%, 20/75=26.67%.

가격 데이터는 yfinance 사용(".KS" 접미사) — pykrx/KRX 서버는 접속 과다로 IP 차단 이력 있어 회피.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

TICKERS = {"439870": "국고채30", "476760": "미국채30", "411060": "금현물"}
WEIGHT_IN_ISA = {"439870": 15 / 75, "476760": 15 / 75, "411060": 20 / 75}
SMA_DAYS = 60


def compute_target_weights(as_of: str | None = None) -> dict:
    """as_of(YYYY-MM-DD, 생략 시 오늘) 기준 각 ETF의 60거래일 SMA 신호로 목표비중 계산.
    반환: {ticker: weight_in_isa}. 신호 OFF면 0 (매도 후 대기, CMA 이체는 수동)."""
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
