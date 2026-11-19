import os
import sys
import json
import time
import datetime
import numpy as np
import cv2

# Ensure we can run it from project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
# Allow importing sibling classifier
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import classify as classifier
import reference_whitelist as whitelist

class RoutingAmbiguityError(Exception):
    pass

def load_design_tokens():
    tokens_path = '00_governance/design-tokens.json'
    if not os.path.exists(tokens_path):
        raise FileNotFoundError(f"Design tokens not found at {tokens_path}")
    with open(tokens_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_closest_color_token(target_bgr, tokens):
    closest_token = None
    min_dist = float('inf')
    target_rgb = (target_bgr[2], target_bgr[1], target_bgr[0]) # Convert BGR to RGB
    
    for token, hex_val in tokens['colors'].items():
        hex_val = hex_val.lstrip('#')
        rgb = tuple(int(hex_val[i:i+2], 16) for i in (0, 2, 4))
        dist = sum((a - b)**2 for a, b in zip(target_rgb, rgb))
        if dist < min_dist:
            min_dist = dist
            closest_token = token
    return closest_token

def log_agent_trace(image_path, output_path, status, duration_ms, error=None):
    trace_path = 'ops/logs/agent_trace.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent_id": "A01",
        "skill_id": "raster-deconstruct",
        "input_artifact": image_path,
        "output_artifact": output_path,
        "status": status,
        "duration_ms": int(duration_ms),
        "warnings": [],
        "error": error
    }
    with open(trace_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def log_routing_conflict(layer_id, message):
    conflict_path = 'ops/logs/routing_conflicts.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "layer_id": layer_id,
        "error": "RoutingAmbiguityError",
        "message": message
    }
    with open(conflict_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def estimate_snr(gray):
    # Noise estimation using Laplacian
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    noise_sigma = np.std(laplacian)
    signal_sigma = np.std(gray)
    
    if signal_sigma == 0 or noise_sigma == 0:
        return 50.0 # High SNR for uniform images
    
    snr_db = 20 * np.log10(signal_sigma / noise_sigma)
    return float(snr_db)

def check_gradients(img):
    # Simple gradient check by comparing unique color count before and after downsampling/blurring
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    diff = cv2.absdiff(gray, blurred)
    mean_diff = np.mean(diff)
    return bool(mean_diff > 1.5)

def route_layer(layer_index, color_count, has_gradient, canny_score, snr_db, layer_id):
    # Matrix Routing Implementation
    if snr_db <= 30.0:
        return "OpenCV+VTracer" if color_count <= 4 else "OpenCV+StarVector"
    
    if has_gradient or color_count > 4:
        return "StarVector"
    
    if canny_score > 0.7 and color_count <= 4:
        return "VTracer"
        
    # Ambiguous case: e.g. color_count <= 4, no gradient, canny_score <= 0.7, snr > 30
    raise RoutingAmbiguityError(
        f"Layer {layer_id} routing is ambiguous (colors: {color_count}, gradient: {has_gradient}, Canny score: {canny_score:.2f}, SNR: {snr_db:.1f} dB)"
    )

def deconstruct_image(image_path, design_tokens):
    start_time = time.time()
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"Could not read image: {image_path}")
        
    h, w, c = img.shape
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # 1. Compute SNR
    snr_db = estimate_snr(gray)
    
    # 2. Extract 2 dominant colors using K-Means
    pixels = img.reshape(-1, 3).astype(np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    compactness, labels, centers = cv2.kmeans(pixels, 2, None, criteria, 10, cv2.KMEANS_RANDOM_CENTERS)
    
    # Identify background and foreground based on corner sample
    corner_pixel = img[0, 0]
    dist0 = np.sum((centers[0] - corner_pixel)**2)
    dist1 = np.sum((centers[1] - corner_pixel)**2)
    
    if dist0 < dist1:
        bg_color_bgr = centers[0]
        fg_color_bgr = centers[1]
    else:
        bg_color_bgr = centers[1]
        fg_color_bgr = centers[0]
        
    # Get closest color tokens
    bg_token = get_closest_color_token(bg_color_bgr, design_tokens)
    fg_token = get_closest_color_token(fg_color_bgr, design_tokens)
    
    # 3. Calculate unique colors (with slight rounding to ignore compression artifacts)
    # We round to nearest multiples of 16 to group close shades
    rounded_pixels = np.round(pixels / 16) * 16
    unique_colors = len(np.unique(rounded_pixels, axis=0))
    
    # 4. Check gradients
    has_gradient = check_gradients(img)
    
    # 5. Compute Canny contrast score
    sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    grad_mag = np.sqrt(sobelx**2 + sobely**2)
    edges = cv2.Canny(gray, 50, 150)
    
    if np.sum(edges > 0) > 0:
        # Average gradient magnitude at edges, normalized
        canny_score = float(np.mean(grad_mag[edges > 0]) / 255.0)
    else:
        canny_score = 0.0
        
    # Determine complexity classification
    if unique_colors <= 4 and not has_gradient and canny_score > 0.7:
        complexity = "Low"
    elif unique_colors <= 16 and canny_score > 0.4:
        complexity = "Medium"
    else:
        complexity = "High"
        
    # Build deconstructed layers
    layers = []
    
    # Layer 1: Background
    layers.append({
        "layer_index": 1,
        "geometry_type": "BoundingRect",
        "engine": "StarVector",
        "color_token": bg_token,
        "bounding_box": [0.0, 0.0, float(w), float(h)]
    })
    
    # Layer 2: Foreground shape
    layer2_id = f"{os.path.basename(image_path)}_layer_2"
    try:
        engine = route_layer(2, unique_colors, has_gradient, canny_score, snr_db, layer2_id)
    except RoutingAmbiguityError as e:
        log_routing_conflict(layer2_id, str(e))
        engine = "StarVector" # Fallback engine
        print(f"[WARNING] {e}. Falling back to StarVector.")
        
    layer2 = {
        "layer_index": 2,
        "geometry_type": "CompoundPath",
        "engine": engine,
        "color_token": fg_token,
        "anchor_points_limit": 45,
        "bounding_box": [0.0, 0.0, float(w), float(h)]
    }
    
    # Add vtracer parameters if applicable
    if "VTracer" in engine:
        layer2["vtracer_params"] = {
            "corner_threshold": 60.0,
            "length_threshold": 4.0,
            "filter_speckle": 4
        }
        
    layers.append(layer2)
    
    # Construct raw output
    # COMP ID is based on file checksum or index; we will generate a simple ID here
    # A02 will assign final COMP-NNN ID based on schema/indexes
    raw_data = {
        "source_file": os.path.basename(image_path),
        "resolution": {
            "width_px": w,
            "height_px": h
        },
        "complexity": complexity,
        "provenance": "Local disk / Test asset",
        "layers": layers,
        "metrics": {
            "unique_colors": unique_colors,
            "has_gradient": has_gradient,
            "canny_score": canny_score,
            "snr_db": snr_db
        }
    }
    
    # A07: classify image (type, render mode, detail, aspect, sketch dirs) — TZ v3.1/3.2
    try:
        cls = classifier.classify(image_path)
        raw_data.update({k: v for k, v in cls.items() if k != "signals"})
        print(f"[CLASSIFY] {os.path.basename(image_path)} -> {cls['input_type']} "
              f"/ {cls['render_mode']} / {cls['vector_detail_level']}")
    except Exception as e:
        print(f"[WARN] Classification failed for {image_path}: {e}; "
              f"defaulting to GRAPHIC/STANDARD_VECTOR")
        raw_data.update({
            "input_type": "ILLUSTRATION", "render_mode": "STANDARD_VECTOR",
            "vector_detail_level": "HIGH",
        })

    duration_ms = (time.time() - start_time) * 1000
    return raw_data, duration_ms

def main():
    print("Starting A01: Deconstructor script...")
    design_tokens = load_design_tokens()
    
    registry = whitelist.load_registry()
    os.makedirs('02_processing', exist_ok=True)
    os.makedirs('ops/logs', exist_ok=True)
    failed = 0
    for entry in registry['references']:
        img_path = entry['canonical_relative_path']
        print(f"Processing: {img_path}")
        try:
            raw_data, duration = deconstruct_image(img_path, design_tokens)
            raw_data['source_file'] = img_path
            raw_data['source_sha256'] = entry['sha256']
            raw_data['provenance'] = 'Owner whitelist; classified from unchanged source bytes'
            identity = __import__('hashlib').sha256(img_path.encode()).hexdigest()[:16]
            out_path = f"02_processing/{identity}_raw.json"
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(raw_data, f, indent=2, ensure_ascii=False)
            log_agent_trace(img_path, out_path, 'Success', duration)
            print(f"[OK] {out_path}")
        except Exception as error:
            failed += 1
            log_agent_trace(img_path, '', 'Failed', 0, str(error))
            print(f"[FAIL] {img_path}: {error}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
