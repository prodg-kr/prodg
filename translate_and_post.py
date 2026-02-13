#!/usr/bin/env python3
"""
pronews.jp 자동 번역 및 워드프레스 게시 시스템
- 소스: jp.pronews.com/feed
- 번역: Google Translate (일본어 → 한국어)
- 게시: prodg.kr WordPress
- 기능: 전체 본문 스크래핑, 이미지 본문 삽입
"""

import os
import sys
import requests
import feedparser
from datetime import datetime, timedelta
from pathlib import Path
import json
import time
from urllib.parse import urlparse, urljoin
from googletrans import Translator
import html2text
from bs4 import BeautifulSoup

# ==========================================
# 설정 (Settings)
# ==========================================
WORDPRESS_URL = "https://prodg.kr"
WORDPRESS_USER = os.environ.get("WP_USER")
WORDPRESS_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
PRONEWS_RSS = "https://jp.pronews.com/feed"
POSTED_ARTICLES_FILE = "posted_articles.json"

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
                    return json.load(f)
                except:
                    return []
        return []
        
    def save_posted_articles(self):
        """게시된 기사 목록 저장"""
        with open(POSTED_ARTICLES_FILE, 'w') as f:
            json.dump(self.posted_articles, f, indent=2)
        
    def fetch_rss_feed(self):
        """RSS 피드 가져오기"""
        print(f"📡 RSS 피드 확인 중: {PRONEWS_RSS}")
        feed = feedparser.parse(PRONEWS_RSS)
        
        # 24시간 이내 기사만
        limit_date = datetime.now() - timedelta(days=1)
        recent_articles = []
        
        print(f"🔍 총 {len(feed.entries)}개의 피드 항목 검색 시작...")

        for entry in feed.entries[:20]:  # 최신 20개 체크
            # 중복 체크
            if not FORCE_UPDATE and entry.link in self.posted_articles:
                print(f"  ⏭️  Pass (이미 게시됨): {entry.title}")
                continue
                
            try:
                article_date = datetime(*entry.published_parsed[:6])
            except:
                article_date = datetime.now()
                
            if article_date > limit_date:
                recent_articles.append({
                    'title': entry.title,
                    'link': entry.link,
                    'date': article_date
                })
        
        print(f"✅ 처리할 새 기사: {len(recent_articles)}개")
        return recent_articles
        
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

    def post_to_wordpress(self, title, content, featured_media_id):
        """워드프레스 포스트 생성"""
        post_data = {
            'title': title,
            'content': content,
            'status': 'publish',
            'featured_media': featured_media_id if featured_media_id else 0
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
        print(f"📰 처리 시작: {article['title']}")
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
        
        # 이미지가 있으면 본문 최상단에 삽입
        if uploaded_img_url:
            final_content += f'<figure style="margin: 0 0 30px 0;">'
            final_content += f'<img src="{uploaded_img_url}" alt="{title_ko}" style="width:100%; height:auto; display:block;" />'
            final_content += f'</figure>\n\n'
        
        # 본문 내용 (줄바꿈 HTML 처리)
        final_content += content_ko.replace("\n", "<br>\n")
        
        # 원문 링크 추가
        final_content += f"\n\n<hr style='margin: 40px 0 20px 0;'>\n"
        final_content += f"<p style='font-size: 14px; color: #666;'>"
        final_content += f"ℹ️ <strong>원문 기사 보기:</strong> "
        final_content += f"<a href='{article['link']}' target='_blank' rel='noopener'>{article['title']}</a>"
        final_content += f"</p>"
        
        # 5. 워드프레스에 게시
        print(f"📤 워드프레스 게시 중...")
        if self.post_to_wordpress(title_ko, final_content, featured_id):
            if not FORCE_UPDATE:
                self.posted_articles.append(article['link'])
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

        # RSS 피드에서 기사 가져오기
        articles = self.fetch_rss_feed()
        
        if not articles:
            print("ℹ️  새로운 기사가 없습니다.")
            return
        
        # 각 기사 처리
        success_count = 0
        for article in articles[:10]:  # 하루 최대 10개
            if self.process_article(article):
                success_count += 1
            time.sleep(3)  # 서버 부하 방지
            
        print(f"\n{'='*70}")
        print(f"🏁 작업 완료: {success_count}/{len(articles)}개 기사 게시됨")
        print(f"{'='*70}\n")

if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
