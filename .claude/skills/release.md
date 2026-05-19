# /release — подготовить новый релиз (CalVer)

Этот project-local override переопределяет плагин-скилл `release`. Используется
формат версии **`YYYY.MM.DD.N`** (CalVer), переход с semver описан в
`docs/superpowers/specs/2026-05-19-calver-migration-design.md`.

## Контекст (что отличается от глобального скилла)

- Версия в `pyproject.toml` имеет формат `YYYY.MM.DD.N`
- Git-теги без `v`-префикса: `2026.05.19.0`, не `v2026.05.19.0`
- Категории `feat` / `fix` / `improvement` / `internal` сохраняются в CHANGELOG,
  но **не определяют bump** — версия всегда date-based

## Последовательность

### 1-4. Состояние, стартовая точка, сбор коммитов, классификация

Без изменений относительно глобального скилла.

### 5. Определи следующую версию (CalVer)

```bash
today=$(date +%Y.%m.%d)
last_n=$(git tag -l "${today}.*" | sort -V | tail -1 | awk -F. '{print $4}')
if [ -z "$last_n" ]; then
  new_version="${today}.0"
else
  new_version="${today}.$((last_n + 1))"
fi
```

Никаких major/minor/patch — `N` инкрементируется в рамках дня. Если в один день
два хотфикса — `.0`, `.1`, `.2`.

**Zero-padding обязателен**: `date +%Y.%m.%d` уже выдаёт `MM` и `DD` в два знака.
Не переписывать в `YYYY.M.D` — `packaging` нормализует `0` и `00`, и
`2026.5.19.0` будет считаться той же версией что `2026.05.19.0`.

### 6-8. pyproject.toml, CHANGELOG, RELEASE_NOTES

Без изменений. Заголовок CHANGELOG-секции теперь
`## [YYYY.MM.DD.N] - YYYY-MM-DD`.

### 9. Тесты, 10. Показ результата, 11. Commit/tag/push

```bash
git add pyproject.toml CHANGELOG.md RELEASE_NOTES.md
git commit -m "chore(release): ${new_version}"
git tag "${new_version}" -m "${new_version}"
git push --follow-tags
```

Без `v`-префикса. Сравнение версий через `sort -V`.

### 12-13. CD и финал

Без изменений.

## Сводка отличий от глобального скилла

| Поле | Глобальный | Project-local |
|------|-----------|---------------|
| Формат | `vX.Y.Z` | `YYYY.MM.DD.N` |
| Bump-логика | semver (BREAKING/feat/fix → major/minor/patch) | date + N в дне |
| Tag prefix | `v` | нет |
