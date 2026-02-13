# QUICKSTART

## 1) 워드프레스 준비

- `https://prodg.kr/wp-admin/` 로그인
- `사용자 > 프로필 > 애플리케이션 비밀번호` 생성
- 이름 예: `GitHub Actions`
- 생성된 비밀번호 복사

## 2) GitHub Secrets 추가

저장소 `Settings > Secrets and variables > Actions`:

- `WP_USER`: 워드프레스 계정
- `WP_APP_PASSWORD`: 앱 비밀번호

## 3) 워크플로우 설정 확인

`.github/workflows/auto-translate.yml` 기본값:

- `DAILY_POST_LIMIT: "10"`
- `SOURCE_SCAN_MAX_PAGES: "80"`
- 매일 오전 7시(KST) 실행

## 4) 수동 테스트

- GitHub `Actions` 탭
- `pronews.jp 자동 번역 및 게시` 선택
- `Run workflow`

## 5) 디자인 적용(선택)

- `pronews-theme.css`를 워드프레스 `추가 CSS`에 붙여넣기
