"""긴급 정지 스위치 — 텔레그램 /pause·/resume로 리밸런싱 파이프라인을 멈추고 다시 켠다(RUNBOOK 7번).
상태는 data/paused.json. 멈춘 파이프라인은 실행되자마자 아무것도 안 하고 끝난다(모니터링·하트비트·성과 기록은 계속).
"""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).parent / "data" / "paused.json"
NAMES = {"coin": "코인", "haa": "HAA", "olweather": "올웨더", "coinexp": "코인 실험"}


def paused() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}


def is_paused(name: str) -> bool:
    return bool(paused().get(name))


def set_paused(target: str, value: bool) -> list[str]:
    """target: coin / haa / olweather / coinexp / all. 바뀐 파이프라인 이름 목록 반환."""
    names = list(NAMES) if target == "all" else [target]
    if any(n not in NAMES for n in names):
        raise ValueError("대상은 coin / haa / olweather / coinexp / all 중 하나")
    state = paused()
    for n in names:
        state[n] = value
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return [NAMES[n] for n in names]
