# shotsort

데스크탑에 쌓인 **스크린샷을 프로젝트별로 자동 분류**하고, **지워도 되는 것들을 묶어 한 번에 정리**하는 도구. 데스크탑 앱(GUI)과 CLI 둘 다 제공합니다.

기존 도구(czkawka·fclones 등)는 "중복/유사 이미지"는 잘 찾지만 "스크린샷 **내용**을 읽고 프로젝트별로 묶기"는 못 해서 직접 만들었습니다.

- 🖼 **OCR로 내용을 읽어** 비슷한 것끼리 그룹화 (macOS Vision, 무료·오프라인)
- 🤖 API 키가 있으면 **Claude가 더 정확히** 프로젝트별로 분류 (없으면 무료 로컬 모드)
- 🗑 지울 것들을 **체크해서 한 번에 휴지통으로** (macOS 휴지통 → 복구 가능)
- 🔄 새 버전이 나오면 **앱이 알리고 원클릭 업데이트**

---

## 🚀 설치

### 방법 1 — 앱 다운로드 (비개발자, 권장)

1. **[최신 릴리스에서 `shotsort.dmg` 다운로드](https://github.com/sj48695-labs/shotsort/releases/latest)**
2. `.dmg` 를 열고 **shotsort 를 Applications 폴더로 드래그**
3. **첫 실행만** — 서명 안 된 빌드라 macOS가 "악성 코드 확인 불가" 경고를 띄웁니다:
   - 경고창은 **완료**로 닫고 → **시스템 설정 → 개인정보 보호 및 보안** → 아래 *"'shotsort'…차단됨"* 옆 **"그래도 열기"** 클릭 → 다시 **"열기"**
   - 또는 터미널 한 줄: `xattr -dr com.apple.quarantine /Applications/shotsort.app`
   - 이후엔 그냥 더블클릭으로 실행됩니다.

API 키 없이 바로 동작합니다(무료 로컬 모드). 터미널·파이썬 설치 불필요.

> 완전 무경고 실행(일반 앱처럼)은 Apple Developer 서명·공증이 필요합니다 — [docs/SIGNING.md](docs/SIGNING.md) 참고.

### 방법 2 — 소스로 실행 (개발자)

**필요한 것**: macOS, `python3`, `git` (없으면 `brew install python git`)

```bash
git clone https://github.com/sj48695-labs/shotsort.git
cd shotsort && ./run.sh            # 앱 (최초 1회 의존성 자동 설치)
./run.sh --browser                # 앱을 브라우저로
./run.sh cli scan ~/Desktop       # CLI
./run.sh cli groups
```

> 💡 `export ANTHROPIC_API_KEY=sk-...` 후 실행하면 Claude가 더 정확히 묶어줍니다.
> 소스(git) 설치는 git 기반 자동 업데이트도 동작합니다.

### 처음 사용

데스크탑 앱 창이 뜨면:
1. **스캔** 버튼 (기본 경로 `~/Desktop`) → 스크린샷이 그룹별로 묶입니다
2. 지울 것들을 체크 → **선택 항목 휴지통으로** (복구 가능)

---

## 사용법

### 데스크탑 앱

`./run.sh` (또는 `python app.py`) 로 독립 창이 뜹니다.

- **경로 지정 → 스캔**: 실시간 진행률 표시, 결과는 그룹별 썸네일 격자로
- **그룹**: 기본 접힘(삭제후보·큰 그룹만 펼침), 크기순 정렬. 헤더의 `이 그룹 휴지통으로` 로 그룹 통째 정리
- **선택 삭제**: 카드 체크 → `선택 항목 휴지통으로` (확인 후 복구 가능한 휴지통으로)
- **모드 전환**: 좌상단 `로컬 모드` 스위치로 무료 휴리스틱 ↔ Claude 분류

### CLI

```bash
./run.sh cli scan                      # ~/Desktop 분석 (캐시된 건 스킵)
./run.sh cli scan ~/Pictures --with-image
./run.sh cli groups                    # 프로젝트별 그룹
./run.sh cli groups --deletable        # 삭제 후보만
./run.sh cli trash --group "영수증"     # 그룹 통째 휴지통(확인 후)
./run.sh cli trash --deletable          # 삭제 후보 전부 휴지통
./run.sh cli open --group "act-server"  # Finder 에서 그룹 위치 보기
./run.sh cli stats                      # 통계
```

(`shotsort.py` 는 `cli.py` 의 하위호환 shim 이라 `python shotsort.py scan ...` 도 동일하게 동작합니다.)

---

## 동작 방식

1. **로컬 OCR** — macOS Vision 으로 이미지에서 텍스트 추출 (무료·오프라인, 한글/영문). 안 되면 tesseract → 그것도 없으면 건너뜀.
2. **분류** — 추출 텍스트(+선택적 썸네일)로 `project / kind / 요약 / 삭제가능` 태그 부여
   - **Claude 모드**(키 있을 때): LLM 이 프로젝트/주제별로 정확히 분류·통합
   - **로컬 모드**(키 없을 때): 규칙 기반 종류 분류 + 토큰 겹침 클러스터링
3. **그룹 정규화** — 비슷한 것끼리 묶고 그룹명을 정리

분석 결과는 `~/.shotsort/cache.db` (SQLite)에 캐시 → 한 번 본 이미지는 재분석하지 않습니다(파일 해시 기준). `--force` 로 전체 재분석.

### 로컬 모드의 그룹핑 (API 키 없을 때)

OCR 휴리스틱만으로는 1장당 1그룹이 되기 쉬워, 그룹이 폭증하지 않도록 압축합니다:

- **신뢰 클러스터만** 프로젝트 그룹으로 유지 — 3장 이상 묶이고 이름이 깨끗할 때
- 나머지(작은·깨진 클러스터, 단독 캡처)는 **종류 버킷**(문서/화면/에러/코드/메시지/영수증…)으로 흡수
- 거의 빈 캡처는 `정리(삭제후보)` 그룹으로 모음

> 무료·오프라인이지만 정확도는 Claude 모드보다 낮습니다. 키를 설정하고 다시 스캔하면 자동으로 Claude 분류로 업그레이드됩니다. 키가 있어도 `--local` 로 로컬 강제 가능.

---

## 자동 업데이트

**소스(git) 설치**: 앱을 열면 백그라운드로 원격과 비교해 새 버전이 있으면 **상단 배너**로 알리고, `업데이트` 버튼으로 `git pull` + 자동 재시작합니다.

**`.app` 설치**: 앱이 GitHub 릴리스의 최신 버전과 비교해 새 버전이 있으면 **상단 배너로 알리고**, `다운로드` 버튼으로 릴리스 페이지를 엽니다. 거기서 새 `.dmg` 를 받아 교체하면 됩니다. (앱 자체 교체는 무서명 제약상 다운로드 안내 방식)

---

## 비용 (Claude 모드)

기본 모델은 `claude-opus-4-8`. 수십~수백 장을 돌릴 때는 훨씬 저렴한 `--model claude-haiku-4-5` 권장. OCR 을 로컬에서 처리해 Claude 에는 텍스트만 전달되므로(기본) 이미지 직접 전송 대비 토큰이 크게 절감됩니다.

---

## 안전장치

- 삭제는 전부 **macOS 휴지통**으로 이동(`Finder delete`) → 복구 가능, put-back 메타 유지
- 분석은 **읽기 전용** — 파일을 옮기거나 바꾸지 않음
- CLI `trash` 는 목록을 보여주고 **y/N 확인** (자동화는 `-y`), 앱도 확인 다이얼로그

---

## 프로젝트 구조

```
engine.py    # 핵심 로직 (OCR·분류·통합·캐시·휴지통·업데이트). print 없음
cli.py       # 커맨드라인 (argparse)
app.py       # 데스크탑 앱 (NiceGUI native 창)
shotsort.py  # 하위호환 shim (== cli.py)
run.sh       # 부트스트랩 실행기 (venv·의존성 자동 설치)
```

### 개발 모드

```bash
SHOTSORT_DEV=1 python app.py    # 파일 변경 시 자동 리로드 (브라우저로 실행)
```

---

## 한계 / TODO

- 중복/유사 이미지 제거는 범위 밖 — czkawka/fclones 와 함께 쓰면 좋음
- 로컬 모드는 OCR 품질에 좌우됨 — 정확한 프로젝트 그룹핑은 Claude 모드 권장
- (백로그) 종류 필터·검색, 잘못 묶인 항목 수동 재분류, 대량 가상 스크롤
