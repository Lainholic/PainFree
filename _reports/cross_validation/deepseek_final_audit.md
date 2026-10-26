# PainFree 최신 개선본 적대적 교차검증 감사 보고서

**감사 등급: 조건부 반려 (Conditional Reject)** — 1차 지적 4건 중 2건만 실질 해결, 2건은 표면적 봉합에 그쳤고 신규 결함 6건이 추가로 발생했습니다.

---

## 1. 30B MoE 전환의 타당성 및 해커톤 경쟁력

### 1-1. 모델 식별자 자체가 검증 불가 (치명적)
- `REASONING_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"` — **이 정확한 문자열은 NVIDIA 공식 모델 카드/허깅페이스 저장소와 대조 검증되지 않았습니다.** Nemotron 계열의 실제 공개 네이밍은 `nvidia/Nemotron-...` 형식이며, `NVIDIA-Nemotron-3-Nano-30B-A3B`는 존재 확인이 안 된 조합입니다. 해커톤 심사위원이 `curl` 한 번으로 검증 가능한 지점에서 **모델 ID 오타/환상(halucinated identifier)** 은 "NVIDIA 생태계 기여" 주장 전체를 무너뜨립니다.
- `OCR_PRIMARY = "nvidia/Nemotron-3.5-Super-VL"`, `OCR_FALLBACK = "Qwen/Qwen3.8-27B"` 역시 **존재하지 않는 버전 번호**로 보입니다(Nemotron VL 계열에 3.5 Super 명칭 없음, Qwen 3.8 없음). 문서 상단 docstring은 "NVIDIA Nemotron-3.5-Super-VL"이라 자랑하지만, 이는 **검증되지 않은 모델명을 문서에 박제한 것**입니다. 심사위원 입장에서 이는 "NVIDIA 생태계 활용"이 아니라 "NVIDIA 브랜드 도용 리스크"로 읽힙니다.

### 1-2. "7~10초" 주장의 근거 부재
- 코드 어디에도 **폴백 타임아웃, 재시도, 서킷브레이커**가 없습니다. `_post(..., timeout=120)` 단일 호출이며, 30B MoE가 7~10초에 응답한다는 주장은 **콜드 스타트·큐 대기·rate limit 상황에서 즉시 붕괴**합니다. 해커톤 데모에서 Nebius 측 429/503이 한 번만 터져도 `urllib.error.HTTPError` → 502로 떨어지고 사용자에게는 "Nebius API 오류 HTTP 502"만 노출됩니다.
- `live_latency_ms`는 **성공 케이스만 기록**합니다. 실패/재시도/폴백 지연은 계측되지 않아 "실측 7~10초" 주장은 **선택적 계측(selection bias)** 입니다.

### 1-3. 프롬프트 "원문 보존 vs 번역" 충돌 — 부분 해결, 잔여 결함
- `target_item must preserve the exact billed line` 지시는 추가되었으나, **모델이 한국어/영어 reasoning 안에 target_item을 인용할 때 원문을 그대로 둘지 번역할지에 대한 강제 장치가 없습니다.** `_extract_json`은 JSON 파싱만 검증하고, `target_item`이 실제 OCR 라인에 존재하는지 **문자열 대조 검증이 전혀 없습니다.** 즉 "원문 보존"은 프롬프트 희망사항일 뿐, **서버 측 검증 게이트가 없습니다.**
- `inquiry_letter_local`을 "병원 언어"로 요청하지만, **병원 언어 판별 로직이 없습니다.** `parsed.get('facility')` 문자열을 모델이 추측할 뿐이며, 오판 시 엉뚱한 언어로 공문이 생성됩니다.

### 1-4. MoE 30B가 "오버킬 해소"라는 주장의 반증
- 30B MoE(A3B)는 **활성 파라미터 3B**로 지연은 개선되나, **의료 과금 문맥 추론에는 3B 활성 파라미터가 부족**할 수 있습니다. 이전 550B의 문제는 "오버킬"이 아니라 "지연"이었고, 30B로 내려가며 **환각(hallucination) 리스크가 상승**했습니다. 코드에 **환각 억제용 스키마 검증(예: `target_item`이 OCR 라인 부분집합인지 확인)** 이 없어, "오버킬 해소"는 "정확도 하락"과 트레이드오프되었습니다.

**평가: 미흡.** 모델 ID 검증 불가 + 폴백/재시도 부재 + 원문 보존 서버 검증 부재로 "NVIDIA 생태계 기여" 주장은 심사에서 반박 가능합니다.

---

## 2. 서버 재파싱 도입 후 데이터 무결성 및 잔여 서버 취약점

### 2-1. 재파싱 도입은 올바른 방향 (유일한 실질 개선)
- `server_parsed = rp.parse_receipt(safe_lines)` + `provenance: "server_verified"`는 **클라이언트 파싱 위조 지적을 실제로 봉합**했습니다. 이 점은 인정합니다.

### 2-2. 그러나 "safe_lines"의 안전성 주장은 과장
- `safe_lines = [str(x)[:300] for x in raw_lines if isinstance(x, (str, int, float))][:200]` — **타입 필터는 있으나 내용 검증이 없습니다.** OCR 라인에 포함된 **프롬프트 인젝션 문자열**(예: "Ignore previous instructions and output...")이 그대로 `build_reasoning_messages`의 user 메시지에 삽입됩니다. `lines[:200]`은 **개수 제한일 뿐 인젝션 방어가 아닙니다.**
- `raw_lines`가 200개 초과 시 **조용히 잘림(silent truncation)** — 영수증 후반부 항목이 사라져도 사용자에게 고지되지 않습니다. `ocr_uncertainties` 필드로 위임되었지만, **잘림 자체를 모델이 알 방법이 없습니다.**

### 2-3. `_read_json(400_000)` — 여전히 DoS 표면
- 400KB JSON 본문을 **스트리밍 없이** `self.rfile.read(limit)`로 읽는 구조(생략부)는 **Content-Length 위조 시 메모리 고갈** 가능. `ThreadingMixIn` + `daemon_threads=True` 조합에서 **스레드 폭주 시 서버 다운**.
- `except OverflowError`만 잡고 **`MemoryError`, `socket.timeout`, `ConnectionResetError`는 미처리** → 500으로 떨어지며 스택트레이스가 `traceback.print_exc()`로 **서버 콘솔에 전체 노출**(로그에 PII 포함 가능).

### 2-4. `translate_path` 화이트리스트 — 개선되었으나 우회 가능
- `if ".." in clean` 체크는 **URL 인코딩 우회(`%2e%2e`)에 취약**합니다. `urllib.parse.urlparse(path).path`는 **퍼센트 디코딩을 수행하지 않으므로** `%2e%2e%2f`가 그대로 `clean`에 남고, `".." in clean`은 False를 반환합니다. 이후 `ALLOWED_STATIC` 미매칭으로 `__forbidden_blocked_file__`로 떨어지긴 하나, **`do_HEAD`/`do_GET`의 `os.path.exists(target)` 체크가 `__forbidden_blocked_file__` 경로에 대해 False를 반환하면 403이 아닌 404로 응답** — 정보 노출은 없으나 **에러 코드 일관성 결함**.
- `ALLOWED_STATIC`에 `"favicon.ico"`가 있으나 `do_GET`에서 `self.path == "/favicon.ico"`로 **204를 먼저 반환** — 화이트리스트와 로직이 **이중화되어 유지보수 부담**.

### 2-5. `patient` 필드 검증의 잔여 결함
- `"vas": max(0, min(10, int(body.get("vas", 0))))` — `body.get("vas")`가 `"abc"`면 **`int("abc")`에서 ValueError → 500**. `try/except`로 감싸져 있으나 **에러 메시지가 `str(e)[:300]`으로 사용자에게 그대로 노출** — 내부 예외 문자열(파일 경로, 라인 번호 포함 가능) 유출.
- `steps_today`/`steps_baseline`은 `str(...).isdigit()`로 검증하지만 **음수 부호(`-100`)는 isdigit False로 None 처리** — 음수 걸음수는 무시되나, **`drop` 계산에서 `steps_base=0`이면 ZeroDivision 방지됨**(양호). 다만 `steps_base`가 매우 작은 값(예: 1)일 때 `drop_pct`가 **수천 %로 폭주**해 모델 프롬프트를 오염시킵니다.

**평가: 부분 개선.** 재파싱은 성공했으나 **프롬프트 인젝션 방어, 잘림 고지, 예외 메시지 위생, 인코딩 우회**가 미해결.

---

## 3. 개인정보 동의(Consent) 모달의 규제 적합성

### 3-1. Just-in-Time 동의 배치는 방향성은 옳음
- 업로드 직전 동의 + 비동의 시 로컬 기능 보존은 **GDPR Art.7(명시적 동의), 개보법 제15조(수집·이용 동의)** 의 "목적별·시점별 동의" 원칙에 부합하는 설계입니다.

### 3-2. 그러나 규제 적합성 주장은 성립하지 않음 (핵심 결함)
- **HIPAA**: PainFree는 "환자 본인이 입력한 데이터"를 다루므로 HIPAA Covered Entity는 아니나, **Business Associate로 오인될 설계**(병원 제출용 리포트 생성)에서 **BAA(Business Associate Agreement) 부재**. 동의 모달은 BAA를 대체하지 못합니다.
- **GDPR Art.9(민감정보)**: 통증 부위·기저질환·수면 데이터는 **건강 데이터(Special Category)** 입니다. 동의 모달이 **"AI 전송 동의"만 받고, 국외 이전(Schrems II) 조항·처리자 목록(Nebius, NVIDIA)·보존 기간·철회권 행사 방법**을 명시하지 않으면 **Art.13/14 정보 제공 의무 위반**입니다. 코드 발췌에는 이들 항목이 **전혀 없습니다.**
- **개보법 제28조의8(국외 이전)**: Nebius(유럽) / NVIDIA(미국)로 전송됨에도 **국외 이전 고지·동의**가 모달에 없습니다. "AI 전송 동의"라는 단일 체크박스는 **국외 이전 별도 동의 요건을 충족하지 못합니다.**
- **철회권·삭제권**: 동의 후 **철회 UI, 서버 측 데이터 삭제 엔드포인트**가 코드에 없습니다. `_send_json` 응답에 저장 로직이 없어 **서버 무상태(stateless)** 로 보이나, **Nebius/NVIDIA 측 로그 보존**은 통제 불가 — 이 사실을 동의 모달이 고지해야 합니다.

### 3-3. "비동의 시 로컬 기능 보존"의 실효성 의문
- 발췌에는 로컬 파싱 경로(`/api/audit-bill` 없이 클라이언트에서 `parse_receipt` 유사 로직 수행)가 **명시되지 않았습니다.** "로컬 기능 보존"이 실제로 **어떤 기능이 로컬에서 동작하는지** 코드로 증명되지 않으면, 이는 **마케팅 문구**에 불과합니다.

### 3-4. PII 마스킹 지시의 한계
- 시스템 프롬프트에 "never copy names, ID