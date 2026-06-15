# shotsort

데스크탑/배경화면에 쌓인 **스크린샷을 프로젝트별로 자동 분류**하고, **지워도 되는 것들을 묶어서 한꺼번에 정리**하는 CLI.

기존 도구(czkawka·fclones 등)는 "중복/유사 이미지"는 잘 찾지만 "스크린샷 내용을 읽고 프로젝트별로 묶기"는 못 해서 직접 만든 도구입니다.

## 🚀 빠른 시작 (처음 사용)

**필요한 것**: macOS, `python3`, `git`. (없으면 `brew install python git`)

터미널(Terminal.app)에서 아래 두 줄이면 끝입니다 — 최초 실행 시 의존성은 자동 설치됩니다.

```bash
git clone https://github.com/sj48695/shotsort.git
cd shotsort && ./run.sh
```

데스크탑 앱 창이 뜨면:
1. **스캔** 버튼 (기본 경로 `~/Desktop`) → 스크린샷이 그룹별로 묶입니다
2. 지울 것들을 체크 → **선택 항목 휴지통으로** (복구 가능)

> API 키 없이도 동작합니다(무료 로컬 모드). `export ANTHROPIC_API_KEY=sk-...` 후 실행하면
> Claude 가 더 정확하게 프로젝트별로 묶어줍니다. 새 버전이 나오면 앱이 알려주고 한 번에 업데이트됩니다.

다른 실행 방법:
```bash
./run.sh --browser          # 앱을 브라우저로
./run.sh cli scan ~/Desktop # CLI 로
./run.sh cli groups
```

---

CLI 와 데스크탑 앱(GUI) 두 가지로 쓸 수 있습니다. 둘 다 같은 엔진(`engine.py`)을 공유합니다.

```
engine.py    # 핵심 로직 (OCR·분류·통합·캐시·휴지통)
cli.py       # 커맨드라인 (argparse)
app.py       # 데스크탑 앱 (NiceGUI native 창)
shotsort.py  # 하위호환 shim (== cli.py)
```

## 동작 방식 (하이브리드)

1. **로컬 OCR** — macOS Vision 프레임워크로 이미지에서 텍스트 추출 (무료·오프라인, 한글/영문)
2. **Claude 분류** — 추출된 텍스트(+선택적 썸네일)를 읽고 `project / kind / 요약 / 삭제가능` 태그 부여
3. **그룹 정규화** — 2차 패스로 비슷한 추정치들을 깔끔한 그룹으로 통합

분석 결과는 `~/.shotsort/cache.db` (SQLite)에 캐시 → 한 번 본 이미지는 재분석 안 함(파일 해시 기준).

## 수동 설치 (대안)

`run.sh` 대신 직접 venv 를 관리하고 싶다면:

```bash
cd shotsort
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python3 app.py          # 앱
.venv/bin/python3 cli.py stats    # CLI
```

> `pyobjc-framework-Vision` 설치가 안 되면 OCR 은 tesseract(`brew install tesseract tesseract-lang`)로 폴백하고, 그것도 없으면 OCR 없이 진행합니다(`--with-image` 권장).

## 데스크탑 앱 (GUI)

```bash
python app.py                      # 독립 앱 창(native)으로 실행
SHOTSORT_BROWSER=1 python app.py    # 브라우저 탭으로 실행
```

한 창에서 경로 지정 → 스캔(실시간 진행률) → 그룹별 썸네일 격자 확인 → 체크해서 **선택 항목 휴지통으로**
(그룹 헤더의 `이 그룹 휴지통으로` 로 그룹 통째 정리도 가능). 좌상단 `로컬 모드` 스위치로
무료 휴리스틱/Claude 분류를 전환합니다. 그룹은 기본 접힘(삭제후보·큰 그룹만 펼침), 크기순 정렬.

### 자동 업데이트

앱을 열면 백그라운드로 원격(git)과 비교해 새 버전이 있으면 상단 배너로 알립니다.
`업데이트` 를 누르면 `git pull` 후 자동 재시작합니다. (git 저장소로 clone 한 경우)

### 개발 모드

```bash
SHOTSORT_DEV=1 python app.py    # 파일 변경 시 자동 리로드(브라우저로 실행)
```

## 사용 (CLI)

```bash
# 1) 데스크탑 분석 (기본 경로 ~/Desktop)
python shotsort.py scan
python shotsort.py scan ~/Pictures/screenshots --with-image

# 2) 프로젝트별 그룹 확인
python shotsort.py groups
python shotsort.py groups --deletable      # 삭제 후보만

# 3) 정리 (휴지통으로 — 복구 가능)
python shotsort.py trash --group "영수증"
python shotsort.py trash --deletable        # 삭제 후보 전부

# 4) Finder 에서 그룹 위치 보기 / 통계
python shotsort.py open --group "act-server"
python shotsort.py stats
```

## API 키 없을 때 (로컬 폴백)

`ANTHROPIC_API_KEY` 가 없으면 자동으로 **로컬 휴리스틱 모드**로 동작합니다 (Claude 미사용):

- **OCR** 텍스트를 규칙으로 분류 (error/receipt/code/chat/diagram/doc/ui/photo)
- **토큰 겹침(Jaccard) 클러스터링**으로 비슷한 스크린샷을 같은 그룹으로 묶음
- 거의 빈 캡처는 `정리(삭제후보)` 그룹으로 모음

무료·오프라인이지만 정확도는 LLM 모드보다 낮습니다. 나중에 키를 설정하고 다시 `scan` 하면
자동으로 LLM 분류로 업그레이드됩니다. 키가 있어도 `--local` 로 강제할 수 있습니다.

```bash
python shotsort.py scan ~/Desktop            # 키 없으면 자동 로컬 모드
python shotsort.py scan ~/Desktop --local     # 키 있어도 로컬 강제
```

## 비용

기본 분류 모델은 `claude-opus-4-8`. 스크린샷 수십~수백 장을 돌릴 때는 훨씬 저렴한
`--model claude-haiku-4-5` 를 권장합니다. OCR 을 로컬에서 처리하므로 Claude 에는
텍스트만 전달되어(기본) 이미지 직접 전송 대비 토큰이 크게 절감됩니다.

## 안전장치

- 삭제는 전부 **macOS 휴지통**으로 이동(`Finder delete`) → 복구 가능, put-back 메타 유지
- `trash` 는 기본적으로 목록을 보여주고 **y/N 확인** (자동화는 `-y`)
- 분석은 읽기 전용. 파일을 옮기거나 바꾸지 않음

## 한계 / TODO

- 중복/유사 이미지 제거는 범위 밖 — czkawka/fclones 와 함께 쓰면 좋음
- 터미널 인라인 썸네일(iTerm2/kitty) 미구현 — 현재는 `open` 으로 Finder 표시
