# Итоговый отчёт об очистке референсов SVG Builder

Дата: 2026-11-19. Основание: owner addendum v1.1. Статус: **LOCAL VALIDATION / PUBLICATION PENDING**.

## Результат

Активный source set — ровно 56 фактических изображений: 55 complex_gradients
и одно high_contrast. Все исходные SHA-256 совпали с проверенным backup.
Архив отсутствует. Неподтверждённые исторические связи не переиспользовались.
Программные модули, SSOT и контракты сохранены; добавлены проверки provenance,
ограничения записи/удаления и reconciliation статусов повторной генерации.

## Измерения до и после

| Metric | Before | After |
|---|---:|---:|
| Raster sources | 194 | 56 |
| COMP records | 124 | 56 |
| Passports | 124 | 56 |
| SVG outputs (release + staging + quarantine) | 242 | 112 |
| 3MF binaries | 22 | 18 |
| 3MF sidecars | 23 | 18 |
| Orphan references | 124 | 0 |
| Archive files | 303 | 0 |
| Broken internal links | 10 | 0 |

Количество файлов считается по путям, не по уникальному содержимому. До очистки
растры учитывались в активных входных каталогах, без архива. До: 121 release SVG
и 121 staging SVG. После: 56 release SVG и
56 staging SVG;
quarantine SVG: 0. Staging регенерируется и не публикуется.
В чистом Git checkout до генерации находятся только 56 release SVG.

`Orphan references` до: 124 source-связи COMP, не подтверждённые каноническим
путём и SHA-256. Это не утверждение о 124 физически отсутствующих файлах.
После: проверены прямые и обратные связи всего текущего набора; отсутствие
binary, sidecar, passport или generation outcome приводит к FAIL.

## Backup и удаление

- Полный all-refs Git bundle и ZIP рабочей папки, включая ignored/untracked и Git metadata.
- Восстановлены и сверены SHA-256 всех 2904 файлов (411284754 байт).
- Bundle восстановлен клонированием; `git fsck --full` завершился успешно.
- SHA-256 ZIP: `47eb2db09bfd87fc11b0898deca43492f773bd43e80f6e96dd1716fc77da035a`.
- SHA-256 bundle: `444ef41d4a6a6836ea4370af1c3b0eee4940ef3b4caa3dac04c4a607dc0bf6e5`.
- Проверенный внешний реестр содержит 1 152 исключённых файла с размером и SHA-256.
- Перед удалением сверены оригинал, восстановленная копия и нахождение пути внутри проекта.
- Исключены архив, старые входы, графы, паспорта, результаты и исторические документы;
  их персональные названия не публикуются. Backup находится вне репозитория.

## Whitelist и provenance

Fingerprint отсортированных пар canonical_relative_path + SHA-256:
`b9c3a0cb516c0d6bc7c1813b8a97686e45850d1005d43f8e2a84b1c1c47719f2`.

Реестр: [reference-whitelist.json](../../reference-whitelist.json).
Подробная цепочка каждого разрешённого исходника:
[REFERENCE_LINEAGE_AUDIT.md](../../REFERENCE_LINEAGE_AUDIT.md).

Все 56 COMP и паспортов проверены по схеме и фактическим байтам источника.
Все 56 release SVG проверены по embedded contract, монохромной политике,
viewBox, режиму, SSIM, eligibility и совпадению с graph mirror.

## SVG и 3MF: разные результаты

- SVG: 56 PASSED, 0 QUARANTINED.
- STANDARD_VECTOR / 3MF-eligible: 42; SKETCH_VECTOR / not eligible: 14.
- 3MF: 18 выпущенных binary и столько же настоящих sidecars.
- 66 отказов вариантов сохранены как `.failure.json`, без фиктивных binary.
- В 66 диагностических записях присутствует превышение printer bounds;
  в 7 также указаны проблемы watertight/winding. Категории пересекаются.
- Все выпущенные 3MF повторно импортированы: watertight, winding consistency,
  положительный объём, допустимые габариты, соответствие sidecar и millimeter units.

Профиль 500 мм по длинной стороне и 550×450×450 мм printer bounds сохранён.
Число отказов не уменьшалось изменением порогов или масштаба.

## Проверки

- 18 regression tests whitelist/containment: PASS.
- 17 SVG contract / STANDARD_VECTOR assertions: PASS.
- 14 3MF geometry assertions: PASS.
- Полная генерация владельца: 56 raw → 56 schema-valid records/passports → 56 SVG PASSED.
- Все 42 допустимых объекта обработаны для cutout и raised: 84 явных outcome.
- Сквозной isolated smoke: 2 геометрических fixtures → 2 SVG → 4 3MF.
- Повтор PASS → FAIL → PASS не оставляет старых binary/sidecar/diagnostics/mirrors.
- Удаление binary обнаруживается аудитом; SKETCH_VECTOR CLI проверен на fixture.
- Зависимости установлены в отдельное чистое venv из requirements.txt.
- Итоговый dataset audit: PASS; orphan references: 0;
  broken internal links: 0.

## Права, приватность и воспроизводимость

Владелец подтвердил право публикации всех 56 изображений и производных.
Полный набор включается в публичный Git tree, с неизменными байтами оригиналов.
Контактные листы просмотрены; проверены доступные EXIF/XMP: GPS, приватные
локальные пути и секреты не обнаружены. Атрибуция и водяные знаки сохранены.
Основание и границы прав: [ASSET_RIGHTS.md](../../ASSET_RIGHTS.md).

Синтетические fixtures для smoke создаются только во внешней временной папке;
они не пополняют набор владельца. `.gitattributes` сохраняет байты SVG и
их SHA-256 при checkout на разных платформах.

## Публикация

Публикация ожидает финальной локальной проверки и Pull Request в `master`. Remote changes ещё не объявляются выполненными.


## Ограничения

Автоматический SVG gate не равен ручной визуальной приёмке или проверке
узнаваемости. SSIM сравнивает результат с маской сегментации/линий, не с
исходным цветным изображением. Не все 3MF успешно изготовлены; физическая
печать не выполнялась. Право публикации подтверждено владельцем, не внешней
юридической экспертизой. История Git не переписывалась; старая ветка dev
и исторические коммиты могут содержать исключённые материалы.

Blender Farm Pro в объём этого задания не входит и не изменялся.
