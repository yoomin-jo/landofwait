"""자산배분 하이브리드 봇 — 텔레그램 봇. d:\\tqqq\\telegram_bot.py 패턴을 이식.
/status: 올웨더 ETF 3종 + HAA 슬리브 + 슬리브간 60% 밴드 통합 현황.
"""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from haa_sleeve import ALL_TICKERS as HAA_TICKERS, EXCHANGE as HAA_EXCHANGE
from kis_overseas import get_access_token, get_current_price, get_us_balance
import kis_domestic
from olweather_etf_sleeve import TICKERS as OLWEATHER_TICKERS
import sleeve_monitor

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HAA_STATE_PATH = Path(__file__).parent / "data" / "haa_state.json"
OLWEATHER_STATE_PATH = Path(__file__).parent / "data" / "olweather_etf_state.json"
COIN_STATE_PATH = Path(__file__).parent / "data" / "coin_state.json"
LOG_DIR = Path(__file__).parent / "logs"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HELP_TEXT = (
    "[자산배분 하이브리드 봇]\n"
    "/status         — 올웨더+HAA+슬리브간 비중 통합 현황\n"
    "/run_haa        — HAA 리밸런싱 수동 실행\n"
    "/run_olweather  — 올웨더 ETF 리밸런싱 수동 실행\n"
    "/run_coin       — 코인 리밸런싱 수동 실행\n"
    "/help           — 명령어 목록"
)


def send_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.RequestException:
        logger.exception("텔레그램 알림 전송 실패")


def get_updates(offset: int) -> list[dict]:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        return resp.json().get("result", [])
    except requests.RequestException:
        return []


def cmd_status_haa() -> str:
    state = json.loads(HAA_STATE_PATH.read_text(encoding="utf-8")) if HAA_STATE_PATH.exists() else {}
    month = state.get("last_rebalance_month", "-")
    target_weights = state.get("target_weights", {})
    mode = state.get("mode", "-")

    token = get_access_token("HAA")
    balance = get_us_balance(token)
    cash = balance["cash_usd"]
    qty = balance["qty"]

    prices = {}
    for ticker in HAA_TICKERS:
        try:
            prices[ticker] = get_current_price(token, ticker, HAA_EXCHANGE[ticker])
        except Exception:
            prices[ticker] = 0.0

    values = {t: qty.get(t, 0) * prices[t] for t in HAA_TICKERS}
    total = sum(values.values()) + cash
    weights = {t: values[t] / total if total > 0 else 0 for t in HAA_TICKERS}

    lines = [
        "[HAA 슬리브]",
        f"평가총액: ${total:,.2f} (현금 ${cash:,.2f})",
        f"마지막 리밸런싱: {month} (모드: {mode})",
    ]
    for t in HAA_TICKERS:
        q = qty.get(t, 0)
        tgt = target_weights.get(t, 0.0)
        if q == 0 and tgt == 0:
            continue
        lines.append(f"  {t:<5s} {q:>4}주  ${values[t]:>10,.2f}  {weights[t]:>6.1%}    목표{tgt:.0%}")

    return "\n".join(lines)


def cmd_status_olweather() -> str:
    state = json.loads(OLWEATHER_STATE_PATH.read_text(encoding="utf-8")) if OLWEATHER_STATE_PATH.exists() else {}
    month = state.get("last_rebalance_month", "-")

    token = kis_domestic.get_access_token("ISA")
    balance = kis_domestic.get_balance(token)
    total = kis_domestic.get_total_assets(token)  # RP 포함 (주식잔고조회는 RP 누락)
    qty = balance["qty"]

    lines = [
        "[올웨더 ETF 슬리브 (ISA)]",
        f"ISA 총자산(RP 포함): ₩{total:,.0f} (예수금 ₩{balance['cash']:,.0f})",
        f"마지막 리밸런싱: {month}",
    ]
    for ticker, name in OLWEATHER_TICKERS.items():
        q = qty.get(ticker, 0)
        if q > 0:
            try:
                price = kis_domestic.get_current_price(token, ticker)
            except Exception:
                price = 0.0
            lines.append(f"  {name}({ticker}): {q}주 (₩{q*price:,.0f})")

    return "\n".join(lines)


def cmd_status_band() -> str:
    totals = sleeve_monitor.compute_sleeve_totals()
    flag = " [60% 밴드 초과]" if max(totals["olweather_ratio"], totals["haa_ratio"]) > sleeve_monitor.BAND else ""
    return (
        f"[슬리브간 비중]{flag}\n"
        f"올웨더: ₩{totals['olweather_krw']:,.0f} ({totals['olweather_ratio']:.1%})\n"
        f"HAA:    ₩{totals['haa_krw']:,.0f} ({totals['haa_ratio']:.1%})"
    )


def cmd_status_coin() -> str:
    from coin_sleeve import MARKETS
    import upbit_api

    state = json.loads(COIN_STATE_PATH.read_text(encoding="utf-8")) if COIN_STATE_PATH.exists() else {}
    balances = upbit_api.get_balances()
    prices = upbit_api.get_prices(list(MARKETS))
    values = {m: balances.get(c, 0.0) * prices[m] for m, c in MARKETS.items()}
    total = balances.get("KRW", 0.0) + sum(values.values())
    mode = "실거래" if state.get("live") else "모의"
    lines = [f"[코인 슬리브 (업비트, {mode})]", f"총액: ₩{total:,.0f} (원화 ₩{balances.get('KRW', 0.0):,.0f})",
             f"마지막 실행: {state.get('last_run_date', '-')}"]
    for m, c in MARKETS.items():
        fc = state.get("forecasts", {}).get(m)
        tgt = state.get("target_weights", {}).get(m, 0.0)
        cur = values[m] / total if total > 0 else 0.0
        lines.append(f"  {c}: ₩{values[m]:,.0f} {cur:.0%}  목표{tgt:.0%}" + (f" (예측 {fc:.1f})" if fc is not None else ""))
    return "\n".join(lines)


def cmd_status_version() -> str:
    """배포 누락 확인용 — Pi에서 실제로 돌고 있는 커밋과 로컬 미커밋 변경 여부."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=Path(__file__).parent, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    head = git("log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M")
    dirty = " (미커밋 변경 있음)" if git("status", "--porcelain", "--untracked-files=no") else ""
    return f"[배포 버전] {head}{dirty}"


def cmd_status() -> str:
    sections = []
    for name, fn in [("올웨더 ETF", cmd_status_olweather), ("HAA", cmd_status_haa), ("밴드", cmd_status_band),
                     ("코인", cmd_status_coin), ("배포 버전", cmd_status_version)]:
        try:
            sections.append(fn())
        except Exception as e:
            sections.append(f"[{name}] 조회 실패: {e}")
    return "\n\n".join(sections)


def _run_pipeline(script: str) -> None:
    log_file = LOG_DIR / f"{script.replace('.py', '')}.log"
    log_file.parent.mkdir(exist_ok=True)
    with open(log_file, "a") as lf:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / script)],
            stdout=lf, stderr=lf,
        )


def handle_command(text: str) -> str | None:
    if text == "/help":
        return HELP_TEXT
    if text == "/status":
        try:
            return cmd_status()
        except Exception as e:
            return f"오류: {e}"
    if text == "/run_haa":
        _run_pipeline("live_pipeline_haa.py")
        return "HAA 리밸런싱 실행 시작 — 완료 시 알림 전송"
    if text == "/run_olweather":
        _run_pipeline("live_pipeline_olweather.py")
        return "올웨더 ETF 리밸런싱 실행 시작 — 완료 시 알림 전송"
    if text == "/run_coin":
        _run_pipeline("live_pipeline_coin.py")
        return "코인 리밸런싱 실행 시작 — 매매 시 알림 전송"
    return None


def main() -> None:
    logger.info("자산배분 하이브리드 봇 시작")
    offset = 0
    while True:
        for update in get_updates(offset):
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            from_id = str(msg.get("chat", {}).get("id", ""))
            if from_id != str(TELEGRAM_CHAT_ID):
                continue
            logger.info("수신: %s", text)
            try:
                reply = handle_command(text)
            except Exception as e:
                reply = f"오류: {e}"
                logger.exception("명령어 처리 오류")
            if reply:
                send_message(reply)


if __name__ == "__main__":
    main()
