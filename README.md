# PainFree (페인프리)

> Patient-side subjective pain telemetry archive and medical bill causal inquiry assistant, built for the Nebius x NVIDIA Global AI Hackathon.
> Designed and directed by a solo non-developer creator pairing with autonomous AI agents (Google Antigravity & OpenAI Codex).

PainFree connects two sides of post-operative and chronic pain care: the patient's daily pain telemetry (VAS, localized anatomical laser pins, and wearable step drops) and the hospital's dense itemized bill. Directed by a solo non-developer creator and built via AI pair programming, it uses NVIDIA Nemotron vision and reasoning models hosted on the Nebius Token Factory to audit billed lines, separate in-hospital surgical drugs from discharge care, and draft polite inquiry letters for hospital billing offices.

---

## Demo Video

[![PainFree Demo Walkthrough](https://img.youtube.com/vi/QE34rI7h5Sg/0.jpg)](https://youtu.be/QE34rI7h5Sg)

- **YouTube Walkthrough**: [https://youtu.be/QE34rI7h5Sg](https://youtu.be/QE34rI7h5Sg)
- **Duration**: 48 seconds (1280x800 HD)
- **Features Highlighted**: Subjective VAS & body map pins, wearable activity drop, authentic US itemized bill scanning via Nemotron-3.5-Super-VL, ~8.2s Nemotron 30B MoE clinical reasoning, in-hospital procedural medication separation, and 1-page doctor summary.

---

## What PainFree Does

1. **Subjective Pain Archive**: Records VAS (0 to 10) pain intensity and maps localized pain points on front and back SVG anatomical body maps.
2. **Wearable Drop Telemetry**: Tracks daily step drops against personal baselines (e.g. -62% reduction during acute flares) and nightly arousal minutes.
3. **Medical Bill Causal Audit**:
   - **Stage 1 (Vision OCR)**: Reads photographed hospital bills using `nvidia/Nemotron-3.5-Super-VL` via Nebius.
   - **Stage 2 (Deterministic Parser)**: Extracts items, codes, and billed amounts into structured tables on the server.
   - **Stage 3 (Nemotron 30B MoE Reasoning)**: Runs `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` on Nebius to cross-reference the patient's recorded telemetry with the billed charges, drafting a specific inquiry letter for hospital billing staff.
4. **Procedural Medication Separation**: Distinguishes in-hospital surgical drugs (Propofol, Rocuronium, Bupivacaine) given under clinical supervision from discharge precautions, preventing misleading home care advice.
5. **Doctor Summary Report**: Prints a clean 1-page summary chart correlating 7-day pain levels with step count telemetry for clinical visits.

---

## Architecture & Data Flow

```text
[User Photo / Real Bill]
          │
          ▼
1. Vision OCR (Nebius Token Factory)
   Model: nvidia/Nemotron-3.5-Super-VL (~4.8s)
          │
          ▼
2. Deterministic Server Parser (Python / regex)
   Extracts: Facility, Date, Currency, Item Lines, Codes, Totals (~1ms)
          │
          ▼
3. Clinical Telemetry Context
   Merged with: Pain VAS, Anatomical Pins, Wearable Step Drop
          │
          ▼
4. Nemotron 30B MoE Reasoning (Nebius Token Factory)
   Model: nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B (~8.2s)
          │
          ▼
[Hospital Inquiry Draft + Procedural Isolation + 1-Page Summary]
```

---

## Local Setup & Quickstart

### Prerequisites
- Python 3.10+
- Nebius API Key (with access to Token Factory endpoints)

### 1. Clone & Configure
```bash
git clone https://github.com/your-username/PainFree.git
cd PainFree
```

Create a `.env` file in the project root:
```env
NEBIUS_API_KEY=your_nebius_api_key_here
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
```

### 2. Install Dependencies
```bash
pip install pillow
```
*(Optional for test automation: `pip install playwright && playwright install chromium`)*

### 3. Run the Server
```bash
python app.py
```
Open `http://127.0.0.1:8585` in your browser.

---

## Key Technical Decisions

- **Nebius Token Factory 30B MoE**: We initially tested large 550B models, but 25–40s response times felt clunky for a patient interface. Switching to Nemotron-3-Nano-30B-A3B cut end-to-end reasoning down to 8.2 seconds without losing clinical extraction quality.
- **Procedural Drug Separation**: Hospital surgical bills include heavy IV anesthetics (Propofol) and paralytics (Rocuronium). Giving home-care advice like "take with food" on these items is clinically incorrect. The pipeline separates in-hospital surgical medications from take-home instructions.
- **Deterministic Server-Side Re-parsing**: Client-provided JSON is never blindly trusted. The Python backend re-parses raw OCR text deterministically to ensure consistent provenance (`server_reparsed`).
- **Solo Creator & AI Agent Orchestration**: As a non-developer creator, I directed autonomous AI agents (Google Antigravity for pipeline engineering, OpenAI Codex for browser automation, and Sol/DeepSeek for adversarial audits). This cross-validation caught dangerous clinical assumptions early and kept the codebase lean and verifiable.

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
