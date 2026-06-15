# 코드 서명 & 공증 계획 (Apple Developer)

목표: 무서명 `.app` 의 Gatekeeper 경고("악성 코드가 없음을 확인할 수 없습니다")를 없애고,
받는 사람이 **그냥 더블클릭**으로 열 수 있게 한다. → Developer ID 서명 + Apple 공증(notarization).

> 현재 상태: 무서명 빌드(`build_app.sh`). 받는 사람이 첫 실행 시 시스템 설정에서 "그래도 열기" 필요.
> 이 문서는 **계획**이며 아직 구현되지 않았다.

## 0. 사전 준비 (1회)

| 항목 | 내용 |
|---|---|
| Apple Developer Program | 가입 **$99/년** (https://developer.apple.com/programs/) |
| 인증서 | **Developer ID Application** 인증서 발급 (배포용, App Store 외 배포) |
| App-specific password | notarytool 인증용. appleid.apple.com → 로그인 및 보안 → 앱 암호 |
| Team ID | developer.apple.com 멤버십 페이지에서 확인 (10자리) |

인증서 발급/설치:
1. Xcode → Settings → Accounts → Apple ID 추가 → Manage Certificates → `+` → **Developer ID Application**
2. 또는 CSR 만들어 developer.apple.com 에서 발급 후 더블클릭으로 키체인 설치
3. 확인: `security find-identity -v -p codesigning` → `Developer ID Application: 이름 (TEAMID)` 보여야 함

## 1. 빌드 시 서명 (PyInstaller 산출물)

PyInstaller 가 만든 `.app` 안에는 다수의 dylib/so 가 있어 **deep 서명** + **hardened runtime** 필요.

```bash
APP="dist/shotsort.app"
IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"

codesign --deep --force --options runtime --timestamp \
  --entitlements build/entitlements.plist \
  --sign "$IDENTITY" "$APP"

codesign --verify --deep --strict --verbose=2 "$APP"   # 검증
```

### entitlements.plist (필요 최소)
파이썬/pyobjc 런타임은 JIT 가 아니므로 보통 아래면 충분. 문제 시 항목 추가.
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict></plist>
```
> `disable-library-validation` 은 PyInstaller 가 번들한 외부 dylib 로드를 위해 보통 필요.

## 2. 공증 (notarization)

서명만으로는 부족하고 Apple 서버 공증을 받아야 Gatekeeper 통과.

```bash
# 자격증명 1회 저장 (키체인 프로파일)
xcrun notarytool store-credentials shotsort-notary \
  --apple-id "<APPLE_ID>" --team-id "<TEAMID>" --password "<APP_SPECIFIC_PW>"

# .dmg 를 공증 (또는 .app 을 zip 으로 묶어 제출)
xcrun notarytool submit dist/shotsort.dmg --keychain-profile shotsort-notary --wait

# 통과하면 티켓을 산출물에 스테이플 (오프라인 검증용)
xcrun stapler staple dist/shotsort.dmg
xcrun stapler validate dist/shotsort.dmg
```

순서 주의: **서명 → .dmg 패키징 → 공증 → 스테이플**. (앱 서명 후 dmg 를 만들고, dmg 를 공증·스테이플)

## 3. build_app.sh 에 통합 (계획)

환경변수로 자격증명을 받아 서명/공증을 **옵트인**으로 추가:
```bash
# 예) SIGN_IDENTITY="Developer ID Application: ... (TEAMID)" NOTARY_PROFILE=shotsort-notary ./build_app.sh
#  - SIGN_IDENTITY 있으면 codesign 단계 실행
#  - NOTARY_PROFILE 있으면 dmg 공증 + staple 실행
#  - 둘 다 없으면 지금처럼 무서명 빌드
```
- `entitlements.plist` 는 `build/` 에 생성(또는 repo 에 `packaging/entitlements.plist` 로 보관)
- 비밀(앱 암호)은 **절대 커밋 금지** → 키체인 프로파일/환경변수로만

## 4. 검증 체크리스트

- [ ] `codesign --verify --deep --strict dist/shotsort.app` 통과
- [ ] `spctl -a -vvv -t install dist/shotsort.app` → `accepted, source=Notarized Developer ID`
- [ ] `xcrun stapler validate dist/shotsort.dmg` 통과
- [ ] 다른 Mac(혹은 새 계정)에서 다운로드 → **더블클릭 무경고 실행**

## 5. 자동화(선택) — CI 서명

GitHub Actions(macOS 러너)에서 릴리스 시 자동 서명/공증:
- 인증서(.p12)·앱 암호·Team ID 를 **Actions Secrets** 로 주입
- `security create-keychain` 으로 임시 키체인에 .p12 import 후 서명
- tag push → build → sign → notarize → staple → release 업로드

## 비용/요약

| | 무서명(현재) | 서명+공증(목표) |
|---|---|---|
| 비용 | 0 | $99/년 |
| 사용자 경험 | 첫 실행 "그래도 열기" 1회 | 더블클릭 무경고 |
| 작업 | 완료 | 인증서 발급 + build_app.sh 통합 + (선택)CI |
