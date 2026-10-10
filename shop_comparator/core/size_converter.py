# -*- coding: utf-8 -*-

from __future__ import annotations

def normalize_size(size_str: str) -> str:
    """
    다양한 형태의 사이즈 문자열을 비교 가능한 형태로 정규화합니다.
    예: '260', '260mm', 'US 8', 'UK 7', 'EU 41', 'L', 'Large' 등
    """
    if not size_str:
        return ""
    
    s = str(size_str).upper().strip()
    
    # 1. 숫자만 남기고 제거 (소수점 포함) -> mm 사이즈나 US/UK 사이즈 숫자만 추출 목적
    # 단, 의류 사이즈(S, M, L 등)는 예외
    if any(c.isdigit() for c in s):
        # 괄호 안의 내용 제거 (예: "260(US 8)" -> "260")
        if '(' in s:
            s = s.split('(')[0]
        
        # 숫자와 소수점만 추출
        import re
        nums = re.findall(r"[-+]?\d*\.\d+|\d+", s)
        if nums:
            # 가장 먼저 나오는 숫자를 사이즈로 간주 (단순화)
            return nums[0]
            
    return s

def get_mm_size(size_str: str, gender='W') -> str | None:
    """
    US/UK/EU 사이즈를 한국 mm 사이즈로 변환합니다.
    gender: 'W' (여성), 'M' (남성), 'U' (공용 - 기본적으로 남성 기준 처리하거나 별도 로직)
    """
    s = str(size_str).strip()
    
    # 이미 mm 사이즈인 경우 (3자리 숫자이고 200 이상)
    if s.isdigit() and int(s) >= 200:
        return s
        
    try:
        size_num = float(s)
    except ValueError:
        return None # 숫자로 변환 불가하면 변환 포기

    # 여성 사이즈 변환표 (제공된 기준)
    # US 5 -> 220, 5.5 -> 225 ... (0.5 단위당 5mm 증가)
    # 기준: US 5 = 220
    if gender == 'W':
        if 5.0 <= size_num <= 12.0: # 대략적인 여성 US 사이즈 범위
            mm = 220 + (size_num - 5.0) * 10
            # 220, 225, 230 처럼 5단위로 떨어지도록 정수화
            return str(int(mm))
    
    # 남성 사이즈 변환표 (제공된 기준)
    # US 7 -> 250, 7.5 -> 255 ...
    # 기준: US 7 = 250
    elif gender == 'M':
        if 7.0 <= size_num <= 15.0: # 대략적인 남성 US 사이즈 범위
            mm = 250 + (size_num - 7.0) * 10
            return str(int(mm))

    return None

def find_ugg_match(musinsa_size: str, kream_sizes: list) -> str | None:
    """
    UGG 브랜드 전용 매칭 로직 (필요시 확장)
    """
    return None
