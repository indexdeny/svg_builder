# Стандарты именования файлов проекта

1. **Паспорта объектов**: `04_passports/COMP-NNN.md` (где NNN — от 3 до 6 цифр).
2. **База знаний / Граф**: `03_knowledge_graph/COMP-NNN.json`.
3. **Результаты деконструкции**: `02_processing/{sha256_canonical_path_16}_raw.json`.
4. **Оригинальные растры**: `01_input_raster/{category}/{source_filename}`.
5. **Выходные SVG**:
   - Финальный релиз: `05_output_svg/svg_{source_base}.svg` (с суффиксом коллизии `_NN` при необходимости).
   - Буфер сборки: `05_output_svg/staging/{stem}_compiled.svg`.
   - Карантин: `05_output_svg/quarantine/{output_filename}`.
6. **Источники истины**: фактические файлы только в `01_input_raster/complex_gradients/` и `01_input_raster/high_contrast/`; реестр — `reference-whitelist.json`.
7. **Идентичность**: канонический относительный путь и SHA-256 исходника. Имя файла само по себе не доказывает происхождение.
8. **3MF**: `07_output_3mf/{svg_stem}_{cutout|raised}.3mf` и соответствующий `.3mf.json`.
9. **Неуспешная генерация 3MF**: `.failure.json` в ignored quarantine. Это диагностика, а не sidecar существующей модели.
10. **Backup**: только вне рабочей папки; активного архива внутри проекта нет.
