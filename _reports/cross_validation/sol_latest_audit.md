# 솔(Sol / gpt-6.1-sol) 2-Pass 적대적 아키텍처 감사 보고서
일시: 2026-10-27
모델: gpt-6.1-sol (Azure Worker)
검토 파일: app.py, receipt_pipeline.py, index.html

환님, 제공된 코드에 한정해 **2-Pass 정적 아키텍처 감사**를 수행했습니다. 파일 수정이나 외부 API 호출은 하지 않았습니다.

**판정: 실제 환자 영수증을 사용하는 공개 서비스로 배포하기에는 차단해야 할 위험이 남아 있습니다.** 핵심은 모델 크기가 아니라 **동의 이전 원본 전송, 검증되지 않은 복약 안내, 클라이언트 조작 가능한 증거, 불완전한 입력·출력 검증**입니다.

### 감사 범위와 증거 한계

- 검토 대상: 본문에 제공된 `app.py`, `receipt_pipeline.py`, `index.html` 발췌.
- 로컬 파일·운영지침·프로젝트 지침은 이 환경에서 조회할 수 없었습니다. 이를 읽었다거나 실제 최신 파일과 일치한다고 보고하지 않습니다.
- 실제 API 모델 제공 여부, 7~10초 지연시간, 브라우저 출력, 침투 테스트는 수행하지 않았습니다.
- 아래 근거는 허위 파일 행 번호 대신 **파일 → 함수/선택자 → 해당 소스 행의 정확한 인용**으로 식별합니다.

---

# Pass 1 — 신뢰 경계·개인정보·입력 처리 공격

## P1-01. 원본 이미지가 마스킹 전에 외부로 전송됩니다
**심각도: Critical — 실제 민감정보 입력 허용 전 해결 필요**

`receipt_pipeline.py → ocr_image()`:
```python
optimized_url = _prepare_image(data_url)
```
동일 함수의 1차·폴백 요청:
```python
{"type": "image_url", "image_url": {"url": optimized_url}}
```
`receipt_pipeline.py → build_reasoning_messages()`:
```python
lines = _mask_identifiers(receipt.get("lines") or [])
```

**문제**
마스킹은 **OCR가 끝난 후 추론용 텍스트를 만들 때** 발생합니다. OCR 요청에는 사진 자체가 들어갑니다. `_prepare_image()`는 해상도·인코딩 변경일 뿐, 이름·주민번호·환자번호·주소를 가리지 않습니다.
따라서 동의 문구가 “마스킹된 정보만 전송”이라는 인상을 준다면 실제 전송 경로와 불일치합니다. 현재 서버의 원본 이미지 전송은 확인됩니다.

---

## P1-02. 추론 단계의 마스킹도 우회 경로가 있습니다
**심각도: High**

`receipt_pipeline.py → _mask_identifiers()`:
```python
if _ID_KW.search(ln) or _BIRTH_KW.search(ln):
    ln = re.sub(r"[A-Z]?\d[\d*\-]{2,}", "***", ln)
```
키워드가 없는 이름·주소·이메일 행은 통과하며, 사용자가 입력한 기저질환·통증 부위 문자열 역시 개인정보 필터 없이 전달됩니다.

---

## P1-03. `server_reparsed`는 입력의 진위나 OCR 출처를 보장하지 않습니다
**심각도: High**

`app.py → do_POST()`:
```python
server_parsed = rp.parse_receipt(safe_lines)
safe_receipt = {"lines": safe_lines, "parsed": server_parsed, "provenance": "server_reparsed"}
```
클라이언트가 제출한 `safe_lines`를 서버가 다시 파싱할 뿐, 이것이 실제 영수증 사진에서 나온 것인지 대조하는 서버 세션/서명이 없습니다.

---

## P1-04. `Content-Length` 방어와 스키마 검증이 fail-closed가 아닙니다
**심각도: High**

잘못된 길이를 0으로 바꾸는 것은 거부가 아니라 빈 요청으로 정규화하는 동작이며, 잘못된 타입이 400이 아닌 500으로 빠질 수 있습니다.

---

# Pass 2 — 임상 안전성·파서 오류·출력·제품 주장 공격

## P2-01. “공인 복약설명서 기반”이라는 표시를 뒷받침하는 경로가 없습니다
**심각도: Critical — 환자 행동에 영향을 주는 안내**

UI 문구는 "공인 복약설명서 기반"이라고 하지만, 코드상에 식약처/FDA DB 조회(RAG)가 연결된 것이 아니라 LLM의 사전 학습 지식에만 의존합니다.
따라서 RAG가 없는 상태에서 "공인 설명서 기반"이라고 표기하는 것은 오해를 부를 수 있습니다.

## P2-02. 결정론적 파서의 금액/급여 판별 한계
`parse_receipt()`에서 최대값을 total로 선택하거나 숫자 열 개수로 비급여를 판단하는 규칙은 복잡한 영수증 서식에서 오차를 낼 수 있습니다.

## P2-03. `setdefault`는 출력 스키마 가드가 아닙니다
잘못된 타입(`questions`가 문자열이거나 `medication_advisory` 내부 타입 오류)이 들어왔을 때 `setdefault`는 이를 고치지 못합니다.

---

# 우선 조치와 출시 게이트

| 우선순위 | 배포 전 요구사항 |
|---|---|
| P0 | 원본 이미지 외부 전송과 동의 문구 일치 (사진 전송 사실 투명 고지) |
| P0 | RAG 조회가 없다면 "공인 설명서 기반"이라는 표현 대신 "일반적인 일상 주의 및 권고"로 문구 정합성 조정 |
| P0 | 서버 OCR 결과와 감사 요청 결합 (세션/토큰 바인딩) |
| P1 | 엄격한 입력·출력 타입 검증 (fail-closed) |
