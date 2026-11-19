import os
import sys
import json
import time
import datetime
import jsonschema
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_whitelist as whitelist

# Ensure we can run it from project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

def load_schema():
    schema_path = '00_governance/schemas/passport_schema_v1.json'
    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Passport schema not found at {schema_path}")
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_existing_passport_id(source_file):
    kg_dir = '03_knowledge_graph'
    if not os.path.exists(kg_dir):
        return None
    for f in os.listdir(kg_dir):
        if f.startswith("COMP-") and f.endswith(".json"):
            filepath = os.path.join(kg_dir, f)
            try:
                with open(filepath, 'r', encoding='utf-8') as jsf:
                    data = json.load(jsf)
                    if data.get('source_file') == source_file:
                        return data.get('passport_id')
            except Exception:
                pass
    return None

def get_next_passport_id():
    kg_dir = '03_knowledge_graph'
    if not os.path.exists(kg_dir):
        return "COMP-100001"
    existing_ids = []
    for f in os.listdir(kg_dir):
        if f.startswith("COMP-") and f.endswith(".json"):
            try:
                num = int(f.split("-")[1].split(".")[0])
                existing_ids.append(num)
            except ValueError:
                pass
    if not existing_ids:
        return "COMP-100001"
    next_num = max(existing_ids) + 1
    return f"COMP-{next_num:03d}"

def log_agent_trace(passport_id, status, duration_ms, error=None, raw_file=None):
    trace_path = 'ops/logs/agent_trace.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent_id": "A02",
        "skill_id": "ontology-normalize",
        "input_artifact": raw_file or '',
        "output_artifact": f"03_knowledge_graph/{passport_id}.json" if status == "Success" else "",
        "status": status,
        "duration_ms": int(duration_ms),
        "warnings": [],
        "error": error
    }
    with open(trace_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def log_validation_error(passport_id, file_name, errors):
    error_path = 'ops/logs/validation_errors.jsonl'
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "passport_id": passport_id,
        "file_name": file_name,
        "errors": errors
    }
    with open(error_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

def generate_markdown_passport(data):
    md = f"# ТЕХНИЧЕСКИЙ ПАСПОРТ ОБЪЕКТА: {data['passport_id']}\n\n"
    md += "## 1. МЕТАДАННЫЕ ИСХОДНИКА\n"
    md += f"- **Файл:** {data['source_file']}\n"
    md += f"- **SHA-256 исходника:** `{data.get('source_sha256', '')}`\n"
    md += f"- **Разрешение:** {data['resolution']['width_px']}x{data['resolution']['height_px']} px\n"
    
    comp_map = {"Low": "Низкая", "Medium": "Средняя", "High": "Высокая"}
    complexity_ru = comp_map.get(data['complexity'], data['complexity'])
    md += f"- **Сложность сцены:** {complexity_ru}\n"
    md += f"- **Провенанс:** {data.get('provenance', 'Локальный диск / Тестовый ассет')}\n\n"

    # Classification block (A07, TZ v3.1)
    if data.get('input_type'):
        md += "## 1A. КЛАССИФИКАЦИЯ И РЕЖИМ РЕНДЕРА\n"
        md += f"- **Класс изображения:** {data['input_type']}\n"
        md += f"- **Режим рендера:** {data.get('render_mode', 'STANDARD_VECTOR')}\n"
        md += f"- **Уровень детализации:** {data.get('vector_detail_level', 'HIGH')}\n"
        if 'aspect_ratio' in data:
            md += f"- **Соотношение сторон:** {data['aspect_ratio']} "
            md += "(сохраняется)\n" if data.get('preserve_aspect_ratio', True) else "\n"
        tc = data.get('target_canvas')
        if tc:
            md += f"- **Целевой canvas:** {tc.get('width')}x{tc.get('height')}\n"
        md += "- **Стандарт вывода:** только #000000, прозрачный фон, полностью вектор\n\n"
    
    md += "## 2. СЕМАНТИЧЕСКАЯ ДЕКОНСТРУКЦИЯ (СЛОИ)\n"
    md += "*Слои в порядке наложения от заднего плана к переднему.*\n\n"
    
    for layer in data['layers']:
        idx = layer['layer_index']
        geom_map = {
            "BoundingRect": "Сплошной прямоугольник (Bounding Box)",
            "CompoundPath": "Замкнутый составной путь (Compound Path)",
            "SimplePath": "Простой путь (Simple Path)",
            "BezierCurve": "Кривая Безье (Bezier Curve)",
            "TextLayer": "Текстовый слой (Text Layer)",
            "GradientMesh": "Градиентная сетка (Gradient Mesh)",
            "ClipMask": "Маска обрезки (Clip Mask)"
        }
        geom_type_ru = geom_map.get(layer['geometry_type'], layer['geometry_type'])
        
        name = "Фоновая подложка" if idx == 1 else "Центральный контур"
        if idx > 2:
            name = f"Дополнительный элемент {idx-2}"
            
        md += f"### Слой {idx}: {name}\n"
        md += f"- **Тип геометрии:** {geom_type_ru}\n"
        
        if 'bounding_box' in layer:
            bbox = [int(x) for x in layer['bounding_box']]
            md += f"- **Координаты границ:** {bbox}\n"
            
        if 'anchor_points_limit' in layer:
            md += f"- **Правило атомарности:** ≤ {layer['anchor_points_limit']} опорных точек на кривую\n"
            
        md += f"- **Выбранный токен цвета:** `{layer['color_token']}`\n"
        
        engine_str = layer['engine']
        if 'vtracer_params' in layer:
            vp = layer['vtracer_params']
            engine_str += f" (`corner_threshold={int(vp.get('corner_threshold', 60))}`, `length_threshold={int(vp.get('length_threshold', 4))}`)"
        md += f"- **Целевой движок:** {engine_str}\n\n"
        
    md += "## 3. СЛУЖЕБНЫЕ МЕТКИ АВТОМАТИЗАЦИИ\n"
    meta = data.get('automation_meta', {})
    status_map = {"Pending": "Ожидает", "InProgress": "В процессе", "Verified": "Верифицировано", "Failed": "Ошибка"}
    status_ru = status_map.get(meta.get('deconstruction_status', 'Verified'), 'Верифицировано')
    md += f"- **Статус деконструкции:** {status_ru}\n"
    md += f"- **Целевой путь экспорта:** {meta.get('export_path', '')}\n"
    md += f"- **Ссылки на граф:** entity_id: {meta.get('entity_id', '')}, source_ref: {meta.get('source_ref', '')}\n"
    
    return md

def validate_released_artifacts():
    """svg_cad T1 audit: check every released SVG in 05_output_svg/ against the
    artifact contract schema (svg_artifact_v1) via its embedded svgcad metadata.
    Non-conformant artifacts are reported (they should never reach release — the
    compiler quarantines them at emission; this is an independent audit)."""
    import glob
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import svg_contract
    files = sorted(glob.glob('05_output_svg/svg_*.svg'))
    ok = bad = 0
    for fp in files:
        with open(fp, 'r', encoding='utf-8') as f:
            svg = f.read()
        meta = svg_contract.extract_metadata(svg)
        if meta is None:
            print(f"[NO METADATA] {os.path.basename(fp)}")
            bad += 1
            continue
        errs = svg_contract.validate_artifact(meta)
        if errs:
            print(f"[SCHEMA FAIL] {os.path.basename(fp)}: {errs}")
            bad += 1
        else:
            ok += 1
    print(f"\nArtifact audit: {ok} conformant / {bad} non-conformant of {len(files)}")
    return bad == 0

def main():
    if '--artifacts' in sys.argv:
        return 0 if validate_released_artifacts() else 1
    print("Starting A02: Validator & Normalizer script...")
    whitelist.load_registry()
    for directory in ('03_knowledge_graph', '04_passports', 'ops/logs'):
        os.makedirs(directory, exist_ok=True)
    for path in __import__('pathlib').Path('03_knowledge_graph').glob('COMP-*.json'):
        with path.open(encoding='utf-8') as stream:
            whitelist.verify_record(json.load(stream))
    failed = 0
    
    try:
        schema = load_schema()
    except Exception as e:
        print(f"[FAIL] Schema load error: {e}")
        return 1
        
    proc_dir = '02_processing'
    if not os.path.exists(proc_dir):
        print(f"Directory {proc_dir} does not exist.")
        return 1
        
    for file in sorted(os.listdir(proc_dir)):
        if file.endswith('_raw.json'):
            raw_path = os.path.join(proc_dir, file)
            start_time = time.time()
            
            try:
                with open(raw_path, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
            except Exception as e:
                failed += 1
                print(f"[FAIL] Failed to read {raw_path}: {e}")
                continue
                
            whitelist.verify_record(raw_data)
            source_file = raw_data['source_file']
            
            # 1. Determine or assign COMP ID
            passport_id = get_existing_passport_id(source_file)
            if not passport_id:
                passport_id = get_next_passport_id()
                
            # 2. Add required fields for schema validation
            raw_data['passport_id'] = passport_id
            
            # Setup automation_meta
            comp_num = passport_id.split('-')[1]
            raw_data['automation_meta'] = {
                "deconstruction_status": "Verified",
                "export_path": f"/05_output_svg/res_comp_{comp_num}.svg",
                "entity_id": f"logo_primitive_{comp_num}",
                "source_ref": f"SRC-{int(comp_num) + 20:04d}"
            }
            
            # 3. Validate against JSON schema
            validator = jsonschema.Draft7Validator(schema)
            errors = sorted(validator.iter_errors(raw_data), key=lambda e: e.path)
            
            duration_ms = (time.time() - start_time) * 1000
            
            if errors:
                failed += 1
                err_messages = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors]
                log_validation_error(passport_id, file, err_messages)
                log_agent_trace(passport_id, "Failed", duration_ms, "; ".join(err_messages), raw_file=raw_path)
                print(f"[FAIL] Validation failed for {file}. Errors logged to ops/logs/validation_errors.jsonl")
            else:
                # 4. Save normalized JSON index
                kg_path = f"03_knowledge_graph/{passport_id}.json"
                with open(kg_path, 'w', encoding='utf-8') as f:
                    json.dump(raw_data, f, indent=2, ensure_ascii=False)
                    
                # 5. Generate and save Technical Passport Markdown
                passport_path = f"04_passports/{passport_id}.md"
                md_content = generate_markdown_passport(raw_data)
                with open(passport_path, 'w', encoding='utf-8') as f:
                    f.write(md_content)
                    
                log_agent_trace(passport_id, "Success", duration_ms, raw_file=raw_path)
                print(f"[OK] Verified and normaled: {passport_id} -> {passport_path} ({duration_ms:.1f}ms)")

    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
