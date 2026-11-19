import os
import sys
import json
import xml.etree.ElementTree as ET
import re
import vtracer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_whitelist as whitelist

def get_anchor_points_count(svg_path):
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        
        # Check namespace
        namespaces = {'svg': 'http://www.w3.org/2000/svg'}
        paths = root.findall('.//{http://www.w3.org/2000/svg}path')
        if not paths:
            paths = root.findall('.//path')
            
        total_points = 0
        for p in paths:
            d = p.get('d', '')
            numbers = re.findall(r'-?\d+(?:\.\d+)?', d)
            total_points += len(numbers) // 2
        return total_points
    except Exception as e:
        print(f"Error parsing SVG to count anchor points: {e}")
        return 0

def find_image_path(source_file, source_sha256=None):
    return str(whitelist.verify_record({'source_file': source_file,
                                      'source_sha256': source_sha256}))

def process_vtracer_layer(passport_id, layer_index, output_path):
    output_path = str(whitelist.output_file(output_path, suffix='.svg'))
    index_path = f"03_knowledge_graph/{passport_id}.json"
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"Passport index not found: {index_path}")
        
    with open(index_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    source_file = data['source_file']
    image_path = find_image_path(source_file, data.get('source_sha256'))
    if not image_path:
        raise FileNotFoundError(f"Source image not found for: {source_file}")
        
    # Find layer
    target_layer = None
    for layer in data['layers']:
        if layer['layer_index'] == layer_index:
            target_layer = layer
            break
            
    if not target_layer:
        raise ValueError(f"Layer index {layer_index} not found in passport {passport_id}")
        
    # Parameters
    v_params = target_layer.get('vtracer_params', {})
    corner_threshold = v_params.get('corner_threshold', 60.0)
    length_threshold = v_params.get('length_threshold', 4.0)
    filter_speckle = v_params.get('filter_speckle', 4)
    anchor_points_limit = target_layer.get('anchor_points_limit', 45)
    
    # We will do iterative scaling if anchor points limit exceeded
    max_attempts = 5
    attempt = 1
    
    # Sanitize image to true PNG for vtracer
    temp_png_path = os.path.abspath(f"02_processing/{passport_id}_layer_{layer_index}_temp.png")
    try:
        from PIL import Image
        img = Image.open(image_path)
        img.save(temp_png_path, "PNG")
        vtracer_input_path = temp_png_path
    except Exception as e:
        print(f"[ERROR] Failed to sanitize image to PNG: {e}")
        vtracer_input_path = os.path.abspath(image_path) # Fallback to original
    
    try:
        while attempt <= max_attempts:
            print(f"Running VTracer (Attempt {attempt}): corner_threshold={corner_threshold}, length_threshold={length_threshold}, filter_speckle={filter_speckle}")
            
            # Call vtracer
            vtracer.convert_image_to_svg_py(
                image_path=vtracer_input_path,
                out_path=os.path.abspath(output_path),
                corner_threshold=int(round(corner_threshold)),
                length_threshold=int(round(length_threshold)),
                filter_speckle=int(filter_speckle)
            )
            
            # Check anchor points
            points = get_anchor_points_count(output_path)
            print(f"Generated path contains {points} anchor points (limit: {anchor_points_limit})")
            
            if points <= anchor_points_limit:
                print("[OK] Anchor points are within limit.")
                break
            else:
                print(f"[WARN] Anchor points limit exceeded. Incrementing length_threshold.")
                length_threshold += 1.0
                attempt += 1
                
        if attempt > max_attempts:
            print("[WARN] Max VTracer attempts reached. Proceeding with last generated output.")
    finally:
        # Cleanup temp file
        if os.path.exists(temp_png_path):
            try:
                os.remove(temp_png_path)
            except Exception:
                pass

def main():
    if len(sys.argv) < 4:
        print("Usage: python vtracer_bridge.py <passport_id> <layer_index> <output_path>")
        sys.exit(1)
        
    passport_id = sys.argv[1]
    layer_index = int(sys.argv[2])
    output_path = sys.argv[3]
    
    # Ensure parent directory of output_path exists
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        
    try:
        process_vtracer_layer(passport_id, layer_index, output_path)
        print("[SUCCESS] VTracer bridge completed successfully.")
    except Exception as e:
        print(f"[ERROR] VTracer bridge failed: {e}")
        sys.exit(2)

if __name__ == "__main__":
    main()
