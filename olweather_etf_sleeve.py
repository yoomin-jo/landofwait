"""올웨더 슬리브 — K올웨더 성장형 ETF 8종. 주식 4종·초단기채 고정비중 + 금/채권 3종 Carver식 연속 추세신호,
월말체크 목표비중 계산 (DESIGN.md Sleeve 1). 2026-11 소형주퀀트를 중단하고 올웨더 슬리브 전체를 ISA에 담는다.

비중 기준: ISA 계좌 총평가액 대비 — S&P500 24%, KOSPI200 8%, 중국CSI300 8%, 인도Nifty50 8%(신호 없이 그냥 보유),
금현물 19%, 미국채30 14%, 국고채30 14%(Carver), 초단기채 5% + 금/채권 신호 미보유분(대기자금).
백테스트(2014-01~2026-10): 금·채권만 Carver 10.1%/MDD -13.5% vs 모두 보유 9.8%/-15.8% vs 전부 Carver 7.7%/-7.9%.
주식은 Carver·60일선·200일선 모두 그냥 보유보다 못해 신호 미적용.

신호(2026-10-05, 60SMA ON/OFF에서 교체): EWMAC 16/64·32/128·64/256 동일가중 결합 예측값(-20~+20)을
롱 전용으로 0 미만은 0 처리, 목표비중 = 기본비중 × min(1, 예측값/10). 상수는 전부 pysystemtrade 기본값·
Carver 책 값 그대로(튜닝 없음) — 변동성은 0.7×EWMA std(span 35) + 0.3×10년 평균, forecast scalar
3.75/2.65/1.87, FDM 1.08(백테스트 3자산 풀링 추정). 백테스트(2013-10~2026-07, 4개 구간)에서 60SMA 대비
4개 구간 모두 Sharpe·CAGR 우위, 회전율 약 1/3 (C:\quant\scripts\olweather_carver_compare.py).

대기자금은 RP 대신 초단기채 ETF로 자동 매수 — RP는 KIS Open API로 매수 불가(API 미지원 확인됨).
가격 데이터는 yfinance 사용(".KS" 접미사) — pykrx/KRX 서버는 접속 과다로 IP 차단 이력 있어 회피.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

TICKERS = {"379800": "KODEX 미국S&P500", "152100": "PLUS 200", "283580": "KODEX 차이나CSI300",
           "453810": "KODEX 인도Nifty50", "411060": "ACE KRX금현물", "476760": "ACE 미국30년국채액티브",
           "385560": "RISE KIS국고채30년Enhanced", "469830": "SOL 초단기채권액티브"}
WEIGHT_IN_ISA = {"379800": 0.24, "152100": 0.08, "283580": 0.08, "453810": 0.08,
                 "411060": 0.19, "476760": 0.14, "385560": 0.14, "469830": 0.05}
TREND_TICKERS = ("411060", "476760", "385560")  # Carver 신호 적용 대상 — 나머지(주식·초단기채)는 고정비중
IDLE_TICKER = "469830"  # 금/채권 신호 미보유분(대기자금)을 담는 초단기채
RULES = [(16, 64, 3.75), (32, 128, 2.65), (64, 256, 1.87)]  # (fast, slow, forecast scalar)
FDM = 1.08
FORECAST_CAP = 20.0
BUFFER = 0.10  # 목표와 현재 비중 차이가 기본비중의 10% 미만이면 매매 안 함 (live_pipeline_olweather.py)
MIN_DAYS = 260


def _forecast(close: pd.Series) -> float:
    """마지막 날 결합 예측값(0~20, 롱 전용)."""
    ret = close.pct_change()
    fast = ret.ewm(span=35, min_periods=10).std()
    slow = fast.rolling(2520, min_periods=250).mean()
    price_vol = (0.7 * fast + 0.3 * slow).fillna(fast) * close
    fcs = [((close.ewm(span=f).mean() - close.ewm(span=s).mean()) / price_vol * k).clip(-FORECAST_CAP, FORECAST_CAP)
           for f, s, k in RULES]
    combined = sum(fcs) / len(fcs) * FDM
    return float(min(FORECAST_CAP, max(0.0, combined.iloc[-1])))


def compute_forecasts(as_of: str | None = None) -> dict:
    """as_of(YYYY-MM-DD, 생략 시 오늘) 기준 각 ETF의 결합 예측값 {ticker: 0~20}."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp.now()
    start = end - pd.DateOffset(years=6)

    forecasts = {}
    for ticker in TREND_TICKERS:
        df = yf.download(f"{ticker}.KS", start=start.strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False)
        close = df["Close"][f"{ticker}.KS"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        close = close.dropna()
        if len(close) < MIN_DAYS:
            raise RuntimeError(f"{ticker}({TICKERS[ticker]}) 추세신호 계산에 필요한 데이터 부족: {len(close)}일")
        forecasts[ticker] = _forecast(close)
    return forecasts


def compute_target_weights(as_of: str | None = None) -> dict:
    """반환: {ticker: weight_in_isa} — 8개 자산 전부 포함, 합계 100%. 주식은 고정비중, 금/채권은 예측값 0이면
    비중 0이고 그 몫은 초단기채로."""
    forecasts = compute_forecasts(as_of)
    weights = {t: WEIGHT_IN_ISA[t] * min(1.0, forecasts[t] / 10) if t in forecasts else WEIGHT_IN_ISA[t] for t in TICKERS}
    weights[IDLE_TICKER] += sum(WEIGHT_IN_ISA[t] - weights[t] for t in TREND_TICKERS)
    return weights


if __name__ == "__main__":
    import json
    print(json.dumps({"forecasts": compute_forecasts(), "weights": compute_target_weights()}, ensure_ascii=False, indent=2))
