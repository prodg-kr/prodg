# pronews.jp 자동 번역 및 워드프레스 게시 시스템

jp.pronews.com 기사를 한국어로 번역해 prodg.kr에 자동 게시합니다.

## 핵심 개선 사항

1. 원문 게시일/시간 유지
- 게시 API에 `date`, `date_gmt`를 함께 전달해 원문 시각 그대로 게시합니다.
- 워드프레스 기본 정렬(최신순)에서 최신 원문이 상단에 유지됩니다.

2. 원문 링크 도메인 정규화
- `ko.pronews.com`, `pronews.jp` 등 잘못된 도메인을 `jp.pronews.com`으로 자동 교정합니다.

3. 과거 기사까지 순차 번역/게시
- RSS(24시간 제한) 대신 원문 WordPress API를 페이지 단위로 조회합니다.
- 이미 게시한 링크를 제외하고 최신순으로 `DAILY_POST_LIMIT`개씩 처리합니다.
- 이 방식으로 매일 실행하면 과거 기사까지 점진적으로 모두 번역할 수 있습니다.

4. 일일 게시 수(기본 10) 검토
- 기본값 10건은 안정성과 처리 시간 측면에서 안전한 값입니다.
- 서버/번역 API 여유가 있으면 15~20건까지 확장 가능합니다.

5. 디자인 개선
- 본문 블록 레이아웃을 정리해 뉴스형 가독성을 높였습니다.
- `pronews-theme.css`를 워드프레스 추가 CSS에 적용하면 jp.pronews.com에 가까운 톤으로 맞출 수 있습니다.

## 파일 구성

```text
prodg/
├── .github/
│   └── workflows/
│       └── auto-translate.yml
├── translate_and_post.py
├── requirements.txt
├── pronews-theme.css
├── QUICKSTART.md
└── README.md
```

## 환경 변수

필수:
- `WP_USER`
- `WP_APP_PASSWORD`

옵션:
- `DAILY_POST_LIMIT` (기본 `10`)
- `SOURCE_SCAN_MAX_PAGES` (기본 `60`)
- `REQUEST_TIMEOUT` (기본 `20`)

## GitHub Actions 설정

워크플로우: `.github/workflows/auto-translate.yml`

기본 실행 시간:
- 매일 오전 7시(KST)

```yaml
schedule:
  - cron: '0 22 * * *'
```

## 디자인 적용 방법

1. 워드프레스 관리자 > `외모` > `사용자 정의하기` > `추가 CSS`
2. `pronews-theme.css` 내용을 붙여넣기
3. 저장 후 기사/목록 페이지 확인
