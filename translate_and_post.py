#!/usr/bin/env python3
"""
pronews.jp 자동 번역 시스템 v4
파이프라인: 일본어 원문 → Groq 1차 번역 → Gemini Flash 2차 SEO 편집 → WordPress 게시

v3 → v4 변경사항:
- googletrans 제거 → Groq API (llama-3.3-70b, 무료, 안정적)
- 2차 SEO 편집 추가 → Gemini 2.5 Flash (무료 플랜)
- 하루 최대 10건 제한 (최신 기사 우선, 부족하면 과거 미게시 기사로 채움)
- 모델명 환경변수로 교체 가능 (GROQ_MODEL, GEMINI_MODEL)
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
GROQ_API_KEY           = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY         = os.environ.get("GEMINI_API_KEY")
PRONEWS_RSS            = "https://jp.pronews.com/feed"
POSTED_ARTICLES_FILE   = "posted_articles.json"
FORCE_UPDATE           = os.environ.get("FORCE_UPDATE", "false").lower() == "true"
DAILY_LIMIT            = 10  # 하루 최대 게시 건수

# 모델 설정 (환경변수로 언제든 교체 가능)
GROQ_MODEL   = os.environ.get("GROQ_MODEL",   "llama-3.3-70b-versatile")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# API 엔드포인트
GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


# ==========================================
# Groq 번역기 (1차: 일본어 → 한국어 직역)
# ==========================================
class GroqTranslator:
    """
    Groq API로 일본어 → 한국어 번역
    - 모델: llama-3.3-70b-versatile (무료, 분당 30회, 일 14,400회)
    - 역할: 빠르고 정확한 직역 (SEO 편집은 Gemini가 담당)
    - HTML 처리: 태그 제거 후 텍스트만 번역, 단락 구조 유지
    """

    def __init__(self):
        self.api_key = GROQ_API_KEY
        if not self.api_key:
            print("❌ GROQ_API_KEY 미설정")
            sys.exit(1)

    def _call_api(self, messages: list, max_tokens: int = 4096) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": GROQ_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3  # 번역은 낮은 temperature (일관성 우선)
        }
        try:
            res = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"⚠️ Groq API 오류: {e}")
            return ""

    def translate_title(self, title_ja: str) -> str:
        """제목 번역"""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a professional Japanese to Korean translator specializing in "
                    "video production and camera industry news. "
                    "Translate the given Japanese title to Korean accurately. "
                    "Output only the translated title, nothing else."
                )
            },
            {"role": "user", "content": f"다음 일본어 제목을 한국어로 번역하세요:\n{title_ja}"}
        ]
        result = self._call_api(messages, max_tokens=200)
        return result if result else title_ja

    def translate_content(self, html_content: str) -> str:
        """
        본문 번역
        - HTML에서 텍스트 추출 → 청크 분할 번역 → HTML 재조립
        - 이미지/헤더 태그는 플레이스홀더로 보존
        """
        if not html_content:
            return ""

        soup = BeautifulSoup(html_content, 'lxml')

        # 이미지 태그 보존
        images = {}
        for i, img in enumerate(soup.find_all('img')):
            placeholder = f"___IMG_{i}___"
            images[placeholder] = str(img)
            img.replace_with(placeholder)

        # 헤더 태그 보존
        headers_map = {}
        for i, tag in enumerate(soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
            placeholder = f"___H{i}_{tag.name}___"
            headers_map[placeholder] = {'tag': tag.name, 'text': tag.get_text(strip=True)}
            tag.replace_with(placeholder)

        # 단락 단위 텍스트 추출
        paragraphs = []
        for elem in soup.find_all(['p', 'li', 'blockquote']):
            text = elem.get_text(separator=' ', strip=True)
            if text and len(text) > 5:
                paragraphs.append(text)

        if not paragraphs:
            full_text = soup.get_text(separator='\n', strip=True)
            paragraphs = [line for line in full_text.split('\n') if line.strip()]

        # 청크 단위 번역 (청크당 최대 2000자)
        translated_paragraphs = []
        chunk, chunk_size = [], 0

        for para in paragraphs:
            if chunk_size + len(para) > 2000 and chunk:
                translated = self._translate_chunk('\n\n'.join(chunk))
                translated_paragraphs.extend(translated.split('\n\n'))
                chunk, chunk_size = [], 0
                time.sleep(0.5)
            chunk.append(para)
            chunk_size += len(para)

        if chunk:
            translated = self._translate_chunk('\n\n'.join(chunk))
            translated_paragraphs.extend(translated.split('\n\n'))

        # HTML 재조립
        translated_html = ""
        for para in translated_paragraphs:
            para = para.strip()
            if not para:
                continue
            if para.startswith('___'):
                translated_html += para + "\n"
            else:
                translated_html += f"<p>{para}</p>\n"

        # 헤더 태그 복원
        for placeholder, info in headers_map.items():
            if placeholder in translated_html:
                header_ko = self._translate_chunk(info['text']) if info['text'] else info['text']
                translated_html = translated_html.replace(
                    placeholder,
                    f"<{info['tag']}>{header_ko}</{info['tag']}>"
                )

        # 이미지 태그 복원
        for placeholder, img_tag in images.items():
            translated_html = translated_html.replace(placeholder, img_tag)

        return translated_html

    def _translate_chunk(self, text: str) -> str:
        """텍스트 청크 번역"""
        if not text.strip():
            return text
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a professional Japanese to Korean translator specializing in "
                    "video production, broadcasting, and camera industry content. "
                    "Translate accurately while preserving paragraph structure. "
                    "Keep technical terms, product names, model numbers, and brand names as-is. "
                    "Keep placeholders like ___IMG_0___ or ___H0_h2___ unchanged. "
                    "Output only the translated text, nothing else."
                )
            },
            {"role": "user", "content": f"다음 일본어를 한국어로 번역하세요:\n\n{text}"}
        ]
        result = self._call_api(messages, max_tokens=4096)
        return result if result else text


# ==========================================
# Gemini SEO 편집기 (2차: 직역 → SEO 최적화)
# ==========================================
class GeminiEditor:
    """
    Gemini 2.5 Flash로 번역된 한국어를 SEO 최적화 편집
    - 역할: 자연스러운 한국어 윤문 + SEO 제목 재작성 + 전문용어 보정
    - 비용: 무료 플랜 (3개월), 10건/일 × 2호출 = 20회/일 (한도 500회 대비 여유)
    - 모델 변경: GEMINI_MODEL 환경변수로 교체 가능
    """

    def __init__(self):
        self.api_key = GEMINI_API_KEY
        self.enabled = bool(self.api_key)
        if not self.enabled:
            print("⚠️ GEMINI_API_KEY 미설정 → SEO 편집 건너뜀 (Groq 번역 결과만 사용)")

    def _call_api(self, prompt: str, max_tokens: int = 2048) -> str:
        if not self.enabled:
            return ""
        # GEMINI_MODEL이 환경변수로 변경될 수 있으므로 매 호출시 URL 재생성
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={self.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.7
            }
        }
        try:
            res = requests.post(url, json=payload, timeout=60)
            res.raise_for_status()
            candidates = res.json().get("candidates", [])
            if candidates:
                return candidates[0]["content"]["parts"][0]["text"].strip()
            return ""
        except Exception as e:
            print(f"⚠️ Gemini API 오류: {e}")
            return ""

    def edit_title(self, title_ko: str, title_ja: str) -> str:
        """제목 SEO 편집 - 핵심 키워드 앞배치, 30자 내외"""
        if not self.enabled:
            return title_ko

        prompt = f"""당신은 영상/카메라 전문 미디어의 SEO 에디터입니다.

일본어 원제: {title_ja}
번역된 제목: {title_ko}

구글 검색 최적화된 한국어 제목을 작성하세요.

규칙:
1. 핵심 제품명/브랜드명 반드시 포함 (Sony, Canon, DJI, Blackmagic, DaVinci 등 원문 표기 유지)
2. 검색 핵심 키워드를 앞쪽에 배치
3. 자연스러운 한국어 (직역체, 어색한 조사 금지)
4. 30자 내외 (최대 40자)
5. 제목만 출력 (설명, 따옴표, 번호 없음)"""

        result = self._call_api(prompt, max_tokens=100)
        if result:
            result = re.sub(r'^[\d\.\)\-\s"\'「」]+', '', result).strip().strip('"\'「」')
            print(f"   ✏️ SEO 제목: {result}")
            return result
        return title_ko

    def edit_content(self, content_ko: str) -> str:
        """본문 SEO 편집 - 직역체 윤문, 전문용어 보정, HTML 태그 유지"""
        if not self.enabled or not content_ko:
            return content_ko

        if len(content_ko) <= 3000:
            return self._edit_chunk(content_ko)

        # 장문은 <p> 태그 기준 청크 분할
        chunks = self._split_html_chunks(content_ko, max_chars=3000)
        edited_chunks = []
        for i, chunk in enumerate(chunks):
            print(f"   📝 Gemini 편집 중... ({i+1}/{len(chunks)})")
            edited = self._edit_chunk(chunk)
            edited_chunks.append(edited if edited else chunk)
            time.sleep(1)
        return "\n".join(edited_chunks)

    def _edit_chunk(self, html_chunk: str) -> str:
        prompt = f"""당신은 영상/카메라 전문 미디어의 한국어 에디터입니다.

아래는 일본어 기사를 AI가 번역한 한국어 HTML 본문입니다.
직역체를 자연스러운 한국어로 윤문하고 SEO를 최적화하세요.

편집 규칙:
1. HTML 태그(<p>, <h2>, <h3>, <img> 등)는 반드시 그대로 유지
2. 직역체, 어색한 조사, 일본식 표현을 자연스러운 한국어로 수정
3. 문체는 반드시 '~합니다', '~했습니다', '~입니다' 등 합쇼체(격식체)로 통일
   - '~한다', '~했다', '~이다' 등 평서체 사용 금지
   - '~해요', '~예요' 등 해요체 사용 금지
4. 영상/카메라 전문용어 정확히 표기:
   - 브랜드명: Sony, Canon, Nikon, DJI, Blackmagic, DaVinci Resolve 등 원문 유지
   - 해상도: 4K, 8K, Full HD
   - 프레임레이트: fps, 24p, 60p
   - 기타: 코덱, 비트레이트, 조리개, 셔터스피드 등 정확한 한국어 사용
5. 단락 구조와 문장 수 유지 (내용 추가/삭제 금지)
6. HTML만 출력 (설명 텍스트 없음)

번역된 HTML:
{html_chunk}"""

        result = self._call_api(prompt, max_tokens=4096)
        return result if result else html_chunk

    def _split_html_chunks(self, html: str, max_chars: int = 3000) -> list:
        """<p> 태그 경계 기준으로 HTML 청크 분할"""
        chunks = []
        current_chunk = ""
        parts = re.split(r'(?=<p>)', html)
        for part in parts:
            if len(current_chunk) + len(part) > max_chars and current_chunk:
                chunks.append(current_chunk)
                current_chunk = part
            else:
                current_chunk += part
        if current_chunk:
            chunks.append(current_chunk)
        return chunks if chunks else [html]


# ==========================================
# 메인 번역 시스템
# ==========================================
class NewsTranslator:
    def __init__(self):
        self.groq = GroqTranslator()
        self.gemini = GeminiEditor()
        self.wordpress_api = f"{WORDPRESS_URL}/wp-json/wp/v2"
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

    def fetch_rss_feed(self) -> list:
        """
        RSS 피드에서 미게시 기사 조회
        - 최신순 정렬
        - 최신 기사가 10건 미만이면 과거 미게시 기사로 채워 항상 최대 10건 반환
        """
        print(f"📡 RSS 피드 확인 중: {PRONEWS_RSS}")
        feed = feedparser.parse(PRONEWS_RSS)
        print(f"🔍 총 {len(feed.entries)}개 피드 항목 검색...")

        unposted = []
        for entry in feed.entries:
            if not FORCE_UPDATE and entry.link in self.posted_articles:
                continue
            try:
                article_date = datetime(*entry.published_parsed[:6])
            except:
                article_date = datetime.now()

            unposted.append({
                'title': entry.title,
                'link': entry.link,
                'date': article_date
            })

        # 최신순 정렬 후 최대 10건 (최신 + 과거 미게시 순서로 자동 채워짐)
        unposted.sort(key=lambda x: x['date'], reverse=True)
        target = unposted[:DAILY_LIMIT]

        print(f"✅ 미게시 기사: {len(unposted)}건 → 오늘 처리: {len(target)}건 (최대 {DAILY_LIMIT}건)")
        return target

    def fetch_full_content(self, url: str):
        """본문 스크래핑 + 불필요 요소 제거"""
        try:
            print(f"📄 스크래핑: {url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'lxml')
            content_div = (
                soup.find('div', class_='entry-content') or
                soup.find('div', class_='post-content') or
                soup.find('div', class_='article-content') or
                soup.find('article')
            )
            if not content_div:
                return None

            # 불필요 텍스트/섹션 제거
            for elem in content_div.find_all(string=re.compile(
                r'원문 게시시각:|출처:|原文掲載時刻:|ソース:|バックナンバー|関連キーワード|この記事をシェア|FOLLOW US'
            )):
                parent = elem.find_parent()
                if parent:
                    parent.decompose()

            remove_headings = ['백 넘버', '関連キーワード', 'バックナンバー',
                               'この記事をシェア', '이 기사 공유', 'FOLLOW US', '関連記事', '관련 기사']
            for h_tag in content_div.find_all(['h2', 'h3', 'h4']):
                if any(kw in h_tag.get_text(strip=True) for kw in remove_headings):
                    next_elem = h_tag.find_next_sibling()
                    h_tag.decompose()
                    while next_elem and next_elem.name not in ['h1', 'h2', 'h3', 'h4']:
                        temp = next_elem.find_next_sibling()
                        next_elem.decompose()
                        next_elem = temp

            for tag in content_div(['script', 'style', 'iframe', 'noscript', 'form',
                                    'nav', 'aside', 'footer', 'header']):
                tag.decompose()

            social_classes = ['social-share', 'share-buttons', 'sns-share', 'social-links',
                               'share-links', 'addtoany', 'sharedaddy', 'jp-relatedposts',
                               'entry-footer', 'post-tags', 'post-categories', 'post-meta']
            for elem in content_div.find_all(class_=lambda x: x and any(
                sc in ' '.join(x).lower() for sc in social_classes
            )):
                elem.decompose()

            remove_keywords = [
                'facebook.com', 'twitter.com', 'line.me', 'instagram.com',
                'youtube.com', 'pronews.jp', 'kr.pronews.com', '/fellowship/',
                'getpocket.com', 'hatena.ne.jp', '/feed', '/columntitle/',
                '/specialtitle/', '/writer/', 'jp.pronews.com'
            ]
            for a in list(content_div.find_all('a')):
                href = a.get('href', '')
                text = a.get_text(strip=True)
                if any(kw in href.lower() for kw in remove_keywords) or href.startswith('//') or not text:
                    a.decompose()

            for tag_name in ['p', 'div', 'span', 'li']:
                for tag in content_div.find_all(tag_name):
                    if not tag.get_text(strip=True) and not tag.find('img'):
                        tag.decompose()

            return str(content_div)

        except Exception as e:
            print(f"⚠️ 스크래핑 실패: {e}")
            return None

    def generate_slug(self, title_ja: str, article_date: datetime) -> str:
        """영문 slug 생성 (영문 키워드 + 날짜)"""
        words = title_ja.split()
        slug_words = []
        for word in words[:6]:
            cleaned = re.sub(r'[^a-zA-Z0-9\-]', '', word.lower())
            if cleaned and len(cleaned) > 2:
                slug_words.append(cleaned)

        date_str = article_date.strftime('%Y%m%d')
        slug = ('-'.join(slug_words[:4]) + f"-{date_str}") if slug_words else f"article-{date_str}-{int(time.time())}"
        return slug[:60]

    def get_main_image_url(self, link: str):
        try:
            res = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(res.text, 'lxml')
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                return og_img['content']
            content = soup.find('div', class_='entry-content')
            if content:
                img = content.find('img')
                if img and img.get('src'):
                    img_url = img['src']
                    return img_url if img_url.startswith('http') else urljoin(link, img_url)
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
            original_filename = os.path.basename(urlparse(url).path).split('?')[0]
            ext = os.path.splitext(original_filename)[1]
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
        """
        WordPress에서 원문 URL 기준으로 중복 게시 여부 확인
        - posted_articles.json 캐시 실패 시 2차 안전망 역할
        - 원문 링크를 본문에 포함하므로 검색으로 찾을 수 있음
        """
        try:
            # 원문 URL의 일부로 WordPress 검색
            search_term = original_url.split('/')[-2] if original_url.endswith('/') else original_url.split('/')[-1]
            res = requests.get(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                params={'search': search_term, 'per_page': 5, 'status': 'publish'},
                timeout=10
            )
            if res.status_code == 200:
                posts = res.json()
                for post in posts:
                    if original_url in post.get('content', {}).get('rendered', ''):
                        print(f"⚠️ 중복 감지 → 스킵: {post['link']}")
                        return True
            return False
        except Exception as e:
            print(f"⚠️ 중복 체크 오류 (계속 진행): {e}")
            return False  # 오류 시 게시 진행 (보수적 처리)

    def commit_posted_articles(self):
        """
        posted_articles.json을 git 저장소에 커밋
        - GitHub Actions 캐시 대신 git으로 영구 보존
        - 캐시가 날아가도 중복 게시 방지
        """
        try:
            import subprocess
            subprocess.run(['git', 'config', 'user.email', 'action@github.com'], check=True)
            subprocess.run(['git', 'config', 'user.name', 'GitHub Action'], check=True)
            subprocess.run(['git', 'add', POSTED_ARTICLES_FILE], check=True)
            result = subprocess.run(
                ['git', 'diff', '--cached', '--quiet'],
                capture_output=True
            )
            if result.returncode != 0:  # 변경사항 있을 때만 커밋
                subprocess.run(
                    ['git', 'commit', '-m', f'chore: update posted_articles [{datetime.now().strftime("%Y-%m-%d %H:%M")}]'],
                    check=True
                )
                subprocess.run(['git', 'push'], check=True)
                print("📝 posted_articles.json → git 커밋 완료")
        except Exception as e:
            print(f"⚠️ git 커밋 실패 (캐시로 대체): {e}")

    def post_to_wordpress(self, title: str, content: str, slug: str,
                           featured_media_id: int, original_date: datetime) -> bool:
        post_data = {
            'title': title,
            'content': content,
            'slug': slug,
            'status': 'publish',
            'featured_media': featured_media_id or 0,
            'date': original_date.strftime('%Y-%m-%dT%H:%M:%S')
        }
        try:
            res = requests.post(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                json=post_data
            )
            res.raise_for_status()
            print(f"✨ 게시 성공: {res.json()['link']}")
            return True
        except Exception as e:
            print(f"❌ 게시 실패: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   {e.response.text[:300]}")
            return False

    def process_article(self, article: dict) -> bool:
        print(f"\n{'='*60}")
        print(f"📰 {article['title'][:60]}")
        print(f"📅 {article['date'].strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")

        # 1. 중복 체크 (posted_articles.json + WordPress 2중 확인)
        if not FORCE_UPDATE and self.is_already_posted_on_wp(article['link']):
            if article['link'] not in self.posted_articles:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return False

        # 2. 본문 스크래핑
        raw_html = self.fetch_full_content(article['link'])
        if not raw_html:
            print("⚠️ 본문 스크래핑 실패 → 스킵")
            return False

        # 3. Groq 1차 번역
        print("🔄 [1단계] Groq 번역 중...")
        title_ko_raw = self.groq.translate_title(article['title'])
        content_ko_raw = self.groq.translate_content(raw_html)
        print(f"   번역 제목: {title_ko_raw}")

        # 4. Gemini 2차 SEO 편집
        print("✏️  [2단계] Gemini SEO 편집 중...")
        title_ko = self.gemini.edit_title(title_ko_raw, article['title'])
        content_ko = self.gemini.edit_content(content_ko_raw)

        # 5. Slug 생성
        slug = self.generate_slug(article['title'], article['date'])
        print(f"🔗 Slug: {slug}")

        # 6. 이미지 처리
        print("🔍 이미지 처리 중...")
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

        # 7. 최종 본문 구성 + 원문 출처
        final_content = content_ko
        final_content += (
            "\n\n<hr style='margin:40px 0 20px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
            f"<p style='font-size:13px;color:#777;'>"
            f"<strong>원문:</strong> "
            f"<a href='{article['link']}' target='_blank' rel='noopener'>{article['title']}</a>"
            f"</p>"
        )

        # 8. WordPress 게시
        print("📤 WordPress 게시 중...")
        if self.post_to_wordpress(title_ko, final_content, slug, featured_id, article['date']):
            if not FORCE_UPDATE:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return True
        return False

    def run(self):
        print(f"\n{'='*60}")
        print(f"pronews.jp → prodg.kr 자동 번역 v4")
        print(f"번역: Groq ({GROQ_MODEL})")
        print(f"편집: Gemini ({GEMINI_MODEL})")
        print(f"일일 한도: 최대 {DAILY_LIMIT}건")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

        if not WORDPRESS_USER or not WORDPRESS_APP_PASSWORD:
            print("❌ WP_USER / WP_APP_PASSWORD 환경변수 필요")
            sys.exit(1)

        articles = self.fetch_rss_feed()
        if not articles:
            print("✅ 처리할 기사 없음 (모두 게시 완료)")
            return

        success = 0
        for i, article in enumerate(articles, 1):
            print(f"\n[{i}/{len(articles)}]")
            if self.process_article(article):
                success += 1
            time.sleep(3)

        print(f"\n{'='*60}")
        print(f"🏁 완료: {success}/{len(articles)}건 게시")
        print(f"{'='*60}\n")

        # 게시 기록 git 커밋 (캐시 유실 방지)
        if success > 0:
            self.commit_posted_articles()


if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
