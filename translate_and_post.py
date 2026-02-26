#!/usr/bin/env python3
"""
pronews.jp 자동 번역 시스템 v6
파이프라인: 일본어 원문 → Gemini 번역+SEO편집 통합 → WordPress 게시

v5 → v6 변경사항:
- Groq 제거 → Gemini 단일 엔진으로 통합
  (Groq llama-3.3-70b의 일본어→한국어 품질 문제 해결)
- 번역+SEO편집을 단일 프롬프트로 처리 (문맥 일관성 향상)
- 제목 잘림 문제 수정: 제품명 포함 시 50자까지 허용
- 일본어 잔존 감지 후 재번역 안전망 추가
- POST_STATUS: publish / draft 선택 가능
- excerpt 자동 생성
- 중복 방지: posted_articles.json + WordPress API 2중 체크
- 게시 후 posted_articles.json git 자동 커밋
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
POSTED_ARTICLES_FILE   = "posted_articles.json"
FORCE_UPDATE           = os.environ.get("FORCE_UPDATE", "false").lower() == "true"
DAILY_LIMIT            = 10  # 하루 최대 게시 건수

# 게시 상태: publish(즉시공개) / draft(임시저장 후 수동 검수)
POST_STATUS      = os.environ.get("POST_STATUS", "publish")
GENERATE_EXCERPT = True  # WordPress SEO용 요약문 자동 생성

# 모델 설정 (환경변수로 교체 가능)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ==========================================
# Gemini 통합 엔진 (번역 + SEO 편집)
# ==========================================
class GeminiEngine:
    """
    Gemini 단일 엔진으로 번역+SEO편집 통합 처리
    - 일본어 → 한국어 번역 (Groq 대비 품질 대폭 향상)
    - SEO 최적화 제목 재작성
    - 합쇼체(~합니다) 문체 통일
    - 전문용어 정확성 보정
    - excerpt 생성
    """

    def __init__(self):
        self.api_key = GEMINI_API_KEY
        if not self.api_key:
            print("❌ GEMINI_API_KEY 미설정")
            sys.exit(1)

    def _call_api(self, prompt: str, max_tokens: int = 4096) -> str:
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
        try:
            res = requests.post(url, json=payload, timeout=90)
            res.raise_for_status()
            candidates = res.json().get("candidates", [])
            if candidates:
                return candidates[0]["content"]["parts"][0]["text"].strip()
            return ""
        except Exception as e:
            print(f"⚠️ Gemini API 오류: {e}")
            return ""

    def translate_and_edit_title(self, title_ja: str) -> str:
        """
        제목 번역 + SEO 편집 통합
        - 제품명/모델명 절대 잘리지 않도록 보호
        - 제품명 포함 시 50자까지 허용, 일반 제목은 35자 내외
        """
        prompt = f"""당신은 영상/카메라 전문 미디어의 SEO 에디터입니다.

일본어 제목: {title_ja}

위 제목을 한국어로 번역하고 구글 SEO에 최적화하세요.

규칙:
1. Sony, Canon, Nikon, DJI, Blackmagic, NIKKOR, LUMIX, FUJIFILM 등 브랜드명/제품명/모델명은 원문 그대로 유지하고 절대 생략하지 마세요
2. 모델 번호(예: NIKKOR Z 70-200mm f/2.8 VR S II)가 있으면 반드시 전체 포함
3. 검색 핵심 키워드를 앞쪽에 배치
4. 자연스러운 한국어 (직역체, 어색한 조사 금지)
5. 제품명 없는 경우 35자 내외, 제품명 포함 시 50자까지 허용
6. 제목만 출력 (설명, 따옴표, 번호 없음)"""

        result = self._call_api(prompt, max_tokens=150)
        if result:
            result = re.sub(r'^[\d\.\)\-\s"\'「」【】]+', '', result).strip().strip('"\'「」【】')
            print(f"   📌 번역 제목: {result}")
            return result
        return title_ja

    def translate_and_edit_content(self, html_content: str) -> str:
        """
        본문 번역 + SEO 편집 통합 (Gemini 단일 처리)
        - 일본어 → 한국어 번역 (문맥 일관성 유지)
        - 직역체 → 자연스러운 합쇼체
        - HTML 태그 구조 유지
        - 일본어 잔존 시 재번역
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

        if not paragraphs:
            return html_content

        # 청크 단위 번역+편집 (Gemini 토큰 한도 고려, 청크당 3000자)
        translated_paragraphs = []
        chunk, chunk_size = [], 0

        for para in paragraphs:
            if chunk_size + len(para) > 3000 and chunk:
                result = self._translate_chunk('\n\n'.join(chunk))
                translated_paragraphs.extend(result.split('\n\n'))
                chunk, chunk_size = [], 0
                time.sleep(1)
            chunk.append(para)
            chunk_size += len(para)

        if chunk:
            result = self._translate_chunk('\n\n'.join(chunk))
            translated_paragraphs.extend(result.split('\n\n'))

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

        # 헤더 태그 복원 (헤더 텍스트도 번역)
        for placeholder, info in headers_map.items():
            if placeholder in translated_html:
                header_ko = self._translate_single(info['text']) if info['text'] else info['text']
                translated_html = translated_html.replace(
                    placeholder,
                    f"<{info['tag']}>{header_ko}</{info['tag']}>"
                )

        # 이미지 태그 복원
        for placeholder, img_tag in images.items():
            translated_html = translated_html.replace(placeholder, img_tag)

        # 일본어 잔존 검사 → 잔존 시 재번역
        if self._has_japanese(translated_html):
            print("   ⚠️ 일본어 잔존 감지 → 재번역 시도...")
            translated_html = self._cleanup_japanese(translated_html)

        return translated_html

    def _translate_chunk(self, text: str) -> str:
        """텍스트 청크 번역 + SEO 편집 통합 프롬프트"""
        if not text.strip():
            return text

        prompt = f"""당신은 영상/카메라 전문 미디어의 한국어 에디터입니다.

아래 일본어 텍스트를 한국어로 번역하고 자연스럽게 편집하세요.

번역+편집 규칙:
1. 일본어를 완전히 한국어로 번역 (히라가나·가타카나·한자 단어 절대 남기지 말 것)
2. 문체는 반드시 '~합니다', '~했습니다', '~입니다' 합쇼체로 통일
   ('~한다', '~했다', '~이다' 평서체 사용 금지)
3. 직역체, 어색한 조사, 일본식 표현을 자연스러운 한국어로 수정
4. 영상/카메라 전문용어 정확히 표기:
   - 브랜드명: Sony, Canon, Nikon, DJI, Blackmagic, DaVinci Resolve 등 원문 유지
   - 해상도: 4K, 8K, Full HD / 프레임레이트: fps, 24p, 60p
   - 기타: 코덱, 비트레이트, 조리개, 셔터스피드, 보케, 손떨림보정 등
5. ___IMG_0___, ___H0_h2___ 같은 플레이스홀더는 절대 변경하지 말 것
6. 단락 구조 유지 (빈 줄로 단락 구분)
7. 번역된 텍스트만 출력 (설명 없음)

일본어 텍스트:
{text}"""

        result = self._call_api(prompt, max_tokens=4096)
        return result if result else text

    def _translate_single(self, text: str) -> str:
        """단일 짧은 텍스트 번역 (헤더용)"""
        if not text.strip():
            return text
        prompt = f"다음 일본어를 자연스러운 한국어 합쇼체로 번역하세요. 번역문만 출력:\n{text}"
        result = self._call_api(prompt, max_tokens=200)
        return result if result else text

    def _has_japanese(self, text: str) -> bool:
        """일본어(히라가나·가타카나) 잔존 여부 검사"""
        japanese_pattern = re.compile(r'[\u3040-\u309f\u30a0-\u30ff]')
        plain_text = BeautifulSoup(text, 'lxml').get_text()
        matches = japanese_pattern.findall(plain_text)
        return len(matches) > 5  # 5자 이상 일본어 잔존 시 재번역

    def _cleanup_japanese(self, html: str) -> str:
        """일본어 잔존 부분만 재번역"""
        soup = BeautifulSoup(html, 'lxml')
        for elem in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'li']):
            text = elem.get_text()
            if re.search(r'[\u3040-\u309f\u30a0-\u30ff]', text):
                translated = self._translate_single(text)
                if translated:
                    elem.string = translated
        return str(soup.find('body') or soup)

    def generate_excerpt(self, title_ko: str, content_ko: str) -> str:
        """
        WordPress SEO용 요약문(excerpt) 생성
        - 80자 내외, 검색결과 스니펫에 최적화
        """
        soup = BeautifulSoup(content_ko, 'lxml')
        plain_text = soup.get_text(separator=' ', strip=True)[:500]

        prompt = f"""당신은 SEO 전문 에디터입니다.

기사 제목: {title_ko}
본문 일부: {plain_text}

구글 검색결과에 노출될 요약문(메타 디스크립션)을 작성하세요.

규칙:
1. 80자 내외 (최대 100자)
2. 핵심 키워드 자연스럽게 포함
3. 독자가 클릭하고 싶어지는 문장
4. ~합니다 합쇼체로 작성
5. 요약문만 출력 (설명 없음)"""

        result = self._call_api(prompt, max_tokens=150)
        if result:
            result = result.strip().strip('"\'')
            print(f"   📋 요약문: {result[:60]}...")
            return result
        return ""


# ==========================================
# 메인 번역 시스템
# ==========================================
class NewsTranslator:
    def __init__(self):
        self.gemini = GeminiEngine()
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
        - 최신 기사 부족 시 과거 미게시 기사로 채워 최대 10건 반환
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

        unposted.sort(key=lambda x: x['date'], reverse=True)
        target = unposted[:DAILY_LIMIT]

        print(f"✅ 미게시: {len(unposted)}건 → 오늘 처리: {len(target)}건 (최대 {DAILY_LIMIT}건)")
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
                               'この記事をシェア', '이 기사 공유', 'FOLLOW US',
                               '関連記事', '관련 기사']
            for h_tag in content_div.find_all(['h2', 'h3', 'h4']):
                if any(kw in h_tag.get_text(strip=True) for kw in remove_headings):
                    next_elem = h_tag.find_next_sibling()
                    h_tag.decompose()
                    while next_elem and next_elem.name not in ['h1', 'h2', 'h3', 'h4']:
                        temp = next_elem.find_next_sibling()
                        next_elem.decompose()
                        next_elem = temp

            for tag in content_div(['script', 'style', 'iframe', 'noscript',
                                    'form', 'nav', 'aside', 'footer', 'header']):
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
        """WordPress에서 원문 URL 기준 중복 게시 여부 확인"""
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
        """posted_articles.json git 커밋 (캐시 유실 방지)"""
        try:
            import subprocess
            subprocess.run(['git', 'config', 'user.email', 'action@github.com'], check=True)
            subprocess.run(['git', 'config', 'user.name', 'GitHub Action'], check=True)
            subprocess.run(['git', 'add', POSTED_ARTICLES_FILE], check=True)
            result = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True)
            if result.returncode != 0:
                subprocess.run(
                    ['git', 'commit', '-m', f'chore: update posted_articles [{datetime.now().strftime("%Y-%m-%d %H:%M")}]'],
                    check=True
                )
                subprocess.run(['git', 'push'], check=True)
                print("📝 posted_articles.json → git 커밋 완료")
        except Exception as e:
            print(f"⚠️ git 커밋 실패 (캐시로 대체): {e}")

    def post_to_wordpress(self, title: str, content: str, slug: str,
                           featured_media_id: int, original_date: datetime,
                           excerpt: str = "", status: str = "publish") -> bool:
        post_data = {
            'title': title,
            'content': content,
            'slug': slug,
            'status': status,
            'featured_media': featured_media_id or 0,
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
            post_info = res.json()
            label = "📝 임시저장" if status == "draft" else "✨ 게시 성공"
            print(f"{label}: {post_info['link']}")
            return True
        except Exception as e:
            print(f"❌ 게시 실패: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   {e.response.text[:300]}")
            return False

    def process_article(self, article: dict) -> bool:
        print(f"\n{'='*60}")
        print(f"📰 {article['title'][:70]}")
        print(f"📅 {article['date'].strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")

        # 1. 중복 체크 (2중 안전망)
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

        # 3. Gemini 제목 번역 + SEO 편집
        print("🔄 [1단계] Gemini 제목 번역+편집 중...")
        title_ko = self.gemini.translate_and_edit_title(article['title'])

        # 4. Gemini 본문 번역 + SEO 편집
        print("✏️  [2단계] Gemini 본문 번역+편집 중...")
        content_ko = self.gemini.translate_and_edit_content(raw_html)

        # 5. excerpt 생성
        excerpt = ""
        if GENERATE_EXCERPT:
            print("📋 [3단계] excerpt 생성 중...")
            excerpt = self.gemini.generate_excerpt(title_ko, content_ko)
            time.sleep(1)

        # 6. Slug 생성
        slug = self.generate_slug(article['title'], article['date'])
        print(f"🔗 Slug: {slug}")

        # 7. 이미지 처리
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

        # 8. 최종 본문 구성 + 원문 출처
        final_content = content_ko
        final_content += (
            "\n\n<hr style='margin:40px 0 20px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
            f"<p style='font-size:13px;color:#777;'>"
            f"<strong>원문:</strong> "
            f"<a href='{article['link']}' target='_blank' rel='noopener'>{article['title']}</a>"
            f"</p>"
        )

        # 9. WordPress 게시
        label = "draft(임시저장)" if POST_STATUS == "draft" else "publish(즉시공개)"
        print(f"📤 [4단계] WordPress {label} 중...")
        if self.post_to_wordpress(title_ko, final_content, slug, featured_id,
                                   article['date'], excerpt=excerpt, status=POST_STATUS):
            if not FORCE_UPDATE:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return True
        return False

    def run(self):
        print(f"\n{'='*60}")
        print(f"pronews.jp → prodg.kr 자동 번역 v6")
        print(f"엔진: Gemini 단일 ({GEMINI_MODEL})")
        print(f"게시: {POST_STATUS.upper()} ({'즉시 공개' if POST_STATUS == 'publish' else '임시저장 → 수동 검수'})")
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

        if success > 0:
            self.commit_posted_articles()


if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
