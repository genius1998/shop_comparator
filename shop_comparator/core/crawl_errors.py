"""Safe, actionable upstream errors; never include response bodies or credentials."""


class CrawlError(RuntimeError):
    def __init__(self, service, code, message):
        self.service, self.code = service, code
        super().__init__(f"{service}: {message}")


def check_status(service, status):
    messages = {
        401: ("auth", "로그인이 만료되었습니다. 다시 로그인해 주세요 (401)."),
        403: ("blocked", "접근이 거절되었습니다. 브라우저에서 접속/인증 상태를 확인해 주세요 (403)."),
        429: ("rate_limit", "요청 한도에 도달했습니다. 잠시 후 다시 실행해 주세요 (429)."),
    }
    if status in messages:
        code, message = messages[status]
        raise CrawlError(service, code, message)
    if status >= 400:
        raise CrawlError(service, "http", f"서버 응답 오류 ({status}).")


def response_json(response, service="무신사"):
    check_status(service, response.status_code)
    try:
        data = response.json()
    except ValueError as exc:
        raise CrawlError(service, "format", "JSON 대신 다른 응답이 왔습니다. 사이트 접속 상태를 확인해 주세요.") from exc
    if not isinstance(data, dict):
        raise CrawlError(service, "schema", "API 응답 형식이 변경되었습니다.")
    if data.get("error") or data.get("success") is False:
        raise CrawlError(service, "api", "API가 오류를 반환했습니다. 로그인/서비스 상태를 확인해 주세요.")
    meta = data.get("meta") or {}
    if meta.get("result") not in (None, "SUCCESS"):
        # The shop's own short reason (e.g. "판매가 종료된 상품이 포함되어 있습니다.") is safe to show.
        reason = " ".join(str(meta.get("message") or "").split())[:100]
        code = str(meta.get("errorCode") or "")[:30]
        detail = f"{reason} [{code}]" if reason and code else reason or code
        raise CrawlError(service, "api", "API가 실패를 반환했습니다" + (f": {detail}" if detail else ". 로그인/서비스 상태를 확인해 주세요."))
    return data
