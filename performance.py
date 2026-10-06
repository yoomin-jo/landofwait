"""실거래 성과 기록 + 월간 리포트 (DESIGN.md "운용 목표와 정상 범위").
하루 2회 슬리브별 평가액(sleeve_monitor.compute_sleeve_totals — RP·외화RP·알트 포함)을 data/performance.csv에
하루 한 줄로 기록하고 구글시트 '자산배분 성과'를 갱신한다: 07:00 KST(미국장 마감 후) HAA·코인, 16:00 KST(국내장
마감 후) 올웨더.
달이 바뀐 첫 기록 때 지난달 리포트를 텔레그램으로 보낸다.

입금·출금·슬리브 간 이동은 /flow 명령으로 data/flows.csv에 기록 → 수익률 계산에서 제외(시간가중수익률).
정상 범위(RANGES)를 벗어나도 즉시 규칙을 바꾸지 않고 분기 검토 대상으로만 표시한다.
"""
from __future__ import annotations

import csv
import os
import time
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


def push_sheet() -> None:
    """구글시트 '자산배분 성과'로 전체 성과표 전송(시트의 Apps Script 웹 앱 doPost — deploy/sheet_webapp.gs).
    매번 전체를 다시 쓰므로 한 번 실패해도 다음 전송에서 복구된다. SHEET_WEBAPP_URL이 없으면 건너뜀."""
    url = os.environ.get("SHEET_WEBAPP_URL")
    if not url:
        return
    idx = load_index()
    snaps = pd.read_csv(SNAP_PATH, parse_dates=["date"]).drop_duplicates("date", keep="last").set_index("date")
    dd = idx / idx.cummax() - 1
    now = idx.index[-1]
    month_start = pd.Timestamp(now.strftime("%Y-%m-01"))
    before_month = idx[idx.index < month_start]
    m_base = before_month.iloc[-1] if not before_month.empty else idx.iloc[0]
    before_year = idx[idx.index < pd.Timestamp(f"{now.year}-01-01")]
    y_base = before_year.iloc[-1] if not before_year.empty else idx.iloc[0]

    summary = [["슬리브", "평가액", "이번 달", "연초 이후", "고점 대비", "낙폭 한도", "상태"]]
    for s, name in SLEEVES.items():
        ok = dd[s].iloc[-1] >= RANGES[s]
        summary.append([name, int(snaps[s].iloc[-1]), float(idx[s].iloc[-1] / m_base[s] - 1),
                        float(idx[s].iloc[-1] / y_base[s] - 1), float(dd[s].iloc[-1]), RANGES[s],
                        "정상" if ok else "범위 이탈 — 분기 검토"])

    month_end = idx.groupby(idx.index.to_period("M")).last()
    month_ret = month_end / month_end.shift(1) - 1
    month_ret.iloc[0] = month_end.iloc[0] / idx.iloc[0] - 1
    monthly = [["월"] + list(SLEEVES.values())] + [
        [str(p)] + [float(month_ret.loc[p, s]) for s in SLEEVES] for p in month_ret.index]

    names = list(SLEEVES.values())
    history = [["날짜"] + names + [f"{n} 누적" for n in names] + [f"{n} 낙폭" for n in names]]
    for d in idx.index:
        history.append([d.strftime("%Y-%m-%d")] + [int(snaps.loc[d, s]) for s in SLEEVES]
                       + [float(idx.loc[d, s]) for s in SLEEVES] + [float(dd.loc[d, s]) for s in SLEEVES])

    flows = [["일시", "슬리브", "금액", "메모"]]
    if FLOW_PATH.exists():
        for _, r in pd.read_csv(FLOW_PATH).fillna("").iterrows():
            flows.append([str(r["datetime"]), SLEEVES.get(r["sleeve"], r["sleeve"]), int(r["amount"]), str(r["note"])])

    payload = {"token": os.environ["SHEET_TOKEN"], "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "summary": summary, "monthly": monthly, "history": history, "flows": flows}
    # 구글 쪽 일시 오류(2026-10-06 20:35 404 한 번 발생)는 재시도로 흡수 — 3번 모두 실패할 때만 예외
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.text.strip() == "ok":
                return
            error = f"{resp.status_code} {resp.text[:200]}"
        except requests.RequestException as e:
            error = str(e)
        if attempt < 2:
            time.sleep(30)
    raise RuntimeError(f"구글시트 전송 3회 실패: {error}")


def notify(text: str) -> None:
    requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=10)


MODE_COLUMNS = {"us": ["haa", "coin"], "kr": ["olweather"]}  # 07:00 미국장 마감 후 / 16:00 국내장 마감 후


def main(mode: str) -> None:
    """하루 한 줄. mode="us"(07:00)는 HAA·코인 칸만, mode="kr"(16:00)은 올웨더 칸만 갱신하고
    나머지 칸은 오늘 줄(없으면 직전 줄) 값을 그대로 둔다."""
    fresh = snapshot()
    row = dict(fresh)
    last_date = None
    if SNAP_PATH.exists():
        prev = pd.read_csv(SNAP_PATH, dtype={"date": str})
        if not prev.empty:
            last_date = prev["date"].iloc[-1]
            base = prev.iloc[-1].to_dict()  # 오늘 줄이 있으면 오늘 줄, 없으면 직전 날짜 줄
            row = {"date": fresh["date"], **{s: int(base[s]) for s in ("olweather", "haa", "coin")}}
            for col in MODE_COLUMNS[mode]:
                row[col] = fresh[col]
            row["total"] = row["olweather"] + row["haa"] + row["coin"]
            prev = prev[prev["date"] != row["date"]]
            prev.to_csv(SNAP_PATH, index=False)
    _append(SNAP_PATH, row)
    # 달이 바뀐 첫 기록 → 지난달 리포트
    if last_date and last_date[:7] != row["date"][:7]:
        notify(report(last_date[:7]))
    push_sheet()


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "kr")
