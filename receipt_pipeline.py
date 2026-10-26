"""PainFree receipt pipeline.

Stage 1  OCR       : NVIDIA Nemotron-3.5-Super-VL transcribes the photo line by line (no interpretation).
Stage 2  Parse     : deterministic Python parser pulls amounts, codes, dates, facility, totals out of the lines.
Stage 3  Reasoning : NVIDIA Nemotron 3 Ultra 550B receives the OCR lines + parsed items + patient context.

Nothing in this file contains receipt contents. Every value shown to the user comes from the uploaded image.
"""
import json
import re
import time
import urllib.request

OCR_MODEL = "nvidia/Nemotron-3.5-Super-VL"
REASONING_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"
MAX_IMAGE_CHARS = 12_000_000  # ~9 MB base64

OCR_PROMPT = ("Transcribe every printed text line of this document exactly as written, top to bottom, "
              "one line per output line. Keep all numbers, codes, dates and currency symbols exactly. "
              "Do not translate, summarize, explain or add anything. If the image is not a document, output NO_TEXT.")


def _post(base_url, api_key, payload, timeout):
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "PainFree-NebiusAgent/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


# ---------------------------------------------------------------- Stage 1: OCR
def ocr_image(base_url, api_key, data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ValueError("이미지 형식이 아닙니다.")
    if len(data_url) > MAX_IMAGE_CHARS:
        raise ValueError("이미지가 너무 큽니다.")
    t0 = time.time()
    data = _post(base_url, api_key, {
        "model": OCR_MODEL, "temperature": 0, "max_tokens": 3000,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": "/no_think"},
                     {"role": "user", "content": [{"type": "text", "text": OCR_PROMPT},
                                                  {"type": "image_url", "image_url": {"url": data_url}}]}]},
        timeout=90)
    text = (data["choices"][0]["message"].get("content") or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines == ["NO_TEXT"]:
        lines = []
    return lines, round((time.time() - t0) * 1000)


# ---------------------------------------------------------------- Stage 2: deterministic parse
_AMOUNT = re.compile(r"(?<![\w/:.\-])([$₩])?\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}|\d{2,7})(?:\s?(원|USD|KRW))?(?![\w/:%])")
_DATE = re.compile(r"(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\b\d{1,2}/\d{1,2}/\d{2}\b|\b\d{3}/\d{2}/\d{2}\b)")
_CPT = re.compile(r"\b(\d{5}|[A-Z]\d{4})\b")
_KR_EDI = re.compile(r"\b([A-Z]{1,2}\d{4,6}[A-Z0-9]{0,2})\b")
_TOTAL_KW = re.compile(r"(합\s*계|총\s*액|총\s*진료비|환자\s*부담\s*총액|납부(한|할)?\s*금액|수납\s*금액|본인\s*부담\s*총액|"
                       r"TOTAL|BALANCE|AMOUNT\s+DUE|PATIENT\s+RESPONSIBILITY|\bSum\b|Receivables|\bCharge\b|"
                       r"合\s*計|總\s*額|實\s*收|實付|應付|自付金額)", re.I)
# Lines that carry identifiers, not money (patient no., chart no., birth date, phone ...)
_ID_KW = re.compile(r"(번호|등록|생년|주민|전화|연락처|序號|代號|證號|病歷號|出生|電話|統一編號|"
                    r"\bNo\.|\bID\b|\bMRN\b|\bAccount\b|\bAcct\b|\bPhone\b|\bTel\b|\bDOB\b|\bBirth|\bPolicy\b|\bMember\b)", re.I)
_VISIT_DATE_KW = re.compile(r"(진료\s*(일|기간)|내원|수납일|就診|就醫日|看診|收費日期|Date of Service|Service Date|DOS|Hosp\.?\s*Date|Visit)", re.I)
_BIRTH_KW = re.compile(r"(생년|출생|出生|DOB|Birth)", re.I)
_NONCOVERED_KW = re.compile(r"(비급여|전액\s*본인|non[-\s]?covered|not\s+covered|self[-\s]?pay|自費|自付)", re.I)
_FACILITY_KW = re.compile(r"(병원|의원|한의원|치과|醫院|医院|Hospital|Medical Center|Clinic|Health System)", re.I)
_LABEL = re.compile(r"[A-Za-z가-힣\u4e00-\u9fff]{2,}")


def _amount_tokens(line):
    clean = _DATE.sub(" ", line)
    out = []
    for m in _AMOUNT.finditer(clean):
        raw = m.group(2)
        if "," not in raw and "." not in raw and len(raw) >= 6 and not m.group(1):
            continue  # long bare digit runs are IDs, not money
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        out.append({"raw": m.group(0).strip(), "value": val, "symbol": m.group(1) or m.group(3) or ""})
    return out


def detect_currency(lines):
    blob = "\n".join(lines)
    if re.search(r"[가-힣]", blob) or "₩" in blob or re.search(r"\d\s?원", blob):
        return "KRW"
    if re.search(r"[\u4e00-\u9fff]", blob):
        return "TWD"
    if "$" in blob or re.search(r"\bUSD\b", blob):
        return "USD"
    if _decimal_money_style(lines) and re.search(r"[A-Za-z]{3,}", blob):
        return "USD?"  # English document with cents-style amounts but no $ sign printed
    return "UNKNOWN"


def _decimal_money_style(lines):
    """True when most money-looking tokens are written with cents (e.g. 12.50). Then bare integers such as
    '20 ML', '1000' (fluid volume) or quantities are never treated as money."""
    dec = whole = 0
    for ln in lines:
        for a in _amount_tokens(ln):
            if re.search(r"\.\d{2}$", a["raw"]):
                dec += 1
            else:
                whole += 1
    return dec >= 3 and dec >= whole


_SECTION = re.compile(r"^\W*\*{2,}\s*(.+?)\s*\*{2,}")


def _split_code(line):
    """Pull a billing code (CPT/HCPCS/Korean EDI/hospital charge code) off the line so it is never read as money."""
    m = re.match(r"^\s*(\d{5}|[A-Z]\d{4}|\d{7,10})\b", line) or _KR_EDI.search(line)
    if not m:
        m = re.search(r"\b(?:CPT|HCPCS|코드)\s*[:#]?\s*(\d{5}|[A-Z]\d{4})\b", line, re.I)
    if not m:  # code in the middle of the row, followed by a text description (e.g. "10/25/2026 99284 ED VISIT ...")
        m = re.search(r"(?<![\d,.$/])\b(\d{5}|[A-Z]\d{4}|\d{7,10})\b(?=\s+[A-Za-z]{2,})", line)
    if not m:
        return "", line
    code = m.group(1)
    return code, (line[:m.start()] + " " + line[m.end():]).strip()


def parse_receipt(lines):
    facility, total = None, None
    visit_date, any_date = None, None
    items, totals = [], []
    decimal_mode = _decimal_money_style(lines)
    section = ""
    for i, line in enumerate(lines):
        if facility is None and _FACILITY_KW.search(line):
            facility = line[:80]
        dm = _DATE.search(line)
        if dm and not _BIRTH_KW.search(line):
            if visit_date is None and _VISIT_DATE_KW.search(line):
                visit_date = dm.group(1)
            if any_date is None:
                any_date = dm.group(1)
        sm = _SECTION.search(_DATE.sub(" ", line))
        if sm:  # e.g. "*** 250 PHARMACY GENERAL ***" (US revenue-code section header)
            section = sm.group(1)
            continue
        body_nodate = _DATE.sub(" ", line).strip()
        code, body = _split_code(body_nodate)
        amounts = [a for a in _amount_tokens(body) if not re.fullmatch(r"0\d+", a["raw"])]  # 0018, 001 are IDs
        if decimal_mode:
            amounts = [a for a in amounts if re.search(r"\.\d{2}$", a["raw"]) or a["symbol"]]
        if _TOTAL_KW.search(line):
            pool = amounts
            if not pool and i + 1 < len(lines) and not _LABEL.search(lines[i + 1]):
                pool = _amount_tokens(lines[i + 1])
            if pool:
                totals.append({"line": line[:120], "value": max(a["value"] for a in pool)})
            continue
        if _ID_KW.search(line):
            continue
        if not _LABEL.search(body):
            continue  # pure number rows are subtotals; they are kept in the raw OCR text
        if not amounts:
            if items and decimal_mode and items[-1]["line_no"] == i - 1 - items[-1].get("_cont", 0):
                items[-1]["detail"] = (items[-1].get("detail", "") + " " + body).strip()[:160]  # wrapped description
                items[-1]["_cont"] = items[-1].get("_cont", 0) + 1
            continue
        body = re.sub(r"\s+", " ", body).strip()
        price = amounts[-1]  # the charge column is the right-most money value
        first_amt_pos = body.find(amounts[0]["raw"])
        name = body[:first_amt_pos].strip(" :|-;") if first_amt_pos > 0 else body
        qty = None
        qm = re.search(r"(?:^|\s)(\d{1,3})$", name)
        if qm and decimal_mode:
            qty = int(qm.group(1))
            name = name[:qm.start()].strip(" ;")
        if not _LABEL.search(name):
            continue
        items.append({
            "name": name[:80], "code": code, "section": section, "qty": qty,
            "amounts": [a["raw"] for a in amounts],
            "amount": price["value"],
            "noncovered": bool(_NONCOVERED_KW.search(line)),
            "line_no": i})
    for it in items:
        it.pop("_cont", None)
    if totals:
        total = max(t["value"] for t in totals)
    return {"facility": facility, "date": visit_date or any_date, "currency": detect_currency(lines),
            "items": items, "total": total, "total_lines": [t["line"] for t in totals],
            "item_sum": round(sum(it["amount"] for it in items), 2),
            "line_count": len(lines)}


# ---------------------------------------------------------------- Stage 3: 550B reasoning
def _fmt_items(parsed):
    rows = []
    for it in parsed.get("items", []):
        tag = " [non-covered keyword]" if it.get("noncovered") else ""
        code = f" code={it['code']}" if it.get("code") else ""
        rows.append(f"- {it['name']}{code} amounts={', '.join(it['amounts'])}{tag}")
    return "\n".join(rows) or "- (parser found no item lines)"


def _mask_identifiers(lines):
    """Mask digit runs on identifier lines (patient no., chart no., phone, birth date) before they leave the server."""
    out = []
    for ln in lines:
        if _ID_KW.search(ln) or _BIRTH_KW.search(ln):
            ln = re.sub(r"[A-Z]?\d[\d*\-]{2,}", "***", ln)
        out.append(ln)
    return out


def build_reasoning_messages(ui_lang, patient, wearable, receipt):
    lines = _mask_identifiers(receipt.get("lines") or [])
    parsed = receipt.get("parsed") or {}
    out_lang = "Korean" if ui_lang == "KR" else "English"
    drop = wearable.get("drop_pct")
    drop_txt = f"{drop}% vs personal baseline" if drop is not None else "baseline not provided"
    system = (
        "You are a patient-side billing communication assistant (NVIDIA Nemotron 3 Ultra on Nebius Token Factory). "
        "You are not a doctor and not a lawyer: never diagnose, never declare fraud or illegality. "
        "Task: read a hospital receipt that was photographed by the patient (OCR text + parser output), compare it "
        "with the patient's own pain record and wearable activity, and identify which billed lines the patient could "
        "reasonably ask the billing office to explain (necessity, coverage category, duplicate or bundled charges, "
        "network status, unit counts). Use the billing framework of the country implied by the receipt "
        "(e.g. Korea: 급여/비급여 기준 and HIRA 진료비 확인 신청; USA: CPT/E&M level documentation and No Surprises Act; "
        "Taiwan: NHI coverage). Only cite items that actually appear in the OCR text; if the text is unclear, say so. "
        "PRIVACY: never copy names, ID/resident numbers, chart/account numbers, phone numbers or addresses from the "
        "receipt into your output; use placeholders such as [환자 이름] / [Patient name]. Do not invent a writing date; "
        "use [작성일] / [Date]. "
        f"LANGUAGE: every field, including inquiry_letter, MUST be written in {out_lang}, even if the receipt is in "
        "another language (quote billed item names as printed, then explain them in "
        f"{out_lang}). If the hospital's language differs from {out_lang}, also add inquiry_letter_local written in the "
        "hospital's language; otherwise set it to an empty string. Respond with JSON only: "
        '{"document_type": "...", "target_item": "exact billed line copied from the receipt", '
        '"reasoning": "evidence-based explanation linking patient data to the billed lines", '
        '"questions": ["polite question 1", "..."], '
        '"inquiry_letter": "polite inquiry draft to the hospital billing office", '
        '"inquiry_letter_local": "", '
        '"ocr_uncertainties": "anything unreadable or ambiguous"}'
    )
    user = (
        "PATIENT RECORD (entered by the patient)\n"
        f"- Pre-existing condition: {patient.get('condition') or 'not entered'}\n"
        f"- Pain regions: {patient.get('pain_regions') or 'not entered'}\n"
        f"- Pain VAS: {patient.get('vas')} / 10\n"
        f"- Body-map pins: {patient.get('pins', 0)}\n\n"
        "WEARABLE / ACTIVITY (entered by the patient)\n"
        f"- Steps today: {wearable.get('steps_today')}\n"
        f"- Usual daily steps (baseline): {wearable.get('steps_baseline')}\n"
        f"- Change: {drop_txt}\n"
        f"- Night-time awake minutes: {wearable.get('arousal_min')}\n\n"
        "RECEIPT - PARSER OUTPUT (deterministic)\n"
        f"- Facility line: {parsed.get('facility')}\n- Date: {parsed.get('date')}\n"
        f"- Currency guess: {parsed.get('currency')}\n- Total guess: {parsed.get('total')}\n"
        f"- Item lines:\n{_fmt_items(parsed)}\n\n"
        "RECEIPT - RAW OCR LINES (NVIDIA Nemotron-3.5-Super-VL)\n" + "\n".join(lines[:200])
    )
    return system, user


def _extract_json(raw_text):
    if "```json" in raw_text:
        raw_text = raw_text.split("```json", 1)[1].split("```", 1)[0]
    s, e = raw_text.find("{"), raw_text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("model returned no JSON")
    return json.loads(raw_text[s:e + 1], strict=False)


def reason(base_url, api_key, ui_lang, patient, wearable, receipt):
    system, user = build_reasoning_messages(ui_lang, patient, wearable, receipt)
    t0 = time.time()
    data = _post(base_url, api_key, {
        "model": REASONING_MODEL, "temperature": 0.1, "max_tokens": 2500,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=120)
    raw = data["choices"][0]["message"].get("content") or ""
    result = _extract_json(raw)
    result["model"] = REASONING_MODEL
    result["live_latency_ms"] = round((time.time() - t0) * 1000)
    result["is_live"] = True
    result["status"] = "success"
    return result
