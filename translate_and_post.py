#!/usr/bin/env python3
"""
pronews.jp 자동 번역 및 워드프레스 게시 시스템
- 소스: jp.pronews.com WordPress API
- 번역: Google Translate (일본어 → 한국어)
- 게시: prodg.kr WordPress
- 기능: 전체 본문 스크래핑, 이미지 본문 삽입
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import time
from urllib.parse import urlparse, urljoin
from googletrans import Translator
import html2text
from bs4 import BeautifulSoup
import re

# ==========================================
# 설정 (Settings)
# ==========================================
WORDPRESS_URL = "https://prodg.kr"
WORDPRESS_USER = os.environ.get("WP_USER")
WORDPRESS_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
PRONEWS_POSTS_API = "https://jp.pronews.com/wp-json/wp/v2/posts"
POSTED_ARTICLES_FILE = "posted_articles.json"
SOURCE_TZ = timezone(timedelta(hours=9))
DAILY_POST_LIMIT = max(1, int(os.environ.get("DAILY_POST_LIMIT", "10")))
SOURCE_SCAN_MAX_PAGES = max(1, int(os.environ.get("SOURCE_SCAN_MAX_PAGES", "60")))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

# 중복 게시 방지 (False로 설정하면 이미 올린 글은 건너뜀)
FORCE_UPDATE = False

class NewsTranslator:
    def __init__(self):
        self.translator = Translator()
        self.wordpress_api = f"{WORDPRESS_URL}/wp-json/wp/v2"
        self.posted_articles = self.load_posted_articles()
        
    def load_posted_articles(self):
        """이미 게시된 기사 목록 로드"""
        if Path(POSTED_ARTICLES_FILE).exists():
            with open(POSTED_ARTICLES_FILE, 'r') as f:
                try:
                    data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
                    if isinstance(data, dict):
                        return set(data.keys())
                    return set()
                except Exception:
                    return set()
        return set()
        
    def save_posted_articles(self):
        """게시된 기사 목록 저장"""
        with open(POSTED_ARTICLES_FILE, 'w') as f:
            json.dump(sorted(self.posted_articles), f, indent=2, ensure_ascii=False)

    def normalize_source_url(self, raw_url):
        """원문 도메인을 jp.pronews.com으로 정규화"""
        if not raw_url:
            return ""

        normalized = raw_url.strip()
        if not normalized.startswith(("http://", "https://")):
            normalized = f"https://{normalized.lstrip('/')}"

        parsed = urlparse(normalized)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        if netloc in {"pronews.jp", "www.pronews.jp", "ko.pronews.com"}:
            netloc = "jp.pronews.com"

        rebuilt = parsed._replace(netloc=netloc).geturl()
        return rebuilt

    def normalize_pronews_domains_in_text(self, text):
        """번역 중 잘못 바뀐 pronews 도메인 복구"""
        if not text:
            return text

        fixed = text
        fixed = re.sub(r"https?://ko\.pronews\.com", "https://jp.pronews.com", fixed)
        fixed = re.sub(r"https?://(?:www\.)?pronews\.jp", "https://jp.pronews.com", fixed)
        return fixed

    def parse_source_datetime(self, date_text=None, date_gmt_text=None):
        """
        원문 게시 시각 파싱.
        - date_gmt가 있으면 UTC 기준으로 파싱
        - 없으면 date를 JST/KST(+09:00)로 처리
        """
        try:
            if date_gmt_text:
                dt = datetime.fromisoformat(date_gmt_text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(SOURCE_TZ)
            if date_text:
                dt = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=SOURCE_TZ)
                return dt.astimezone(SOURCE_TZ)
        except Exception:
            pass
        return datetime.now(SOURCE_TZ)

    def to_wordpress_dates(self, source_dt):
        """WordPress 게시용 date/date_gmt 생성"""
        local_dt = source_dt.astimezone(SOURCE_TZ)
        gmt_dt = source_dt.astimezone(timezone.utc)
        return (
            local_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            gmt_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        
    def fetch_source_articles(self):
        """원문 WordPress API에서 최신순 기사 수집 (미게시 우선)"""
        print(f"📡 원문 API 확인 중: {PRONEWS_POSTS_API}")
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; proDG-bot/1.0)"
        }
        collected = []
        seen_links = set()

        for page in range(1, SOURCE_SCAN_MAX_PAGES + 1):
            try:
                params = {
                    "per_page": 100,
                    "page": page,
                    "orderby": "date",
                    "order": "desc",
                    "_fields": "date,date_gmt,link,title",
                }
                res = requests.get(
                    PRONEWS_POSTS_API,
                    params=params,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )

                # 마지막 페이지 이후 요청 시 WordPress가 400을 반환하는 경우가 있음
                if res.status_code == 400:
                    print(f"   ℹ️ 페이지 {page} 이후 기사 없음")
                    break

                res.raise_for_status()
                posts = res.json()
                if not posts:
                    break

                print(f"   🔎 페이지 {page}: {len(posts)}개 확인")
                for post in posts:
                    link = self.normalize_source_url(post.get("link", ""))
                    if not link or link in seen_links:
                        continue
                    seen_links.add(link)

                    if not FORCE_UPDATE and link in self.posted_articles:
                        continue

                    title_html = post.get("title", {}).get("rendered", "")
                    title_text = BeautifulSoup(title_html, "lxml").get_text(" ", strip=True)
                    article_date = self.parse_source_datetime(
                        post.get("date"),
                        post.get("date_gmt")
                    )

                    collected.append({
                        "title": title_text or "제목 없음",
                        "link": link,
                        "date": article_date,
                    })

                    if len(collected) >= DAILY_POST_LIMIT:
                        break

                if len(collected) >= DAILY_POST_LIMIT:
                    break

            except Exception as e:
                print(f"⚠️ 원문 목록 수집 실패 (page={page}): {e}")
                break

        # 최신 기사부터 게시되도록 날짜 내림차순 보장
        collected.sort(key=lambda x: x["date"], reverse=True)
        print(f"✅ 처리할 기사: {len(collected)}개 (일일 한도: {DAILY_POST_LIMIT})")
        return collected[:DAILY_POST_LIMIT]
        
    def fetch_full_content(self, url):
        """
        BeautifulSoup을 사용하여 실제 기사 본문 전체 스크래핑
        """
        try:
            print(f"📄 기사 원문 스크래핑 중: {url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # pronews.com의 본문 영역 찾기
            # 일반적인 워드프레스 구조: entry-content, post-content, article-content 등
            content_div = soup.find('div', class_='entry-content')
            if not content_div:
                content_div = soup.find('div', class_='post-content')
            if not content_div:
                content_div = soup.find('div', class_='article-content')
            if not content_div:
                content_div = soup.find('article')
                
            if not content_div:
                print("⚠️ 본문 영역을 찾지 못했습니다.")
                return None

            # 불필요한 태그 제거 (스크립트, 스타일, 광고 등)
            for tag in content_div(['script', 'style', 'iframe', 'noscript', 'form', 'nav']):
                tag.decompose()
            
            # 광고 클래스 제거
            for ad_class in ['ad', 'advertisement', 'banner', 'sidebar']:
                for elem in content_div.find_all(class_=lambda x: x and ad_class in x.lower()):
                    elem.decompose()
                
            # HTML 문자열 반환
            return str(content_div)
            
        except Exception as e:
            print(f"⚠️ 본문 가져오기 실패: {e}")
            return None

    def translate_text(self, text):
        """번역 함수 (긴 텍스트 자동 분할 처리)"""
        if not text: 
            return ""
        
        try:
            # HTML을 텍스트로 변환
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True  # 이미지는 별도 처리
            h.body_width = 0  # 줄바꿈 방지
            plain_text = h.handle(text)
            
            # 너무 길면 청크로 나눠서 번역 (Google API 제한 대비)
            max_chunk_size = 4000
            if len(plain_text) > max_chunk_size:
                print(f"   📏 긴 텍스트 감지 ({len(plain_text)}자) - 분할 번역 시작")
                chunks = [plain_text[i:i+max_chunk_size] for i in range(0, len(plain_text), max_chunk_size)]
                translated_parts = []
                
                for i, chunk in enumerate(chunks, 1):
                    print(f"   🔄 청크 {i}/{len(chunks)} 번역 중...")
                    res = self.translator.translate(chunk, src='ja', dest='ko')
                    translated_parts.append(res.text)
                    time.sleep(1.5)  # API 제한 방지
                    
                return "\n\n".join(translated_parts)
            else:
                result = self.translator.translate(plain_text, src='ja', dest='ko')
                time.sleep(0.8)
                return result.text
                
        except Exception as e:
            print(f"⚠️ 번역 중 오류 발생: {e}")
            return text  # 실패 시 원문 반환

    def download_image(self, url):
        """이미지 다운로드"""
        if not url: 
            return None
        try:
            print(f"🖼️  이미지 다운로드: {url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            res = requests.get(url, headers=headers, timeout=15)
            res.raise_for_status()
            
            # 파일명 처리
            filename = os.path.basename(urlparse(url).path)
            if not filename or len(filename) > 100:
                filename = f"image_{int(time.time())}.jpg"
            
            # 쿼리 파라미터 제거
            if '?' in filename:
                filename = filename.split('?')[0]
                
            path = Path(f"/tmp/{filename}")
            with open(path, 'wb') as f:
                f.write(res.content)
            
            print(f"   ✅ 저장 완료: {path.name}")
            return path
            
        except Exception as e:
            print(f"⚠️ 이미지 다운로드 에러: {e}")
        return None

    def upload_media(self, image_path):
        """워드프레스 미디어 업로드"""
        if not image_path or not image_path.exists(): 
            return None
        try:
            url = f"{self.wordpress_api}/media"
            headers = {
                'Content-Disposition': f'attachment; filename={image_path.name}'
            }
            with open(image_path, 'rb') as img:
                files = {'file': (image_path.name, img, 'image/jpeg')}
                res = requests.post(
                    url,
                    auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                    headers=headers,
                    files=files
                )
                res.raise_for_status()
                media_data = res.json()
                print(f"   ✅ 업로드 완료: ID {media_data['id']}")
                return media_data  # {id, source_url, ...}
                
        except Exception as e:
            print(f"⚠️ 이미지 업로드 실패: {e}")
            if hasattr(e, 'response'):
                print(f"   상세: {e.response.text[:200]}")
        return None

    def get_main_image_url(self, link):
        """Open Graph 등을 통해 대표 이미지 URL 추출"""
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(link, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'lxml')
            
            # 1. Open Graph 이미지
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                img_url = og_img['content']
                print(f"   📸 OG 이미지 발견")
                return img_url
            
            # 2. Twitter Card 이미지
            tw_img = soup.find('meta', attrs={'name': 'twitter:image'})
            if tw_img and tw_img.get('content'):
                img_url = tw_img['content']
                print(f"   📸 Twitter Card 이미지 발견")
                return img_url
            
            # 3. 본문 첫 이미지
            content = soup.find('div', class_='entry-content')
            if content:
                img = content.find('img')
                if img and img.get('src'):
                    img_url = img['src']
                    # 상대 경로를 절대 경로로
                    if not img_url.startswith('http'):
                        img_url = urljoin(link, img_url)
                    print(f"   📸 본문 이미지 발견")
                    return img_url
            
        except Exception as e:
            print(f"⚠️ 이미지 검색 실패: {e}")
        return None

    def post_to_wordpress(self, title, content, featured_media_id, article_date):
        """워드프레스 포스트 생성"""
        post_date, post_date_gmt = self.to_wordpress_dates(article_date)
        post_data = {
            'title': title,
            'content': content,
            'status': 'publish',
            'featured_media': featured_media_id if featured_media_id else 0,
            'date': post_date,
            'date_gmt': post_date_gmt
        }
        
        try:
            res = requests.post(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                json=post_data
            )
            res.raise_for_status()
            post_info = res.json()
            print(f"✨ 게시 성공! 링크: {post_info['link']}")
            return True
            
        except Exception as e:
            print(f"❌ 게시 실패: {e}")
            if hasattr(e, 'response'):
                print(f"   상세: {e.response.text[:300]}")
            return False

    def process_article(self, article):
        """기사 하나 처리: 스크래핑 → 번역 → 이미지 → 게시"""
        print(f"\n{'='*70}")
        source_date = article["date"].astimezone(SOURCE_TZ)
        print(f"📰 처리 시작: {article['title']}")
        print(f"🕒 원문 게시시각: {source_date.strftime('%Y-%m-%d %H:%M:%S %z')}")
        print(f"{'='*70}")
        
        # 1. 본문 전체 가져오기
        raw_html = self.fetch_full_content(article['link'])
        if not raw_html:
            print("   ⚠️  본문을 가져오지 못해 건너뜁니다.")
            return False
            
        # 2. 번역 (제목 및 본문)
        print(f"🔄 제목 번역 중...")
        title_ko = self.translate_text(article['title'])
        print(f"   ✅ \"{title_ko}\"")
        
        print(f"🔄 본문 번역 중...")
        content_ko = self.translate_text(raw_html)
        print(f"   ✅ 본문 번역 완료 ({len(content_ko)}자)")
        
        # 3. 이미지 처리
        print(f"🔍 이미지 검색 중...")
        img_url = self.get_main_image_url(article['link'])
        featured_id = 0
        uploaded_img_url = ""
        
        if img_url:
            local_img = self.download_image(img_url)
            if local_img:
                media_info = self.upload_media(local_img)
                if media_info:
                    featured_id = media_info['id']
                    uploaded_img_url = media_info['source_url']
                    
                # 임시 파일 삭제
                try: 
                    local_img.unlink()
                except: 
                    pass
        else:
            print("   ℹ️  이미지 없음")

        # 4. 본문 구성 (이미지 삽입 + 원본 링크)
        final_content = ""
        normalized_source_link = self.normalize_source_url(article["link"])
        
        # 이미지가 있으면 본문 최상단에 삽입
        if uploaded_img_url:
            final_content += f'<figure style="margin: 0 0 30px 0;">'
            final_content += f'<img src="{uploaded_img_url}" alt="{title_ko}" style="width:100%; height:auto; display:block;" />'
            final_content += f'</figure>\n\n'

        # 본문 메타 + 본문 내용
        final_content += "<div class='pronews-kr-article' style='font-family: \"Noto Sans KR\", sans-serif; line-height:1.85; font-size:17px;'>"
        final_content += "<div style='border-top:2px solid #111; border-bottom:1px solid #ddd; padding:10px 0; margin:0 0 24px 0;'>"
        final_content += f"<p style='margin:0; color:#555; font-size:13px;'>원문 게시시각: {source_date.strftime('%Y-%m-%d %H:%M')} (JST)</p>"
        final_content += f"<p style='margin:6px 0 0 0; color:#111; font-size:13px;'>출처: <a href='{normalized_source_link}' target='_blank' rel='noopener'>jp.pronews.com</a></p>"
        final_content += "</div>"
        final_content += self.normalize_pronews_domains_in_text(content_ko).replace("\n", "<br>\n")
        final_content += "</div>"

        # 원문 링크 추가
        final_content += f"\n\n<hr style='margin: 40px 0 20px 0;'>\n"
        final_content += f"<p style='font-size: 14px; color: #666;'>"
        final_content += f"ℹ️ <strong>원문 기사 보기:</strong> "
        final_content += f"<a href='{normalized_source_link}' target='_blank' rel='noopener'>{article['title']}</a>"
        final_content += f"</p>"
        
        # 5. 워드프레스에 게시
        print(f"📤 워드프레스 게시 중...")
        if self.post_to_wordpress(title_ko, final_content, featured_id, article["date"]):
            if not FORCE_UPDATE:
                self.posted_articles.add(normalized_source_link)
                self.save_posted_articles()
            return True
        return False

    def run(self):
        """메인 실행 함수"""
        print(f"\n{'🚀'*35}")
        print(f"  pronews.jp 자동 번역 시스템 시작")
        print(f"  실행 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'🚀'*35}\n")
        
        # 환경 변수 확인
        if not WORDPRESS_USER or not WORDPRESS_APP_PASSWORD:
            print("❌ 환경 변수 설정 필요!")
            print("   WP_USER와 WP_APP_PASSWORD를 GitHub Secrets에 추가하세요.")
            sys.exit(1)

        # 원문 WordPress API에서 기사 가져오기
        articles = self.fetch_source_articles()
        
        if not articles:
            print("ℹ️  새로운 기사가 없습니다.")
            return
        
        # 각 기사 처리
        success_count = 0
        for article in articles:
            if self.process_article(article):
                success_count += 1
            time.sleep(3)  # 서버 부하 방지
            
        print(f"\n{'='*70}")
        print(f"🏁 작업 완료: {success_count}/{len(articles)}개 기사 게시됨")
        print(f"{'='*70}\n")

if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
