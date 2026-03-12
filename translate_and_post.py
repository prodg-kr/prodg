#!/usr/bin/env python3
"""
pronews.jp 자동 번역 시스템 v7.5.3
파이프라인: 일본어 원문 → Gemini 1회 JSON 통합 번역 → WordPress Draft

v7.5.2 → v7.5.3 변경사항:
- fetch_archive_articles 내 urljoin 인자 마크다운 찌꺼기 완전 수정
  "[https://jp.pronews.com](https://jp.pronews.com)" → "https://jp.pronews.com"
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
import html

# ==========================================
# 설정
# ==========================================
WORDPRESS_URL          = "https://prodg.kr"
WORDPRESS_USER         = os.environ.get("WP_USER")
WORDPRESS_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
GEMINI_API_KEY         = os.environ.get("GEMINI_API_KEY")
PRONEWS_RSS            = "https://jp.pronews.com/feed"
PRONEWS_ARCHIVE_BASE   = "https://jp.pronews.com/news/page"
PRONEWS_BASE_URL       = "https://jp.pronews.com"
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
2. 문체: 반드시 '~다', '~했다', '~이다' 등 기사 형식의 평어체(해라체/한다체)로 통일
3. 브랜드명·모델명 원문 유지: Sony, Canon, Nikon, DJI, Blackmagic, Sigma 등
4. 해상도: 4K, 8K, Full HD / 프레임레이트: fps, 24p, 60p
5. ★중요★: 본문에 포함된 <img>, <figure>, <iframe> 등의 HTML 미디어 태그와 속성(src, alt 등)은 절대 삭제하거나 수정하지 말고 제자리에 그대로 유지하세요.
6. 기계 번역 느낌 없이 사람이 쓴 듯 자연스럽게 (Google SEO·AdSense 품질 기준)

=== 출력 JSON 규칙 ===
- title: SEO 최적화 제목 (브랜드명·모델명 필수 포함, 최대 50자)
- content: 번역 본문 (원본 HTML 구조 및 이미지 태그 완벽 유지)
- excerpt: 구글 스니펫용 요약 (80~100자, 평어체)
- tldr: 핵심 요약 3~4항목 (<ul><li> HTML, 평어체)
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
일본어 부분을 자연스러운 한국어 평어체(~다, ~했다, ~이다)로 번역하고 전체 본문을 반환하세요.
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

        actual_max_page = ARCHIVE_MAX_PAGES
        if oldest_first:
            try:
                print("   🔍 실제 마지막 페이지 번호 탐색 중...")
                res = requests.get(f"{PRONEWS_ARCHIVE_BASE}/1/", headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'lxml')
                    pages = []
                    for a in soup.find_all('a', href=True):
                        match = re.search(r'/news/page/(\d+)', a['href'])
                        if match:
                            pages.append(int(match.group(1)))
                    if pages:
                        actual_max_page = min(max(pages), ARCHIVE_MAX_PAGES)
                        print(f"   ✅ 탐색된 시작 페이지: {actual_max_page}")
            except Exception as e:
                print(f"   ⚠️ 페이지 탐색 오류 (기본값 {ARCHIVE_MAX_PAGES} 사용): {e}")

        collected = []
        seen_links = set()
        page = actual_max_page if oldest_first else 1

        while len(collected) < need * 3 and 1 <= page <= ARCHIVE_MAX_PAGES:
            url = f"{PRONEWS_ARCHIVE_BASE}/{page}/"
            try:
                res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)

                if res.status_code == 404:
                    if oldest_first:
                        print(f"   페이지 {page} 없음 → 이전 페이지 탐색")
                        page -= 1
                        continue
                    else:
                        print(f"   페이지 {page} 없음 → 크롤링 종료")
                        break

                res.raise_for_status()
                soup = BeautifulSoup(res.text, 'lxml')
                found = []

                # article 태그 기반 파싱
                for article in soup.find_all('article'):
                    a_tag = article.find('a', href=True)
                    if not a_tag:
                        continue
                    link = a_tag['href']
                    if not link.startswith('http'):
                        link = urljoin("https://jp.pronews.com", link)
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

                # article 태그 없으면 URL 패턴으로 파싱
                if not found:
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if not href.startswith('http'):
                            href = urljoin("https://jp.pronews.com", href)
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
                page = page - 1 if oldest_first else page + 1
                time.sleep(1)

            except Exception as e:
                print(f"⚠️ 아카이브 페이지 {page} 오류: {e}")
                page = page - 1 if oldest_first else page + 1

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

    def fetch_full_content(self, url: str):
        try:
            print(f"📄 스크래핑: {url}")
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, timeout=15)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'lxml')

            article_date = None
            time_tag = soup.find('time', datetime=True)
            if time_tag:
                try:
                    article_date = datetime.fromisoformat(time_tag['datetime'].replace('Z', '+00:00'))
                except:
                    pass

            if not article_date:
                date_text = soup.select_one('.articleHeader-date')
                if date_text:
                    try:
                        article_date = datetime.strptime(date_text.get_text(strip=True)[:10], "%Y.%m.%d")
                    except:
                        pass

            content_div = (
                soup.find('div', class_='articleBody-inner') or
                soup.find('div', class_='articleBody') or
                soup.find('div', class_='entry-content') or
                soup.find('div', class_='post-content') or
                soup.find('div', class_='article-content') or
                soup.find('article')
            )
            if not content_div:
                return "", None

            noise_classes = [
                'articleAside', 'mainLayout-side', 'articleShareSticky',
                'articleShare', 'relatedKeyword', 'relatedArticle', 'prnbox'
            ]
            for noise_class in noise_classes:
                for noise in content_div.find_all(class_=noise_class):
                    noise.decompose()

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

            return str(content_div), article_date

        except Exception as e:
            print(f"⚠️ 스크래핑 실패: {e}")
            return "", None

    def generate_seo_slug(self, title_ko: str, article_date: datetime, title_ja: str = "") -> str:
        """
        슬러그 생성 우선순위:
        1. 한국어 제목에서 영문/숫자 추출
        2. 없으면 일본어 원문 제목에서 영문/숫자 추출
        3. 둘 다 없으면 브랜드/모델명 사전 기반 키워드 추출
        4. 최종 fallback: news-날짜
        """
        date_str = article_date.strftime('%Y%m%d') if article_date else datetime.now().strftime('%Y%m%d')

        # 브랜드/제품 키워드 사전 (일본어 → 슬러그용 영문)
        BRAND_SLUG = {
            'ソニー': 'sony', 'キヤノン': 'canon', 'ニコン': 'nikon',
            'タムロン': 'tamron', 'シグマ': 'sigma', 'フジフイルム': 'fujifilm',
            'パナソニック': 'panasonic', 'オリンパス': 'olympus', 'ライカ': 'leica',
            'ブラックマジック': 'blackmagic', 'アップル': 'apple', 'アドビ': 'adobe',
            'ゴープロ': 'gopro', 'ドローン': 'drone', 'カメラ': 'camera',
            'レンズ': 'lens', 'ミラーレス': 'mirrorless', '動画': 'video',
            '映像': 'video', '写真': 'photo', '撮影': 'shooting',
        }

        def extract_english(text: str) -> str:
            """영문·숫자·모델번호 추출"""
            words = re.findall(r'[a-zA-Z][a-zA-Z0-9]*|[0-9]+[a-zA-Z]+|[a-zA-Z]+[0-9]+', text)
            # 너무 짧은 단어(1~2글자) 제외, 단 숫자+영문 조합(4K, 8K, R5 등)은 유지
            filtered = [w.lower() for w in words if len(w) >= 2]
            return '-'.join(filtered[:6])

        # 1. 한국어 제목에서 영문 추출
        slug = extract_english(title_ko)

        # 2. 일본어 원문에서 영문 추출
        if len(slug) < 3 and title_ja:
            slug = extract_english(title_ja)

        # 3. 브랜드 사전으로 키워드 추출 (일본어 원문 기반)
        if len(slug) < 3 and title_ja:
            brand_words = []
            for ja, en in BRAND_SLUG.items():
                if ja in title_ja:
                    brand_words.append(en)
            if brand_words:
                slug = '-'.join(brand_words[:3])

        # 4. 최종 fallback
        slug = re.sub(r'-+', '-', slug).strip('-')
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


    def _strip_html(self, html_text: str) -> str:
        try:
            soup = BeautifulSoup(html_text or "", "lxml")
            text = soup.get_text(" ", strip=True)
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            return (html_text or "").strip()

    def build_lede_summary(self, excerpt: str, tldr_html: str) -> str:
        """
        2~3문장 요약을 본문 최상단에 넣기 위한 텍스트 생성.
        우선순위: excerpt → tldr(텍스트화) → 빈 문자열
        """
        base = (excerpt or "").strip()
        if not base:
            base = self._strip_html(tldr_html)
        if not base:
            return ""

        # 문장 2~3개만 잘라내기 (한/영 혼용 대응)
        # 구분자: . ! ? … 그리고 '다.' 같은 한국어 종결도 포함
        parts = re.split(r"(?<=[\.\!\?\u2026])\s+|(?<=다\.)\s+|(?<=요\.)\s+", base)
        parts = [p.strip() for p in parts if p.strip()]
        summary = " ".join(parts[:3]).strip()
        # 너무 길면 컷
        if len(summary) > 380:
            summary = summary[:377].rstrip() + "..."
        return summary

    def fetch_recent_posts(self, per_page: int = 50) -> list:
        """
        내부링크 후보를 만들기 위해 최근 게시글을 가져온다.
        실패하면 빈 리스트 반환.
        """
        try:
            res = requests.get(
                f"{self.wordpress_api}/posts",
                params={"per_page": per_page, "status": "publish"},
                timeout=10
            )
            if res.status_code != 200:
                return []
            return res.json()
        except Exception:
            return []

    def pick_related_posts(self, title: str, limit: int = 3) -> list:
        """
        매우 단순한 토큰 겹침 기반 관련 글 선택(빠르고 안정적).
        """
        posts = self.fetch_recent_posts(per_page=60)
        if not posts:
            return []

        # 토큰화: 영문/숫자/한글 단어
        def tokens(s: str) -> set:
            s = (s or "").lower()
            return set(re.findall(r"[a-z0-9]+|[가-힣]{2,}", s))

        t_tokens = tokens(title)
        if not t_tokens:
            return []

        scored = []
        for p in posts:
            p_title = self._strip_html(p.get("title", {}).get("rendered", ""))
            p_link = p.get("link", "")
            if not p_link:
                continue
            p_tokens = tokens(p_title)
            score = len(t_tokens & p_tokens)
            if score <= 0:
                continue
            scored.append((score, p_title, p_link))

        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = []
        used_links = set()
        for _, pt, pl in scored:
            if pl in used_links:
                continue
            used_links.add(pl)
            picked.append({"title": pt, "link": pl})
            if len(picked) >= limit:
                break
        return picked

    def build_internal_links_html(self, title: str, limit: int = 3) -> str:
        related = self.pick_related_posts(title, limit=limit)
        # fallback: 카테고리/홈 링크만
        pronews_category_url = f"{WORDPRESS_URL}/category/pronews/"
        if not related:
            return (
                "<hr style='margin:32px 0 18px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
                "<h3 style='margin:0 0 10px 0;'>관련 글</h3>\n"
                "<ul>\n"
                f"<li><a href='{pronews_category_url}'>proNEWS 카테고리</a></li>\n"
                f"<li><a href='{WORDPRESS_URL}/'>홈</a></li>\n"
                "</ul>\n"
            )

        items = "\n".join(
            f"<li><a href='{html.escape(r['link'])}'>{html.escape(r['title'])}</a></li>"
            for r in related
        )
        return (
            "<hr style='margin:32px 0 18px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
            "<h3 style='margin:0 0 10px 0;'>관련 글</h3>\n"
            "<ul>\n"
            f"{items}\n"
            "</ul>\n"
        )

    def post_to_wordpress(self, title: str, content: str, slug: str,
                           featured_media_id: int, original_date: datetime,
                           excerpt: str = "", status: str = "draft") -> bool:
        target_date = original_date.strftime('%Y-%m-%dT%H:%M:%S') if original_date else datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        post_data = {
            'title': title, 'content': content, 'slug': slug,
            'status': status, 'featured_media': featured_media_id or 0,
            'date': target_date,
            'categories': [430]  # proNEWS 카테고리 고정
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
            print(f"{label} ({target_date}): {res.json()['link']}")
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

        body_text, exact_date = self.fetch_full_content(article['link'])
        if not body_text:
            print("⚠️ 본문 스크래핑 실패 → 스킵")
            return False

        if exact_date:
            article['date'] = exact_date
            print(f"🕒 원문 시각 복원 성공: {exact_date.strftime('%Y-%m-%d %H:%M:%S')}")

        print("🔄 [1단계] Gemini 번역 (1회 JSON 통합)...")
        translated = self.gemini.translate_article(article['title'], body_text)

        if not translated or not translated.get('title') or not translated.get('content'):
            print("❌ 번역 실패 → 스킵")
            return False

        title_ko   = translated['title']
        content_ko = translated['content']
        excerpt    = translated.get('excerpt', '')
        tldr_html  = translated.get('tldr', '')
        print(f"   📌 제목: {title_ko}")

        if self.gemini._has_japanese(content_ko):
            print("   ⚠️ 일본어 잔존 → 재번역 1회 시도...")
            content_ko = self.gemini.retranslate_content(content_ko)
            if self.gemini._has_japanese(content_ko):
                print("   ⚠️ 재번역 후 일부 잔존 → 경고 후 게시 진행")

        slug = self.generate_seo_slug(title_ko, article['date'], title_ja=article.get('title', ''))
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

        # ── SEO 보강: 2~3문장 요약 + 내부링크 ──
        lede_summary = self.build_lede_summary(excerpt, tldr_html)
        internal_links_html = self.build_internal_links_html(title_ko, limit=3)

        final_content = ""
        if lede_summary:
            final_content += (
                f"<p style='font-size:15px;line-height:1.7;color:#222;margin:0 0 18px 0;'>"
                f"{lede_summary}</p>\n"
            )

        if tldr_html:
            final_content += (
                '<div style="background:#f8f9fa;padding:20px;border-radius:8px;'
                'border-left:5px solid #0056b3;margin-bottom:30px;">\n'
                '<h3 style="margin-top:0;color:#0056b3;">💡 핵심 요약</h3>\n'
                f'{tldr_html}\n</div>\n\n'
            )

        final_content += content_ko
        final_content += "\n\n" + internal_links_html
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
        print(f"pronews.jp → prodg.kr 자동 번역 v7.8")
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
