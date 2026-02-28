#!/usr/bin/env python3
"""
pronews.jp 자동 번역 시스템 v7.2 (노이즈 제거 및 HTML 유지 완벽 패치)
파이프라인: 일본어 원문 → Gemini 1회 JSON 통합 번역 → WordPress Draft

v6 → v7.2 변경사항:
- fetch_full_content 반환값 str(content_div) 변경 (본문 이미지 유지)
- 번역 프롬프트 HTML 태그 유지 지시 및 글자수(15000) 한도 확장
- 사이드바, SNS 공유버튼, 관련기사 등 불필요한 UI(Noise) 완벽 제거
- 모델: gemini-2.5-flash-lite (RPM 15, RPD 1,000)
- 호출 구조: 기사당 1회 JSON 통합 (TPM 절감, 처리량 극대화)
- 재번역: 일본어 잔존 시 최대 1회 추가 (총 2회 상한)
- Slug: 정규식 대체
- 429 처리: 지수 백오프 후 즉시 런 종료, 미기록 → 다음 런 자동 이월
- 일본어 잔존 스킵 제거: 경고 후 무조건 게시
- 실행 모드 분리: schedule(최신 우선), workflow_dispatch(아카이브 우선)
- 아카이브 크롤링: /news/page/N/ 페이지네이션
- API 호출 간격: 7초 / 기사 간 대기: 10초
- POST_STATUS 기본값: draft
"""

import os
import sys
import requests
import feedparser
from datetime import datetime
from pathlib import Path
import json
import time
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import hashlib
import re

# ==========================================
# 설정
# ==========================================
WORDPRESS_URL          = "https://prodg.kr"
WORDPRESS_USER         = os.environ.get("WP_USER")
WORDPRESS_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
GEMINI_API_KEY         = os.environ.get("GEMINI_API_KEY")
PRONEWS_RSS            = "https://jp.pronews.com/feed"
PRONEWS_ARCHIVE_BASE   = "https://jp.pronews.com/news/page"
POSTED_ARTICLES_FILE   = "posted_articles.json"
FORCE_UPDATE           = os.environ.get("FORCE_UPDATE", "false").lower() == "true"
DAILY_LIMIT            = 10
ARCHIVE_MAX_PAGES      = 20

# 실행 모드 감지
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
IS_SCHEDULED      = GITHUB_EVENT_NAME == "schedule"

# 게시 상태
POST_STATUS  = os.environ.get("POST_STATUS", "draft")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


# ==========================================
# Gemini 통합 엔진
# ==========================================
class GeminiEngine:
    def __init__(self):
        self.api_key         = GEMINI_API_KEY
        if not self.api_key:
            print("❌ GEMINI_API_KEY 미설정")
            sys.exit(1)
        self.last_call_time  = 0.0
        self.rate_limit_hit  = False

    def _call_api(self, prompt: str, max_tokens: int = 8192) -> str:
        if self.rate_limit_hit:
            return ""

        # 호출 간격 보장 (7초)
        elapsed = time.time() - self.last_call_time
        if elapsed < 7:
            time.sleep(7 - elapsed)
        self.last_call_time = time.time()

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.4
            }
        }

        backoff = [15, 30, 60]
        for attempt in range(3):
            try:
                res = requests.post(url, json=payload, timeout=120)

                if res.status_code == 429:
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    print(f"⚠️ 429 Rate Limit (시도 {attempt+1}/3) → {wait}초 대기...")
                    time.sleep(wait)
                    if attempt == 2:
                        print("❌ 429 반복 → 런 종료 (미게시 기사는 다음 런 자동 이월)")
                        self.rate_limit_hit = True
                        return ""
                    continue

                res.raise_for_status()
                candidates = res.json().get("candidates", [])
                if candidates:
                    parts = candidates[0]["content"]["parts"]
                    for part in parts:
                        if not part.get("thought", False) and "text" in part:
                            return part["text"].strip()
                    for part in reversed(parts):
                        if "text" in part:
                            return part["text"].strip()

                print(f"⚠️ Gemini 응답 없음 (시도 {attempt+1}/3)")

            except Exception as e:
                print(f"⚠️ Gemini API 오류 (시도 {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(backoff[attempt])

        return ""

    def translate_article(self, title_ja: str, body_text: str) -> dict:
        prompt = f"""당신은 영상/카메라 전문 미디어의 한국어 에디터입니다.
아래 일본어 기사(HTML)를 한국어로 번역·편집하여 JSON으로만 출력하세요.

=== 일본어 원문 ===
제목: {title_ja}

본문:
{body_text[:15000]}

=== 번역 규칙 ===
1. 일본어(히라가나·가타카나·한자)를 완전히 한국어로 번역
2. 문체: 반드시 '~합니다', '~했습니다', '~입니다' 합쇼체 통일
3. 브랜드명·모델명 원문 유지: Sony, Canon, Nikon, DJI, Blackmagic, Sigma 등
4. 해상도: 4K, 8K, Full HD / 프레임레이트: fps, 24p, 60p
5. ★중요★: 본문에 포함된 <img>, <figure>, <iframe> 등의 HTML 미디어 태그와 속성(src, alt 등)은 절대 삭제하거나 수정하지 말고 제자리에 그대로 유지하세요.
6. 기계 번역 느낌 없이 사람이 쓴 듯 자연스럽게 (Google SEO·AdSense 품질 기준)

=== 출력 JSON 규칙 ===
- title: SEO 최적화 제목 (브랜드명·모델명 필수 포함, 최대 50자)
- content: 번역 본문 (원본 HTML 구조 및 이미지 태그 완벽 유지)
- excerpt: 구글 스니펫용 요약 (80~100자, 합쇼체)
- tldr: 핵심 요약 3~4항목 (<ul><li> HTML, 합쇼체)
- 마크다운 백틱 없이 JSON만 출력

{{
  "title": "SEO 제목",
  "content": "<p>본문</p> <figure><img src='...'></figure>",
  "excerpt": "요약문",
  "tldr": "<ul><li>요약1</li><li>요약2</li><li>요약3</li></ul>"
}}"""

        result = self._call_api(prompt, max_tokens=8192)
        if not result:
            return {}

        try:
            clean = re.sub(r'```(?:json)?', '', result).strip().rstrip('`').strip()
            match = re.search(r'(\{.*\})', clean, re.DOTALL)
            if match:
                return json.loads(match.group(1))
        except Exception as e:
            print(f"⚠️ JSON 파싱 실패: {e} | 원문: {result[:200]}")

        return {}

    def retranslate_content(self, content_ko: str) -> str:
        prompt = f"""아래 한국어 본문(HTML 포함)에 일본어가 섞여 있습니다.
일본어 부분을 자연스러운 한국어 합쇼체로 번역하고 전체 본문을 반환하세요.
★중요★ <img>, <figure> 등 모든 HTML 태그와 속성은 절대 건드리지 말고 그대로 유지할 것. 
본문만 출력:

{content_ko[:15000]}"""
        result = self._call_api(prompt, max_tokens=8192)
        return result if result else content_ko

    def _has_japanese(self, text: str) -> bool:
        plain = BeautifulSoup(text, 'lxml').get_text()
        return len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', plain)) > 5


# ==========================================
# 메인 번역 시스템
# ==========================================
class NewsTranslator:
    def __init__(self):
        self.gemini          = GeminiEngine()
        self.wordpress_api   = f"{WORDPRESS_URL}/wp-json/wp/v2"
        self.posted_articles = self.load_posted_articles()

    def load_posted_articles(self) -> list:
        if Path(POSTED_ARTICLES_FILE).exists():
            with open(POSTED_ARTICLES_FILE, 'r') as f:
                try:
                    return json.load(f)
                except:
                    return []
        return []

    def save_posted_articles(self):
        with open(POSTED_ARTICLES_FILE, 'w') as f:
            json.dump(self.posted_articles, f, indent=2)

    def fetch_rss_articles(self) -> list:
        print(f"📡 RSS 피드 확인: {PRONEWS_RSS}")
        feed = feedparser.parse(PRONEWS_RSS)
        articles = []
        for entry in feed.entries:
            if not FORCE_UPDATE and entry.link in self.posted_articles:
                continue
            try:
                article_date = datetime(*entry.published_parsed[:6])
            except:
                article_date = datetime.now()
            articles.append({
                'title': entry.title,
                'link': entry.link,
                'date': article_date,
                'source': 'rss'
            })
        print(f"   RSS 미게시: {len(articles)}건")
        return articles

    def fetch_archive_articles(self, need: int, oldest_first: bool = False) -> list:
        print(f"📚 아카이브 크롤링 (필요: {need}건, 오래된순: {oldest_first})...")
        collected = []
        seen_links = set()
        page = 1 if not oldest_first else ARCHIVE_MAX_PAGES

        while len(collected) < need * 3 and 1 <= page <= ARCHIVE_MAX_PAGES:
            url = f"{PRONEWS_ARCHIVE_BASE}/{page}/"
            try:
                res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                if res.status_code == 404:
                    print(f"   페이지 {page} 없음 → 종료")
                    break
                res.raise_for_status()
                soup = BeautifulSoup(res.text, 'lxml')
                found = []

                for article in soup.find_all('article'):
                    a_tag = article.find('a', href=True)
                    if not a_tag:
                        continue
                    link = a_tag['href']
                    if not link.startswith('http'):
                        link = urljoin("[https://jp.pronews.com](https://jp.pronews.com)", link)
                    if '/news/' not in link or link in seen_links:
                        continue

                    title_tag = article.find(['h2', 'h3', 'h1'])
                    title = title_tag.get_text(strip=True) if title_tag else a_tag.get_text(strip=True)
                    if not title:
                        continue

                    date_tag = article.find('time')
                    article_date = datetime.now()
                    if date_tag:
                        try:
                            article_date = datetime.fromisoformat(
                                date_tag.get('datetime', date_tag.get_text(strip=True))[:19]
                            )
                        except:
                            pass

                    found.append({'title': title, 'link': link, 'date': article_date, 'source': 'archive'})

                if not found:
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if not href.startswith('http'):
                            href = urljoin("[https://jp.pronews.com](https://jp.pronews.com)", href)
                        if re.search(r'/news/\d{10,}', href) and href not in seen_links:
                            title = a.get_text(strip=True)
                            if title and len(title) > 5:
                                found.append({'title': title, 'link': href,
                                              'date': datetime.now(), 'source': 'archive'})

                for art in found:
                    if art['link'] not in seen_links:
                        seen_links.add(art['link'])
                        if FORCE_UPDATE or art['link'] not in self.posted_articles:
                            collected.append(art)

                print(f"   페이지 {page}: {len(found)}건 발견, 누적 미게시: {len(collected)}건")
                page = page + 1 if not oldest_first else page - 1
                time.sleep(1)

            except Exception as e:
                print(f"⚠️ 아카이브 페이지 {page} 오류: {e}")
                page = page + 1 if not oldest_first else page - 1

        collected.sort(key=lambda x: x['date'], reverse=not oldest_first)
        result = collected[:need]
        print(f"   아카이브 수집 완료: {len(result)}건")
        return result

    def get_articles_to_process(self) -> list:
        if IS_SCHEDULED:
            print("🕐 자동 실행: 최신 우선 + 아카이브 보충")
            rss = self.fetch_rss_articles()
            rss.sort(key=lambda x: x['date'], reverse=True)
            target = rss[:DAILY_LIMIT]
            need = DAILY_LIMIT - len(target)
            if need > 0:
                print(f"   RSS {len(target)}건 → 아카이브에서 {need}건 보충")
                rss_links = {a['link'] for a in target}
                archive = self.fetch_archive_articles(need * 2, oldest_first=False)
                archive = [a for a in archive if a['link'] not in rss_links]
                target += archive[:need]
            target = target[:DAILY_LIMIT]
        else:
            print("📖 수동 실행: 아카이브 오래된 순 10건 (블로그 채우기)")
            target = self.fetch_archive_articles(DAILY_LIMIT, oldest_first=True)

        print(f"✅ 처리 대상: {len(target)}건")
        return target

    def fetch_full_content(self, url: str) -> str:
        try:
            print(f"📄 스크래핑: {url}")
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, timeout=15)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'lxml')

            # 1. 본문 영역을 더 정밀하게 찾기
            content_div = (
                soup.find('div', class_='articleBody-inner') or
                soup.find('div', class_='articleBody') or
                soup.find('div', class_='entry-content') or
                soup.find('div', class_='post-content') or
                soup.find('div', class_='article-content') or
                soup.find('article')
            )
            if not content_div:
                return ""

            # =========================================================
            # [추가] 2. 지저분한 웹사이트 껍데기(UI, 관련기사, 메뉴) 강제 삭제
            noise_classes = [
                'articleAside', 'mainLayout-side', 'articleShareSticky', 
                'articleShare', 'relatedKeyword', 'relatedArticle', 'prnbox'
            ]
            for noise_class in noise_classes:
                for noise in content_div.find_all(class_=noise_class):
                    noise.decompose()
            # =========================================================

            removed = False
            for mv_class in ['articleBody-mv', 'article-mv', 'post-thumbnail',
                             'entry-thumbnail', 'article-eye-catch']:
                mv_area = content_div.find(class_=mv_class)
                if mv_area:
                    mv_area.decompose()
                    print(f"🗑️ 본문 상단 이미지 제거 ({mv_class})")
                    removed = True
                    break

            if not removed:
                first_child = content_div.find(recursive=False)
                if first_child and first_child.name in ['figure', 'picture']:
                    first_child.decompose()
                    print("🗑️ 본문 최상단 figure 제거")
                elif first_child and first_child.name == 'img':
                    first_child.decompose()
                    print("🗑️ 본문 최상단 img 제거")
                elif first_child and first_child.name in ['div', 'p']:
                    inner = first_child.find_all(recursive=False)
                    if len(inner) == 1 and inner[0].name in ['img', 'figure', 'picture']:
                        first_child.decompose()
                        print("🗑️ 본문 최상단 이미지 래퍼 제거")

            for elem in content_div.find_all(string=re.compile(
                r'原文掲載時刻:|ソース:|バックナンバー|関連キーワード|この記事をシェア|FOLLOW US'
            )):
                parent = elem.find_parent()
                if parent:
                    parent.decompose()

            for h_tag in content_div.find_all(['h2', 'h3', 'h4']):
                if any(kw in h_tag.get_text(strip=True) for kw in
                       ['バックナンバー', 'この記事をシェア', 'FOLLOW US', '関連記事', '関連キーワード']):
                    next_elem = h_tag.find_next_sibling()
                    h_tag.decompose()
                    while next_elem and next_elem.name not in ['h1', 'h2', 'h3', 'h4']:
                        temp = next_elem.find_next_sibling()
                        next_elem.decompose()
                        next_elem = temp

            for tag in content_div(['script', 'style', 'noscript', 'form', 'nav', 'aside', 'footer', 'header']):
                tag.decompose()

            for iframe in list(content_div.find_all('iframe')):
                if not any(v in iframe.get('src', '').lower() for v in ['youtube', 'youtu.be', 'vimeo']):
                    iframe.decompose()

            for elem in content_div.find_all(class_=lambda x: x and any(
                sc in ' '.join(x).lower() for sc in
                ['social-share', 'share-buttons', 'addtoany', 'sharedaddy', 'entry-footer', 'post-meta']
            )):
                elem.decompose()

            for a in list(content_div.find_all('a')):
                href = a.get('href', '')
                if any(kw in href.lower() for kw in
                       ['facebook.com', 'twitter.com', 'line.me', '/fellowship/', 'hatena.ne.jp']) \
                        or href.startswith('//') or not a.get_text(strip=True):
                    a.decompose()

            for tag_name in ['p', 'div', 'span', 'li']:
                for tag in content_div.find_all(tag_name):
                    if not tag.get_text(strip=True) and not tag.find('img'):
                        tag.decompose()

            # HTML 구조 그대로 반환하도록 변경 (기존 get_text 삭제)
            return str(content_div)

        except Exception as e:
            print(f"⚠️ 스크래핑 실패: {e}")
            return ""

    def generate_seo_slug(self, title_ko: str, article_date: datetime) -> str:
        slug = re.sub(r'[^a-zA-Z0-9\s]', '', title_ko)
        slug = slug.lower().strip().replace(' ', '-')
        slug = re.sub(r'-+', '-', slug).strip('-')
        date_str = article_date.strftime('%Y%m%d')
        return f"{slug[:50]}-{date_str}" if len(slug) >= 3 else f"news-{date_str}"

    def get_main_image_url(self, link: str):
        try:
            res = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(res.text, 'lxml')
            og = soup.find('meta', property='og:image')
            if og and og.get('content'):
                return og['content']
            content = soup.find('div', class_='entry-content')
            if content:
                img = content.find('img')
                if img and img.get('src'):
                    src = img['src']
                    return src if src.startswith('http') else urljoin(link, src)
        except:
            pass
        return None

    def download_image(self, url: str):
        if not url:
            return None
        try:
            print(f"🖼️ 이미지 다운로드: {url}")
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            res.raise_for_status()
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            ext = os.path.splitext(os.path.basename(urlparse(url).path).split('?')[0])[1]
            if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                ext = '.jpg'
            path = Path(f"/tmp/pronews_{int(time.time())}_{url_hash}{ext}")
            with open(path, 'wb') as f:
                f.write(res.content)
            print(f"   ✅ {path.name}")
            return path
        except Exception as e:
            print(f"⚠️ 이미지 다운로드 실패: {e}")
            return None

    def upload_media(self, image_path: Path):
        if not image_path or not image_path.exists():
            return None
        try:
            with open(image_path, 'rb') as img:
                res = requests.post(
                    f"{self.wordpress_api}/media",
                    auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                    headers={'Content-Disposition': f'attachment; filename={image_path.name}'},
                    files={'file': (image_path.name, img, 'image/jpeg')}
                )
                res.raise_for_status()
                return res.json()
        except Exception as e:
            print(f"⚠️ 미디어 업로드 실패: {e}")
            return None

    def is_already_posted_on_wp(self, original_url: str) -> bool:
        try:
            search_term = original_url.split('/')[-2] if original_url.endswith('/') else original_url.split('/')[-1]
            res = requests.get(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                params={'search': search_term, 'per_page': 5, 'status': 'any'},
                timeout=10
            )
            if res.status_code == 200:
                for post in res.json():
                    if original_url in post.get('content', {}).get('rendered', ''):
                        print(f"⚠️ 중복 감지 → 스킵: {post['link']}")
                        return True
            return False
        except Exception as e:
            print(f"⚠️ 중복 체크 오류 (계속 진행): {e}")
            return False

    def commit_posted_articles(self):
        try:
            import subprocess
            subprocess.run(['git', 'config', 'user.email', 'action@github.com'], check=True)
            subprocess.run(['git', 'config', 'user.name', 'GitHub Action'], check=True)
            subprocess.run(['git', 'add', POSTED_ARTICLES_FILE], check=True)
            result = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True)
            if result.returncode != 0:
                subprocess.run(['git', 'commit', '-m',
                    f'chore: update posted_articles [{datetime.now().strftime("%Y-%m-%d %H:%M")}]'], check=True)
                subprocess.run(['git', 'push'], check=True)
                print("📝 posted_articles.json → git 커밋 완료")
        except Exception as e:
            print(f"⚠️ git 커밋 실패: {e}")

    def post_to_wordpress(self, title: str, content: str, slug: str,
                           featured_media_id: int, original_date: datetime,
                           excerpt: str = "", status: str = "draft") -> bool:
        post_data = {
            'title': title, 'content': content, 'slug': slug,
            'status': status, 'featured_media': featured_media_id or 0,
            'date': original_date.strftime('%Y-%m-%dT%H:%M:%S')
        }
        if excerpt:
            post_data['excerpt'] = excerpt
        try:
            res = requests.post(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                json=post_data
            )
            res.raise_for_status()
            label = "📝 임시저장" if status == "draft" else "✨ 게시 성공"
            print(f"{label}: {res.json()['link']}")
            return True
        except Exception as e:
            print(f"❌ 게시 실패: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   {e.response.text[:300]}")
            return False

    def process_article(self, article: dict) -> bool:
        print(f"\n{'='*60}")
        print(f"📰 {article['title'][:70]}")
        print(f"📅 {article['date'].strftime('%Y-%m-%d %H:%M')} [{article.get('source','?')}]")
        print(f"{'='*60}")

        if self.gemini.rate_limit_hit:
            print("🛑 429 플래그 → 다음 런 이월")
            return False

        if not FORCE_UPDATE and self.is_already_posted_on_wp(article['link']):
            if article['link'] not in self.posted_articles:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return False

        body_text = self.fetch_full_content(article['link'])
        if not body_text:
            print("⚠️ 본문 스크래핑 실패 → 스킵")
            return False

        print("🔄 [1단계] Gemini 번역 (1회 JSON 통합)...")
        translated = self.gemini.translate_article(article['title'], body_text)

        if not translated or not translated.get('title') or not translated.get('content'):
            print("❌ 번역 실패 → 스킵")
            return False

        title_ko  = translated['title']
        content_ko = translated['content']
        excerpt   = translated.get('excerpt', '')
        tldr_html = translated.get('tldr', '')
        print(f"   📌 제목: {title_ko}")

        if self.gemini._has_japanese(content_ko):
            print("   ⚠️ 일본어 잔존 → 재번역 1회 시도...")
            content_ko = self.gemini.retranslate_content(content_ko)
            if self.gemini._has_japanese(content_ko):
                print("   ⚠️ 재번역 후 일부 잔존 → 경고 후 게시 진행")

        slug = self.generate_seo_slug(title_ko, article['date'])
        print(f"🔗 Slug: {slug}")

        print("🔍 특성 이미지(Featured Image) 처리 중...")
        featured_id = 0
        img_url = self.get_main_image_url(article['link'])
        if img_url:
            local_img = self.download_image(img_url)
            if local_img:
                media_info = self.upload_media(local_img)
                if media_info:
                    featured_id = media_info['id']
                try:
                    local_img.unlink()
                except:
                    pass

        final_content = ""
        if tldr_html:
            final_content += (
                '<div style="background:#f8f9fa;padding:20px;border-radius:8px;'
                'border-left:5px solid #0056b3;margin-bottom:30px;">\n'
                '<h3 style="margin-top:0;color:#0056b3;">💡 핵심 요약</h3>\n'
                f'{tldr_html}\n</div>\n\n'
            )
        final_content += content_ko
        final_content += (
            "\n\n<hr style='margin:40px 0 20px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
            f"<p style='font-size:13px;color:#777;'><strong>원문:</strong> "
            f"<a href='{article['link']}' target='_blank' rel='noopener'>{article['title']}</a></p>"
        )

        label = "draft(임시저장)" if POST_STATUS == "draft" else "publish(즉시공개)"
        print(f"📤 [2단계] WordPress {label} 중...")
        if self.post_to_wordpress(title_ko, final_content, slug, featured_id,
                                   article['date'], excerpt=excerpt, status=POST_STATUS):
            if not FORCE_UPDATE:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return True
        return False

    def run(self):
        print(f"\n{'='*60}")
        print(f"pronews.jp → prodg.kr 자동 번역 v7.2")
        print(f"엔진: {GEMINI_MODEL} | 호출: 기사당 1회 JSON 통합")
        print(f"모드: {'자동 (최신→아카이브 보충)' if IS_SCHEDULED else '수동 (아카이브 오래된 순)'}")
        print(f"게시: {POST_STATUS.upper()} | 일일 한도: {DAILY_LIMIT}건")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

        if not WORDPRESS_USER or not WORDPRESS_APP_PASSWORD:
            print("❌ WP_USER / WP_APP_PASSWORD 환경변수 필요")
            sys.exit(1)

        print("🔑 Gemini API 키 검증...")
        test = self.gemini._call_api("テスト를 한국어로 번역:", max_tokens=30)
        if not test:
            print("❌ Gemini API 키 오류 → 종료")
            sys.exit(1)
        print(f"   ✅ API 정상: '{test}'")

        articles = self.get_articles_to_process()
        if not articles:
            print("✅ 처리할 기사 없음")
            return

        success = 0
        try:
            for i, article in enumerate(articles, 1):
                if self.gemini.rate_limit_hit:
                    print(f"\n🛑 429 런 종료 → 남은 {len(articles)-i+1}건 다음 런 이월")
                    break
                print(f"\n[{i}/{len(articles)}]")
                if self.process_article(article):
                    success += 1
                if i < len(articles):
                    time.sleep(10)
        finally:
            print(f"\n{'='*60}")
            print(f"🏁 완료: {success}/{len(articles)}건 게시")
            print(f"{'='*60}\n")
            if success > 0:
                self.commit_posted_articles()


if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
