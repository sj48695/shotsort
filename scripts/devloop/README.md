# devloop (vendored)

이 디렉토리는 devloop-hub 의 `devloop-init.sh` 가 복사해 둔 자체완결 스크립트입니다.
devloop-hub 없이 이 프로젝트만 clone 해도 이슈를 생성할 수 있습니다.

```bash
# 목표 기반 AI 이슈 자동 생성 (AI_PROVIDER=claude|codex)
./scripts/devloop/devloop-seed-issues.sh . --ai "<목표>" --count 5 --dry-run
./scripts/devloop/devloop-seed-issues.sh . --ai "<목표>" --count 5

# 스펙 파일로 생성
./scripts/devloop/devloop-seed-issues.sh . --from backlog.json
```

- AI 라우터: `ai.sh` (`.devloop`의 `AI_PROVIDER` 또는 `DEVLOOP_AI_PROVIDER` 사용).
- 엔진: `create_meeting_issues.sh` (gh/glab 양쪽). 시드 스크립트가 같은 디렉토리에서 자동 참조.
- 실제 devloop 루프(plan/code/MR) 실행에는 별도로 devloop-hub(또는 ~/.claude/scripts) 설치가 필요합니다.
