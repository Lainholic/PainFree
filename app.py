import os
import sys
import json
import re
import urllib.request
import urllib.error
import urllib.parse
import http.server
import socketserver
import time

PORT = 8585
HOST = "127.0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. API 키 보안: 소스코드에 하드코딩하지 않고 .env 파일 및 환경변수에서만 안전하게 로드
ENV_PATH = os.path.join(BASE_DIR, ".env")
NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY", "")
NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")

import receipt_pipeline as rp

if os.path.exists(ENV_PATH):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if line.startswith("NEBIUS_API_KEY="):
                    NEBIUS_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("NEBIUS_BASE_URL="):
                    NEBIUS_BASE_URL = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        print(f"[알림] .env 로드 예외: {e}")

USE_LIVE_API = bool(NEBIUS_API_KEY)


class HackathonHandler(http.server.SimpleHTTPRequestHandler):
    ALLOWED_STATIC = {
        "": "index.html",
        "index.html": "index.html",
        "body.png": "body.png",
        "body_front.svg": "body_front.svg",
        "body_back.svg": "body_back.svg",
        "sample_receipt_1.jpg": "sample_receipt_1.jpg",
        "sample_receipt_2.jpg": "sample_receipt_2.jpg",
        "favicon.ico": "favicon.ico"
    }

    def translate_path(self, path):
        """철저한 보안 격리: 오직 화이트리스트에 등록된 정적 자산만 서빙하고 .env, .py 등은 접근 원천 차단"""
        parsed = urllib.parse.urlparse(path).path
        clean = parsed.lstrip("/").replace("\\", "/")
        if ".." in clean:
            return os.path.join(BASE_DIR, "__forbidden_path_traversal__")

        if clean in self.ALLOWED_STATIC:
            return os.path.join(BASE_DIR, self.ALLOWED_STATIC[clean])

        return os.path.join(BASE_DIR, "__forbidden_blocked_file__")

    def do_HEAD(self):
        target = self.translate_path(self.path)
        if "__forbidden" in target or not os.path.exists(target):
            self.send_error(403, "Access Denied: Protected Resource")
            return
        super().do_HEAD()

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        target = self.translate_path(self.path)
        if "__forbidden" in target or not os.path.exists(target):
            self.send_error(403, "Access Denied: Protected Resource")
            return
        super().do_GET()

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, limit):
        length = int(self.headers.get("Content-Length", 0))
        if length > limit:
            raise OverflowError("payload too large")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(raw) if raw else {}

    def do_POST(self):
        if self.path not in ("/api/scan-receipt", "/api/audit-bill"):
            self.send_error(404, "Not Found")
            return
        if not USE_LIVE_API:
            self._send_json(503, {"status": "error", "error": "NEBIUS_API_KEY 미설정: 실제 모델을 호출할 수 없습니다."})
            return
        try:
            if self.path == "/api/scan-receipt":
                body = self._read_json(rp.MAX_IMAGE_CHARS + 10_000)
                lines, ocr_ms, model_used = rp.ocr_image(NEBIUS_BASE_URL, NEBIUS_API_KEY, body.get("image"))
                t0 = time.time()
                parsed = rp.parse_receipt(lines)
                parse_ms = round((time.time() - t0) * 1000, 1)
                self._send_json(200, {"status": "success", "ocr_model": model_used, "ocr_latency_ms": ocr_ms,
                                      "parse_latency_ms": parse_ms, "lines": lines, "parsed": parsed})
                return

            body = self._read_json(400_000)
            receipt = body.get("receipt") or {}
            raw_lines = receipt.get("lines") or []
            if not raw_lines:
                self._send_json(400, {"status": "error", "error": "영수증 인식 결과가 없습니다. 먼저 영수증 사진을 올려 주세요."})
                return
            try:
                vas_val = int(body.get("vas", 0))
            except (ValueError, TypeError):
                vas_val = 0
            try:
                pins_val = int(body.get("pins", 0))
            except (ValueError, TypeError):
                pins_val = 0
            patient = {
                "condition": str(body.get("condition", ""))[:120],
                "pain_regions": str(body.get("pain_regions", ""))[:200],
                "vas": max(0, min(10, vas_val)),
                "pins": max(0, pins_val),
            }
            w = body.get("wearable") or {}
            steps_today = int(w["steps_today"]) if str(w.get("steps_today", "")).isdigit() else None
            steps_base = int(w["steps_baseline"]) if str(w.get("steps_baseline", "")).isdigit() else None
            drop = round((steps_today - steps_base) / steps_base * 100) if steps_today is not None and steps_base else None
            wearable = {"steps_today": steps_today, "steps_baseline": steps_base, "drop_pct": drop,
                        "arousal_min": int(w["arousal_min"]) if str(w.get("arousal_min", "")).isdigit() else None}
            safe_lines = [str(x)[:300] for x in raw_lines if isinstance(x, (str, int, float))][:200]
            # Server-side deterministic re-parse ensures data integrity and provenance
            server_parsed = rp.parse_receipt(safe_lines)
            safe_receipt = {"lines": safe_lines, "parsed": server_parsed, "provenance": "server_verified"}
            result = rp.reason(NEBIUS_BASE_URL, NEBIUS_API_KEY, "EN" if body.get("ui_lang") == "EN" else "KR",
                               patient, wearable, safe_receipt)
            self._send_json(200, result)
        except OverflowError:
            self._send_json(413, {"status": "error", "error": "요청이 너무 큽니다."})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            print(f"[!] Nebius HTTP {e.code}: {detail}")
            self._send_json(502, {"status": "error", "error": f"Nebius API 오류 HTTP {e.code}"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
            print(f"[!] 처리 오류: {e}", flush=True)
            self._send_json(500, {"status": "error", "error": str(e)[:300]})


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"[*] 페인프리 서버 가동: http://{HOST}:{PORT}")
    print(f"[*] Nebius 라이브 모드: {USE_LIVE_API} | OCR={rp.OCR_MODEL} | 추론={rp.REASONING_MODEL}")
    with ThreadingServer((HOST, PORT), HackathonHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[*] 서버가 안전하게 종료되었습니다.")
