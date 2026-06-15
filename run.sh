#!/usr/bin/env bash
# shotsort 실행기 — 최초 1회 가상환경 생성·의존성 설치 후 앱을 띄운다.
# 사용:  ./run.sh           # 데스크탑 앱(독립 창)
#        ./run.sh --browser # 브라우저로 실행
#        ./run.sh cli ...   # CLI (예: ./run.sh cli scan ~/Desktop)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 가 필요합니다. macOS: brew install python  (또는 https://python.org)" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "최초 설정 중: 가상환경 생성 + 의존성 설치 (수십 초 걸릴 수 있어요)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo "설치 완료."
fi

# 'cli' 로 시작하면 CLI, 아니면 앱
if [ "${1:-}" = "cli" ]; then
  shift
  exec .venv/bin/python3 cli.py "$@"
elif [ "${1:-}" = "--browser" ]; then
  exec env SHOTSORT_BROWSER=1 .venv/bin/python3 app.py
else
  exec .venv/bin/python3 app.py "$@"
fi
