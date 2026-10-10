import subprocess
import re
import sys

def get_serial_number(product_num):
    # 1) 여기 안에 "네 환경에서 잘 돌아가는 curl 명령어"를 그대로 붙여넣으면 됨
    curl_cmd = rf'''curl "https://www.musinsa.com/products/{product_num}" ^
      -H "accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7" ^
      -H "accept-language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7" ^
      -H "cache-control: no-cache" ^
      -b "_gf=A; tr[vid]=68ee0af160f930.53764623; tr[vd]=1760430833; ..." ^
      -H "pragma: no-cache" ^
      -H "priority: u=0, i" ^
      -H "referer: https://www.musinsa.com/main/musinsa/ranking?gf=A&storeCode=musinsa&sectionId=199&contentsId=&categoryCode=103000&ageBand=AGE_BAND_ALL&subPan=product" ^
      -H "sec-ch-ua: \"Chromium\";v=\"142\", \"Google Chrome\";v=\"142\", \"Not_A Brand\";v=\"99\"" ^
      -H "sec-ch-ua-mobile: ?0" ^
      -H "sec-ch-ua-platform: \"Windows\"" ^
      -H "sec-fetch-dest: document" ^
      -H "sec-fetch-mode: navigate" ^
      -H "sec-fetch-site: same-origin" ^
      -H "sec-fetch-user: ?1" ^
      -H "upgrade-insecure-requests: 1" ^
      -H "user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    '''

    # 2) curl 실행 (cmd에서 치는 거랑 똑같이)
    proc = subprocess.run(
        curl_cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding='utf-8'
    )

    if proc.returncode != 0:
        # stderr에 에러를 출력하고 None을 반환
        print(f"curl 실행 에러 for product_num {product_num}: {proc.stderr}", file=sys.stderr)
        return None

    html = proc.stdout

    # 3) HTML 안에서 "styleNo":"IF6490" 같은 패턴 찾기
    m = re.search(r'"styleNo"\s*:\s*"([^"]+)"', html)
    if m:
        style_no = m.group(1)
        return style_no
    else:
        # 찾지 못한 경우 stderr에 메시지를 출력하고 None을 반환
        print(f"styleNo(품번) 패턴을 HTML에서 찾지 못했음 for product_num {product_num}", file=sys.stderr)
        return None

if __name__ == "__main__":
    if len(sys.argv) > 1:
        product_num = sys.argv[1]
        serial = get_serial_number(product_num)
        if serial:
            print(serial)
    else:
        print("Usage: python musinsa_serial.py <product_num>", file=sys.stderr)