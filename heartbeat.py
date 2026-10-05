"""하루 1회 생존 신호 — 매일 09:30 KST(코인 09:05·올웨더 09:10 실행 뒤) 텔레그램으로 상태 요약을 보낸다.
이 메시지가 안 오면 Pi·네트워크·봇에 문제가 있다는 뜻. 거래소 API는 부르지 않고 로컬 상태만 본다
(API 장애와 무관하게 항상 보내지도록).
"""
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

DATA = Path(__file__).parent / "data"
UNITS = ["haa-rebalance.timer", "olweather-rebalance.timer", "sleeve-monitor.timer",
         "coin-rebalance.timer", "hybrid-bot.service"]
REBALANCE_WINDOW_BDAYS = 5  # live_pipeline_haa/olweather와 같은 값


def _state(name: str) -> dict:
    path = DATA / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _run(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=Path(__file__).parent, capture_output=True, text=True, timeout=10).stdout.strip()


def build() -> str:
    today = pd.Timestamp.now().normalize()
    month = today.strftime("%Y-%m")
    window_over = len(pd.bdate_range(today.replace(day=1), today)) > REBALANCE_WINDOW_BDAYS
    warnings = []

    inactive = [u for u in UNITS if _run("systemctl", "is-active", u) != "active"]
    if inactive:
        warnings.append(f"비활성 서비스/타이머: {', '.join(inactive)}")

    haa, olw, coin = _state("haa_state.json"), _state("olweather_etf_state.json"), _state("coin_state.json")
    for name, st in (("HAA", haa), ("올웨더", olw)):
        if window_over and st.get("last_rebalance_month") != month:
            warnings.append(f"{name} 이번 달({month}) 리밸런싱 미완료 — 월초 매매창 지남, 수동 확인 필요")
    if coin.get("last_run_date") != today.strftime("%Y-%m-%d"):
        warnings.append(f"코인 오늘 실행 기록 없음 (마지막 {coin.get('last_run_date', '-')})")

    free = shutil.disk_usage("/").free / shutil.disk_usage("/").total
    if free < 0.10:
        warnings.append(f"디스크 여유 {free:.0%}")

    head = "⚠️ [하트비트] 확인 필요" if warnings else "✅ [하트비트] 정상"
    fc = coin.get("forecasts", {})
    alt_fc = coin.get("alt_forecasts", {})
    lines = [
        head,
        f"HAA: {haa.get('last_rebalance_month', '-')} 리밸런싱 ({haa.get('mode', '-')})",
        f"올웨더: {olw.get('last_rebalance_month', '-')} 리밸런싱",
        f"코인({'실거래' if coin.get('live') else '모의'}): " + ", ".join(
            [f"{m.split('-')[1]} {v:.1f}" for m, v in fc.items()] + [f"{c} {v:.1f}" for c, v in alt_fc.items()]),
        f"버전: {_run('git', 'log', '-1', '--format=%h')} / {datetime.now():%m-%d %H:%M}",
    ]
    return "\n".join(lines + [f"- {w}" for w in warnings])


def main() -> None:
    requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": build()}, timeout=10)


if __name__ == "__main__":
    main()
