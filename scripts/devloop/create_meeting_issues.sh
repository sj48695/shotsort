#!/usr/bin/env bash
# template: playwright-e2e v0.1
# Create issues from meeting candidates (B3-2).
#
# Input: JSON array via stdin
#   [{"id":"...","title":"...","description":"...","labels":[...],"attachments":[...],"approved":true}, ...]
# Env:
#   PROJECT_DIR (required) — repo working tree
#   PLATFORM    (required) — "github" or "gitlab"
# Output: JSON to stdout: {"ok":[iid…], "fail":[{"title":"...","error":"..."},...]}

set -euo pipefail

: "${PROJECT_DIR:?PROJECT_DIR required}"
: "${PLATFORM:?PLATFORM required (github|gitlab)}"

if [[ ! -d "$PROJECT_DIR" ]]; then
  printf '{"ok":[],"fail":[{"title":"(setup)","error":"PROJECT_DIR not found: %s"}]}\n' "$PROJECT_DIR"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  printf '{"ok":[],"fail":[{"title":"(setup)","error":"jq not installed"}]}\n'
  exit 1
fi

cd "$PROJECT_DIR"

# ─── 타겟 정합성 가드 (cross-project 누수 방지) ─────────────────────────────────
# autoblog 이슈가 act-server 에 생성된 사고(2026-06-13 #297) 재발 방지.
# cwd 의 origin remote 가 (a) PLATFORM 호스트 와 (b) PROJECT_DIR 프로젝트명 둘 다와
# 일치해야만 생성한다. 어긋나면 한 건도 만들지 않고 명확한 사유로 거부한다.
_remote="$(git remote get-url origin 2>/dev/null || echo "")"
_proj_name="$(basename "$PROJECT_DIR")"
if [[ -n "$_remote" ]]; then
  if { [[ "$PLATFORM" == "github" ]] && [[ "$_remote" != *github.com* ]]; } \
  || { [[ "$PLATFORM" == "gitlab" ]] && [[ "$_remote" == *github.com* ]]; }; then
    printf '{"ok":[],"fail":[{"title":"(guard)","error":"PLATFORM=%s 인데 origin=%s — 플랫폼 불일치, 생성 거부(cross-project 방지)"}]}\n' "$PLATFORM" "$_remote"
    exit 1
  fi
  if [[ "$_remote" != *"$_proj_name"* ]]; then
    printf '{"ok":[],"fail":[{"title":"(guard)","error":"PROJECT_DIR=%s 인데 origin=%s — 프로젝트명 불일치, 생성 거부(cross-project 방지)"}]}\n' "$_proj_name" "$_remote"
    exit 1
  fi
fi

input="$(cat -)"
if [[ -z "$input" ]]; then
  echo '{"ok":[],"fail":[],"skipped":[]}'
  exit 0
fi

# ─── 자식 이슈 중복 제거 (모든 생성 경로 공용 백스톱) ───────────────────────────
# DevLoop 의 자식 이슈 생성은 여러 경로(부모 잔여작업 분석기·턴초과 분리·회의 PM)가 있고
# 각자 제각각의 dedup 만 있어, 부모를 거의 그대로 재서술한 자식(#245↔#247 류)이 새어나간다.
# 모든 경로가 결국 이 스크립트를 통과하므로 여기서 생성 직전 1회 통합 차단한다(제목 완전일치 +
# 부모/형제 토큰겹침). python3 없거나 ISSUE_DEDUP=off 면 조용히 생략(기존 동작 유지).
skipped_json="[]"
_dedup_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${ISSUE_DEDUP:-on}" != "off" ]] && command -v python3 >/dev/null 2>&1 && [[ -f "$_dedup_dir/issue-dedup.py" ]]; then
  existing_json="[]"
  if [[ "$PLATFORM" == "gitlab" ]]; then
    # per_page 최대 100 → 페이지 순회. 열린 이슈만(닫힌 자식 재제안은 의도일 수 있어 제외).
    existing_json="$(glab api --paginate "projects/:id/issues?state=opened&per_page=100" 2>/dev/null \
      | jq -s 'add // [] | map({iid:.iid, title:(.title // ""), body:(.description // "")})' 2>/dev/null || echo "[]")"
  else
    existing_json="$(gh issue list --state open --limit 500 --json number,title,body 2>/dev/null \
      | jq 'map({iid:.number, title:(.title // ""), body:(.body // "")})' 2>/dev/null || echo "[]")"
  fi
  [[ -z "$existing_json" ]] && existing_json="[]"
  _dedup_in="$(jq -n --argjson c "$input" --argjson e "$existing_json" --arg t "${ISSUE_DEDUP_THRESHOLD:-0.7}" \
    '{candidates:$c, existing:$e, threshold:($t|tonumber)}' 2>/dev/null || echo "")"
  if [[ -n "$_dedup_in" ]] && _dedup_out="$(printf '%s' "$_dedup_in" | python3 "$_dedup_dir/issue-dedup.py" 2>/dev/null)" && [[ -n "$_dedup_out" ]]; then
    input="$(printf '%s' "$_dedup_out" | jq '.keep')"
    skipped_json="$(printf '%s' "$_dedup_out" | jq '.skip')"
    _nskip="$(printf '%s' "$skipped_json" | jq 'length' 2>/dev/null || echo 0)"
    if [[ "${_nskip:-0}" -gt 0 ]]; then
      printf '%s' "$skipped_json" | jq -r '.[] | "[issue-dedup] skip \"\(.title)\" (\(.reason) → #\(.dupOf))"' >&2 || true
    fi
  fi
fi

count="$(printf '%s' "$input" | jq 'length')"
ok=()
fail_titles=()
fail_errors=()

json_escape() {
  printf '%s' "$1" | jq -Rs .
}

upload_glab_attachment() {
  # stdout = markdown link, stderr = error
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "file not found: $file" >&2
    return 1
  fi
  local resp
  if ! resp="$(glab api "projects/:id/uploads" -F "file=@${file}" 2>/dev/null)"; then
    echo "glab upload failed" >&2
    return 1
  fi
  printf '%s' "$resp" | jq -r '.markdown // empty'
}

upload_gh_attachment() {
  # stdout = raw URL, stderr = error
  # Push to assets/screenshots branch (default branch 트리거 회피)
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "file not found: $file" >&2
    return 1
  fi
  local repo
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  if [[ -z "$repo" ]]; then
    echo "gh repo not detected" >&2
    return 1
  fi
  local ts
  ts="$(date +%s)"
  local base
  base="$(basename "$file")"
  local rel="meeting-attachments/${ts}-${base}"
  local b64
  b64="$(base64 < "$file" | tr -d '\n')"
  local body
  body="$(jq -n --arg msg "chore: attach $base for meeting issue" --arg content "$b64" --arg branch "assets/screenshots" \
    '{message:$msg, content:$content, branch:$branch}')"

  # ensure branch exists (best effort: try once)
  gh api -X PUT "repos/${repo}/contents/${rel}" --input - <<<"$body" >/dev/null 2>&1 || {
    echo "gh contents PUT failed" >&2
    return 1
  }
  printf 'https://raw.githubusercontent.com/%s/assets/screenshots/%s' "$repo" "$rel"
}

create_glab_issue() {
  local title="$1" desc="$2" labels_csv="$3"
  local args=(issue create --title "$title" --description "$desc")
  [[ -n "$labels_csv" ]] && args+=(--label "$labels_csv")
  glab "${args[@]}" 2>&1 | grep -oE '/issues/[0-9]+' | head -1 | grep -oE '[0-9]+'
}

create_gh_issue() {
  local title="$1" body="$2" labels_csv="$3"
  local args=(issue create --title "$title" --body "$body")
  if [[ -n "$labels_csv" ]]; then
    IFS=',' read -ra LARR <<<"$labels_csv"
    for lbl in "${LARR[@]}"; do
      [[ -n "$lbl" ]] && args+=(--label "$lbl")
    done
  fi
  local url
  url="$(gh "${args[@]}" 2>&1 | tail -1)"
  basename "$url"
}

for i in $(seq 0 $((count - 1))); do
  title="$(printf '%s' "$input" | jq -r ".[$i].title // \"\"")"
  desc="$(printf '%s' "$input" | jq -r ".[$i].description // \"\"")"
  labels_csv="$(printf '%s' "$input" | jq -r ".[$i].labels // [] | join(\",\")")"
  attachments="$(printf '%s' "$input" | jq -r ".[$i].attachments // [] | .[]")"

  if [[ -z "$title" ]]; then
    fail_titles+=("(empty)")
    fail_errors+=("title 비어있음")
    continue
  fi

  attach_section=""
  if [[ -n "$attachments" ]]; then
    while IFS= read -r att; do
      [[ -z "$att" ]] && continue
      if [[ "$PLATFORM" == "gitlab" ]]; then
        if md="$(upload_glab_attachment "$att" 2>/dev/null)" && [[ -n "$md" ]]; then
          attach_section+="${md}"$'\n'
        fi
      else
        if raw="$(upload_gh_attachment "$att" 2>/dev/null)" && [[ -n "$raw" ]]; then
          attach_section+="![](${raw})"$'\n'
        fi
      fi
    done <<<"$attachments"
  fi

  full_desc="$desc"
  if [[ -n "$attach_section" ]]; then
    full_desc+=$'\n\n---\n'"$attach_section"
  fi

  iid=""
  err=""
  if [[ "$PLATFORM" == "gitlab" ]]; then
    if ! iid="$(create_glab_issue "$title" "$full_desc" "$labels_csv" 2>&1)"; then
      err="glab issue create 실패"
    fi
  else
    if ! iid="$(create_gh_issue "$title" "$full_desc" "$labels_csv" 2>&1)"; then
      err="gh issue create 실패"
    fi
  fi

  if [[ -n "$iid" && "$iid" =~ ^[0-9]+$ ]]; then
    ok+=("$iid")
  else
    fail_titles+=("$title")
    fail_errors+=("${err:-iid 파싱 실패: $iid}")
  fi
done

# 생성된 자식 이슈('Parent: #N')에 실제 GitLab 링크 멱등 생성 (best-effort).
# stdout 은 순수 JSON 결과여야 하므로 링크 스크립트의 stdout 은 차단(로그는 stderr).
if [[ "$PLATFORM" == "gitlab" ]]; then
  _self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for _cand in "$_self_dir/link-parent-children.sh" "$HOME/.claude/scripts/link-parent-children.sh"; do
    if [[ -f "$_cand" ]]; then
      bash "$_cand" "$PROJECT_DIR" 1>/dev/null || true
      break
    fi
  done
fi

# Build JSON output
ok_json="[]"
if [[ ${#ok[@]} -gt 0 ]]; then
  ok_json="$(printf '%s\n' "${ok[@]}" | jq -R 'tonumber? // .' | jq -s .)"
fi

fail_json="[]"
if [[ ${#fail_titles[@]} -gt 0 ]]; then
  fail_json="[]"
  for idx in "${!fail_titles[@]}"; do
    fail_json="$(jq -n --argjson acc "$fail_json" --arg t "${fail_titles[$idx]}" --arg e "${fail_errors[$idx]}" \
      '$acc + [{title:$t, error:$e}]')"
  done
fi

jq -n --argjson ok "$ok_json" --argjson fail "$fail_json" --argjson skipped "$skipped_json" '{ok:$ok, fail:$fail, skipped:$skipped}'
