# 🚀 빠른 시작 가이드 (5분)

## ✅ 체크리스트

### 1단계: prodg.kr 워드프레스 준비

- [ ] https://prodg.kr/wp-admin/ 접속
- [ ] **사용자** → **프로필** → **애플리케이션 비밀번호**
- [ ] 이름: `GitHub Actions` → **추가** 클릭
- [ ] 📋 생성된 비밀번호 복사 (예: `xxxx xxxx xxxx xxxx`)

### 2단계: GitHub Secrets 설정

- [ ] https://github.com/prodg-kr/prodg 접속
- [ ] **Settings** → **Secrets and variables** → **Actions**
- [ ] **New repository secret** 클릭

**Secret 1:**
```
Name: WP_USER
Value: [워드프레스 관리자 아이디]
```

**Secret 2:**
```
Name: WP_APP_PASSWORD
Value: [위에서 복사한 비밀번호 - 공백 포함!]
```

### 3단계: 파일 업로드

- [ ] 이 폴더의 모든 파일을 GitHub에 업로드
- [ ] 특히 `.github/workflows/auto-translate.yml` 경로 확인

### 4단계: 테스트 실행

- [ ] GitHub → **Actions** 탭
- [ ] "pronews.jp 자동 번역 및 게시" 선택
- [ ] **Run workflow** 클릭
- [ ] 5분 대기 ⏱️
- [ ] https://prodg.kr 확인! 🎉

---

## 🎯 완료!

이제 매일 오전 7시마다 자동으로 pronews.jp 기사가:
1. 🔍 검색되고
2. 🔄 번역되고
3. 🖼️ 이미지와 함께
4. 📝 prodg.kr에 게시됩니다!

## ⚙️ 다음 단계

### 실행 시간 변경하고 싶다면?

`.github/workflows/auto-translate.yml` 파일에서:

```yaml
# 현재: 매일 오전 7시
- cron: '0 22 * * *'

# 변경 예시:
- cron: '0 21 * * *'  # 오전 6시
- cron: '0 23 * * *'  # 오전 8시
- cron: '0 0 * * *'   # 오전 9시
```

### 더 많은 기사를 게시하고 싶다면?

`translate_and_post.py` 파일에서:

```python
for article in articles[:10]:  # 10 → 20으로 변경
```

### 48시간 내 기사까지 가져오고 싶다면?

```python
limit_date = datetime.now() - timedelta(days=2)  # 1 → 2로 변경
```

---

## 🆘 도움이 필요하면?

1. GitHub Actions 로그 확인
2. README.md의 "문제 해결" 섹션 참고
3. GitHub Issues에 질문 등록
