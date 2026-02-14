#!/usr/bin/env python3
"""
pronews.jp 자동 번역 시스템 v3 (최종)
개선사항:
1. 최신 기사부터 10건씩 번역 (오래된 기사는 나중에)
2. 원문 게시시각, 출처 텍스트 제거
3. 영문 slug + 불필요 콘텐츠 제거
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
import hashlib
import re

# ==========================================
# 설정
# ==========================================
WORDPRESS_URL = "https://prodg.kr"
WORDPRESS_USER = os.environ.get("WP_USER")
WORDPRESS_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
PRONEWS_RSS = "https://jp.pronews.com/feed"
POSTED_ARTICLES_FILE = "posted_articles.json"
FORCE_UPDATE = os.environ.get("FORCE_UPDATE", "false").lower() == "true"

class NewsTranslator:
    def __init__(self):
        self.translator = Translator()
        self.wordpress_api = f"{WORDPRESS_URL}/wp-json/wp/v2"
        self.posted_articles = self.load_posted_articles()
        
    def load_posted_articles(self):
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
        
    def fetch_rss_feed(self):
        """
        [개선 1] 최신 기사부터 모두 처리 (제한 없음)
        """
        print(f"📡 RSS 피드 확인 중: {PRONEWS_RSS}")
        feed = feedparser.parse(PRONEWS_RSS)
        
        all_articles = []
        print(f"🔍 총 {len(feed.entries)}개의 피드 항목 검색...")

        for entry in feed.entries:
            if not FORCE_UPDATE and entry.link in self.posted_articles:
                continue
                
            try:
                article_date = datetime(*entry.published_parsed[:6])
            except:
                article_date = datetime.now()
                
            all_articles.append({
                'title': entry.title,
                'link': entry.link,
                'date': article_date
            })
        
        # [개선 1] 최신순 정렬 (역순)
        all_articles.sort(key=lambda x: x['date'], reverse=True)
        
        print(f"✅ 처리할 최신 기사: {len(all_articles)}개 (제한 없음)")
        return all_articles  # 모든 기사 반환
        
    def fetch_full_content(self, url):
        """
        [개선 2] 본문 스크래핑 + 불필요한 요소 제거
        """
        try:
            print(f"📄 스크래핑: {url}")
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'lxml')
            
            # pronews.com의 본문 영역 찾기
            content_div = soup.find('div', class_='entry-content')
            if not content_div:
                content_div = soup.find('div', class_='post-content')
            if not content_div:
                content_div = soup.find('div', class_='article-content')
            if not content_div:
                content_div = soup.find('article')
                
            if not content_div:
                return None

            # [개선 2] "원문 게시시각", "출처" 텍스트 제거
            for elem in content_div.find_all(string=re.compile(r'원문 게시시각:|출처:|原文掲載時刻:|ソース:|バックナンバー|関連キーワード|この記事をシェア|FOLLOW US')):
                parent = elem.find_parent()
                if parent:
                    # 해당 문단 전체 제거
                    parent.decompose()
            
            # h3 제목이 "백 넘버", "관련 키워드", "이 기사 공유" 등인 섹션 제거
            for h_tag in content_div.find_all(['h3', 'h2', 'h4']):
                h_text = h_tag.get_text(strip=True)
                if any(keyword in h_text for keyword in ['백 넘버', '関連キーワード', 'バックナンバー', 
                                                          'この記事をシェア', '이 기사 공유', 'FOLLOW US',
                                                          '関連記事', '관련 기사']):
                    # h 태그 다음의 모든 형제 요소도 제거 (섹션 전체)
                    next_elem = h_tag.find_next_sibling()
                    h_tag.decompose()
                    while next_elem and next_elem.name not in ['h1', 'h2', 'h3', 'h4']:
                        temp = next_elem.find_next_sibling()
                        next_elem.decompose()
                        next_elem = temp

            # 불필요한 태그 완전 제거
            for tag in content_div(['script', 'style', 'iframe', 'noscript', 'form', 
                                   'nav', 'aside', 'footer', 'header']):
                tag.decompose()
            
            # 소셜 공유 버튼 제거 (클래스명 기반)
            for social_class in ['social-share', 'share-buttons', 'sns-share', 'social-links', 
                                'share-links', 'addtoany', 'sharedaddy', 'jp-relatedposts',
                                'entry-footer', 'post-tags', 'post-categories', 'post-meta']:
                for elem in content_div.find_all(class_=lambda x: x and any(sc in str(x).lower() for sc in [social_class])):
                    elem.decompose()
            
            # 특정 텍스트 포함 요소 제거
            remove_keywords = [
                'FOLLOW US', '관련 기사', 'Related', 'Share this', 'Tweet',
                '뉴스 일람', '칼럼 타이틀', '특집 타이틀', '라이터 목록',
                'facebook.com', 'twitter.com', 'line.me', 'instagram.com',
                'youtube.com', 'pronews.jp', 'kr.pronews.com', '/fellowship/',
                'getpocket.com', 'hatena.ne.jp', '/feed', '/news/', '/columntitle/',
                '/specialtitle/', '/writer/', 'jp.pronews.com'
            ]
            
            # a 태그 제거 (본문 외부 링크)
            for a in list(content_div.find_all('a')):
                href = a.get('href', '')
                text = a.get_text(strip=True)
                
                # 제거 조건
                should_remove = any([
                    any(kw in href.lower() for kw in remove_keywords),
                    any(kw in text for kw in ['FOLLOW', 'Share', 'Tweet', 'More', 'Read more']),
                    href.startswith('//www.facebook.com'),
                    href.startswith('//twitter.com'),
                    href.startswith('//line.me'),
                    href.startswith('//'),  # 프로토콜 없는 외부 링크
                    not text  # 빈 링크
                ])
                
                if should_remove:
                    a.decompose()
            
            # 빈 태그 제거
            for tag_name in ['p', 'div', 'span', 'li', 'ul', 'ol']:
                for tag in content_div.find_all(tag_name):
                    if not tag.get_text(strip=True) and not tag.find('img'):
                        tag.decompose()
            
            # 연속된 br 태그 정리
            for br in content_div.find_all('br'):
                next_sibling = br.find_next_sibling()
                if next_sibling and next_sibling.name == 'br':
                    br.decompose()
                    
            return str(content_div)
            
        except Exception as e:
            print(f"⚠️ 실패: {e}")
            return None

    def generate_english_slug(self, title):
        """영문 slug 생성"""
        # 간단한 키워드 추출 (첫 3-5단어)
        words = title.split()[:5]
        
        # 영문, 숫자만 추출
        slug_words = []
        for word in words:
            # 영문자, 숫자, 하이픈만 남김
            cleaned = re.sub(r'[^a-zA-Z0-9\-]', '', word.lower())
            if cleaned and len(cleaned) > 2:
                slug_words.append(cleaned)
        
        # slug 생성
        if slug_words:
            slug = '-'.join(slug_words[:4])  # 최대 4단어
        else:
            # 영문이 없으면 타임스탬프 기반
            slug = f"article-{int(time.time())}"
        
        # 길이 제한 (50자)
        return slug[:50]

    def translate_text(self, text):
        """
        [개선 2] 번역 + "원문 게시시각", "출처" 제거
        [개선 4] HTML 헤더 태그 유지
        """
        if not text: 
            return ""
        
        try:
            # BeautifulSoup으로 HTML 파싱
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, 'lxml')
            
            # h1~h6 태그를 임시로 저장
            headers = {}
            for i, tag in enumerate(soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
                placeholder = f"___HEADER_{i}___"
                headers[placeholder] = {
                    'tag': tag.name,
                    'class': tag.get('class', []),
                    'text': tag.get_text(strip=True)
                }
                tag.replace_with(placeholder)
            
            # HTML을 텍스트로 변환
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            plain_text = h.handle(str(soup))
            
            # [개선 2] "원문 게시시각:", "출처:" 텍스트 제거
            plain_text = re.sub(r'원문 게시시각:.*?\n', '', plain_text)
            plain_text = re.sub(r'出典:.*?\n', '', plain_text)
            plain_text = re.sub(r'ソース:.*?\n', '', plain_text)
            plain_text = re.sub(r'原文掲載時刻:.*?\n', '', plain_text)
            
            # 번역
            if len(plain_text) > 4000:
                chunks = [plain_text[i:i+4000] for i in range(0, len(plain_text), 4000)]
                translated_parts = []
                for chunk in chunks:
                    res = self.translator.translate(chunk, src='ja', dest='ko')
                    translated_parts.append(res.text)
                    time.sleep(1)
                translated_text = "\n\n".join(translated_parts)
            else:
                result = self.translator.translate(plain_text, src='ja', dest='ko')
                time.sleep(0.5)
                translated_text = result.text
            
            # 헤더 태그 복원
            for placeholder, header_info in headers.items():
                tag_name = header_info['tag']
                classes = ' '.join(header_info['class']) if header_info['class'] else ''
                
                # 플레이스홀더를 찾아서 번역
                if placeholder in translated_text:
                    # 원본 텍스트도 번역
                    try:
                        translated_header = self.translator.translate(header_info['text'], src='ja', dest='ko').text
                        time.sleep(0.3)
                    except:
                        translated_header = header_info['text']
                    
                    # HTML 태그로 복원
                    if classes:
                        replacement = f'<{tag_name} class="{classes}">{translated_header}</{tag_name}>'
                    else:
                        replacement = f'<{tag_name}>{translated_header}</{tag_name}>'
                    
                    translated_text = translated_text.replace(placeholder, replacement)
            
            return translated_text
            
        except Exception as e:
            print(f"⚠️ 번역 오류: {e}")
            return text

    def download_image(self, url):
        if not url: 
            return None
        try:
            print(f"🖼️ 다운로드: {url}")
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=15)
            res.raise_for_status()
            
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            timestamp = int(time.time())
            
            original_filename = os.path.basename(urlparse(url).path)
            if '?' in original_filename:
                original_filename = original_filename.split('?')[0]
            
            ext = os.path.splitext(original_filename)[1]
            if not ext or ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                ext = '.jpg'
            
            filename = f"pronews_{timestamp}_{url_hash}{ext}"
            path = Path(f"/tmp/{filename}")
            
            with open(path, 'wb') as f:
                f.write(res.content)
            
            print(f"   ✅ {filename}")
            return path
        except Exception as e:
            print(f"⚠️ 실패: {e}")
        return None

    def upload_media(self, image_path):
        if not image_path or not image_path.exists(): 
            return None
        try:
            url = f"{self.wordpress_api}/media"
            with open(image_path, 'rb') as img:
                files = {'file': (image_path.name, img, 'image/jpeg')}
                headers = {'Content-Disposition': f'attachment; filename={image_path.name}'}
                res = requests.post(
                    url,
                    auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                    headers=headers,
                    files=files
                )
                res.raise_for_status()
                return res.json()
        except Exception as e:
            print(f"⚠️ 업로드 실패: {e}")
        return None

    def get_main_image_url(self, link):
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
                    if not img_url.startswith('http'):
                        img_url = urljoin(link, img_url)
                    return img_url
        except:
            pass
        return None

    def post_to_wordpress(self, title, content, slug, featured_media_id, original_date):
        post_data = {
            'title': title,
            'content': content,
            'slug': slug,
            'status': 'publish',
            'featured_media': featured_media_id if featured_media_id else 0,
            'date': original_date.strftime('%Y-%m-%dT%H:%M:%S')
        }
        
        try:
            res = requests.post(
                f"{self.wordpress_api}/posts",
                auth=(WORDPRESS_USER, WORDPRESS_APP_PASSWORD),
                json=post_data
            )
            res.raise_for_status()
            post_info = res.json()
            print(f"✨ 게시 성공! {post_info['link']}")
            return True
        except Exception as e:
            print(f"❌ 실패: {e}")
            if hasattr(e, 'response'):
                print(f"   {e.response.text[:200]}")
            return False

    def process_article(self, article):
        print(f"\n{'='*60}")
        print(f"📰 {article['title'][:50]}...")
        print(f"📅 {article['date'].strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")
        
        # 본문 스크래핑
        raw_html = self.fetch_full_content(article['link'])
        if not raw_html:
            return False
            
        # 번역
        print(f"🔄 번역 중...")
        title_ko = self.translate_text(article['title'])
        content_ko = self.translate_text(raw_html)
        
        # 영문 slug 생성
        slug = self.generate_english_slug(article['title'])
        print(f"🔗 Slug: {slug}")
        
        # 이미지 처리
        print(f"🔍 이미지...")
        img_url = self.get_main_image_url(article['link'])
        featured_id = 0
        
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

        # 본문 구성
        final_content = content_ko.replace("\n", "<br>\n")
        
        # 원문 링크 (하단)
        final_content += f"\n\n<hr style='margin:40px 0 20px 0;border:0;border-top:1px solid #e0e0e0;'>\n"
        final_content += f"<p style='font-size:13px;color:#777;'>"
        final_content += f"<strong>원문:</strong> <a href='{article['link']}' target='_blank' rel='noopener'>{article['title']}</a>"
        final_content += f"</p>"
        
        # 게시
        print(f"📤 게시...")
        if self.post_to_wordpress(title_ko, final_content, slug, featured_id, article['date']):
            if not FORCE_UPDATE:
                self.posted_articles.append(article['link'])
                self.save_posted_articles()
            return True
        return False

    def run(self):
        print(f"\n{'='*60}")
        print(f"pronews.jp → prodg.kr 자동 번역 v3")
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")
        
        if not WORDPRESS_USER or not WORDPRESS_APP_PASSWORD:
            print("❌ 환경 변수 필요!")
            sys.exit(1)

        articles = self.fetch_rss_feed()
        
        if not articles:
            print("✅ 처리할 기사 없음")
            return
        
        success = 0
        for article in articles:
            if self.process_article(article):
                success += 1
            time.sleep(3)
            
        print(f"\n{'='*60}")
        print(f"🏁 완료: {success}/{len(articles)}개 최신 기사 게시")
        print(f"{'='*60}\n")

if __name__ == "__main__":
    bot = NewsTranslator()
    bot.run()
