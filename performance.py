"""실거래 성과 기록 + 월간 리포트 (DESIGN.md "운용 목표와 정상 범위").
매일 16:30 KST 슬리브별 평가액(sleeve_monitor.compute_sleeve_totals — RP·외화RP·알트 포함)을
data/performance.csv에 기록하고, 달이 바뀐 첫 기록 때 지난달 리포트를 텔레그램으로 보낸다.

입금·출금·슬리브 간 이동은 /flow 명령으로 data/flows.csv에 기록 → 수익률 계산에서 제외(시간가중수익률).
정상 범위(RANGES)를 벗어나도 즉시 규칙을 바꾸지 않고 분기 검토 대상으로만 표시한다.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

DATA = Path(__file__).parent / "data"
SNAP_PATH = DATA / "performance.csv"
FLOW_PATH = DATA / "flows.csv"
SLEEVES = {"olweather": "올웨더", "haa": "HAA", "coin": "코인", "total": "전체"}

# 정상 범위 초안(2026-10-05, DESIGN.md) — 고점 대비 낙폭이 이보다 깊으면 "분기 검토 대상"
RANGES = {"olweather": -0.30, "haa": -0.20, "coin": -0.55, "total": -0.25}


def snapshot() -> dict:
    import sleeve_monitor
    t = sleeve_monitor.compute_sleeve_totals()
    row = {"date": datetime.now().strftime("%Y-%m-%d"), "olweather": round(t["olweather_krw"]),
           "haa": round(t["haa_krw"]), "coin": round(t["coin_krw"])}
    row["total"] = row["olweather"] + row["haa"] + row["coin"]
    return row


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def record_flow(sleeve: str, amount: float, note: str = "") -> None:
    """입금 +, 출금 −. 슬리브 간 이동은 두 번 기록(보낸 쪽 −, 받은 쪽 +)."""
    if sleeve not in ("olweather", "haa", "coin"):
        raise ValueError("슬리브는 olweather / haa / coin 중 하나")
    _append(FLOW_PATH, {"datetime": datetime.now().strftime("%Y-%m-%d %H:%M"), "sleeve": sleeve,
                        "amount": round(amount), "note": note})


def load_index() -> pd.DataFrame:
    """슬리브별 시간가중 누적지수(첫 기록 = 1.0). 기록 사이 자금 이동은 다음 기록일 수익에서 제외."""
    snaps = pd.read_csv(SNAP_PATH, parse_dates=["date"]).drop_duplicates("date", keep="last").set_index("date")
    flows = pd.DataFrame(columns=["datetime", "sleeve", "amount"])
    if FLOW_PATH.exists():
        flows = pd.read_csv(FLOW_PATH, parse_dates=["datetime"])
    out = {}
    for s in SLEEVES:
        if s == "total":
            f = flows
        else:
            f = flows[flows["sleeve"] == s]
        # 각 자금 이동을 그 시점 이후 첫 기록일에 귀속
        flow_by_day = pd.Series(0.0, index=snaps.index)
        for _, r in f.iterrows():
            later = snaps.index[snaps.index >= r["datetime"].normalize()]
            if len(later):
                flow_by_day[later[0]] += r["amount"]
        v = snaps[s].astype(float)
        ret = (v - flow_by_day) / v.shift(1) - 1
        out[s] = (1 + ret.fillna(0)).cumprod()
    return pd.DataFrame(out)


def report(month: str | None = None) -> str:
    """month(YYYY-MM)의 월간 리포트. 생략하면 이번 달 누적(월초 이후)."""
    idx = load_index()
    month = month or datetime.now().strftime("%Y-%m")
    start = pd.Timestamp(month + "-01")
    end = start + pd.offsets.MonthEnd(0)
    prev = idx[idx.index < start]
    cur = idx[(idx.index >= start) & (idx.index <= end)]
    if cur.empty:
        return f"[성과] {month} 기록 없음"
    base = prev.iloc[-1] if not prev.empty else cur.iloc[0]
    year_base_rows = idx[idx.index < pd.Timestamp(f"{start.year}-01-01")]
    year_base = year_base_rows.iloc[-1] if not year_base_rows.empty else idx.iloc[0]
    upto = idx[idx.index <= end]
    dd = upto.iloc[-1] / upto.cummax().iloc[-1] - 1
    snaps = pd.read_csv(SNAP_PATH, parse_dates=["date"]).set_index("date")
    last_val = snaps[snaps.index <= end].iloc[-1]

    lines = [f"📊 [월간 성과] {month}" + ("" if not prev.empty else " (기록 시작 이후)")]
    breaches = []
    for s, name in SLEEVES.items():
        m = cur[s].iloc[-1] / base[s] - 1
        ytd = upto[s].iloc[-1] / year_base[s] - 1
        ok = dd[s] >= RANGES[s]
        if not ok:
            breaches.append(name)
        lines.append(f"{name}: {m:+.1%} (연초 이후 {ytd:+.1%}) / 고점대비 {dd[s]:.1%} "
                     f"{'✅' if ok else '⚠️'} 범위 {RANGES[s]:.0%} / ₩{last_val[s]:,.0f}")
    if breaches:
        lines.append(f"⚠️ 정상 범위 이탈: {', '.join(breaches)} — 규칙은 바꾸지 말고 분기 검토에서 원인 분석")
    lines.append("자금 이동(입출금·슬리브 간 이동)이 있었다면 /flow로 기록해야 수익률이 정확합니다")
    return "\n".join(lines)


def notify(text: str) -> None:
    requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=10)


def main() -> None:
    row = snapshot()
    last_date = None
    if SNAP_PATH.exists():
        prev = pd.read_csv(SNAP_PATH)
        last_date = str(prev["date"].iloc[-1]) if not prev.empty else None
    _append(SNAP_PATH, row)
    # 달이 바뀐 첫 기록 → 지난달 리포트
    if last_date and last_date[:7] != row["date"][:7]:
        notify(report(last_date[:7]))


if __name__ == "__main__":
    main()
