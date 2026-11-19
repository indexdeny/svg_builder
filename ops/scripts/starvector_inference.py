import os
import sys
import json
import time
import datetime
from PIL import Image

# Ensure we can run it from project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_whitelist as whitelist

def find_image_path(source_file, source_sha256=None):
    return str(whitelist.verify_record({'source_file': source_file,
                                      'source_sha256': source_sha256}))

def log_agent_trace(passport_id, layer_index, status, duration_ms, error=None):
    trace_path = 'ops/logs/agent_trace.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent_id": "A04",
        "skill_id": "starvector-inference",
        "input_artifact": f"COMP-{passport_id}_layer_{layer_index}",
        "output_artifact": f"05_output_svg/staging/{passport_id}_layer_{layer_index}.svg",
        "status": status,
        "duration_ms": int(duration_ms),
        "warnings": [],
        "error": error
    }
    with open(trace_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def run_vtracer_fallback(image_path, output_path):
    import vtracer
    print("[INFO] Running high-fidelity VTracer fallback...")
    
    # Sanitize image to true PNG for vtracer
    temp_png_path = os.path.abspath(output_path + "_temp.png")
    try:
        from PIL import Image
        img = Image.open(image_path)
        img.save(temp_png_path, "PNG")
        vtracer_input_path = temp_png_path
    except Exception as e:
        print(f"[ERROR] Failed to sanitize image to PNG: {e}")
        vtracer_input_path = os.path.abspath(image_path)
        
    try:
        # Using high-fidelity settings to mimic neural detailing
        vtracer.convert_image_to_svg_py(
            image_path=vtracer_input_path,
            out_path=os.path.abspath(output_path),
            corner_threshold=10,
            length_threshold=1,
            filter_speckle=1
        )
    finally:
        if os.path.exists(temp_png_path):
            try:
                os.remove(temp_png_path)
            except Exception:
                pass

import xml.etree.ElementTree as ET
import re

def is_valid_svg_xml(svg_code):
    try:
        root = ET.fromstring(svg_code)
        tag = root.tag.split('}')[-1]
        if tag != 'svg':
            return False
            
        has_elements = False
        for elem in root.iter():
            local_tag = elem.tag.split('}')[-1]
            if local_tag in ['path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text']:
                has_elements = True
                break
        return has_elements
    except Exception as e:
        print(f"[WARN] SVG validation failed: {e}")
        return False

def run_neural_inference(image_path, output_path):
    import torch
    from transformers import AutoProcessor, AutoModelForCausalLM, BitsAndBytesConfig
    
    # 1. Try 8B Model (INT4)
    try:
        model_name = "starvector/starvector-8b-im2svg"
        print(f"Attempting to load StarVector 8B: {model_name}...")
        
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            llm_int8_skip_modules=["lm_head"],
        )
        
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            cache_dir=os.environ.get('HF_HUB_CACHE'),
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True,
            attn_implementation="eager",
        )
        model.eval()
        
        # Tie weights after loading to ensure output embeddings use trained weights
        print("Tying Starcoder2 model weights...")
        model.model.svg_transformer.transformer.tie_weights()
        
        print("Processing image for 8B model...")
        image_pil = Image.open(image_path).convert("RGB")
        pixel_values = model.process_images([image_pil])
        if len(pixel_values.shape) == 5:
            pixel_values = pixel_values.squeeze(0)
        pixel_values = pixel_values.to("cuda")
        batch = {
            "image": pixel_values
        }
        
        print("Running 8B model inference...")
        with torch.no_grad():
            outputs = model.generate_im2svg(batch, do_sample=False, max_length=4096, num_beams=1)
        output_svg = outputs[0]
        
        # Clean common attribute comma issues
        output_svg = re.sub(r'",\s*', '" ', output_svg)
        
        if not is_valid_svg_xml(output_svg):
            raise ValueError("8B model generated invalid or empty SVG XML.")
        
        # Save output
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(output_svg)
        print("[OK] Neural vectorization (8B) succeeded.")
        return
        
    except Exception as e:
        import traceback
        print(f"[WARN] StarVector 8B inference failed ({e}).\n{traceback.format_exc()}\nTrying 1B fallback...")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            
    # 2. Try 1B Model (float16)
    model_name = "starvector/starvector-1b-im2svg"
    print(f"Loading StarVector 1B: {model_name}...")
    
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        trust_remote_code=True
    )
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")
    model.to(device)
    model.eval()
    
    processor = model.model.processor
    tokenizer = model.model.svg_transformer.tokenizer
    
    print("Preparing image for 1B model...")
    image_pil = Image.open(image_path).convert("RGB")
    image_tensor = processor(image_pil, return_tensors="pt")['pixel_values'].to(device)
    if len(image_tensor.shape) == 5:
        image_tensor = image_tensor.squeeze(0)
        
    print("Generating SVG paths (1B)...")
    with torch.no_grad():
        output_ids = model.generate(image_tensor, max_length=4096)
        
    output_svg = tokenizer.decode(output_ids[0], skip_special_tokens=True)
    
    # Clean common attribute comma issues
    output_svg = re.sub(r'",\s*', '" ', output_svg)
    
    if not is_valid_svg_xml(output_svg):
        raise ValueError("1B model generated invalid or empty SVG XML.")
    
    # Save the output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(output_svg)
    print("[OK] Neural vectorization (1B) succeeded.")

def main():
    if len(sys.argv) < 4:
        print("Usage: python starvector_inference.py <passport_id> <layer_index> <output_path>")
        sys.exit(1)
        
    passport_id = sys.argv[1]
    layer_index = int(sys.argv[2])
    output_path = str(whitelist.output_file(sys.argv[3], suffix='.svg'))
    
    start_time = time.time()
    
    index_path = f"03_knowledge_graph/{passport_id}.json"
    if not os.path.exists(index_path):
        print(f"[ERROR] Passport index not found: {index_path}")
        sys.exit(2)
        
    with open(index_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    source_file = data['source_file']
    image_path = find_image_path(source_file, data.get('source_sha256'))
    if not image_path:
        print(f"[ERROR] Source image not found for: {source_file}")
        sys.exit(3)
        
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        
    # Attempt neural inference, fallback if fails
    error_msg = None
    try:
        run_neural_inference(image_path, output_path)
        status = "Success"
    except Exception as e:
        import traceback
        error_msg = f"{e}\n{traceback.format_exc()}"
        print(f"[WARN] StarVector neural inference failed: {e}")
        try:
            run_vtracer_fallback(image_path, output_path)
            status = "Fallback_VTracer"
        except Exception as fallback_err:
            error_msg += f"\nFallback error: {fallback_err}"
            status = "Failed"
            print(f"[ERROR] Fallback vectorization failed: {fallback_err}")
            
    duration_ms = (time.time() - start_time) * 1000
    log_agent_trace(passport_id, layer_index, status, duration_ms, error_msg)
    
    if status in ["Success", "Fallback_VTracer"]:
        print(f"[OK] StarVector inference completed with status: {status} ({duration_ms:.1f}ms)")
    else:
        sys.exit(4)

if __name__ == "__main__":
    main()
