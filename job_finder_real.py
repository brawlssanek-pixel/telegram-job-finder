#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import json
import csv
from datetime import datetime
import sys
import time
import os
import re
import logging
import traceback
import hashlib
import html
import random
import difflib
import threading
import concurrent.futures

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Files
SETTINGS_FILE = os.path.join(SCRIPT_DIR, 'bot_settings.json')
NOTIFIED_FILE = os.path.join(SCRIPT_DIR, 'notified_jobs.json')
ERROR_LOG = os.path.join(SCRIPT_DIR, 'job_finder_errors.log')
LOG_FILE = os.path.join(SCRIPT_DIR, 'job_finder.log')
STATUS_FILE = os.path.join(SCRIPT_DIR, 'job_finder_status.json')
STATUS_FILE = os.path.join(SCRIPT_DIR, 'search_status.json')
JOBS_LIVE_FILE = os.path.join(SCRIPT_DIR, 'jobs_live.json')

# Logging
logger = logging.getLogger('job_finder')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
try:
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
except Exception:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

print('\n' + '='*80)
print('JOB FINDER - АВТОПОИСК УДАЛЕННЫХ ВАКАНСИЙ')
print('='*80 + '\n')

# User-Agent rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
]

SKILLS = ["ai", "chatgpt", "gpt", "claude", "копирайтинг", "поиск информации",
          "excel", "python", "парсинг", "веб", "контент", "writing", "content", "remote"]

def log_error(message):
    """Логирует ошибку в файл и через logger"""
    try:
        logger.error(message)
        with open(ERROR_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        try:
            logger.exception('Failed to write to error log')
        except Exception:
            pass

def load_settings():
    """Загрузка настроек из bot_settings.json (если есть)"""
    defaults = {
        'search_interval': 600,
        'paused': False,
        'min_salary': 0,
        'custom_skills': [],
        'applied_jobs': [],
        'daily_report_hour': 9,
        # Digest mode: send single digest message for new jobs (reduces spam)
        'use_digest': True,
        # Maximum items in a digest message
        'digest_max_items': 8
        ,
        # whether to fetch vacancy detail pages to extract salary when summary has none
        'fetch_salary_from_page': True
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                defaults.update(cfg)
    except Exception as e:
        log_error(f'load_settings error: {str(e)[:200]}')
    return defaults

def load_notified():
    try:
        if os.path.exists(NOTIFIED_FILE):
            with open(NOTIFIED_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
    except Exception:
        log_error('Failed to load notified jobs')
    return set()

def save_notified(notified_set):
    try:
        with open(NOTIFIED_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(notified_set), f, ensure_ascii=False, indent=2)
    except Exception:
        log_error('Failed to save notified jobs')

def job_uid(job):
    """Уникальный идентификатор вакансии (по URL или хэшу)"""
    url = job.get('url', '') or ''
    if url:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()
    # fallback: hash of title+company
    key = (job.get('title', '') + '|' + job.get('company', '')).strip()
    return hashlib.sha256(key.encode('utf-8')).hexdigest()

# Telegram config defaults
TELEGRAM_CONFIG = os.path.join(SCRIPT_DIR, 'telegram_config.json')
telegram_enabled = False
telegram_token = None
telegram_chat_id = None

def load_telegram_config():
    global telegram_enabled, telegram_token, telegram_chat_id
    # Prefer environment variables for secrets
    token_env = os.environ.get('TELEGRAM_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_env = os.environ.get('TELEGRAM_CHAT_ID')
    if token_env and chat_env:
        telegram_token = token_env
        telegram_chat_id = chat_env
        telegram_enabled = True
        print('✓ Telegram уведомления включены (из окружения)\n')
        return

    # Fallback to config file (less secure)
    if os.path.exists(TELEGRAM_CONFIG):
        try:
            with open(TELEGRAM_CONFIG, 'r', encoding='utf-8') as f:
                config = json.load(f)
                telegram_token = config.get('token')
                telegram_chat_id = config.get('chat_id')
                if telegram_token and telegram_chat_id:
                    telegram_enabled = True
                    print('✓ Telegram уведомления включены (из telegram_config.json)\n')
        except Exception:
            log_error('Failed to read telegram_config.json')

def send_telegram_notification(job):
    """Отправляет уведомление в Telegram"""
    if not telegram_enabled or not telegram_token or not telegram_chat_id:
        return False

    try:
        uid = job_uid(job)
        # Load persisted notified set once
        notified = load_notified()
        if uid in notified:
            return False

        # Clean HTML from description and produce safe plain text
        desc_raw = job.get('description', '') or ''
        try:
            desc_text = BeautifulSoup(desc_raw, 'html.parser').get_text(separator=' ', strip=True)
        except Exception:
            desc_text = re.sub(r'<[^>]+>', ' ', str(desc_raw))
        description = html.escape(desc_text)[:500]

        title = html.escape(job.get('title', 'No title'))[:120]
        company = html.escape(job.get('company', ''))[:60]
        source = html.escape(job.get('source', ''))
        url = job.get('url', '') or ''

        # Format salary: show provided string and approximate USD if normalized exists
        raw_salary = job.get('salary') or 'Не указана'
        salary_disp = str(raw_salary)
        try:
            usd_min = job.get('salary_usd_min')
            usd_max = job.get('salary_usd_max')
            if usd_min and usd_max and usd_min != usd_max:
                salary_disp = f"{raw_salary} (≈${int(usd_min)}-${int(usd_max)})"
            elif usd_min:
                salary_disp = f"{raw_salary} (≈${int(usd_min)})"
        except Exception:
            pass

        salary_disp = html.escape(salary_disp)

        # Compose HTML message (safe: title/company/salary/description are escaped)
        message = (f"<b>🎯 НОВАЯ ВАКАНСИЯ!</b>\n\n"
                   f"<b>{title}</b>\n"
                   f"🏢 {company}\n"
                   f"📌 {source}\n\n"
                   f"📋 {description}\n"
                   f"💰 <b>Зарплата:</b> {salary_disp}\n\n")
        if url:
            # link itself is not escaped inside href, but included as attribute
            message += f"<a href=\"{html.escape(url)}\">🔗 Посмотреть полностью →</a>"

        url_api = f'https://api.telegram.org/bot{telegram_token}/sendMessage'
        data = {
            'chat_id': telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        resp = requests.post(url_api, json=data, timeout=10)

        if resp.ok:
            # mark as notified
            notified.add(uid)
            try:
                save_notified(notified)
            except Exception:
                log_error('Failed to persist notified set after sending')
            return True
        else:
            log_error(f'Telegram send failed: {resp.status_code} {resp.text[:200]}')
            return False
    except Exception as e:
        log_error(f'send_telegram_notification error: {str(e)[:200]}\n{traceback.format_exc()}')
        return False

def send_telegram_digest(jobs_list):
    """Sends a grouped digest (HTML) of jobs_list to Telegram, chunked by settings."""
    if not telegram_enabled or not telegram_token or not telegram_chat_id:
        return False
    if not jobs_list:
        return False
    try:
        settings = load_settings()
        chunk = int(settings.get('digest_max_items', 8))
        url_api = f'https://api.telegram.org/bot{telegram_token}/sendMessage'

        def format_job(j, idx):
            title = html.escape(j.get('title', 'No title'))[:120]
            company = html.escape(j.get('company', ''))[:60]
            source = html.escape(j.get('source', ''))
            desc_raw = j.get('description', '') or ''
            try:
                desc = BeautifulSoup(desc_raw, 'html.parser').get_text(separator=' ', strip=True)[:200]
            except Exception:
                desc = re.sub(r'<[^>]+>', ' ', str(desc_raw))[:200]
            salary = j.get('salary') or 'Не указана'
            try:
                usd = j.get('salary_usd_min')
                if usd:
                    salary = f"{salary} (≈${int(usd)})"
            except Exception:
                pass
            url = j.get('url', '') or ''
            s = f"<b>{idx}. {title}</b>\n🏢 {company} • {source}\n💰 {html.escape(str(salary))}\n{html.escape(desc)}\n"
            if url:
                s += f"<a href=\"{html.escape(url)}\">🔗 Ссылка</a>\n"
            s += "\n"
            return s

        messages_sent = 0
        # chunk jobs into groups
        for i in range(0, len(jobs_list), chunk):
            part = jobs_list[i:i+chunk]
            body = f"<b>📦 Дайджест вакансий ({len(part)})</b>\n\n"
            for idx, job in enumerate(part, start=i+1):
                body += format_job(job, idx)
            data = {
                'chat_id': telegram_chat_id,
                'text': body,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            resp = requests.post(url_api, json=data, timeout=15)
            if resp.ok:
                messages_sent += 1
            else:
                log_error(f'Digest send failed: {resp.status_code} {resp.text[:200]}')
                # continue trying remaining chunks
        return messages_sent > 0
    except Exception as e:
        log_error(f'send_telegram_digest error: {str(e)[:200]}')
        return False


def send_telegram_digest(jobs, max_items=8):
    """Send a single digest message containing multiple jobs.
    Returns set of UIDs that were marked as notified (on success), or empty set on failure.
    """
    if not telegram_enabled or not telegram_token or not telegram_chat_id:
        return set()

    try:
        total = len(jobs)
        display = jobs[:max_items]
        parts = [f"<b>📦 Дайджест — {total} новых вакансий</b>\n\n"]
        for i, job in enumerate(display, 1):
            title = html.escape(job.get('title', ''))[:100]
            company = html.escape(job.get('company', ''))[:60]
            source = html.escape(job.get('source', ''))
            salary = job.get('salary') or ''
            try:
                usd = job.get('salary_usd_min')
                if usd:
                    salary = f"{salary} (≈${int(usd)})"
            except Exception:
                pass
            salary = html.escape(str(salary))
            url = job.get('url', '') or ''

            parts.append(f"<b>{i}. {title}</b>\n🏢 {company} • {source}\n💰 {salary}\n")
            if url:
                parts.append(f"<a href=\"{html.escape(url)}\">🔗 Открыть</a>\n")
            parts.append("\n")

        if total > max_items:
            parts.append(f"...и ещё {total - max_items} вакансий. Используйте /jobs чтобы посмотреть все.\n")

        message = ''.join(parts)
        url_api = f'https://api.telegram.org/bot{telegram_token}/sendMessage'
        data = {
            'chat_id': telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        resp = requests.post(url_api, json=data, timeout=10)
        if resp.ok:
            uids = set()
            for job in jobs:
                try:
                    uids.add(job_uid(job))
                except Exception:
                    pass
            # persist notified
            try:
                notified = load_notified()
                notified.update(uids)
                save_notified(notified)
            except Exception:
                log_error('Failed to persist notified set after digest')
            return uids
        else:
            log_error(f'Digest send failed: {resp.status_code} {resp.text[:200]}')
            return set()
    except Exception as e:
        log_error(f'send_telegram_digest error: {str(e)[:200]}')
        return set()

def create_session():
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def safe_get_json(session, url, headers=None, timeout=10, verify=True):
    # Fast path: return prefetched JSON if available
    try:
        if url in PREFETCHED_JSON_RESPONSES:
            return PREFETCHED_JSON_RESPONSES[url]
    except Exception:
        pass

    try:
        settings = load_settings()
        headers = headers or {}
        if 'User-Agent' not in headers:
            headers['User-Agent'] = random.choice(USER_AGENTS)

        # support proxies from settings (list or dict)
        proxies = settings.get('proxies') if isinstance(settings, dict) else None
        proxy = None
        if proxies:
            if isinstance(proxies, list) and proxies:
                proxy = random.choice(proxies)
            elif isinstance(proxies, dict):
                proxy = proxies

        kwargs = {'headers': headers, 'timeout': timeout}
        if proxy:
            # support proxy as string or dict
            if isinstance(proxy, str):
                kwargs['proxies'] = {'http': proxy, 'https': proxy}
            elif isinstance(proxy, dict):
                kwargs['proxies'] = proxy
        if not verify:
            kwargs['verify'] = False

        resp = session.get(url, **kwargs)
        if resp.status_code != 200:
            log_error(f'HTTP {resp.status_code} for {url}')
            return None
        try:
            return resp.json()
        except Exception:
            # attempt to load relaxed JSON
            try:
                return json.loads(resp.text)
            except Exception as e:
                log_error(f'JSON parse error for {url}: {str(e)[:200]}')
                return None
    except Exception as e:
        log_error(f'HTTP request error for {url}: {str(e)[:200]}')
        return None


# Exchange rates cache (timestamped)
EXCHANGE_RATES_CACHE = {'ts': 0, 'rates': {}, 'base': 'USD'}

# Prefetched JSON responses cache (url -> parsed JSON)
PREFETCHED_JSON_RESPONSES = {}


def get_exchange_rates(base='USD', ttl=3600):
    """Получает курсы валют с exchangerate.host (кэширует на `ttl` секунд).
    Возвращает маппинг `rates` такой, что `rates[CUR]` = количество CUR за 1 USD (base).
    Для перевода суммы из CUR в USD используем: usd = amount / rates[CUR].
    """
    try:
        now = time.time()
        if EXCHANGE_RATES_CACHE.get('rates') and EXCHANGE_RATES_CACHE.get('base') == base and now - EXCHANGE_RATES_CACHE.get('ts', 0) < ttl:
            return EXCHANGE_RATES_CACHE['rates']
    except Exception:
        pass

    try:
        url = f'https://api.exchangerate.host/latest?base={base}'
        resp = requests.get(url, timeout=10)
        if resp.ok:
            data = resp.json()
            rates = data.get('rates', {}) or {}
            if not rates:
                log_error('get_exchange_rates: empty rates from API, using fallback values')
            else:
                EXCHANGE_RATES_CACHE['rates'] = rates
                EXCHANGE_RATES_CACHE['ts'] = time.time()
                EXCHANGE_RATES_CACHE['base'] = base
                return rates
    except Exception as e:
        log_error(f'get_exchange_rates error: {str(e)[:200]}')

    # fallback approximate rates (rates per 1 USD)
    # NOTE: these are conservative defaults used when API fails.
    # EUR set so that 1 EUR ~= 1.08 USD (tests expect ~1.08 multiplier).
    return {'USD': 1.0, 'EUR': 1.0 / 1.08, 'GBP': 0.79, 'RUB': 82.0}

class JobFinder:
    def __init__(self):
        self.results = []
        self.session = create_session()
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        self.lock = threading.Lock()
        # optional async prefetch cache (url -> SimpleResponse)
        self.prefetched_responses = {}
        # Status tracking for external monitoring (written to JSON)
        self._status_lock = threading.Lock()
        self._completed_scrapers = 0
        self.status_file = STATUS_FILE
        # Load settings (min_salary, custom_skills, paused, etc.)
        try:
            self.settings = load_settings()
            for s in self.settings.get('custom_skills', []):
                if s and s.lower() not in [x.lower() for x in SKILLS]:
                    SKILLS.append(s.lower())
        except Exception:
            self.settings = load_settings()
    
    def request_get(self, url, headers=None, timeout=10, verify=True):
        """Wrapper around session.get that rotates User-Agent and supports proxies from settings"""
        try:
            # If we have a prefetched response (from async prefetch), return it
            if hasattr(self, 'prefetched_responses') and url in self.prefetched_responses:
                return self.prefetched_responses[url]
            headers = headers or {}
            if 'User-Agent' not in headers:
                headers['User-Agent'] = random.choice(USER_AGENTS)

            proxies = self.settings.get('proxies') if isinstance(self.settings, dict) else None
            proxy = None
            if proxies:
                if isinstance(proxies, list) and proxies:
                    proxy = random.choice(proxies)
                elif isinstance(proxies, dict):
                    proxy = proxies

            kwargs = {'headers': headers, 'timeout': timeout}
            if proxy:
                if isinstance(proxy, str):
                    kwargs['proxies'] = {'http': proxy, 'https': proxy}
                elif isinstance(proxy, dict):
                    kwargs['proxies'] = proxy
            if not verify:
                kwargs['verify'] = False

            return self.session.get(url, **kwargs)
        except Exception as e:
            log_error(f'request_get error for {url}: {str(e)[:200]}')
            return None

    def write_status(self, info: dict | None = None):
        """Write a small JSON status blob to the shared status file.
        Keeps keys: last_update, stage, current_scraper, completed, total, found_total, last_message
        """
        try:
            now = datetime.now().isoformat()
            base = {
                'last_update': now,
                'stage': 'idle',
                'current_scraper': None,
                'completed': int(self._completed_scrapers),
                'total': 0,
                'found_total': len(self.results),
                'last_message': None
            }
            if info:
                base.update(info)
            tmp = self.status_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(base, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp, self.status_file)
            except Exception:
                if os.path.exists(self.status_file):
                    os.remove(self.status_file)
                os.replace(tmp, self.status_file)
        except Exception as e:
            log_error(f'write_status error: {str(e)[:200]}')

    async def _aio_get(self, session, url, sem, timeout, proxy=None, headers=None):
        async with sem:
            try:
                kwargs = {'timeout': timeout}
                if headers:
                    kwargs['headers'] = headers
                if proxy:
                    kwargs['proxy'] = proxy
                async with session.get(url, **kwargs) as resp:
                    content = await resp.read()
                    text = None
                    try:
                        text = await resp.text()
                    except Exception:
                        text = None
                    return url, resp.status, content, text
            except Exception as e:
                log_error(f'aio fetch error {url}: {str(e)[:200]}')
                return url, None, None, None

    def async_prefetch_urls(self, urls, concurrency=8, timeout=10):
        """Префетчит список URL параллельно через aiohttp и сохраняет ответы в `self.prefetched_responses`"""
        try:
            import asyncio
            import aiohttp

            async def _runner(urls):
                sem = asyncio.Semaphore(concurrency)
                headers = {'User-Agent': random.choice(USER_AGENTS)}
                settings = getattr(self, 'settings', load_settings())
                proxies = settings.get('proxies') if isinstance(settings, dict) else None
                async with aiohttp.ClientSession() as session:
                    tasks = []
                    for u in urls:
                        proxy = None
                        if proxies:
                            if isinstance(proxies, list) and proxies:
                                proxy = random.choice(proxies)
                            elif isinstance(proxies, dict):
                                # if dict provided, try http/https
                                proxy = proxies.get('http') or proxies.get('https')
                        tasks.append(self._aio_get(session, u, sem, timeout, proxy=proxy, headers=headers))
                    results = await asyncio.gather(*tasks)
                    # store as SimpleResponse-like objects
                    class SimpleResponse:
                        def __init__(self, content, status, text=None):
                            self.content = content or b''
                            self.status_code = status or 0
                            self.text = text or (self.content.decode('utf-8', errors='ignore') if isinstance(self.content, (bytes, bytearray)) else str(self.content))
                            self.ok = 200 <= self.status_code < 300

                    for url, status, content, text in results:
                        if url is None:
                            continue
                        self.prefetched_responses[url] = SimpleResponse(content, status, text)

            asyncio.run(_runner(urls))
        except Exception as e:
            log_error(f'async_prefetch_urls error: {str(e)[:200]}')
    
    def match_skills(self, text):
        if not text:
            return False
        text_lower = text.lower()
        # Accept if explicit remote markers present (English or Russian)
        if 'remote' in text_lower or 'удал' in text_lower or 'удалён' in text_lower or 'удален' in text_lower:
            return True
        return any(skill in text_lower for skill in SKILLS)
    
    def extract_salary(self, text):
        """Пытается извлечь информацию о зарплате из текста"""
        if not text:
            return 'Не указана'
        import re
        s = str(text)
        # normalize non-breaking spaces
        s = s.replace('\u00A0', ' ')

        # 1) currency-prefixed ranges like $1000-2000, €1000-2000
        m = re.search(r'([$€£]\s*[\d,\.\skK]+\s*[-–—to]+\s*[$€£]?\s*[\d,\.\skK]+)', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 2) ranges with currency words, e.g. "50 000 - 80 000 руб"
        m = re.search(r'((?:\d{1,3}(?:[ \u00A0]\d{3})+|\d+)\s*[-–—to]+\s*(?:\d{1,3}(?:[ \u00A0]\d{3})+|\d+)\s*(?:руб|р|₽|rur|rub))', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 3) single value with currency symbol or word, e.g. "150 000 руб", "₽150000", "$2000"
        m = re.search(r'(?:₽|руб\.?|р\.?|rur|rub)\s*(\d{1,3}(?:[ \u00A0]\d{3})+|\d+)', s, re.IGNORECASE)
        if m:
            return (m.group(0).strip())
        m = re.search(r'((?:\d{1,3}(?:[ \u00A0]\d{3})+|\d+)\s*(?:руб\.?|р\.?|₽|rur|rub))', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 4) USD/EUR/GBP single values
        m = re.search(r'([$€£]\s*[\d,\.\skK]+)', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 5) generic numeric with k/к suffix e.g. 50k, 50к
        m = re.search(r'((?:\d+[\s\u00A0]?)(?:k|к)\b)', s, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 6) fallback: first standalone number (may be false-positive)
        m = re.search(r'(\d{1,3}(?:[ \u00A0]\d{3})+|\d+)', s)
        if m:
            return m.group(1).strip()

        return 'Не указана'
    
    def extract_salary_number(self, text):
        """Пытается извлечь первое числовое значение из строки зарплаты"""
        if not text:
            return 0
        text = str(text)
        # remove currency symbols
        import re
        m = re.search(r'(\d+)', text.replace(',', ''))
        if not m:
            return 0
        try:
            return int(m.group(1))
        except Exception:
            return 0

    def normalize_salary_fields(self, job):
        """Normalize salary: populate salary_min, salary_max, salary_currency,
        salary_usd_min and salary_usd_max. Optionally fetch vacancy page when
        salary is missing (controlled by settings.fetch_salary_from_page).
        """
        try:
            settings = getattr(self, 'settings', load_settings())
            raw = job.get('salary') or job.get('description') or ''

            # If salary not present or is placeholder, try to fetch from page
            if (not raw or str(raw).strip() in ('', 'Не указана')) and settings.get('fetch_salary_from_page', False):
                url = job.get('url') or ''
                if url:
                    try:
                        fetched = self.get_salary_from_page(url)
                        if fetched:
                            job['salary'] = fetched
                            raw = fetched
                    except Exception:
                        pass

            s = str(raw or '').strip()
            s_low = s.lower()
            rates = get_exchange_rates('USD')

            def parse_num(tok):
                if not tok:
                    return None
                t = str(tok).replace('\u00A0', '').replace(' ', '').replace(',', '').lower()
                try:
                    if t.endswith('k') or t.endswith('к'):
                        return int(float(t[:-1]) * 1000)
                    t2 = re.sub(r'[^0-9\.]', '', t)
                    if t2 == '':
                        return None
                    return int(float(t2))
                except Exception:
                    return None

            mn = mx = None
            currency = None

            # 1) currency-symbol range like $1000-2000
            m = re.search(r'([$€£])\s*([\d\s\u00A0,\.kK]+)\s*[-–—to]+\s*[$€£]?\s*([\d\s\u00A0,\.kK]+)', s)
            if m:
                sym = m.group(1)
                a = parse_num(m.group(2))
                b = parse_num(m.group(3))
                curmap = {'$': 'USD', '€': 'EUR', '£': 'GBP'}
                currency = curmap.get(sym)
                if a is not None and b is not None:
                    mn, mx = min(a, b), max(a, b)
                else:
                    mn = a or b
                    mx = a or b
            else:
                # 2) range with currency word (e.g., "50 000 - 80 000 руб")
                m2 = re.search(r'([\d\s\u00A0,\.kK]+)\s*[-–—to]+\s*([\d\s\u00A0,\.kK]+)\s*(руб|р|₽|rur|rub|usd|eur|gbp)?', s_low, re.IGNORECASE)
                if m2:
                    a = parse_num(m2.group(1))
                    b = parse_num(m2.group(2))
                    cur = m2.group(3)
                    if cur:
                        cur = cur.upper()
                        if cur in ('РУБ', 'Р', '₽'):
                            cur = 'RUB'
                    currency = cur
                    if a is not None and b is not None:
                        mn, mx = min(a, b), max(a, b)
                    else:
                        mn = a or b
                        mx = a or b
                else:
                    # 3) single value with currency word/symbol
                    m3 = re.search(r'([\d\s\u00A0,\.kK]+)\s*(руб|р|₽|rur|rub|usd|eur|gbp)', s_low, re.IGNORECASE)
                    if m3:
                        val = parse_num(m3.group(1))
                        cur = m3.group(2).upper()
                        if cur in ('РУБ', 'Р', '₽'):
                            cur = 'RUB'
                        currency = cur
                        mn = mx = val
                    else:
                        m4 = re.search(r'([$€£])\s*([\d\s\u00A0,\.kK]+)', s)
                        if m4:
                            sym = m4.group(1)
                            val = parse_num(m4.group(2))
                            curmap = {'$': 'USD', '€': 'EUR', '£': 'GBP'}
                            currency = curmap.get(sym)
                            mn = mx = val
                        else:
                            # fallback: any number
                            m5 = re.search(r'([\d\s\u00A0,\.kK]+)', s)
                            if m5:
                                val = parse_num(m5.group(1))
                                mn = mx = val

            # compute USD equivalents
            usd_min = usd_max = None
            try:
                cur_var = currency.upper() if currency else None
                if mn is not None:
                    if not cur_var or cur_var == 'USD':
                        usd_min = int(mn)
                    else:
                        rate = rates.get(cur_var)
                        if rate and rate > 0:
                            usd_min = int(mn / rate)
                if mx is not None:
                    if not cur_var or cur_var == 'USD':
                        usd_max = int(mx)
                    else:
                        rate = rates.get(cur_var)
                        if rate and rate > 0:
                            usd_max = int(mx / rate)
            except Exception:
                pass

            job['salary_min'] = mn
            job['salary_max'] = mx
            job['salary_currency'] = cur_var if 'cur_var' in locals() else (currency.upper() if currency else None)
            job['salary_usd_min'] = usd_min
            job['salary_usd_max'] = usd_max
        except Exception as e:
            log_error(f'normalize_salary_fields error: {str(e)[:200]}')
            job.setdefault('salary_min', None)
            job.setdefault('salary_max', None)
            job.setdefault('salary_currency', None)
            job.setdefault('salary_usd_min', None)
            job.setdefault('salary_usd_max', None)
        return None

    def get_salary_from_page(self, url):
        """Fetch vacancy page and try to extract salary mentions from its text."""
        try:
            resp = self.request_get(url, timeout=8)
            if not resp:
                return None
            text = ''
            try:
                text = resp.text
            except Exception:
                try:
                    text = resp.content.decode('utf-8', errors='ignore')
                except Exception:
                    return None

            soup = BeautifulSoup(text, 'html.parser')
            page_text = soup.get_text(separator=' ', strip=True)
            # common Russian patterns
            m = re.search(r'(зарплат[аы]|з/п|зп)[:\s\-]{0,12}([\d\s\u00A0,\-–]+)\s*(₽|руб|rur|rub|USD|\$|€|EUR)?', page_text, re.IGNORECASE)
            if m:
                num = m.group(2).strip()
                cur = (m.group(3) or '').strip()
                return (num + ' ' + cur).strip()

            # fallback: any number + currency symbol nearby
            m2 = re.search(r'([\d\s\u00A0]{2,8}[\d])\s*(₽|руб|rur|rub|USD|\$|€|EUR)', page_text, re.IGNORECASE)
            if m2:
                return (m2.group(1).strip() + ' ' + m2.group(2).strip())
        except Exception as e:
            log_error(f'get_salary_from_page error for {url}: {str(e)[:200]}')
        return None
    
    def scrape_indeed(self):
        print('[1] Ищем на Indeed...')
        sys.stdout.flush()
        try:
            url = 'https://www.indeed.com/rss?q=remote&l=&sort=date'
            feed = feedparser.parse(url)
            
            for entry in feed.entries[:50]:
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                
                if self.match_skills(title + ' ' + summary):
                    job = {
                        'source': 'Indeed',
                        'title': title,
                        'company': entry.get('author', 'Unknown'),
                        'description': summary[:200],
                        'salary': self.extract_salary(summary),
                        'url': entry.get('link', ''),
                        'posted': entry.get('published', ''),
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Indeed"])} вакансий')
        except Exception as e:
            error_msg = f'Indeed Error: {str(e)[:100]}'
            print(f'    Ошибка: {str(e)[:80]}')
            log_error(error_msg)
        sys.stdout.flush()
    
    def scrape_hh_ru(self):
        print('[2] Ищем на HH.ru...')
        sys.stdout.flush()
        try:
            url = 'https://hh.ru/vacancy/search/rss?text=AI&area=113'
            feed = feedparser.parse(url)
            
            for entry in feed.entries[:50]:
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                
                if self.match_skills(title + ' ' + summary):
                    job = {
                        'source': 'HH.ru',
                        'title': title,
                        'company': entry.get('author', 'Unknown'),
                        'description': summary[:200],
                        'salary': self.extract_salary(summary),
                        'url': entry.get('link', ''),
                        'posted': entry.get('published', ''),
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="HH.ru"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    def scrape_hh_api(self):
                """Scrape HeadHunter public API (more structured salary info)."""
                print('[HH API] Ищем через API HeadHunter...')
                sys.stdout.flush()
                try:
                    url = 'https://api.hh.ru/vacancies?text=AI&area=113&per_page=50'
                    data = safe_get_json(self.session, url, headers=self.headers, timeout=10)
                    if not data:
                        print('    HH API returned empty')
                        return
                    items = data.get('items', []) if isinstance(data, dict) else []
                    for v in items[:50]:
                        title = v.get('name', '')
                        snippet = ''
                        try:
                            snippet = (v.get('snippet') or {}).get('responsibility') or (v.get('snippet') or {}).get('requirement') or ''
                        except Exception:
                            snippet = ''
                        if self.match_skills(title + ' ' + snippet):
                            sal = v.get('salary')
                            salary_str = 'Не указана'
                            if sal:
                                fr = sal.get('from')
                                to = sal.get('to')
                                cur = sal.get('currency')
                                if fr and to:
                                    salary_str = f"{int(fr)}-{int(to)} {cur or ''}"
                                elif fr:
                                    salary_str = f"{int(fr)} {cur or ''}"
                                elif to:
                                    salary_str = f"{int(to)} {cur or ''}"

                            job = {
                                'source': 'HH API',
                                'title': title,
                                'company': v.get('employer', {}).get('name', 'Unknown') if isinstance(v.get('employer'), dict) else 'Unknown',
                                'description': snippet[:200],
                                'salary': salary_str,
                                'url': v.get('alternate_url', ''),
                                'posted': v.get('published_at', ''),
                                'type': 'Remote',
                                'relevance': 1
                            }
                            with self.lock:
                                self.results.append(job)
                    print(f'    Найдено: {len([r for r in self.results if r.get("source")=="HH API"])} вакансий (HH API)')
                except Exception as e:
                    print(f'    HH API error: {str(e)[:120]}')
                    log_error(f'HH API error: {str(e)[:200]}')
    
    def scrape_remoteok(self):
        print('[3] Ищем на RemoteOK...')
        sys.stdout.flush()
        try:
            url = 'https://remoteok.com/api'
            requests.packages.urllib3.disable_warnings()
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10, verify=False)
            if not jobs:
                print('    Получен пустой ответ от RemoteOK')
                return
            
            for job in jobs[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job_obj = {
                            'source': 'RemoteOK',
                            'title': title,
                            'company': job.get('company', 'Unknown'),
                            'description': description[:200],
                            'salary': self.extract_salary(description),
                            'url': job.get('url', ''),
                            'posted': job.get('date', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job_obj)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="RemoteOK"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_we_work_remotely(self):
        print('[4] Ищем на We Work Remotely...')
        sys.stdout.flush()
        try:
            url = 'https://weworkremotely.com/remote-jobs/search?keyword=remote'
            response = self.request_get(url, timeout=10)
            if not response:
                print('    Ошибка получения страницы We Work Remotely')
                return
            soup = BeautifulSoup(response.content, 'html.parser')
            
            jobs = soup.find_all('a', class_='job-link')
            for job in jobs[:30]:
                title = job.get_text(strip=True)
                link = job.get('href', '')
                
                if self.match_skills(title):
                    job = {
                        'source': 'We Work Remotely',
                        'title': title,
                        'company': 'Unknown',
                        'description': 'Смотрите на сайте',
                        'salary': 'Не указана',
                        'url': link,
                        'posted': '',
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="We Work Remotely"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_habr_career(self):
        print('[5] Ищем на Habr Career...')
        sys.stdout.flush()
        try:
            url = 'https://career.habr.com/vacancies?type=all'
            response = self.request_get(url, timeout=10)
            if not response:
                print('    Ошибка получения страницы Habr Career')
                return
            soup = BeautifulSoup(response.content, 'html.parser')
            
            jobs = soup.find_all('a', class_='vacancy-card__title-link')
            for job in jobs[:30]:
                title = job.get_text(strip=True)
                link = job.get('href', '')
                
                if self.match_skills(title):
                    job = {
                        'source': 'Habr Career',
                        'title': title,
                        'company': 'Unknown',
                        'description': 'Смотрите на сайте',
                        'salary': 'Не указана',
                        'url': 'https://career.habr.com' + link if link else '',
                        'posted': '',
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Habr Career"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_github_jobs(self):
        print('[6] Ищем на GitHub Jobs...')
        sys.stdout.flush()
        try:
            requests.packages.urllib3.disable_warnings()
            url = 'https://jobs.github.com/positions.json?search=remote'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10, verify=False)
            if not jobs:
                print('    Получен пустой ответ от GitHub Jobs')
                return
            
            for job in jobs[:30]:
                title = job.get('title', '')
                description = job.get('description', '')
                
                if self.match_skills(title + ' ' + description):
                    job = {
                        'source': 'GitHub Jobs',
                        'title': title,
                        'company': job.get('company', 'Unknown'),
                        'description': description[:150],
                        'url': job.get('url', ''),
                        'posted': job.get('created_at', ''),
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="GitHub Jobs"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_jusremote(self):
        print('[7] Ищем на JustRemote...')
        sys.stdout.flush()
        try:
            url = 'https://www.justremote.co/api/v1/jobs?limit=50'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            if not jobs:
                print('    Получен пустой ответ от JustRemote')
                return
            
            for job in (jobs.get('data', []) if isinstance(jobs, dict) else jobs)[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'JustRemote',
                            'title': title,
                            'company': job.get('company_name', 'Unknown'),
                            'description': description[:200],
                            'salary': self.extract_salary(description),
                            'url': job.get('url', ''),
                            'posted': job.get('created_at', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="JustRemote"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_working_nomads(self):
        print('[8] Ищем на Working Nomads...')
        sys.stdout.flush()
        try:
            url = 'https://www.workingnomads.co/api/jobs'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            if not jobs:
                print('    Получен пустой ответ от Working Nomads')
                return
            
            for job in jobs[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'Working Nomads',
                            'title': title,
                            'company': job.get('company_name', 'Unknown'),
                            'description': description[:150],
                            'url': job.get('url', ''),
                            'posted': job.get('published_at', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Working Nomads"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_remoteco(self):
        print('[9] Ищем на Remote.co...')
        sys.stdout.flush()
        try:
            url = 'https://remote.co/api/jobs'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            if not jobs:
                print('    Получен пустой ответ от Remote.co')
                return
            
            for job in (jobs if isinstance(jobs, list) else jobs.get('data', []))[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'Remote.co',
                            'title': title,
                            'company': job.get('company', 'Unknown'),
                            'description': description[:150],
                            'url': job.get('url', ''),
                            'posted': job.get('date_posted', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Remote.co"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_stackoverflow(self):
        print('[10] Ищем на Stack Overflow Jobs...')
        sys.stdout.flush()
        try:
            url = 'https://stackoverflow.com/jobs/feed?q=remote&r=true'
            feed = feedparser.parse(url)
            
            for entry in feed.entries[:50]:
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                
                if self.match_skills(title + ' ' + summary):
                    job = {
                        'source': 'Stack Overflow',
                        'title': title,
                        'company': entry.get('author', 'Unknown'),
                        'description': summary[:150],
                        'url': entry.get('link', ''),
                        'posted': entry.get('published', ''),
                        'type': 'Remote',
                        'relevance': 1
                    }
                    with self.lock:
                        self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Stack Overflow"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_devto(self):
        print('[11] Ищем на Dev.to...')
        sys.stdout.flush()
        try:
            url = 'https://dev.to/api/articles?tag=jobs&per_page=50'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            if not jobs:
                print('    Получен пустой ответ от Dev.to')
                return
            
            for job in jobs[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if 'remote' in title.lower() and self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'Dev.to',
                            'title': title,
                            'company': job.get('user', {}).get('name', 'Unknown') if job.get('user') else 'Unknown',
                            'description': description[:150],
                            'url': job.get('url', ''),
                            'posted': job.get('published_at', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Dev.to"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_angel_list(self):
        print('[12] Ищем на AngelList...')
        sys.stdout.flush()
        try:
            url = 'https://api.angel.co/1/jobs?filter_data[job_titles][]=1&filter_data[locations][]=0'
            jobs = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            if not jobs:
                print('    Получен пустой ответ от AngelList')
                return
            
            for job in (jobs.get('jobs', []) if isinstance(jobs, dict) else jobs)[:50]:
                if isinstance(job, dict):
                    title = job.get('title', '')
                    description = job.get('description', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'AngelList',
                            'title': title,
                            'company': job.get('company_name', 'Unknown'),
                            'description': description[:150],
                            'url': job.get('url', ''),
                            'posted': job.get('created_at', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="AngelList"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_superjob(self):
        print('[13] Ищем на SuperJob...')
        sys.stdout.flush()
        try:
            url = 'https://api.superjob.ru/2.0/vacancies/?keyword=remote&remote=1&archive=0'
            data = safe_get_json(self.session, url, headers=self.headers, timeout=10)
            jobs = data.get('objects', []) if isinstance(data, dict) else []
            if not jobs:
                print('    Получен пустой ответ от SuperJob')
                return
            
            for job in jobs[:50]:
                if isinstance(job, dict):
                    title = job.get('job_title', '')
                    description = job.get('body', '')
                    
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'SuperJob',
                            'title': title,
                            'company': job.get('company_name', 'Unknown'),
                            'description': description[:150],
                            'url': job.get('link', ''),
                            'posted': job.get('date_published', ''),
                            'type': 'Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="SuperJob"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_freelance_ru(self):
        print('[14] Ищем на Freelance.ru...')
        sys.stdout.flush()
        try:
            url = 'https://www.freelance.ru/projects/'
            response = self.request_get(url, timeout=10)
            if not response:
                print('    Ошибка получения страницы Freelance.ru')
                return
            soup = BeautifulSoup(response.content, 'html.parser')
            
            projects = soup.find_all('div', class_='ProjectCard')
            for project in projects[:30]:
                title_elem = project.find('a', class_='ProjectCard__title')
                desc_elem = project.find('div', class_='ProjectCard__description')
                if title_elem and desc_elem:
                    title = title_elem.get_text(strip=True)
                    description = desc_elem.get_text(strip=True)
                    link = title_elem.get('href', '')
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'Freelance.ru',
                            'title': title,
                            'company': 'Freelancer',
                            'description': description[:150],
                            'url': 'https://www.freelance.ru' + link if link else '',
                            'posted': '',
                            'type': 'Freelance/Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Freelance.ru"])} проектов')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_kwork(self):
        print('[15] Ищем на Kwork...')
        sys.stdout.flush()
        try:
            url = 'https://kwork.ru/projects'
            response = self.request_get(url, timeout=10)
            if not response:
                print('    Ошибка получения страницы Kwork')
                return
            soup = BeautifulSoup(response.content, 'html.parser')
            
            projects = soup.find_all('div', class_='ProjectCard')
            for project in projects[:30]:
                title_elem = project.find('a', class_='ProjectCard__title')
                desc_elem = project.find('div', class_='ProjectCard__description')
                if title_elem and desc_elem:
                    title = title_elem.get_text(strip=True)
                    description = desc_elem.get_text(strip=True)
                    link = title_elem.get('href', '')
                    if self.match_skills(title + ' ' + description):
                        job = {
                            'source': 'Kwork',
                            'title': title,
                            'company': 'Kwork',
                            'description': description[:150],
                            'url': 'https://kwork.ru' + link if link else '',
                            'posted': '',
                            'type': 'Freelance/Remote',
                            'relevance': 1
                        }
                        with self.lock:
                            self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Kwork"])} проектов')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def scrape_avito(self):
        print('[16] Ищем на Avito...')
        sys.stdout.flush()
        try:
            url = 'https://www.avito.ru/rossiya?cd=1&s=104'
            response = self.request_get(url, timeout=10)
            if not response:
                print('    Ошибка получения страницы Avito')
                return
            soup = BeautifulSoup(response.content, 'html.parser')
            
            items = soup.find_all('div', class_='iva-item-root')
            for item in items[:30]:
                title_elem = item.find('a', class_='iva-item-title')
                desc_elem = item.find('div', class_='iva-item-descriptionStep')
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    description = desc_elem.get_text(strip=True) if desc_elem else ''
                    link = title_elem.get('href', '')

                    if 'удаленно' in title.lower() or 'удаленно' in description.lower():
                        if self.match_skills(title + ' ' + description):
                            job = {
                                'source': 'Avito',
                                'title': title,
                                'company': 'Unknown',
                                'description': description[:150],
                                'url': 'https://www.avito.ru' + link if link else '',
                                'posted': '',
                                'type': 'Remote',
                                'relevance': 1
                            }
                            with self.lock:
                                self.results.append(job)
            print(f'    Найдено: {len([r for r in self.results if r["source"]=="Avito"])} вакансий')
        except Exception as e:
            print(f'    Ошибка: {str(e)[:80]}')
        sys.stdout.flush()
    
    def deduplicate(self):
        seen = set()
        unique = []
        for job in self.results:
            # Дедупликация по URL (основной критерий)
            url_key = job.get('url', '').lower()
            if url_key and url_key in seen:
                continue
            
            # Если нет URL, дедупликация по (title, company)
            if not url_key:
                key = (job['title'].lower(), job['company'].lower())
                if key in seen:
                    continue
                seen.add(key)
            else:
                seen.add(url_key)
            
            unique.append(job)
        self.results = unique

    def cluster_similar_jobs(self, threshold=0.78):
        """Группирует похожие вакансии по заголовку (простая кластеризация на основе сходства строк)"""
        clusters = []
        used = [False] * len(self.results)
        for i, job in enumerate(self.results):
            if used[i]:
                continue
            group = [job]
            used[i] = True
            title_i = (job.get('title') or '').lower()
            for j in range(i + 1, len(self.results)):
                if used[j]:
                    continue
                title_j = (self.results[j].get('title') or '').lower()
                try:
                    ratio = difflib.SequenceMatcher(None, title_i, title_j).ratio()
                except Exception:
                    ratio = 0
                if ratio >= threshold:
                    group.append(self.results[j])
                    used[j] = True
            clusters.append(group)
        return clusters

    def embed_texts(self, texts):
        """Попытие получить эмбеддинги для списка текстов.
        Поддерживает OpenAI или локальную модель sentence-transformers.
        Возвращает список векторов или None.
        """
        provider = self.settings.get('embedding_provider', 'auto')
        # Try OpenAI if requested or API key present
        if provider in ('openai', 'auto'):
            api_key = self.settings.get('openai_api_key') or os.environ.get('OPENAI_API_KEY')
            if api_key:
                try:
                    import openai
                    openai.api_key = api_key
                    model = self.settings.get('openai_embedding_model', 'text-embedding-3-small')
                    resp = openai.Embedding.create(model=model, input=texts)
                    return [d['embedding'] for d in resp['data']]
                except Exception as e:
                    log_error(f'OpenAI embed error: {str(e)[:200]}')

        # Try local sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            model_name = self.settings.get('local_embedding_model', 'all-MiniLM-L6-v2')
            model = SentenceTransformer(model_name)
            emb = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
            try:
                import numpy as _np
                return [_np.array(x).tolist() for x in emb]
            except Exception:
                return [list(x) for x in emb]
        except Exception as e:
            log_error(f'Local embedding error: {str(e)[:200]}')

        return None

    def cluster_with_embeddings(self, threshold=0.75):
        """Группирует вакансии, используя косинусное сходство эмбеддингов.
        Возвращает список групп (каждая группа — список вакансий).
        """
        # If numpy is not installed, skip embedding-based clustering gracefully
        try:
            import numpy as np
        except ImportError:
            logger.info('numpy not available — falling back to simple string clustering')
            return self.cluster_similar_jobs(threshold=threshold)

        try:
            texts = [(j.get('title', '') + ' ' + j.get('company', '') + ' ' + j.get('description', '')) for j in self.results]
            emb = self.embed_texts(texts)
            if not emb:
                return self.cluster_similar_jobs(threshold=threshold)

            emb = [np.array(v, dtype=float) for v in emb]
            clusters = []
            centers = []
            for idx, vec in enumerate(emb):
                if not centers:
                    clusters.append([self.results[idx]])
                    centers.append(vec.copy())
                    continue
                sims = [float(np.dot(vec, c) / (np.linalg.norm(vec) * np.linalg.norm(c) + 1e-9)) for c in centers]
                best = max(sims)
                bi = int(sims.index(best))
                if best >= threshold:
                    clusters[bi].append(self.results[idx])
                    # update center (running mean)
                    centers[bi] = (centers[bi] * (len(clusters[bi]) - 1) + vec) / len(clusters[bi])
                else:
                    clusters.append([self.results[idx]])
                    centers.append(vec.copy())
            return clusters
        except Exception as e:
            log_error(f'cluster_with_embeddings error: {str(e)[:200]}')
            return self.cluster_similar_jobs(threshold=threshold)
    
    async def async_fetch_json(self, url, headers=None, timeout=10):
        """Асинхронно получает JSON по URL с поддержкой прокси и кэша префетча."""
        # fast-path: prefetched JSON
        try:
            if url in PREFETCHED_JSON_RESPONSES:
                return PREFETCHED_JSON_RESPONSES[url]
        except Exception:
            pass

        try:
            import aiohttp
            import asyncio
            settings = getattr(self, 'settings', load_settings())
            proxies = settings.get('proxies') if isinstance(settings, dict) else None
            proxy = None
            if proxies:
                if isinstance(proxies, list) and proxies:
                    proxy = random.choice(proxies)
                elif isinstance(proxies, dict):
                    proxy = proxies.get('http') or proxies.get('https') or proxies

            hdrs = headers or {}
            if 'User-Agent' not in hdrs:
                hdrs['User-Agent'] = random.choice(USER_AGENTS)

            timeout_obj = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(url, headers=hdrs, timeout=timeout_obj, proxy=proxy) as resp:
                        text = await resp.text()
                        if resp.status != 200:
                            log_error(f'HTTP {resp.status} for {url} (async)')
                            return None
                        try:
                            return await resp.json()
                        except Exception:
                            try:
                                return json.loads(text)
                            except Exception:
                                return None
                except Exception as e:
                    log_error(f'async fetch error for {url}: {str(e)[:200]}')
                    return None
        except Exception as e:
            log_error(f'async_fetch_json setup error: {str(e)[:200]}')
            return None

    def async_scrape_json_sites(self):
        """Асинхронный сбор для JSON API сайтов: запускает задачи и добавляет результаты в self.results."""
        try:
            import asyncio

            async def runner():
                sem = asyncio.Semaphore(self.settings.get('aiohttp_concurrency', 8))
                endpoints = {
                    'RemoteOK': 'https://remoteok.com/api',
                    'GitHub Jobs': 'https://jobs.github.com/positions.json?search=remote',
                    'JustRemote': 'https://www.justremote.co/api/v1/jobs?limit=50',
                    'Working Nomads': 'https://www.workingnomads.co/api/jobs',
                    'Remote.co': 'https://remote.co/api/jobs',
                    'Dev.to': 'https://dev.to/api/articles?tag=jobs&per_page=50',
                    'AngelList': 'https://api.angel.co/1/jobs?filter_data[job_titles][]=1&filter_data[locations][]=0',
                    'SuperJob': 'https://api.superjob.ru/2.0/vacancies/?keyword=remote&remote=1&archive=0'
                }

                async def work(name, url):
                    async with sem:
                        data = await self.async_fetch_json(url, headers=self.headers, timeout=15)
                        if not data:
                            return
                        try:
                            if name == 'RemoteOK' and isinstance(data, list):
                                for j in data[:50]:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'RemoteOK', 'title': title, 'company': j.get('company', 'Unknown'), 'description': desc[:200], 'salary': self.extract_salary(desc), 'url': j.get('url', ''), 'posted': j.get('date', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'GitHub Jobs' and isinstance(data, list):
                                for j in data[:30]:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'GitHub Jobs', 'title': title, 'company': j.get('company', 'Unknown'), 'description': desc[:150], 'url': j.get('url', ''), 'posted': j.get('created_at', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'JustRemote':
                                items = (data.get('data', []) if isinstance(data, dict) else data)[:50]
                                for j in items:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'JustRemote', 'title': title, 'company': j.get('company_name', 'Unknown'), 'description': desc[:200], 'salary': self.extract_salary(desc), 'url': j.get('url', ''), 'posted': j.get('created_at', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'Working Nomads' and isinstance(data, list):
                                for j in data[:50]:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'Working Nomads', 'title': title, 'company': j.get('company_name', 'Unknown'), 'description': desc[:150], 'url': j.get('url', ''), 'posted': j.get('published_at', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'Remote.co':
                                items = (data if isinstance(data, list) else data.get('data', []))[:50]
                                for j in items:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'Remote.co', 'title': title, 'company': j.get('company', 'Unknown'), 'description': desc[:150], 'url': j.get('url', ''), 'posted': j.get('date_posted', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'Dev.to' and isinstance(data, list):
                                for j in data[:50]:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if 'remote' in title.lower() and self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'Dev.to', 'title': title, 'company': j.get('user', {}).get('name', 'Unknown') if j.get('user') else 'Unknown', 'description': desc[:150], 'url': j.get('url', ''), 'posted': j.get('published_at', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'AngelList':
                                items = (data.get('jobs', []) if isinstance(data, dict) else data)[:50]
                                for j in items:
                                    title = j.get('title', '')
                                    desc = j.get('description', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'AngelList', 'title': title, 'company': j.get('company_name', 'Unknown'), 'description': desc[:150], 'url': j.get('url', ''), 'posted': j.get('created_at', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                            elif name == 'SuperJob':
                                items = (data.get('objects', []) if isinstance(data, dict) else data)[:50]
                                for j in items:
                                    title = j.get('job_title', '')
                                    desc = j.get('body', '')
                                    if self.match_skills(title + ' ' + desc):
                                        obj = {'source': 'SuperJob', 'title': title, 'company': j.get('company_name', 'Unknown'), 'description': desc[:150], 'url': j.get('link', ''), 'posted': j.get('date_published', ''), 'type': 'Remote', 'relevance': 1}
                                        with self.lock:
                                            self.results.append(obj)
                        except Exception as e:
                            log_error(f'processing {name} data error: {str(e)[:200]}')

                tasks = [asyncio.create_task(work(n, u)) for n, u in endpoints.items()]
                await asyncio.gather(*tasks)

            asyncio.run(runner())
        except Exception as e:
            log_error(f'async_scrape_json_sites error: {str(e)[:200]}')
    
    def log_to_file(self):
        """Логирует найденные вакансии в общий файл"""
        log_file = 'jobs_log.csv'
        mode = 'a' if __import__('os').path.exists(log_file) else 'w'
        if self.results:
            settings = getattr(self, 'settings', load_settings())
            min_salary = settings.get('min_salary', 0)
            # Load persisted notified jobs once to avoid duplicates across runs
            persisted_notified = load_notified()
            sent_this_run = set()
            with open(log_file, mode, newline='', encoding='utf-8') as f:
                fieldnames = ['timestamp', 'source', 'title', 'company', 'description', 'salary', 'salary_min', 'salary_max', 'salary_currency', 'salary_usd_min', 'salary_usd_max', 'url']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if mode == 'w':
                    writer.writeheader()

                # Collect candidate jobs to notify (deduped and filtered by min_salary)
                candidate_jobs = []
                for job in self.results:
                    row = {
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'source': job.get('source', ''),
                        'title': job.get('title', '')[:60],
                        'company': job.get('company', '')[:30],
                        'description': job.get('description', '')[:100],
                        'salary': job.get('salary', 'Не указана')[:50],
                        'salary_min': job.get('salary_min', ''),
                        'salary_max': job.get('salary_max', ''),
                        'salary_currency': job.get('salary_currency', ''),
                        'salary_usd_min': job.get('salary_usd_min', ''),
                        'salary_usd_max': job.get('salary_usd_max', ''),
                        'url': job.get('url', '')[:200]
                    }
                    writer.writerow(row)

                    uid = job_uid(job)
                    if uid in persisted_notified or uid in sent_this_run:
                        continue

                    # Check min_salary threshold
                    salary_num = None
                    if job.get('salary_usd_min'):
                        try:
                            salary_num = int(job.get('salary_usd_min'))
                        except Exception:
                            salary_num = None
                    else:
                        try:
                            salary_num = self.extract_salary_number(job.get('salary', '') or job.get('description', '') or '')
                        except Exception:
                            salary_num = None

                    if min_salary and salary_num and salary_num < int(min_salary):
                        logger.info(f"Skipping notification due to min_salary ({min_salary}): {job.get('title')}")
                        continue

                    candidate_jobs.append(job)

                # Send notifications: either as one digest or per-job fallback
                if candidate_jobs:
                    try:
                        use_digest = bool(settings.get('use_digest', True))
                        if use_digest:
                            sent = send_telegram_digest(candidate_jobs)
                            if sent:
                                for job in candidate_jobs:
                                    try:
                                        persisted_notified.add(job_uid(job))
                                    except Exception:
                                        pass
                        else:
                            for job in candidate_jobs:
                                try:
                                    sent = send_telegram_notification(job)
                                    if sent:
                                        persisted_notified.add(job_uid(job))
                                except Exception as e:
                                    log_error(f'Notification send error: {str(e)[:200]}')
                    except Exception as e:
                        log_error(f'digest/fallback error: {str(e)[:200]}')

            # Persist updated notified set
            try:
                save_notified(persisted_notified)
            except Exception:
                log_error('Failed to save notified set at end of log_to_file')

            # Also write a live JSON file with recent vacancies for the Telegram bot
            try:
                live_jobs = []
                settings = getattr(self, 'settings', load_settings())
                max_live = int(settings.get('max_jobs_live', 200)) if settings.get('max_jobs_live') else 200
                for job in (self.results or [])[:max_live]:
                    item = {
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'source': job.get('source', ''),
                        'title': job.get('title', ''),
                        'company': job.get('company', ''),
                        'description': job.get('description', ''),
                        'salary': job.get('salary', 'Не указана'),
                        'salary_min': job.get('salary_min', ''),
                        'salary_max': job.get('salary_max', ''),
                        'salary_currency': job.get('salary_currency', ''),
                        'salary_usd_min': job.get('salary_usd_min', ''),
                        'salary_usd_max': job.get('salary_usd_max', ''),
                        'url': job.get('url', ''),
                        'posted': job.get('posted', ''),
                        'type': job.get('type', ''),
                        'relevance': job.get('relevance', 1)
                    }
                    live_jobs.append(item)
                try:
                    with open(JOBS_LIVE_FILE, 'w', encoding='utf-8') as lf:
                        json.dump(live_jobs, lf, ensure_ascii=False, indent=2)
                except Exception:
                    # fallback: try writing without indent for low-memory environments
                    with open(JOBS_LIVE_FILE, 'w', encoding='utf-8') as lf:
                        json.dump(live_jobs, lf, ensure_ascii=False)
            except Exception as e:
                log_error(f'Failed to write live jobs file: {str(e)[:200]}')
    
    def save_results(self):
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        json_file = f'jobs_{ts}.json'
        csv_file = f'jobs_{ts}.csv'
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        
        if self.results:
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                fieldnames = ['source', 'title', 'company', 'description', 'salary', 'salary_min', 'salary_max', 'salary_currency', 'salary_usd_min', 'salary_usd_max', 'url', 'posted', 'type', 'relevance']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                # ensure all rows have these keys
                rows = []
                for r in self.results:
                    row = {k: r.get(k, '') for k in fieldnames}
                    rows.append(row)
                writer.writerows(rows)
        
        print(f'\n✓ Сохранено: {json_file}')
        print(f'✓ Сохранено: {csv_file}')
    
    def run(self):
        print('Ищем вакансии по вашим навыкам...\n')
        sys.stdout.flush()
        # initialize status
        try:
            self._completed_scrapers = 0
        except Exception:
            self._completed_scrapers = 0
        try:
            self.write_status({'stage': 'starting', 'found_total': len(self.results), 'last_message': 'initializing'})
        except Exception:
            pass

        # Run scrapers in parallel to speed up collection
        # Optionally prefetch common endpoints asynchronously (aiohttp)
        if getattr(self, 'settings', {}).get('use_aiohttp_prefetch'):
            try:
                self.write_status({'stage': 'prefetching', 'last_message': 'prefetching async JSON endpoints'})
                # Use async JSON scraper to collect from common API endpoints.
                # This will append parsed jobs directly into self.results.
                self.async_scrape_json_sites()
                self.write_status({'stage': 'prefetching_done', 'found_total': len(self.results)})
            except Exception as e:
                log_error(f'prefetch invocation error: {str(e)[:200]}')

        scrapers = [
            self.scrape_indeed,
            self.scrape_hh_api,
            self.scrape_hh_ru,
            self.scrape_remoteok,
            self.scrape_we_work_remotely,
            self.scrape_habr_career,
            self.scrape_github_jobs,
            self.scrape_jusremote,
            self.scrape_working_nomads,
            self.scrape_remoteco,
            self.scrape_stackoverflow,
            self.scrape_devto,
            self.scrape_angel_list,
            self.scrape_superjob,
            self.scrape_freelance_ru,
            self.scrape_kwork,
            self.scrape_avito,
        ]

        total = len(scrapers)
        try:
            self.write_status({'stage': 'scraping', 'total': total, 'completed': 0})
        except Exception:
            pass

        max_workers = min(8, len(scrapers))
        def _run_scraper(scr):
            name = getattr(scr, '__name__', str(scr))
            try:
                try:
                    self.write_status({'stage': 'scraping', 'current_scraper': name, 'last_message': f'start {name}'})
                except Exception:
                    pass
                scr()
            except Exception as e:
                log_error(f'Error in scraper {name}: {str(e)[:200]}')
            finally:
                with self._status_lock:
                    try:
                        self._completed_scrapers += 1
                    except Exception:
                        self._completed_scrapers = getattr(self, '_completed_scrapers', 0) + 1
                    completed = int(self._completed_scrapers)
                try:
                    self.write_status({'stage': 'scraping', 'current_scraper': None, 'completed': completed, 'found_total': len(self.results)})
                except Exception:
                    pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_run_scraper, scr): scr.__name__ for scr in scrapers}
            for fut in concurrent.futures.as_completed(futures):
                name = futures.get(fut)
                try:
                    fut.result()
                except Exception as e:
                    log_error(f'Scraper {name} raised: {str(e)[:200]}\n{traceback.format_exc()}')
                    try:
                        self.write_status({'stage': 'scraping', 'last_message': f'{name} error: {str(e)[:200]}', 'found_total': len(self.results)})
                    except Exception:
                        pass
        
        self.deduplicate()
        try:
            self.write_status({'stage': 'normalizing', 'found_total': len(self.results), 'completed': int(getattr(self, '_completed_scrapers', 0))})
        except Exception:
            pass
        # Normalize salary fields for each job
        try:
            for job in self.results:
                try:
                    self.normalize_salary_fields(job)
                except Exception as e:
                    log_error(f'normalize per-job error: {str(e)[:200]}')
        except Exception as e:
            log_error(f'normalize loop error: {str(e)[:200]}')
        try:
            self.write_status({'stage': 'clustering', 'found_total': len(self.results)})
        except Exception:
            pass
        # Cluster similar jobs to help grouping
        try:
            ts_clusters = datetime.now().strftime('%Y%m%d_%H%M%S')
            clusters = None
            if getattr(self, 'settings', {}).get('use_embeddings'):
                try:
                    clusters = self.cluster_with_embeddings(threshold=self.settings.get('embedding_threshold', 0.78))
                except Exception as e:
                    log_error(f'embedding cluster error: {str(e)[:200]}')

            if not clusters:
                clusters = self.cluster_similar_jobs()

            clusters_summary = []
            for idx, grp in enumerate(clusters, 1):
                rep = grp[0]
                clusters_summary.append({
                    'cluster_id': idx,
                    'count': len(grp),
                    'title': rep.get('title'),
                    'source': rep.get('source'),
                    'example_url': rep.get('url')
                })
            with open(f'jobs_clusters_{ts_clusters}.json', 'w', encoding='utf-8') as f:
                json.dump({'generated': ts_clusters, 'clusters': clusters_summary}, f, ensure_ascii=False, indent=2)
            try:
                self.write_status({'stage': 'clusters_saved', 'found_total': len(self.results), 'last_message': f'clusters: {len(clusters_summary)}'})
            except Exception:
                pass
        except Exception as e:
            log_error(f'cluster write error: {str(e)[:200]}')
        
        print('\n' + '='*80)
        print(f'НАЙДЕНО: {len(self.results)} вакансий\n')
        
        for i, job in enumerate(self.results[:10], 1):
            print(f'{i}. {job["title"][:65]}')
            print(f'   {job["source"]} | {job["url"][:55]}')
            if job['description']:
                print(f'   {job["description"][:65]}...')
            print()
        
        if len(self.results) > 10:
            print(f'... и еще {len(self.results) - 10} вакансий в файле')
        
        print('='*80 + '\n')
        
        try:
            self.write_status({'stage': 'saving', 'found_total': len(self.results)})
        except Exception:
            pass
        self.save_results()
        try:
            self.write_status({'stage': 'completed', 'found_total': len(self.results), 'last_message': 'search finished'})
        except Exception:
            pass
        self.log_to_file()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Job Finder - автопоиск вакансий')
    parser.add_argument('--once', action='store_true', help='Выполнить один проход и выйти')
    parser.add_argument('--no-tg', action='store_true', help='Не отправлять Telegram уведомления')
    parser.add_argument('--interval', type=int, help='Интервал поиска в секундах (переопределяет настройки)')
    args = parser.parse_args()

    print('\n' + '='*80)
    print('JOB FINDER START')
    print('='*80)
    load_telegram_config()

    if args.no_tg:
        telegram_enabled = False
        logger.info('Telegram disabled via --no-tg')

    run_count = 0

    settings_global = load_settings()
    search_interval = args.interval if args.interval else settings_global.get('search_interval', 600)

    try:
        while True:
            run_count += 1
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f'\n{"="*80}')
            print(f'[{timestamp}] Поиск #{run_count}')
            print('='*80)

            # reload settings each cycle so changes via bot apply
            settings_global = load_settings()
            if settings_global.get('paused'):
                logger.info('Поиск приостановлен (paused=True). Ожидаем...')
                time.sleep(60)
                continue

            finder = JobFinder()
            finder.run()

            next_time = (datetime.now().timestamp() + search_interval)
            next_search = datetime.fromtimestamp(next_time).strftime('%H:%M:%S')
            print(f'\n⏰ Следующий поиск: {next_search} (через {search_interval//60} мин)')
            print(f'📝 История: jobs_log.csv\n')

            if args.once:
                break

            time.sleep(search_interval)

    except KeyboardInterrupt:
        print('\n\n' + '='*80)
        print(f'✓ Поиск остановлен. Выполнено поисков: {run_count}')
        print(f'✓ Все результаты сохранены в jobs_log.csv')
        print('='*80 + '\n')
