# Spec — exclusão de arquivos no check `file_size` (com merge aditivo de listas)

## 1. Problema

O check `file_size` reprova todo PR que inclui artefatos gerados pelo EF Core: migrations
(`*.Designer.cs`, ~5.400 linhas) e o `BackofficeContextModelSnapshot.cs`, cujo método `BuildModel`
único tem ~5.380 linhas. Hoje o runner só ignora arquivos por **extensão hardcoded**
(`_IGNORED_EXTENSIONS` em `app/runners/file_size.py`), sem nenhuma forma de excluir por caminho.

## 2. Objetivo

1. Permitir excluir arquivos do check `file_size` por **padrão de caminho (glob)**, configurável.
2. Tornar o merge de listas de config **aditivo**: o default global em `quality_gate.yaml` é mantido
   e cada projeto, via `.babysit.yml`, **acrescenta** padrões — sem precisar repetir a lista global.

## 3. Escopo

- **Inclui:** novo campo `exclude` no check `file_size`; mudança no `_deep_merge` para concatenar
  listas com dedup; defaults globais para artefatos .NET gerados; testes; doc.
- **Não inclui:** mudanças em complexity/duplication/secrets; sintaxe de "remoção" de itens herdados;
  semântica gitignore estrita (usaremos `fnmatch`).

## 4. Requisitos funcionais

- **RF1** — `file_size.exclude` é uma lista de globs; arquivos cujo caminho relativo casar com
  qualquer padrão são ignorados pelo check (não geram violação nem entram em `max_file_lines` /
  `max_function_lines`).
- **RF2** — match via `fnmatch` sobre o caminho relativo normalizado (`/`, sem prefixo `./` ou `/`).
  `*` cruza separadores (`*Migrations*` casa em qualquer profundidade; `*.Designer.cs` casa por sufixo).
- **RF3** — `exclude` ausente ou vazio ⇒ comportamento idêntico ao atual (retrocompatível).
- **RF4** — Listas em config passam a ser **somadas** no merge: `resultado = base + override`,
  preservando ordem (base primeiro) e **removendo duplicatas**. Vale para qualquer chave-lista,
  não só `file_size.exclude`.
- **RF5** — Defaults globais cobrem artefatos .NET gerados; projetos só precisam adicionar o que for
  específico (ex.: templates HTML).

## 5. Contexto do código

- `app/runners/file_size.py` — `FileSizeRunner.__init__(max_lines_per_file=400,
  max_lines_per_function=80)`. `run()` pula `_IGNORED_EXTENSIONS`, mede `max_file_lines` e funções.
- `app/gates/orchestrator.py:21-26` — instancia `FileSizeRunner` a partir de `checks["file_size"]`.
- `settings.py:45-73` — `load_quality_gate_config(workspace)` faz
  `_deep_merge(quality_gate.yaml, <workspace>/.babysit.yml)`. **Hoje `_deep_merge` SUBSTITUI listas.**
- Confirmado: **nenhuma config atual usa listas**, então mudar o merge para somar não causa regressão.

## 6. Design / mudanças

### 6.1 `settings.py` — merge aditivo de listas

Alterar `_deep_merge` para concatenar listas com dedup preservando ordem:

```python
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        base_value = merged.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            merged[key] = _deep_merge(base_value, value)
        elif isinstance(value, list) and isinstance(base_value, list):
            merged[key] = _merge_lists(base_value, value)
        else:
            merged[key] = value
    return merged


def _merge_lists(base: list, override: list) -> list:
    merged = list(base)
    for item in override:
        if item not in merged:
            merged.append(item)
    return merged
```

### 6.2 `app/runners/file_size.py` — suporte a `exclude`

- `import fnmatch` no topo.
- `__init__` ganha `exclude: list[str] | None = None`; guardar `self.exclude = exclude or []`.
- Helper:

```python
def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    normalized = rel_path.lstrip("/").replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pat) for pat in patterns)
```

- Em `run()`, logo após o filtro de extensão (≈ linha 64), **antes de medir**:

```python
if _matches_any(rel_path, self.exclude):
    continue
```

### 6.3 `app/gates/orchestrator.py` — repassar `exclude`

```python
runners.append(FileSizeRunner(
    max_lines_per_file=cfg.get("max_lines_per_file", 400),
    max_lines_per_function=cfg.get("max_lines_per_function", 80),
    exclude=cfg.get("exclude", []),
))
```

### 6.4 `quality_gate.yaml` — defaults globais

```yaml
    file_size:
      enabled: true
      max_lines_per_file: 400
      max_lines_per_function: 80
      exclude:
        - "*Migrations*"        # migrations EF Core (qualquer profundidade)
        - "*.Designer.cs"       # arquivos gerados pelo designer
        - "*.g.cs"              # source generators
        - "*.generated.cs"
```

### 6.5 (Opcional) `.babysit.yml` do projeto — só acréscimos

Com o merge aditivo, o projeto adiciona sem repetir os globais. Ex.: energia incluindo o template:

```yaml
quality_gate:
  checks:
    file_size:
      exclude:
        - "*Templates*"   # fatura.html (template com CSS embutido, ~452 linhas)
```

Resultado efetivo (energia) = defaults globais + `*Templates*`.

## 7. Testes (pytest)

### `tests/test_quality_gate_config.py` — merge aditivo
- **`test_deep_merge_concatenates_lists_with_dedup`** — base
  `exclude: ["*Migrations*", "*.Designer.cs"]` + projeto `exclude: ["*Templates*", "*Migrations*"]`
  ⇒ `["*Migrations*", "*.Designer.cs", "*Templates*"]` (ordem preservada, sem duplicata).
- **`test_load_config_extends_global_file_size_exclude`** — `.babysit.yml` com
  `file_size: { exclude: ["*Templates*"] }` ⇒ resultado contém os globais **e** `*Templates*`.

### `tests/test_file_size_exclude.py` (novo) — runner
- **`test_skips_excluded_glob`** — `src/.../Migrations/v066.Designer.cs` (600 linhas) +
  `exclude=["*Migrations*"]` ⇒ `passed`, `violations == []`, `max_file_lines == 0`.
- **`test_skips_excluded_by_suffix`** — `Foo.Designer.cs` (600 linhas) + `exclude=["*.Designer.cs"]`
  ⇒ `passed`, sem violação.
- **`test_still_flags_non_excluded`** — `Service.cs` (600 linhas) + `Migrations/x.Designer.cs` (600),
  `exclude=["*Migrations*"]` ⇒ `failed`, 1 violação (no `Service.cs`), `max_file_lines == 600`.
- **`test_empty_exclude_is_noop`** — `exclude=[]` ⇒ comportamento atual preservado.

## 8. Critérios de aceite

- [ ] `pytest` verde (todos os novos testes + suíte existente).
- [ ] `_deep_merge` soma listas com dedup; suíte de config existente continua passando.
- [ ] Gate local em `20260605014703_v066.Designer.cs` + `BackofficeContextModelSnapshot.cs` ⇒
      `file_size` **passed** apenas com os defaults globais (sem `.babysit.yml`).
- [ ] `.babysit.yml` do energia com `exclude: ["*Templates*"]` ⇒ `fatura.html` deixa de reprovar **e**
      as migrations seguem excluídas (prova do merge aditivo).
- [ ] `.cs` escrito à mão com 600 linhas (fora de `Migrations/`) ⇒ continua **failed** (sem regressão).
- [ ] `exclude` ausente ⇒ no-op.

## 9. Riscos / notas

- `_merge_lists` usa `item not in merged` para dedup — assume itens hasháveis/comparáveis (strings de
  glob; ok). Listas de dicts não são esperadas nas configs atuais.
- `fnmatch` não é gitignore estrito (`*` cruza `/`). Suficiente para artefatos .NET; migrar para
  `pathspec` só se surgir necessidade real de semântica gitignore.
- Mudança em `_deep_merge` é global a toda a config; mitigado pelo fato de que **nenhuma config atual
  usa listas** (verificado) e pelos testes de merge acima.
- Excluir o arquivo pula também o `BuildModel` gigante (a exclusão é por arquivo, antes de
  `_count_function_lines`) — comportamento desejado.
