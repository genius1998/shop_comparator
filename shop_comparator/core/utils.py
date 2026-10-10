# -*- coding: utf-8 -*-

from __future__ import annotations

import tkinter.scrolledtext as scrolledtext
import json
import os
import os
import sys
import pandas as pd
import logging
import re
from typing import Optional

def setup_file_logger(log_name, file_path, level=logging.INFO):
    """Sets up a logger that writes to a specific file."""
    # 로거를 가져옵니다.
    logger = logging.getLogger(log_name)
    logger.setLevel(level)

    # 핸들러 중복 추가를 방지하기 위해 기존 핸들러를 제거합니다.
    if logger.hasHandlers():
        logger.handlers.clear()

    # 파일 핸들러를 생성하고 포매터를 설정합니다.
    # 'w' 모드로 파일을 열어 매번 실행 시 로그 파일을 새로 작성합니다.
    handler = logging.FileHandler(file_path, mode='w', encoding='utf-8')
    handler.setLevel(level)
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # 로거에 핸들러를 추가합니다.
    logger.addHandler(handler)

    return logger

def get_musinsa_brands_from_csv(csv_path):
    """Reads the specified CSV, filters for Musinsa brands, and returns a unique list of brand names."""
    try:
        df = pd.read_csv(csv_path)
        musinsa_brands = df[df['other_mall'] == 'musinsa']['other_mall_brand_id'].dropna().unique()
        return sorted(list(musinsa_brands))
    except FileNotFoundError:
        print(f"Error: The file {csv_path} was not found.")
        return []
    except Exception as e:
        print(f"An error occurred while reading or processing the CSV: {e}")
        return []

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def get_persistent_path(relative_path):
    """Get absolute path to a file that needs to persist across runs,
       placing it next to the executable in bundled apps."""
    if getattr(sys, 'frozen', False):
        # Running as a bundled executable
        base_path = os.path.dirname(sys.executable)
    else:
        # Running as a normal script
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

from shop_comparator.core.config import KREAM_COOKIES_FILE

def krw(n) -> str:
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)

def parse_krw(s: str) -> int | None:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        # 숫자와 빼기(-) 기호만 남기고 모든 문자 제거
        cleaned_s = ''.join(filter(lambda char: char.isdigit() or char == '-', s))
        if not cleaned_s:
            return None
        return int(cleaned_s)
    except (ValueError, TypeError):
        return None

def append_log(box: scrolledtext.ScrolledText, text: str):
    if not box:
        return
    box.configure(state="normal")
    box.insert("end", text + "\n")
    box.see("end")
    box.configure(state="disabled")

def invalidate_kream_token() -> tuple[bool, str]:
    """kream_cookies.json 파일의 첫 번째 쿠키 값을 변경하여 강제로 토큰을 만료시킵니다."""
    try:
        if not os.path.exists(KREAM_COOKIES_FILE):
            return False, f"{KREAM_COOKIES_FILE}이 존재하지 않습니다."
        
        with open(KREAM_COOKIES_FILE, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
        
        if not cookies:
            return False, "쿠키 파일이 비어있습니다."
        
        # 가장 중요한 인증 토큰일 가능성이 높은 첫 번째 쿠키의 값을 변경
        cookies[0]['value'] = "invalidated_by_test"
        
        with open(KREAM_COOKIES_FILE, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, indent=2)
            
        return True, "KREAM 쿠키가 성공적으로 무효화되었습니다. 다음 요청 시 재로그인이 필요합니다."
    except Exception as e:
        return False, f"쿠키 무효화 중 오류 발생: {e}"

from shop_comparator.core import size_converter

def get_product_gender(item_data: dict) -> str:
    """Inspects product name and category to determine its gender."""
    name = item_data.get("goodsName", "").upper()
    category = item_data.get("baseCategoryFullName", "").upper()
    
    # Keywords for women
    women_keywords = ["우먼스", "WOMEN", "WMNS", "여성"]
    if any(keyword in name or keyword in category for keyword in women_keywords):
        return "women"

    # Keywords for men
    men_keywords = ["맨즈", "MEN", "남성"]
    if any(keyword in name or keyword in category for keyword in men_keywords):
        return "men"
        
    return "unisex"

def _normalize_apparel_size(size_str: str) -> str:
    """Normalizes apparel size strings for consistent comparison."""
    if not isinstance(size_str, str):
        return ""
    
    s = size_str.strip().upper()
    
    # Rule: Convert 2XL -> XXL, 3XL -> XXXL, etc.
    def replacer(match):
        count = int(match.group(1))
        letters = match.group(2)
        return letters[0] * count + letters[1:]
    
    # Handles cases like 2XL, 3XXL, 2XS
    s = re.sub(r'(\d+)(X+[SL])', replacer, s)
    
    return s

def find_matching_musinsa_size(kream_size: str, musinsa_available_sizes: list[str], item_data: dict) -> Optional[str]:
    """
    Finds a matching Musinsa size. It first attempts a direct/normalized string comparison
    for apparel sizes, then falls back to a canonical 'mm' representation for shoe sizes.
    """
    if not kream_size or not musinsa_available_sizes:
        return None

    # 1. Direct and normalized comparison for apparel sizes
    normalized_kream_size = _normalize_apparel_size(kream_size)
    for m_size in musinsa_available_sizes:
        normalized_musinsa_size = _normalize_apparel_size(m_size)
        if normalized_musinsa_size == normalized_kream_size:
            return m_size  # Match found

    # 2. Fallback to 'mm' conversion for shoe sizes if no apparel match
    gender = get_product_gender(item_data)
    kream_mm = size_converter.get_mm_size(kream_size, gender)
    if not kream_mm:
        return None  # Cannot determine a comparable base size for the Kream item

    for m_size in musinsa_available_sizes:
        musinsa_mm = size_converter.get_mm_size(m_size, gender)
        if kream_mm == musinsa_mm:
            return m_size  # Return the original Musinsa size string

    return None
