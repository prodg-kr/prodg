# pronews.jp 자동 번역 시스템 v3 (최종)

## ✅ 적용된 3가지 개선사항

### 1. 매일 오전 7시 자동 실행 ⏰

**GitHub Actions 스케줄:**
```yaml
schedule:
  - cron: '0 22 * * *'  # UTC 22:00 = KST 07:00
```

**확인 방법:**
- GitHub → Actions → 좌측 메뉴에서 워크플로우 확인
- 다음 실행 예정: 매일 오전 7:00

**수동 실행:**
- Actions → "Run workflow" 버튼으로 언제든 실행 가능

---

### 2. 최신 기사부터 10건씩 번역 📰

**변경 전 (문제):**
```python
# 오래된 순으로 정렬
all_articles.sort(key=lambda x: x['date'])  # 오름차순
```

**변경 후 (개선):**
```python
# 최신순 정렬
all_articles.sort(key=lambda x: x['date'], reverse=True)  # 내림차순
```

**효과:**
- ✅ 가장 최신 기사 10개가 먼저 번역됨
- ✅ 사용자는 항상 최신 뉴스를 볼 수 있음
- ✅ 이전 게시물 삭제 후 재실행 시 최신 10개만 다시 올라감

**예시:**
```
2026-02-14 기사 (최신) ← 1순위
2026-02-13 기사         ← 2순위
2026-02-12 기사         ← 3순위
...
2026-02-05 기사         ← 10순위
```

---

### 3. "원문 게시시각", "출처" 텍스트 제거 🧹

**제거 전:**
```
블랙 매직 디자인은 DaVinci Resolve 20.3.2를 발표했다.

원문 게시시각: 2026-02-12 15:03 (JST)
출처: jp.pronews.com

동사 Web 페이지에서 무료로 다운로드 가능...
```

**제거 후:**
```
블랙 매직 디자인은 DaVinci Resolve 20.3.2를 발표했다.

동사 Web 페이지에서 무료로 다운로드 가능...

---
원문: [링크]
```

**구현 방법:**

1. **HTML 단계에서 제거:**
```python
# 스크래핑 시 해당 요소 삭제
for elem in content_div.find_all(string=re.compile(r'원문 게시시각:|출처:')):
    parent.decompose()
```

2. **번역 전 텍스트 제거:**
```python
# 정규식으로 제거
plain_text = re.sub(r'원문 게시시각:.*?\n', '', plain_text)
plain_text = re.sub(r'出典:.*?\n', '', plain_text)
```

**제거되는 패턴:**
- `원문 게시시각: YYYY-MM-DD HH:MM (JST)`
- `출처: jp.pronews.com`
- `原文掲載時刻: ...`
- `ソース: ...`

---

## 📊 전체 개선 효과

### Before (개선 전)

**실행:**
- 수동으로만 실행 가능
- 오래된 기사부터 처리

**본문:**
```
원문 게시시각: 2026-02-12 15:03 (JST)
출처: jp.pronews.com

[본문 내용]

* [ ](//facebook.com/...)
* [ ](//twitter.com/...)
FOLLOW US
```

**URL:**
```
/sennheiser-%ed%81%b4%eb%9d%bc%ec%9a%b0%eb%93%9c...
```

---

### After (개선 후)

**실행:**
- ✅ 매일 오전 7시 자동 실행
- ✅ 최신 기사 10개 처리

**본문:**
```
[깨끗한 본문만]

---
원문: [링크]
```

**URL:**
```
/davinci-resolve-20-3-2
```

---

## 🚀 적용 방법

### 1. GitHub 저장소 업데이트

**prodg 저장소:**
```
.github/
  └── workflows/
      └── auto-translate.yml  ← 새로 추가/교체

translate_and_post.py  ← 교체
requirements.txt       ← 확인
```

### 2. 파일 업로드

1. GitHub → prodg 저장소
2. 기존 `translate_and_post.py` 삭제
3. 새 파일들 업로드:
   - `translate_and_post.py` (v3)
   - `.github/workflows/auto-translate.yml`
   - `requirements.txt`

### 3. 기존 게시물 삭제 (선택)

WordPress 관리자에서:
1. 게시물 → 전체 선택
2. 휴지통으로 이동
3. GitHub Actions 캐시 삭제:
   - Actions → Caches → `posted-articles-` 삭제

### 4. 첫 실행

**수동 실행:**
- Actions → "Run workflow" 클릭
- 최신 10개 기사 번역 시작

**자동 실행:**
- 내일 오전 7시부터 매일 자동 실행

---

## ⏰ 실행 스케줄 변경

다른 시간에 실행하고 싶다면:

### 오전 6시:
```yaml
- cron: '0 21 * * *'  # UTC 21:00 = KST 06:00
```

### 오후 7시:
```yaml
- cron: '0 10 * * *'  # UTC 10:00 = KST 19:00
```

### 하루 2번 (오전 7시, 오후 7시):
```yaml
schedule:
  - cron: '0 22 * * *'  # 오전 7시
  - cron: '0 10 * * *'  # 오후 7시
```

---

## 🔍 문제 해결

### "오전 7시에 실행 안 됨"

**원인 1: 워크플로우 비활성화**
- Actions → 워크플로우 활성화 확인

**원인 2: .github/workflows 경로 오류**
- 정확한 경로: `.github/workflows/auto-translate.yml`
- 폴더 이름 확인 (workflows, 복수형)

**원인 3: Secrets 미설정**
- Settings → Secrets → WP_USER, WP_APP_PASSWORD 확인

### "여전히 오래된 기사가 올라옴"

**해결:**
1. 코드 재확인:
   ```python
   # 이 부분 확인
   all_articles.sort(key=lambda x: x['date'], reverse=True)
   ```
2. GitHub 캐시 삭제
3. 재실행

### "원문 게시시각이 여전히 보임"

**해결:**
1. 정규식 패턴 확인
2. 실제 원문 텍스트 확인 (일본어/한글)
3. 필요시 패턴 추가

---

## 📝 다음 개선 아이디어

- [ ] 카테고리 자동 분류
- [ ] 태그 자동 생성
- [ ] 이메일 알림
- [ ] Slack 알림
- [ ] 에러 발생 시 자동 재시도
