import os
import sys
import json
import time
import datetime
import subprocess
import xml.etree.ElementTree as ET
import re
import glob

# Ensure we can run it from project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
# Allow importing sibling scripts (hifi_vectorize)
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
import reference_whitelist as whitelist

def load_design_tokens():
    tokens_path = '00_governance/design-tokens.json'
    if not os.path.exists(tokens_path):
        raise FileNotFoundError(f"Design tokens not found at {tokens_path}")
    with open(tokens_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def log_agent_trace(passport_id, status, duration_ms, error=None, agent_id="A05", metrics=None):
    trace_path = 'ops/logs/agent_trace.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent_id": agent_id,
        "skill_id": "svg-qa-gate",
        "input_artifact": f"05_output_svg/staging/{passport_id}_compiled.svg",
        "output_artifact": f"05_output_svg/{passport_id}.svg" if status == "Success" else f"05_output_svg/quarantine/{passport_id}.svg",
        "status": status,
        "duration_ms": int(duration_ms),
        "warnings": [],
        "error": error
    }
    if metrics:
        log_entry["metrics"] = metrics
        name = metrics.get('output_filename')
        if name:
            log_entry['input_artifact'] = f"05_output_svg/staging/{os.path.splitext(name)[0]}_compiled.svg"
            log_entry['output_artifact'] = f"05_output_svg/{name}" if status == 'Success' else ''
    with open(trace_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def log_qa_failure(passport_id, errors):
    failure_path = 'ops/logs/qa_failures.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "passport_id": passport_id,
        "errors": errors
    }
    with open(failure_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def run_layer_generation(passport_id, layer_index, engine, output_path):
    print(f"Generating layer {layer_index} using engine {engine}...")
    
    if "VTracer" in engine:
        script = "ops/scripts/vtracer_bridge.py"
    else:
        script = "ops/scripts/starvector_inference.py"
        
    cmd = [sys.executable, script, passport_id, str(layer_index), output_path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    
    if res.returncode != 0:
        print(f"[FAIL] Layer generation failed. stdout: {res.stdout}, stderr: {res.stderr}")
        return False, res.stderr
    print(f"[OK] Layer generated: {output_path}")
    return True, None

def parse_hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    if len(hex_str) == 3:
        hex_str = ''.join([c*2 for c in hex_str])
    if len(hex_str) == 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return (0, 0, 0)

def get_closest_color_token(hex_color, design_tokens):
    if not hex_color or not hex_color.startswith('#'):
        return None
    rgb = parse_hex_to_rgb(hex_color)
    
    closest_token = None
    min_dist = float('inf')
    for token, hex_val in design_tokens.get('colors', {}).items():
        token_rgb = parse_hex_to_rgb(hex_val)
        dist = sum((a - b)**2 for a, b in zip(rgb, token_rgb))
        if dist < min_dist:
            min_dist = dist
            closest_token = token
    return closest_token

def parse_style_str(style_str):
    props = {}
    if not style_str:
        return props
    for item in style_str.split(';'):
        if ':' in item:
            k, v = item.split(':', 1)
            props[k.strip().lower()] = v.strip()
    return props

def get_inherited_attribute(elem, attr_name, parent_map):
    curr = elem
    while curr is not None:
        val = curr.get(attr_name)
        if val:
            return val
        style = curr.get('style')
        if style:
            props = parse_style_str(style)
            if attr_name in props:
                return props[attr_name]
        curr = parent_map.get(curr)
    return None

def get_element_fill(elem, parent_map):
    val = get_inherited_attribute(elem, 'fill', parent_map)
    if val == 'none':
        return None
    return val

def is_inside_defs(elem, parent_map):
    curr = elem
    while curr in parent_map:
        curr = parent_map[curr]
        tag = curr.tag.split('}')[-1]
        if tag in ['defs', 'clipPath', 'style']:
            return True
    return False

def is_inside_text(elem, parent_map):
    curr = elem
    while curr in parent_map:
        curr = parent_map[curr]
        tag = curr.tag.split('}')[-1]
        if tag == 'text':
            return True
    return False

def clean_element(elem, design_tokens):
    tag = elem.tag.split('}')[-1]
    attrib = {}
    
    style_props = {}
    if 'style' in elem.attrib:
        style_props = parse_style_str(elem.attrib['style'])
        
    combined_attrib = {}
    for k, v in elem.attrib.items():
        k_clean = k.split('}')[-1]
        if k_clean != 'style':
            combined_attrib[k_clean] = v
            
    for k, v in style_props.items():
        combined_attrib[k] = v
        
    allowed_attribs = [
        'd', 'x', 'y', 'width', 'height', 'points', 'cx', 'cy', 'r', 'rx', 'ry', 
        'x1', 'y1', 'x2', 'y2', 'transform', 
        'font-family', 'font-size', 'text-anchor', 'font-weight', 'dx', 'dy', 'rotate',
        'stroke-width', 'stroke-miterlimit', 'letter-spacing'
    ]
    
    for k, v in combined_attrib.items():
        if k in allowed_attribs:
            attrib[k] = v
        elif k == 'fill':
            if v == 'none':
                attrib['fill'] = 'none'
            elif v.startswith('#'):
                token = get_closest_color_token(v, design_tokens)
                if token:
                    attrib['fill'] = f"var({token})"
            else:
                attrib['fill'] = v
        elif k == 'stroke':
            if v == 'none':
                attrib['stroke'] = 'none'
            elif v.startswith('#'):
                token = get_closest_color_token(v, design_tokens)
                if token:
                    attrib['stroke'] = f"var({token})"
            else:
                attrib['stroke'] = v
                
    cleaned = ET.Element(tag, **attrib)
    if elem.text:
        cleaned.text = elem.text
    if elem.tail:
        cleaned.tail = elem.tail
        
    for child in elem:
        cleaned.append(clean_element(child, design_tokens))
    return cleaned

def extract_layer_shapes(layer_svg_path, target_color_token, design_tokens):
    tree = ET.parse(layer_svg_path)
    root = tree.getroot()
    
    parent_map = {c: p for p in root.iter() for c in p}
    shapes = []
    allowed_tags = ['path', 'rect', 'polygon', 'circle', 'ellipse', 'polyline', 'line', 'text', 'tspan']
    
    for elem in root.iter():
        tag = elem.tag.split('}')[-1]
        if tag in allowed_tags:
            if is_inside_defs(elem, parent_map) or is_inside_text(elem, parent_map):
                continue
                
            fill = get_element_fill(elem, parent_map)
            if fill:
                closest_token = get_closest_color_token(fill, design_tokens)
                if closest_token and closest_token != target_color_token:
                    print(f"  [FILTER] Discarding element {tag} with fill {fill} (mapped to {closest_token}, target is {target_color_token})")
                    continue
            shapes.append(clean_element(elem, design_tokens))
    return shapes

def qa_audit_svg(svg_content, design_tokens):
    errors = []
    
    # 1. XML Validity check
    try:
        ET.fromstring(svg_content)
    except Exception as e:
        errors.append(f"Invalid XML syntax: {e}")
        return errors
        
    # Strip the style block for forbidden color audits, as design tokens themselves
    # contain valid token definitions (e.g. --white: #FFFFFF) which should not trigger QA blocks.
    svg_content_audit = re.sub(r'<style>.*?</style>', '', svg_content, flags=re.DOTALL)
    
    # Strip any design token references (e.g. var(--white), var(--dark)) to avoid false positives on token names
    svg_content_audit = re.sub(r'var\(--[a-zA-Z0-9-]+\)', '', svg_content_audit)
    
    # 2. Forbidden colors check (Zero Black Policy)
    # Match any hex colors or color names in the audited SVG text (outside style block)
    hex_colors_audit = re.findall(r'#[0-9a-fA-F]{3,6}', svg_content_audit)
    color_names_audit = re.findall(r'\b(?:black|white|red|green|blue)\b', svg_content_audit.lower())
    
    forbidden = design_tokens.get('forbidden', [])
    for hc in hex_colors_audit:
        if hc.lower() in forbidden:
            errors.append(f"Forbidden color code found: {hc}")
            
    for cn in color_names_audit:
        if cn in forbidden:
            errors.append(f"Forbidden color name found: {cn}")
            
    # Also verify that no raw hex color codes are used outside the <style> block
    if hex_colors_audit:
        errors.append(f"Raw hex color codes found outside the <style> block: {', '.join(hex_colors_audit)}")

    return errors

def normalize_monochrome(svg_content):
    """Forces the monochrome-black standard (TZ v3.1 RULE 2): every fill/stroke
    colour becomes #000000 (except 'none'); any stray hex collapses to black."""
    s = re.sub(r'(fill|stroke)="(?!none")[^"]*"', r'\1="#000000"', svg_content)
    s = re.sub(r'#[0-9a-fA-F]{3,6}', '#000000', s)
    return s


def validate_monochrome(svg_content):
    """Release validation per TZ v3.1 RULE 1-5. Returns list of violations."""
    errors = []
    try:
        ET.fromstring(svg_content)
    except Exception as e:
        return [f"Invalid XML syntax: {e}"]

    # RULE 1/4: no background rectangle / opaque canvas layer
    if re.search(r'<rect\b', svg_content):
        errors.append("RULE1/4: background <rect> present (must be transparent)")

    # RULE 3/5: no gradients, filters, raster images, embedded bitmap data
    for token, label in [('data:image', 'embedded raster (base64)'),
                         ('<image', 'raster <image> element'),
                         ('linearGradient', 'linear gradient'),
                         ('radialGradient', 'radial gradient'),
                         ('<filter', 'filter')]:
        if token in svg_content:
            errors.append(f"RULE3/5: {label} present (must be pure vector)")

    # RULE 2: only #000000 / black (and 'none')
    colors = set(re.findall(r'(?:fill|stroke)="([^"]+)"', svg_content))
    bad = [c for c in colors if c not in ('#000000', 'none')]
    if bad:
        errors.append(f"RULE2: non-black fills/strokes {sorted(bad)}")
    strays = {h.lower() for h in re.findall(r'#[0-9a-fA-F]{3,6}', svg_content)
              if h.lower() != '#000000'}
    if strays:
        errors.append(f"RULE2: stray non-black hex {sorted(strays)}")
    return errors


def source_too_low_detail(image_path):
    """Suitability gate (the cat_3 class). A blurry / low-resolution / featureless
    source has almost no edges (edge_frac ~ 0) and near-zero high-frequency detail
    (lap_var ~ 0). A monochrome FILL of it is a meaningless blob and a SKETCH is
    sparse noise — yet BOTH pass the SSIM gate (which measures fidelity to the
    segmentation/line mask, not recognizability). So reject the SOURCE up front
    rather than release garbage. Real graphics/photos always carry real edges, so
    this only fires on degenerate inputs."""
    import cv2
    img = cv2.imread(image_path)
    if img is None:
        return False, {}
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edge = float((cv2.Canny(g, 80, 200) > 0).mean())
    lap = float(cv2.Laplacian(g, cv2.CV_64F).var())
    return (edge < 0.005 and lap < 60.0), {'edge_frac': round(edge, 4),
                                           'lap_var': round(lap, 1)}


def compile_object_svg_mono(passport_id, design_tokens, output_name=None):
    """A08 Monochrome Standard (TZ v3.1/3.2): dispatch by render_mode, then enforce
    black-only / transparent / pure-vector release standard. Output named per
    CR v3.3 (svg_<stem>.svg)."""
    import hifi_vectorize
    import sketch_vectorize

    start_time = time.time()
    index_path = f"03_knowledge_graph/{passport_id}.json"
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"Passport index not found: {index_path}")
    with open(index_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    image_path = find_source_image(data['source_file'], data.get('source_sha256'))
    if not image_path:
        raise FileNotFoundError(f"Source image not found for: {data['source_file']}")

    # Suitability gate: reject blurry/featureless sources up front (the cat_3
    # blob class) — neither fill nor sketch of them is meaningful, yet both pass
    # the SSIM gate. No SVG is emitted; the artifact is recorded as quarantined.
    low, sig = source_too_low_detail(image_path)
    if low:
        reason = (f"source too blurry/low-detail to vectorize meaningfully "
                  f"(edge_frac={sig['edge_frac']}, lap_var={sig['lap_var']})")
        # drop any stale released-artifact metadata so Stage 4 skips it cleanly
        try:
            data.pop('artifact_metadata', None)
            data['compile_status'] = 'rejected_low_detail'
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        log_qa_failure(passport_id, [reason])
        log_agent_trace(passport_id, "Failed", (time.time() - start_time) * 1000,
                        reason, agent_id="A08")
        print(f"[FAIL] {passport_id} -> quarantine (unsuitable source): {reason}")
        return False

    render_mode = data.get('render_mode', 'STANDARD_VECTOR')
    detail = data.get('vector_detail_level', 'HIGH')
    input_type = data.get('input_type', 'ILLUSTRATION')
    if output_name is None:
        output_name = make_output_name(data['source_file'], set())
    annotate_passport(passport_id, data['source_file'], output_name)
    stem = os.path.splitext(output_name)[0]

    def _render(mode):
        if mode == 'SKETCH_VECTOR':
            return sketch_vectorize.vectorize_sketch(image_path, detail)
        return hifi_vectorize.vectorize_monochrome(image_path, detail)

    # CR v3.5: mode-recovery fallback. The classifier sometimes routes a tonal /
    # line-art image to STANDARD fill (e.g. a B&W line portrait tagged LOGO) — it
    # renders as a dense blob and fails the perceptual gate. If the classified
    # mode fails its gate, try the OTHER mode and accept it only if IT passes its
    # own gate (and, for sketch, has a sane line fraction). This can never
    # regress an image that already passes, and recovers misrouted sketches.
    classified_mode = render_mode
    result = _render(render_mode)
    fallback = None
    if not result.get('gate_passed', True):
        other = 'STANDARD_VECTOR' if render_mode == 'SKETCH_VECTOR' else 'SKETCH_VECTOR'
        alt = _render(other)
        accept = alt.get('gate_passed', False)
        if other == 'SKETCH_VECTOR':
            lf = alt.get('line_fraction', 0.0)
            rbf = alt.get('render_black_frac', 1.0)
            # not near-empty, and the rendered drawing is not a near-black blob
            # (dark sources can pass the SSIM gate yet render mostly black).
            accept = accept and (0.004 <= lf <= 0.45) and (rbf <= 0.5)
        if accept:
            result, render_mode, fallback = alt, other, other
            print(f"  [FALLBACK] {classified_mode} failed gate -> {other} passed "
                  f"(SSIM={result['ssim_ref']:.4f})")

    svg_content = normalize_monochrome(result['svg'])

    # svg_cad T2: orthogonal quality axes. Colour (monochrome conformance) and
    # topological (closed paths, no raster/gradient) are independent of the
    # visual SSIM axis. 3mf_eligible is derived from colour+topological+mode.
    import svg_contract
    import pipeline_config
    color_violations = validate_monochrome(svg_content)
    topo = svg_contract.topology_axis(svg_content)
    vb = re.search(r'viewBox="([^"]+)"', svg_content)
    viewbox = vb.group(1) if vb else "0 0 0 0"
    threshold = pipeline_config.gate(render_mode, 'ssim_min', 0.75)

    # svg_cad T1: assemble + (below) validate + embed the contract metadata.
    meta = svg_contract.build_metadata(
        comp_id=passport_id, spec_version=pipeline_config.spec_version(),
        engine="deterministic_vtracer", render_mode=render_mode,
        fallback_from=(classified_mode if fallback else None),
        source_filename=os.path.basename(data['source_file']),
        output_filename=output_name, ssim=result['ssim_ref'],
        ssim_threshold=threshold, color_violations=color_violations,
        topo=topo, viewbox=viewbox, source_relative_path=data['source_file'],
        source_sha256=data['source_sha256'])

    metrics = {
        "input_type": input_type,
        "render_mode": render_mode,
        "classified_mode": classified_mode,
        "fallback_from": (classified_mode if fallback else None),
        "detail": detail,
        "ssim": round(result['ssim_ref'], 4),
        "3mf_eligible": meta['3mf_eligible'],
        "source_filename": os.path.basename(data['source_file']),
        "output_filename": output_name,
        "vtracer_params": result.get('params'),
    }

    os.makedirs("05_output_svg/staging", exist_ok=True)
    with open(f"05_output_svg/staging/{stem}_compiled.svg", 'w',
              encoding='utf-8') as f:
        f.write(svg_contract.embed_metadata(svg_content, meta))

    # Release decision (2D) is governed by the VISUAL axis (SSIM gate) + colour
    # axis + naming. 3mf_eligible is an independent metadata flag and does NOT
    # block a 2D release (audit principle 2).
    errors = list(color_violations)
    if not re.match(r'^svg_.+\.svg$', output_name):           # CR v3.3 §8.1
        errors.append(f"Output filename violates naming standard: {output_name}")
    if not result.get('gate_passed', True):
        errors.append(f"Perceptual gate failed: SSIM={result['ssim_ref']:.4f}")
    if result.get('invariant_violation'):                     # svg_cad T3 (R-03)
        errors.append(f"T3 filtration invariant: {result['invariant_violation']}")
    schema_errs = svg_contract.validate_artifact(meta)        # svg_cad T1 schema
    if schema_errs:
        errors.append(f"Artifact schema violation: {schema_errs}")

    # Embed the contract metadata so the standalone artifact is self-describing.
    svg_out = svg_contract.embed_metadata(svg_content, meta)
    annotate_passport(passport_id, data['source_file'], output_name, meta)

    duration_ms = (time.time() - start_time) * 1000
    if errors:
        log_qa_failure(passport_id, errors)
        log_agent_trace(passport_id, "Failed", duration_ms, "; ".join(errors),
                        agent_id="A08", metrics=metrics)
        quarantine_path = f"05_output_svg/quarantine/{output_name}"
        os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)
        with open(quarantine_path, 'w', encoding='utf-8') as f:
            f.write(svg_out)
        with open(index_path, encoding='utf-8') as f:
            data = json.load(f)
        data['compile_status'] = 'QUARANTINED'
        data.pop('mesh_artifact_metadata', None)
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[FAIL] {passport_id} ({render_mode}) -> quarantine/{output_name}: {errors}")
        return False

    final_path = f"05_output_svg/{output_name}"
    with open(final_path, 'w', encoding='utf-8') as f:
        f.write(svg_out)
    with open(index_path, encoding='utf-8') as f:
        data = json.load(f)
    data['compile_status'] = 'PASSED'
    data.pop('mesh_artifact_metadata', None)
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log_agent_trace(passport_id, "Success", duration_ms, agent_id="A08", metrics=metrics)
    print(f"[OK] {passport_id} [{input_type}/{render_mode}/{detail}] "
          f"3mf={meta['3mf_eligible']}: SSIM={result['ssim_ref']:.4f} "
          f"-> {final_path} ({duration_ms:.0f}ms)")
    return True


def get_svg_viewbox(filepath):
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        viewbox = root.get('viewBox')
        if viewbox:
            parts = [float(x) for x in re.split(r'[\s,]+', viewbox.strip()) if x]
            if len(parts) == 4:
                return parts[0], parts[1], parts[2], parts[3]
        width = root.get('width')
        height = root.get('height')
        if width and height:
            w_val = float(re.sub(r'[^\d.]', '', width))
            h_val = float(re.sub(r'[^\d.]', '', height))
            return 0.0, 0.0, w_val, h_val
    except Exception as e:
        print(f"[WARN] Failed to parse SVG viewBox from {filepath}: {e}")
    return 0.0, 0.0, None, None

def compile_object_svg(passport_id, design_tokens):
    start_time = time.time()
    
    index_path = f"03_knowledge_graph/{passport_id}.json"
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"Passport index not found: {index_path}")
        
    with open(index_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    resolution = data['resolution']
    width = resolution['width_px']
    height = resolution['height_px']
    
    # 1. Generate layers in staging
    layer_files = []
    for layer in data['layers']:
        layer_idx = layer['layer_index']
        engine = layer['engine']
        layer_svg = f"05_output_svg/staging/{passport_id}_layer_{layer_idx}.svg"
        
        if os.path.exists(layer_svg):
            print(f"Layer {layer_idx} already exists at {layer_svg}. Skipping generation (using cached).")
            ok, err = True, None
        else:
            ok, err = run_layer_generation(passport_id, layer_idx, engine, layer_svg)
        if not ok:
            log_agent_trace(passport_id, "Failed", 0, f"Layer {layer_idx} generation failed: {err}")
            return False
        layer_files.append((layer, layer_svg))
        
    # 2. Compile into single composite SVG
    root = ET.Element("svg", 
                      xmlns="http://www.w3.org/2000/svg", 
                      viewBox=f"0 0 {width} {height}", 
                      width=str(width), 
                      height=str(height))
                      
    # Inject design tokens style block
    style = ET.SubElement(root, "style")
    style_content = "\n  :root {\n"
    # Inject colors
    for k, v in design_tokens.get('colors', {}).items():
        style_content += f"    {k}: {v};\n"
    # Inject typography
    for k, v in design_tokens.get('typography', {}).items():
        style_content += f"    {k}: {v};\n"
    # Inject strokes
    for k, v in design_tokens.get('strokes', {}).items():
        style_content += f"    {k}: {v};\n"
    style_content += "  }\n"
    style.text = style_content
    
    # Add shapes from layers
    for layer, filepath in layer_files:
        layer_idx = layer['layer_index']
        color_token = layer['color_token']
        geom_type = layer['geometry_type']
        
        if geom_type == "BoundingRect" and layer_idx == 1:
            # Output solid background rect directly
            ET.SubElement(root, "rect", width=str(width), height=str(height), fill=f"var({color_token})")
        else:
            # Parse layer shapes, clean them, and wrap in a group
            shapes = extract_layer_shapes(filepath, color_token, design_tokens)
            if not shapes:
                raise ValueError(f"Foreground layer {layer_idx} has no shapes extracted (empty layer).")
                
            # Determine scaling and translation offsets
            x_min, y_min, source_w, source_h = get_svg_viewbox(filepath)
            scale_attrib = {}
            if source_w and source_h:
                sx = width / source_w
                sy = height / source_h
                if x_min != 0.0 or y_min != 0.0:
                    scale_attrib["transform"] = f"scale({sx:.6f}, {sy:.6f}) translate({-x_min:.6f}, {-y_min:.6f})"
                elif abs(source_w - width) > 1e-2 or abs(source_h - height) > 1e-2:
                    scale_attrib["transform"] = f"scale({sx:.6f}, {sy:.6f})"
                
            group = ET.SubElement(root, "g", fill=f"var({color_token})", **scale_attrib)
            for s in shapes:
                group.append(s)
                    
    # Serialize to string
    # We want to format the XML nicely
    ET.indent(root, space="  ", level=0)
    svg_content = ET.tostring(root, encoding='utf-8').decode('utf-8')
    
    # Write to temp staging first
    staging_output = f"05_output_svg/staging/{passport_id}_compiled.svg"
    with open(staging_output, 'w', encoding='utf-8') as f:
        f.write(svg_content)
        
    # 3. QA Audit
    errors = qa_audit_svg(svg_content, design_tokens)
    duration_ms = (time.time() - start_time) * 1000
    
    if errors:
        log_qa_failure(passport_id, errors)
        log_agent_trace(passport_id, "Failed", duration_ms, "; ".join(errors))
        
        # Move to quarantine
        quarantine_path = f"05_output_svg/quarantine/{passport_id}.svg"
        os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)
        with open(quarantine_path, 'w', encoding='utf-8') as f:
            f.write(svg_content)
            
        print(f"[FAIL] QA Audit failed for {passport_id}. Moved to quarantine. Errors: {errors}")
        return False
    else:
        # Move to final output
        final_path = f"05_output_svg/{passport_id}.svg"
        with open(final_path, 'w', encoding='utf-8') as f:
            f.write(svg_content)
            
        log_agent_trace(passport_id, "Success", duration_ms)
        print(f"[OK] QA Audit passed for {passport_id}. Composite SVG saved to {final_path} ({duration_ms:.1f}ms)")
        return True

def find_source_image(source_file, source_sha256=None):
    return str(whitelist.verify_record({'source_file': source_file,
                                      'source_sha256': source_sha256}))

# ---- CR v3.3: output naming standard + clean rebuild ------------------------

def natural_key(s):
    """Natural sort key: '02' < '10', so source ordering is preserved."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r'(\d+)', s)]

def make_output_name(source_file, used):
    """svg_<stem>.svg with collision suffix _NN (CR v3.3 §4-5)."""
    stem = os.path.splitext(os.path.basename(source_file))[0]
    name = f"svg_{stem}.svg"
    if name in used:
        i = 1
        while f"svg_{stem}_{i:02d}.svg" in used:
            i += 1
        name = f"svg_{stem}_{i:02d}.svg"
    used.add(name)
    return name

def clean_outputs():
    """Full clean rebuild (CR v3.3 §1): remove every previously generated SVG
    from the release root, staging and quarantine. Sources / passports / graph
    / logs are left untouched."""
    removed = whitelist.clean_generated(whitelist.ROOT / '05_output_svg',
                                        ['*.svg', 'staging/*.svg', 'quarantine/*.svg'])
    print(f"[CLEAN] Removed {removed} old SVG file(s) before full rebuild.")

def annotate_passport(passport_id, source_file, output_name, artifact_metadata=None):
    """Persist source/output filename mapping into the passport (CR v3.3 §7) and
    mirror the artifact contract metadata into the knowledge graph (svg_cad T1)."""
    path = f"03_knowledge_graph/{passport_id}.json"
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        d['source_filename'] = os.path.basename(source_file)
        d['output_filename'] = output_name
        if artifact_metadata is not None:
            d['artifact_metadata'] = artifact_metadata
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not annotate passport {passport_id}: {e}")

def compile_object_svg_hifi(passport_id, design_tokens):
    """A06 HiFi path (TZ v3.0): deterministic vectorization with perceptual QA."""
    import hifi_vectorize

    start_time = time.time()

    index_path = f"03_knowledge_graph/{passport_id}.json"
    if not os.path.exists(index_path):
        raise FileNotFoundError(f"Passport index not found: {index_path}")
    with open(index_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    image_path = find_source_image(data['source_file'], data.get('source_sha256'))
    if not image_path:
        raise FileNotFoundError(f"Source image not found for: {data['source_file']}")

    result = hifi_vectorize.vectorize(image_path, design_tokens)
    svg_content = result['svg']
    metrics = {
        "ssim_reference": round(result['ssim_ref'], 4),
        "ssim_original": round(result['ssim_orig'], 4),
        "clusters": result['k'],
        "vtracer_params": result['params'],
    }

    staging_output = f"05_output_svg/staging/{passport_id}_compiled.svg"
    with open(staging_output, 'w', encoding='utf-8') as f:
        f.write(svg_content)

    errors = qa_audit_svg(svg_content, design_tokens)
    if not result['gate_passed']:
        errors.append(
            f"Perceptual gate failed: SSIM(reference)={result['ssim_ref']:.4f} "
            f"< {hifi_vectorize.SSIM_RELEASE_GATE}")

    duration_ms = (time.time() - start_time) * 1000
    if errors:
        log_qa_failure(passport_id, errors)
        log_agent_trace(passport_id, "Failed", duration_ms, "; ".join(errors),
                        agent_id="A06", metrics=metrics)
        quarantine_path = f"05_output_svg/quarantine/{passport_id}.svg"
        os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)
        with open(quarantine_path, 'w', encoding='utf-8') as f:
            f.write(svg_content)
        print(f"[FAIL] QA failed for {passport_id}: {errors}")
        return False

    final_path = f"05_output_svg/{passport_id}.svg"
    with open(final_path, 'w', encoding='utf-8') as f:
        f.write(svg_content)
    log_agent_trace(passport_id, "Success", duration_ms, agent_id="A06", metrics=metrics)
    print(f"[OK] {passport_id}: SSIM(ref)={result['ssim_ref']:.4f}, "
          f"SSIM(orig)={result['ssim_orig']:.4f} -> {final_path} ({duration_ms:.0f}ms)")
    return True

def main():
    # Render strategy: default = v3.1 Monochrome Standard (black on transparent).
    #   --color  -> v3.0 HiFi colour pipeline (design tokens)
    #   --legacy -> original VTracer-bridge/StarVector pipeline
    if '--legacy' in sys.argv:
        compiler, mode = compile_object_svg, "LEGACY (VTracer-bridge/StarVector)"
    elif '--color' in sys.argv:
        compiler, mode = compile_object_svg_hifi, "HiFi colour (TZ v3.0)"
    else:
        compiler, mode = compile_object_svg_mono, "Monochrome Standard (TZ v3.1)"
    mono = compiler is compile_object_svg_mono
    print(f"Starting A05: SVG Compiler & QA Auditor script... mode: {mode}")
    design_tokens = load_design_tokens()
    whitelist.load_registry()
    os.makedirs('ops/logs', exist_ok=True)
    # Validate every record before a clean rebuild can remove any output.
    for path in glob.glob('03_knowledge_graph/COMP-*.json'):
        with open(path, encoding='utf-8') as stream:
            whitelist.verify_record(json.load(stream))

    kg_dir = "03_knowledge_graph"
    if not os.path.exists(kg_dir):
        print(f"Directory {kg_dir} does not exist.")
        return

    # CR v3.3: full clean rebuild — wipe old SVGs (only for the monochrome
    # standard pipeline, which owns the svg_<stem>.svg naming).
    if mono and '--no-clean' not in sys.argv:
        clean_outputs()

    # Build the work list and order it by SOURCE filename (natural sort) so
    # outputs follow input order and collision suffixes are deterministic.
    work = []
    for file in os.listdir(kg_dir):
        if file.startswith("COMP-") and file.endswith(".json"):
            passport_id = os.path.splitext(file)[0]
            try:
                with open(os.path.join(kg_dir, file), encoding='utf-8') as f:
                    src = json.load(f).get('source_file', passport_id)
            except Exception:
                src = passport_id
            work.append((passport_id, src))
    work.sort(key=lambda ps: natural_key(ps[1]))

    success_count = 0
    total_count = 0
    used_names = set()

    for passport_id, src in work:
        print(f"\n--- Compiling {passport_id} ({src}) ---")
        total_count += 1
        try:
            if mono:
                out_name = make_output_name(src, used_names)
                ok = compiler(passport_id, design_tokens, out_name)
            else:
                ok = compiler(passport_id, design_tokens)
            if ok:
                success_count += 1
        except Exception as e:
            import traceback
            err_msg = f"Compilation failed: {e}\n{traceback.format_exc()}"
            print(f"[FAIL] Error compiling {passport_id}: {e}")
            log_qa_failure(passport_id, [f"Compilation error: {e}"])
            log_agent_trace(passport_id, "Failed", 0, err_msg)

    print(f"\n--- Compilation Summary ---")
    print(f"Total compiled: {total_count}")
    print(f"Success (QA Passed): {success_count}")
    print(f"Failed (QA Failed): {total_count - success_count}")
    return 0 if total_count == success_count else 1

if __name__ == "__main__":
    sys.exit(main())
