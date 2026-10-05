"""systemd 서비스 실패 알림 — deploy/notify-failure@.service가 OnFailure로 호출한다.
리밸런싱 스크립트가 알림을 보내기 전에 죽는 경우(라이브러리 오류·문법 오류·예상 못 한 예외)를 잡기 위함.
사용: python notify_failure.py <실패한 유닛 이름>
"""
import os
import subprocess
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    unit = sys.argv[1] if len(sys.argv) > 1 else "(알 수 없음)"
    try:
        log = subprocess.run(["journalctl", "-u", unit, "-n", "12", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # 로그를 못 읽어도 알림은 보낸다
        log = ""
    text = f"⚠️ [서비스 실패] {unit}\n" + (f"마지막 로그:\n{log[-1500:]}" if log else "로그 없음 — Pi에서 확인 필요")
    requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                  json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=10)


if __name__ == "__main__":
    main()
