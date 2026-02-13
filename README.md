# pronews.jp 자동 번역 및 워드프레스 게시 시스템

jp.pronews.com의 최신 뉴스를 자동으로 한국어로 번역하여 prodg.kr 워드프레스에 게시하는 시스템입니다.

## 🎯 기능

- ✅ **매일 오전 7시 자동 실행** (한국 시간)
- ✅ **pronews.jp RSS 피드 모니터링** (24시간 이내 기사)
- ✅ **Google Translate 한국어 번역** (긴 텍스트 자동 분할)
- ✅ **전체 본문 스크래핑** (RSS 요약이 아닌 실제 기사 전체)
- ✅ **이미지 자동 처리** (다운로드 + 업로드 + 본문 삽입)
- ✅ **중복 게시 방지**
- ✅ **완전 무료** (GitHub Actions 무료 티어 사용)

## 📋 시스템 구성

- **소스**: https://jp.pronews.com/feed
- **목적지**: https://prodg.kr (WordPress)
- **실행 환경**: GitHub Actions
- **번역**: Google Translate API (무료 50만자/월)

## 🚀 설치 방법

### 1️⃣ prodg.kr 워드프레스 설정

#### Application Password 생성

1. https://prodg.kr/wp-admin/ 로그인
2. 좌측 **사용자** → **프로필**
3. 아래로 스크롤 → **애플리케이션 비밀번호** 섹션
4. 이름: `GitHub Actions` 입력
5. **새 애플리케이션 비밀번호 추가** 클릭
6. 📋 생성된 비밀번호 복사 (공백 포함 전체)

### 2️⃣ GitHub 저장소 설정

이 저장소: https://github.com/prodg-kr/prodg

#### GitHub Secrets 추가

1. 저장소 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret** 클릭

**첫 번째 Secret:**
- Name: `WP_USER`
- Value: prodg.kr 워드프레스 관리자 아이디

**두 번째 Secret:**
- Name: `WP_APP_PASSWORD`
- Value: 위에서 생성한 Application Password (공백 포함!)

### 3️⃣ 파일 업로드

이 폴더의 모든 파일을 GitHub 저장소에 업로드:

```
prodg/
├── .github/
│   └── workflows/
│       └── auto-translate.yml
├── translate_and_post.py
├── requirements.txt
├── .gitignore
└── README.md
```

### 4️⃣ Actions 활성화

1. 저장소 → **Actions** 탭
2. 워크플로우 활성화 (필요시)
3. **I understand my workflows** 클릭

## ✅ 테스트 실행

수동으로 즉시 테스트:

1. **Actions** 탭 클릭
2. 좌측 "pronews.jp 자동 번역 및 게시" 클릭
3. **Run workflow** 버튼 클릭
4. **Run workflow** 다시 클릭
5. 5-10분 대기
6. https://prodg.kr 에서 확인! 🎉

## 📅 자동 실행 스케줄

- **기본**: 매일 오전 7시 (한국 시간)
- **수정**: `.github/workflows/auto-translate.yml`의 cron 값 변경

```yaml
schedule:
  - cron: '0 22 * * *'  # 매일 오전 7시 (UTC 22:00 = KST 07:00)
  # cron: '0 21,9 * * *'  # 오전 6시, 오후 6시
  # cron: '0 */3 * * *'  # 3시간마다
```

## 🔧 커스터마이징

### 게시할 기사 개수 변경

`translate_and_post.py` 파일에서:

```python
for article in articles[:10]:  # 하루 최대 10개
```

### 기간 변경 (24시간 → 48시간)

```python
limit_date = datetime.now() - timedelta(days=2)  # 2일로 변경
```

### 강제 업데이트 모드

이미 올린 기사도 다시 올리고 싶다면:

```python
FORCE_UPDATE = True  # False → True로 변경
```

## 📊 모니터링

### 실행 로그 확인

1. GitHub → **Actions** 탭
2. 최근 워크플로우 실행 클릭
3. **translate-and-post** 클릭
4. 각 단계 로그 확인

### 게시된 기사 확인

- 워드프레스 관리: https://prodg.kr/wp-admin/edit.php
- 블로그 메인: https://prodg.kr

## ⚠️ 문제 해결

### "새로운 기사가 없습니다"

- 정상입니다! 24시간 내 새 기사가 없으면 이 메시지가 표시됩니다.
- pronews.jp에 실제로 새 기사가 올라왔는지 확인

### "인증 실패" 오류

- GitHub Secrets에 `WP_USER`, `WP_APP_PASSWORD` 정확히 입력됐는지 확인
- Application Password를 공백 포함해서 복사했는지 확인

### "번역 실패" 오류

- Google Translate 무료 할당량 확인 (월 50만자)
- 잠시 후 자동으로 재시도됨

### "이미지 업로드 실패"

- 워드프레스 미디어 업로드 권한 확인
- 이미지 URL이 접근 가능한지 확인

## 💡 추가 기능 제안

- [ ] 여러 RSS 소스 지원 (다른 일본 미디어 추가)
- [ ] 카테고리 자동 분류
- [ ] 태그 자동 생성
- [ ] 이메일 알림
- [ ] Slack 알림
- [ ] DeepL API로 번역 품질 향상

## 📝 기술 스택

- **Python 3.11**
- **Google Translate** (googletrans)
- **BeautifulSoup4** (HTML 파싱)
- **WordPress REST API**
- **GitHub Actions** (CI/CD)

## 🙋‍♂️ 지원

문제가 있으면 GitHub Issues에 등록해주세요.

## 📄 라이선스

MIT License
