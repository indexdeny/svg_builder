import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reference_whitelist import verify_path
if len(sys.argv) != 2:
    raise SystemExit('Legacy experiment: pass an exact approved source path; not part of the active pipeline')
TEST_IMAGE = str(verify_path(sys.argv[1]))
import torch
from transformers import AutoProcessor, AutoModelForCausalLM, BitsAndBytesConfig
from PIL import Image

MODEL_ID = "starvector/starvector-8b-im2svg"
HF_CACHE = os.environ.get('HF_HUB_CACHE')

print(f"CUDA available: {torch.cuda.is_available()}")
print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"VRAM total: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

# Конфигурация INT4
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    llm_int8_skip_modules=["lm_head"],
)

print("Loading processor...")
processor = AutoProcessor.from_pretrained(
    MODEL_ID,
    cache_dir=HF_CACHE,
    trust_remote_code=True,
)

print("Loading model (INT4)...")
model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    cache_dir=HF_CACHE,
    quantization_config=bnb_config,
    device_map="auto",
    trust_remote_code=True,
    attn_implementation="eager",   # ← обход flash_attn
)
model.eval()

print("Tying weights...")
print("Before tying:", model.model.svg_transformer.transformer.lm_head.weight is model.model.svg_transformer.transformer.model.embed_tokens.weight)
model.model.svg_transformer.transformer.tie_weights()
print("After tying:", model.model.svg_transformer.transformer.lm_head.weight is model.model.svg_transformer.transformer.model.embed_tokens.weight)

vram_used = torch.cuda.memory_allocated() / 1e9
print(f"Model loaded. VRAM used: {vram_used:.2f} GB")

# Тестовый инференс на одном изображении
image = Image.open(TEST_IMAGE).convert("RGB")

print("Processing image...")
pixel_values = model.process_images([image])
if len(pixel_values.shape) == 5:
    pixel_values = pixel_values.squeeze(0)
pixel_values = pixel_values.to("cuda")
batch = {
    "image": pixel_values
}

print("Running inference...")
with torch.no_grad():
    outputs = model.generate_im2svg(batch, do_sample=False, max_length=2048, num_beams=1)

svg_code = outputs[0]
print(f"Output length: {len(svg_code)} chars")
print(f"Contains <svg>: {'<svg' in svg_code}")
print(f"Contains <path>: {'<path' in svg_code.lower()}")
print("--- First 500 chars of SVG output (escaped) ---")
print(svg_code[:500].encode('utf-8', errors='replace').decode('cp1251', errors='replace'))

with open("ops/scripts/test_output.svg", "w", encoding="utf-8") as f:
    f.write(svg_code)
print("Saved to ops/scripts/test_output.svg")
