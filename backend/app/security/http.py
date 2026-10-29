from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .. import config

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _scheme(request: Request) -> str:
    if config.TRUST_PROXY:
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            return proto.split(",")[0].strip()
    return request.url.scheme


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        https = _scheme(request) == "https"
        if config.FORCE_HTTPS and not https and request.url.path != "/api/health":
            return RedirectResponse(str(URL(str(request.url)).replace(scheme="https")), status_code=308)

        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.MAX_BODY_BYTES:
            return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)

        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
