import re
import base64
import os

with open(r"C:\AI\painfree\body_front.svg", "r", encoding="utf-8") as f:
    text = f.read()

m = re.search(r'data:image/png;base64,([^"]+)', text)
if m:
    b = base64.b64decode(m.group(1))
    p = r"C:\Users\USER\.gemini\antigravity\brain\c52852dc-6dc3-4ec7-bbe7-31b64342009f\painfree_front_extracted.png"
    with open(p, "wb") as out:
        out.write(b)
    print("Extracted front png, size:", len(b))
else:
    print("No base64 in front svg")

# Check all images in painfree folder directly
print("All pngs in C:\\AI\\painfree:")
for f in os.listdir(r"C:\AI\painfree"):
    if f.endswith(('.png', '.svg', '.jpg')):
        fp = os.path.join(r"C:\AI\painfree", f)
        print(f, os.path.getsize(fp))
