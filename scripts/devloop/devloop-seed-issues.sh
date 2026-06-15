#!/usr/bin/env bash
# template: devloop-seed-issues v0.1
# DevLoop 가 작업할 이슈를 한 프로젝트에 자동 생성한다 — 대시보드 없이 standalone.
#
# 두 가지 소스:
#   1) --from <file.json>   직접 정의한 이슈 JSON 배열 ([{title, description?, labels?}, ...])
#   2) --ai "<목표>"        claude 로 repo 를 탐색해 목표 기반 이슈를 자동 생성
#
# 공통 처리: 라벨 존재 보장(없으면 생성) → 기존 열린 이슈와 title 중복 스킵 →
#            create_meeting_issues.sh(범용 엔진)로 gh/glab 이슈 생성.
# devloop 가 픽업하도록 기본 라벨은 task (MR 없는 열린 이슈만 픽업됨).
#
# 사용법:
#   ~/.claude/scripts/devloop-seed-issues.sh <project-dir> --ai "결제 모듈 MVP" --count 5
#   ~/.claude/scripts/devloop-seed-issues.sh <project-dir> --from backlog.json
#   ~/.claude/scripts/devloop-seed-issues.sh <project-dir> --ai "..." --dry-run
#
# 옵션:
#   --from <file>        이슈 정의 JSON 배열 파일
#   --ai "<목표>"        claude 자동 생성 (repo 탐색 + 목표)
#   --count <n>          --ai 생성 개수 힌트 (기본: 5)
#   --label <csv>        기본 라벨 (기본: task). 각 이슈에 라벨 없으면 이걸 부여
#   --model <model>      --ai 모델 (기본: sonnet)
#   --platform g..|gl..  (기본: .devloop PLATFORM → remote 자동감지)
#   --dry-run            생성 안 하고 만들 이슈만 출력
#   -h | --help
set -euo pipefail

# 자기 자신(심링크 가능)의 실제 디렉토리 → create_meeting_issues.sh 위치 해석.
REAL_SRC="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$REAL_SRC")" && pwd)"
source "$SCRIPT_DIR/ai.sh"

PROJECT_DIR="."
FROM_FILE=""
AI_GOAL=""
COUNT=5
DEFAULT_LABEL="task"
MODEL="sonnet"
PLATFORM=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)     FROM_FILE="$2"; shift 2 ;;
    --ai)       AI_GOAL="$2"; shift 2 ;;
    --count)    COUNT="$2"; shift 2 ;;
    --label)    DEFAULT_LABEL="$2"; shift 2 ;;
    --model)    MODEL="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --*)        echo "❌ 알 수 없는 옵션: $1" >&2; exit 1 ;;
    *)          PROJECT_DIR="$1"; shift ;;
  esac
done

die() { printf '❌ %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*" >&2; }

command -v jq >/dev/null 2>&1 || die "jq 미설치"
[[ -d "$PROJECT_DIR" ]] || die "프로젝트 디렉토리 없음: $PROJECT_DIR"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "git repo 아님: $PROJECT_DIR"

ENGINE=""
for cand in "$SCRIPT_DIR/create_meeting_issues.sh" "$HOME/.claude/scripts/create_meeting_issues.sh"; do
  [[ -f "$cand" ]] && { ENGINE="$cand"; break; }
done
[[ -n "$ENGINE" ]] || die "create_meeting_issues.sh(생성 엔진)를 찾을 수 없음"

# ─── 플랫폼 결정: --platform > .devloop PLATFORM > remote 자동감지 ──────────────
if [[ -z "$PLATFORM" && -f "$PROJECT_DIR/.devloop" ]]; then
  PLATFORM="$(grep -E '^PLATFORM=' "$PROJECT_DIR/.devloop" | head -1 | cut -d= -f2 | tr -d ' \n' || true)"
fi
if [[ -z "$PLATFORM" ]]; then
  remote_url="$(git -C "$PROJECT_DIR" remote get-url origin 2>/dev/null || echo "")"
  case "$remote_url" in
    *github.com*) PLATFORM="github" ;;
    *gitlab*)     PLATFORM="gitlab" ;;
    *)            die "플랫폼 감지 실패 — --platform github|gitlab 지정" ;;
  esac
fi
[[ "$PLATFORM" == "github" || "$PLATFORM" == "gitlab" ]] || die "PLATFORM 은 github|gitlab (받음: $PLATFORM)"

# CLI 인증 점검
if [[ "$PLATFORM" == "github" ]]; then
  command -v gh >/dev/null 2>&1 || die "gh CLI 미설치"
  gh auth status >/dev/null 2>&1 || die "gh 미인증 (gh auth login)"
else
  command -v glab >/dev/null 2>&1 || die "glab CLI 미설치"
  glab auth status >/dev/null 2>&1 || die "glab 미인증 (glab auth login)"
fi

cd "$PROJECT_DIR"
log "🌱 이슈 시드: $(basename "$PROJECT_DIR") (platform=$PLATFORM)"

# ─── 1) 이슈 JSON 확보 ────────────────────────────────────────────────────────
raw_json=""
if [[ -n "$FROM_FILE" ]]; then
  [[ -f "$FROM_FILE" ]] || die "--from 파일 없음: $FROM_FILE"
  raw_json="$(cat "$FROM_FILE")"
elif [[ -n "$AI_GOAL" ]]; then
  provider="$(devloop_ai_provider "$PROJECT_DIR")"
  command -v "$provider" >/dev/null 2>&1 || die "$provider CLI 미설치 (--ai 모드 필요)"
  log "🤖 $provider($MODEL) 로 이슈 자동 생성 중… (목표: $AI_GOAL)"
  ai_prompt="너는 이 저장소를 분석해 DevLoop 자동화가 작업할 GitHub/GitLab 이슈를 설계한다.

목표: ${AI_GOAL}

저장소(현재 디렉토리)의 구조/README/기존 코드를 Read/Glob/Grep 으로 살펴본 뒤,
독립적으로 구현 가능한 단위 작업 이슈를 최대 ${COUNT}개 제안하라.

규칙:
- 각 이슈는 하나의 PR 로 머지 가능한 작은 단위. 서로 의존이 적게.
- title 은 한국어 명령형 한 줄. description 은 배경/완료조건(AC)/관련 파일 포함, 마크다운.
- labels 는 [\"task\"] 기본. 버그성이면 [\"bug\"], 큰 묶음이면 [\"phase\"].
- 이미 구현된 기능은 제외.

출력: 오직 JSON 배열 하나만. 다른 설명/문장 금지. 형식:
[{\"title\":\"...\",\"description\":\"...\",\"labels\":[\"task\"]}]"
  ai_out="$(devloop_ai_print "$PROJECT_DIR" "pm" "$MODEL" "$ai_prompt" 2>/dev/null || true)"
  # 추출 전략 (견고하게): 코드펜스 제거 → 첫 '[' 부터 마지막 ']' 까지 greedy 슬라이스(멀티라인/중첩배열 대응).
  cleaned="$(printf '%s' "$ai_out" | sed 's/```json//g; s/```//g')"
  # 첫 '[' 부터 마지막 ']' 까지 greedy 슬라이스 (멀티라인/중첩배열 대응). perl -0777 로 전체 슬럽.
  raw_json="$(printf '%s' "$cleaned" | perl -0777 -ne 'print $1 if /(\[.*\])/s' 2>/dev/null || true)"
  # 폴백: 전체가 이미 순수 배열인 경우
  if ! printf '%s' "$raw_json" | jq -e 'type=="array"' >/dev/null 2>&1; then
    raw_json="$(printf '%s' "$cleaned" | jq -c 'if type=="array" then . else empty end' 2>/dev/null | head -1 || true)"
  fi
  if ! printf '%s' "$raw_json" | jq -e 'type=="array"' >/dev/null 2>&1; then
    log "AI 원본 출력:"; printf '%s\n' "$ai_out" >&2
    die "$provider 출력에서 JSON 배열을 추출하지 못함"
  fi
else
  die "이슈 소스 미지정 — --from <file.json> 또는 --ai \"<목표>\" 필요"
fi

# 검증 + 기본 라벨 주입(라벨 없으면 DEFAULT_LABEL)
printf '%s' "$raw_json" | jq -e 'type=="array"' >/dev/null 2>&1 || die "이슈 JSON 이 배열이 아님"
issues_json="$(printf '%s' "$raw_json" | jq --arg dl "$DEFAULT_LABEL" '
  [ .[] | select(.title and (.title|length>0)) | {
      title: .title,
      description: (.description // ""),
      labels: ((.labels // []) | if length==0 then [$dl] else . end)
    } ]')"
total="$(printf '%s' "$issues_json" | jq 'length')"
[[ "$total" -gt 0 ]] || die "생성할 유효한 이슈가 없음"

# ─── 2) 기존 열린 이슈 title 로 중복 스킵 ─────────────────────────────────────
existing_titles="$(
  if [[ "$PLATFORM" == "github" ]]; then
    gh issue list --state open --limit 200 --json title -q '.[].title' 2>/dev/null || true
  else
    glab issue list --state opened --per-page 200 --output json 2>/dev/null | jq -r '.[].title' 2>/dev/null || true
  fi
)"
if [[ -n "$existing_titles" ]]; then
  issues_json="$(printf '%s' "$issues_json" | jq --argjson ex "$(printf '%s' "$existing_titles" | jq -Rsc 'split("\n")|map(select(length>0))')" \
    '[ .[] | select(.title as $t | ($ex | index($t)) | not) ]')"
fi
after="$(printf '%s' "$issues_json" | jq 'length')"
skipped=$((total - after))
[[ "$skipped" -gt 0 ]] && log "  · 기존과 title 중복 ${skipped}건 스킵"
[[ "$after" -gt 0 ]] || { log "✅ 새로 만들 이슈 없음 (전부 중복)"; exit 0; }

# ─── 3) dry-run ───────────────────────────────────────────────────────────────
if [[ "$DRY_RUN" -eq 1 ]]; then
  log "🔎 dry-run — 생성할 이슈 ${after}건:"
  printf '%s' "$issues_json" | jq -r '.[] | "  • [\(.labels|join(","))] \(.title)"'
  exit 0
fi

# ─── 4) 라벨 존재 보장 (없으면 생성 — fresh repo 대비) ────────────────────────
labels_used="$(printf '%s' "$issues_json" | jq -r '[.[].labels[]] | unique | .[]')"
while IFS= read -r lbl; do
  [[ -z "$lbl" ]] && continue
  if [[ "$PLATFORM" == "github" ]]; then
    gh label create "$lbl" --color ededed 2>/dev/null || true
  else
    glab label create --name "$lbl" --color '#ededed' 2>/dev/null || true
  fi
done <<< "$labels_used"

# ─── 5) 생성 (범용 엔진 호출) ─────────────────────────────────────────────────
log "📝 생성 중… (${after}건)"
result="$(printf '%s' "$issues_json" | PROJECT_DIR="$PROJECT_DIR" PLATFORM="$PLATFORM" bash "$ENGINE")"
ok_n="$(printf '%s' "$result" | jq '.ok | length')"
fail_n="$(printf '%s' "$result" | jq '.fail | length')"
log ""
log "✅ 이슈 생성: 성공 ${ok_n}건  실패 ${fail_n}건"
printf '%s' "$result" | jq -r '.ok[]? | "  • #\(.)"' >&2
printf '%s' "$result" | jq -r '.fail[]? | "  ✗ \(.title) — \(.error)"' >&2
log ""
log "다음: ~/.claude/scripts/scheduler.sh --test --project $(basename "$PROJECT_DIR")  (MR 없는 이슈 픽업)"

# 머신 파싱용 결과는 stdout 으로
printf '%s\n' "$result"
