# Plano: camadas no MCP para review semântica por subagentes

> **Para agentes:** implemente task a task, na ordem. Cada step tem checkbox (`- [ ]`).
> Fluxo por task: escrever teste que falha → rodar e ver falhar → implementar → rodar e ver passar.

**Goal:** permitir que um cliente MCP com múltiplos agentes revise um PR grande *semanticamente*
sem rodar tudo inline: o servidor entrega contexto fatiado (diff + padrões + schema de finding),
cada subagente julga a sua fatia com o próprio LLM e devolve findings estruturados, e o servidor
consolida tudo — findings de agente e checks determinísticos — em um único veredito. PR pequeno
continua inline, com a mesma régua.

**Architecture:** hoje o fan-out existente (`plan_pr_review` → `run_review_task` →
`get_review_plan` → `close_review_plan`) paraleliza apenas os *runners determinísticos*: o
subagente é um gatilho, não um revisor. Este plano acrescenta duas tools que quebram esse
acoplamento em camadas:

| Camada | Tool | Executor |
|---|---|---|
| Plano | `plan_pr_review(mode=auto\|inline\|sharded)` | servidor decide partição |
| Determinística | `run_review_task(plan_id, task_id, detail)` | runners no servidor, por shard |
| Contexto | `get_task_context(plan_id, task_id)` | servidor entrega diff, `worktree_path`, `standards`, `finding_schema` e os checks determinísticos da shard |
| Julgamento | — | subagente (LLM do cliente), fora do servidor |
| Retorno | `submit_task_findings(plan_id, task_id, findings)` | servidor converte em `CheckResult(check="agent_review")` |
| Consolidação | `get_review_plan(plan_id, force?)` | agrega, aplica ratchet uma vez, devolve tabela + seção de findings |
| Limpeza | `close_review_plan(plan_id)` | remove worktree e registros |

O ponto de projeto é **híbrido**: o que é barato e reprodutível (tamanho, complexidade,
duplicação, secrets) continua no servidor; o que é caro e semântico sai do servidor e vai para o
subagente. O caminho Ollama (`big_o`, `ai_review`) **permanece**, desligado por padrão, como
alternativa para CI headless sem agente.

**Restrições que definem o desenho** (verificadas no código atual):

| Fato | Onde | Consequência no plano |
|---|---|---|
| Shards determinísticas são empacotadas por peso (`added_lines`), maior primeiro | `app/gates/review_plan.py:60` | Review semântica **não** reusa essa partição: agrupa por diretório antes, com teto próprio |
| `Violation` já tem `severity: str` (default `"medium"`) | `app/gates/models.py:16` | `high\|medium\|low` cabe nativo; faltam só `category` e `suggestion` |
| `build_quality_gate_table` só imprime métricas numéricas | `app/gates/mcp_table.py:23` | Findings sumiriam da saída: precisa de seção própria abaixo da tabela |
| `aggregate_checks` funde `CheckResult` por nome, dedupa por `(file, line, message)` | `app/gates/review_plan.py:283` | Findings de agente entram na consolidação sem tabela nova e sem caminho de merge novo |
| `ratchet.extract_metrics` promove toda métrica numérica a baseline | `app/gates/ratchet.py:16` | Métrica de `agent_review` precisa ser excluída explicitamente: não é reprodutível |
| `_one_shot_task` emite `call.args = {plan_id, files}` | `app/gates/review_plan.py:95` | Bug: `run_local_gate` exige `workspace` e não aceita `plan_id`. Corrigir direto |
| `load_standards()` resolve `settings.standards_path` relativo ao CWD do servidor | `app/ai/prompts.py:6` | Precisa aceitar workspace para ler o padrão **do projeto revisado** |

**Tech Stack:** Python 3.11, pytest, Pydantic, SQLAlchemy (SQLite), YAML de config, MCP stdio.

---

## Decisões travadas (entrevista de 2026-08-20)

1. **Híbrido.** Determinístico no servidor, semântico no subagente. Ollama fica, sem promoção.
2. **Findings** usam vocabulário próprio `high|medium|low`, mapeados na agregação
   (`high` → `failed`, `medium` → `warning`, `low` → informativo), controlado por
   `agent_review.block_on`.
3. **Nada de agente entra no baseline.** Findings influenciam o veredito do run, nunca o ratchet.
4. **O plano decide** quais fatias vão para subagente (tasks `review-N`), não o cliente.
5. **Partição semântica é própria:** agrupa por diretório, depois empacota, com tetos menores
   (6 arquivos / 400 linhas).
6. **Contexto nunca trunca em silêncio:** `max_context_chars` corta por arquivo e a resposta traz
   `truncated: [paths]`.
7. **Critério vem do servidor:** `standards` + `finding_schema` no contexto, para a régua ser a
   mesma entre clientes diferentes e entre `one_shot` e `sharded`.
8. **Task de agente que não volta não trava o plano:** `get_review_plan(force=true)` consolida,
   marca as faltantes como `skipped` e impede veredito `passed` limpo.
9. **Modo `WORKTREE`** revisa o checkout vivo (rápido, volátil): `volatile: true` na resposta,
   sem worktree temporário e sem gravar baseline.
10. **Saída legível:** tabela + seção de findings, topo 20 por severidade, resto via
    `get_gate_run(run_id)`.

---

## Contrato das tools (alvo)

```jsonc
// 1) plan_pr_review — agora com mode e tasks semânticas
{ "workspace": "C:\\Projetos\\meu-projeto", "base_ref": "origin/main", "head_ref": "HEAD", "mode": "auto" }
// -> sharded, com agent_review habilitado
{
  "plan_id": "…uuid…",
  "mode": "sharded",
  "files_total": 63,
  "parallel_hint": 4,
  "volatile": false,
  "tasks": [
    { "task_id": "global",   "kind": "deterministic", "checks": ["duplication", "secrets", "complexity"],
      "files": ["…"], "call": { "tool": "run_review_task", "args": {"plan_id": "…", "task_id": "global"} } },
    { "task_id": "files-1",  "kind": "deterministic", "checks": ["file_size", "big_o"],
      "files": ["…"], "call": { "tool": "run_review_task", "args": {"plan_id": "…", "task_id": "files-1"} } },
    { "task_id": "review-1", "kind": "agent", "checks": ["agent_review"],
      "files": ["app/gates/review_plan.py", "app/gates/orchestrator.py"],
      "call":   { "tool": "get_task_context",     "args": {"plan_id": "…", "task_id": "review-1"} },
      "submit": { "tool": "submit_task_findings", "args": {"plan_id": "…", "task_id": "review-1"} } }
  ]
}

// modo one_shot: uma call determinística + a régua, para o agente principal revisar inline
{ "plan_id": "…", "mode": "one_shot", "files_total": 4,
  "tasks": [{ "task_id": "single", "kind": "deterministic", "checks": ["*"], "files": ["…"],
              "call": { "tool": "run_local_gate", "args": { "workspace": "C:\\…", "files": ["…"] } } }],
  "standards": "…conteúdo de docs/coding-standards.md…",
  "finding_schema": { "…": "…" } }

// 2) get_task_context
{ "plan_id": "…", "task_id": "review-1" }
// -> resposta
{
  "plan_id": "…", "task_id": "review-1",
  "worktree_path": "C:\\Users\\…\\Temp\\babysit-review-plan-…\\repo",
  "volatile": false,
  "files": [ { "path": "app/gates/review_plan.py", "added_lines": 120, "diff": "@@ -1,4 +1,9 @@…" } ],
  "truncated": [],
  "deterministic_checks": [ { "check": "file_size", "status": "passed", "metrics": {"max_file_lines": 353}, "violations": [] } ],
  "standards": "…",
  "finding_schema": {
    "file": "caminho relativo ao worktree",
    "line": "int ou null",
    "severity": "high | medium | low",
    "category": "correctness | security | performance | design | test | style",
    "message": "o problema, uma frase",
    "suggestion": "a correção concreta"
  },
  "instructions": "Revise apenas os arquivos desta task. Use worktree_path para ler o entorno…"
}

// 3) submit_task_findings
{ "plan_id": "…", "task_id": "review-1",
  "findings": [ { "file": "app/gates/review_plan.py", "line": 95, "severity": "high",
                  "category": "correctness", "message": "…", "suggestion": "…" } ] }
// -> resposta enxuta
{ "plan_id": "…", "task_id": "review-1", "accepted": 3, "by_severity": {"high": 1, "medium": 2, "low": 0}, "remaining_tasks": 2 }
```

---

## File Structure

- Modificar `app/gates/models.py`: `Violation.category` e `Violation.suggestion` (aditivos).
- Modificar `app/gates/review_plan.py`: fix do `one_shot`; partição semântica (`review-N`);
  mapeamento severity→status; exclusão de `agent_review` do baseline; `force`.
- Modificar `app/gates/git_diff.py`: `file_diffs(repo_root, base, head, paths, mode)`.
- Modificar `app/gates/mcp_table.py`: seção de findings abaixo da tabela.
- Modificar `app/ai/prompts.py`: `load_standards(workspace)` e `FINDING_SCHEMA`.
- Modificar `mcp_server_standalone.py`: `mode` em `plan_pr_review`, modo `WORKTREE`, `detail` em
  `run_review_task`, novas tools `get_task_context` e `submit_task_findings`, `force` em
  `get_review_plan`.
- Modificar `app/storage/database.py`: PRAGMA WAL + `busy_timeout` no engine.
- Modificar `quality_gate.yaml`: `checks.agent_review` e novas chaves em `pr_review`.
- Criar `tests/test_agent_review_plan.py`: partição semântica e mapeamento de severidade.
- Criar `tests/test_task_context.py`: montagem de contexto, truncamento, modo volátil.
- Criar `tests/test_agent_findings.py`: submissão, agregação, baseline e `force`.
- Modificar `tests/test_quality_gate_config.py`, `tests/test_review_plan.py`,
  `tests/test_review_plan_mcp.py`, `tests/test_mcp_table.py`.
- Modificar `README.md`: seção "Review semântica com subagentes".

---

### Task 1: Config `agent_review` e tetos da partição semântica

**Files:**
- Modify: `quality_gate.yaml`
- Test: `tests/test_quality_gate_config.py`

- [x] **Step 1: Teste que falha**

```python
def test_quality_gate_config_exposes_agent_review_defaults():
    cfg = Settings().load_quality_gate_config()
    checks = cfg["quality_gate"]["checks"]
    pr = cfg["quality_gate"]["pr_review"]

    assert checks["agent_review"] == {"enabled": False, "block_on": "high"}
    assert pr["max_context_chars"] == 120000
    assert pr["max_files_per_review_task"] == 6
    assert pr["max_diff_lines_per_review_task"] == 400


def test_project_config_can_enable_agent_review(tmp_path):
    (tmp_path / ".babysit.yml").write_text(
        "quality_gate:\n  checks:\n    agent_review:\n      enabled: true\n      block_on: none\n",
        encoding="utf-8",
    )
    cfg = Settings().load_quality_gate_config(tmp_path)

    assert cfg["quality_gate"]["checks"]["agent_review"]["enabled"] is True
    assert cfg["quality_gate"]["checks"]["agent_review"]["block_on"] == "none"
    # o merge não pode apagar os checks determinísticos
    assert cfg["quality_gate"]["checks"]["file_size"]["enabled"] is True
```

- [x] **Step 2: Rodar e ver falhar** — `pytest tests/test_quality_gate_config.py -v` (KeyError `agent_review`).

- [x] **Step 3: Adicionar em `quality_gate.yaml`**

```yaml
    agent_review:
      enabled: false        # o cliente MCP precisa saber fazer fan-out de subagente
      block_on: high        # high | none
```

```yaml
  pr_review:
    # …chaves atuais…
    max_context_chars: 120000
    max_files_per_review_task: 6
    max_diff_lines_per_review_task: 400
```

- [x] **Step 4: Rodar e ver passar.** `enabled: false` por padrão mantém o comportamento de quem
  já usa o servidor hoje: nenhuma task `review-N` é emitida.

---

### Task 2: `Violation` ganha `category` e `suggestion`

**Files:**
- Modify: `app/gates/models.py`
- Test: `tests/test_agent_findings.py` (criar)

Um finding de agente carrega mais informação que uma violation de runner: a categoria do problema
e a correção sugerida. Os campos são **aditivos e opcionais**, então nenhum runner atual muda.

- [x] **Step 1: Testes que falham**

```python
def test_violation_accepts_category_and_suggestion():
    violation = Violation(
        file="app/x.py", line=10, severity="high",
        category="correctness", message="…", suggestion="…",
    )

    assert violation.category == "correctness"
    assert violation.suggestion == "…"


def test_violation_defaults_keep_existing_runners_working():
    violation = Violation(file="app/x.py", line=1, message="…")

    assert violation.severity == "medium"
    assert violation.category == ""
    assert violation.suggestion == ""
```

- [x] **Step 2: Rodar e ver falhar** (Pydantic ignora/rejeita campo desconhecido).

- [x] **Step 3: Implementar** — dois campos `str = ""` em `Violation`
  (`app/gates/models.py:16`). Não mexer em `CheckResult.suggestion`, que continua sendo a
  sugestão *do check inteiro* usada pelo caminho Ollama.

- [x] **Step 4: Rodar a suíte inteira** — `pytest -q`. Serialização de `CheckResult` já usa
  `model_dump(mode="json")` em `mcp_server_standalone.py:539`; os campos novos passam a viajar
  para o SQLite sem alteração de schema (o payload é `Text`/JSON).

---

### Task 3: Fix do `one_shot` e régua compartilhada

**Files:**
- Modify: `app/gates/review_plan.py`, `app/ai/prompts.py`, `mcp_server_standalone.py`
- Test: `tests/test_review_plan.py`, `tests/test_review_plan_mcp.py`

Dois problemas juntos: (a) a call do `one_shot` é inexecutável — manda `plan_id`, que
`run_local_gate` não aceita, e omite `workspace`, que ele exige
(`mcp_server_standalone.py:236`); (b) no modo inline o agente principal não recebe a régua, então
PR pequeno seria revisado com critério diferente do grande.

- [x] **Step 1: Testes que falham**

```python
def test_one_shot_call_targets_run_local_gate_with_workspace():
    plan = review_plan.build_plan(files=_files(4, added=30), config=_cfg(), plan_id="p1",
                                  workspace="C:\\Projetos\\x")
    call = plan["tasks"][0]["call"]

    assert call["tool"] == "run_local_gate"
    assert call["args"]["workspace"] == "C:\\Projetos\\x"
    assert set(call["args"]) == {"workspace", "files"}     # nada de plan_id


def test_one_shot_plan_carries_standards_and_schema():
    plan = review_plan.build_plan(files=_files(4, added=30), config=_cfg(agent_review=True),
                                  plan_id="p1", workspace="/tmp/x", standards="PADRÃO")

    assert plan["standards"] == "PADRÃO"
    assert plan["finding_schema"]["severity"] == "high | medium | low"


def test_standards_are_read_from_the_reviewed_workspace(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "coding-standards.md").write_text("REGRAS DO PROJETO", encoding="utf-8")

    assert prompts.load_standards(tmp_path) == "REGRAS DO PROJETO"
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar**

  - `_one_shot_task(files, plan_id, workspace)` passa a emitir
    `{"tool": "run_local_gate", "args": {"workspace": workspace, "files": paths}}`
    (`app/gates/review_plan.py:88`). `build_plan` recebe `workspace: str = ""` e `standards: str = ""`.
  - `app/ai/prompts.py`: `load_standards(workspace: Path | None = None)` resolve
    `<workspace>/<settings.standards_path>` quando existir, senão cai no caminho atual relativo ao
    CWD. Adicionar `FINDING_SCHEMA: dict` no mesmo módulo (fonte única do schema, usado pelo plano
    e por `get_task_context`).
  - `plan_pr_review` passa `workspace=str(repo_root)` e `standards=load_standards(repo_root)` para
    `build_plan`. Import de `prompts` é leve (não puxa Ollama) — manter no topo só se não arrastar
    `app.ai.ollama`; caso contrário, import tardio dentro da função.

- [x] **Step 4: Rodar e ver passar** — `pytest tests/test_review_plan.py tests/test_review_plan_mcp.py -v`.

---

### Task 4: `mode` e partição semântica (`review-N`)

**Files:**
- Modify: `app/gates/review_plan.py`, `mcp_server_standalone.py`, `quality_gate.yaml`
- Test: `tests/test_agent_review_plan.py` (criar)

Duas coisas nesta task porque compartilham o mesmo ponto de código (`build_plan`): o parâmetro
`mode` (`auto|inline|sharded`) e as tasks `review-N`. A partição semântica **não** reusa
`_pack_shards`: agrupar por peso joga arquivos sem relação na mesma fatia, e o subagente precisa
de coerência de módulo para julgar bem.

- [x] **Step 1: Testes que falham**

```python
def test_mode_inline_forces_one_shot_on_a_big_pr():
    plan = review_plan.build_plan(files=_files(63, added=60), config=_cfg(), mode="inline")
    assert plan["mode"] == "one_shot"


def test_mode_sharded_forces_fan_out_on_a_small_pr():
    plan = review_plan.build_plan(files=_files(3, added=10), config=_cfg(), mode="sharded")
    assert plan["mode"] == "sharded"


def test_agent_review_disabled_emits_no_review_tasks():
    plan = review_plan.build_plan(files=_files(40, added=50), config=_cfg())     # default: false
    assert all(task["kind"] == "deterministic" for task in plan["tasks"])


def test_review_tasks_group_by_directory_before_packing():
    files = [_file("app/gates/a.py", 50), _file("web/ui/b.tsx", 50),
             _file("app/gates/c.py", 50), _file("web/ui/d.tsx", 50)]
    tasks = _agent_tasks(review_plan.build_plan(files=files, config=_cfg(agent_review=True), mode="sharded"))

    for task in tasks:
        directories = {path.rsplit("/", 1)[0] for path in task["files"]}
        assert len(directories) == 1          # nenhuma task mistura módulos


def test_review_tasks_respect_their_own_smaller_caps():
    plan = review_plan.build_plan(files=_files(30, added=100, prefix="app/gates"),
                                  config=_cfg(agent_review=True), mode="sharded")
    tasks = _agent_tasks(plan)

    assert all(len(t["files"]) <= 6 for t in tasks)
    assert all(sum(_added(t)) <= 400 or len(t["files"]) == 1 for t in tasks)
    assert sum(len(t["files"]) for t in tasks) == 30       # cobertura total, sem overlap


def test_review_task_declares_both_calls():
    task = _agent_tasks(review_plan.build_plan(files=_files(8, added=100),
                                               config=_cfg(agent_review=True), plan_id="p1",
                                               mode="sharded"))[0]

    assert task["call"]["tool"] == "get_task_context"
    assert task["submit"]["tool"] == "submit_task_findings"
    assert task["checks"] == ["agent_review"]
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar em `app/gates/review_plan.py`**

  - `build_plan(..., mode: str = "auto")`: `inline` devolve `one_shot_plan("modo inline forçado
    pelo cliente")`; `sharded` pula o teste de limites; `auto` mantém a regra atual
    (`app/gates/review_plan.py:129`). Valor desconhecido → `ValueError` com os três aceitos.
  - `AGENT_SCOPED = ("agent_review",)` e `_DEFAULT_ENABLED["agent_review"] = False`, para
    `enabled_checks` (`app/gates/review_plan.py:44`) já filtrar pela config.
  - `_pack_review_tasks(files, max_files, max_lines)`: agrupa por `Path(path).parent` (ordem
    alfabética de diretório, arquivos por peso decrescente dentro do grupo), empacota cada grupo
    com a mesma regra de estouro de `_pack_shards`, **sem** misturar grupos. Arquivo único acima do
    teto ocupa uma task sozinho.
  - `_review_task(...)` emite `kind: "agent"`, `checks: ["agent_review"]`, `call` para
    `get_task_context` e `submit` para `submit_task_findings`.
  - Marcar as tasks existentes com `kind: "deterministic"` (aditivo; cliente antigo ignora).
  - No modo `one_shot` **nenhuma** task `review-N` é emitida: quem revisa inline é o agente
    principal, com `standards`/`finding_schema` do plano (Task 3).

- [x] **Step 4: Expor `mode` em `plan_pr_review`** (`mcp_server_standalone.py:389`) como
  `Annotated[str, "auto | inline | sharded"] = "auto"`, repassado a `build_plan`. Rodar
  `pytest tests/test_agent_review_plan.py tests/test_review_plan.py -v`.

---

### Task 5: Modo `WORKTREE` (review rápida do checkout vivo)

**Files:**
- Modify: `mcp_server_standalone.py`, `app/gates/git_diff.py`
- Test: `tests/test_review_plan_mcp.py`

Review rápida de trabalho não commitado, sem cópia: `head_ref="WORKTREE"` compara `base_ref` com a
árvore de trabalho atual e **não** cria worktree detached. Como o usuário pode editar durante o
fan-out, o plano se declara volátil e o resultado nunca vira baseline.

- [x] **Step 1: Testes que falham**

```python
def test_worktree_mode_does_not_create_a_detached_worktree(tmp_repo, monkeypatch):
    calls = _record_git_calls(monkeypatch)
    plan = json.loads(asyncio.run(plan_pr_review(workspace=str(tmp_repo), base_ref="HEAD",
                                                 head_ref="WORKTREE")))

    assert plan["volatile"] is True
    assert not any(call[:2] == ["worktree", "add"] for call in calls)


def test_worktree_mode_sees_uncommitted_changes(tmp_repo):
    (tmp_repo / "app" / "novo.py").write_text("print('x')\n", encoding="utf-8")
    plan = json.loads(asyncio.run(plan_pr_review(workspace=str(tmp_repo), base_ref="HEAD",
                                                 head_ref="WORKTREE")))

    assert "app/novo.py" in _all_files(plan)


def test_worktree_mode_never_saves_baseline(tmp_repo):
    # use_ratchet permanece False no runtime do plano; finalize_plan não grava baseline
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar**

  - `git_diff.changed_files_with_weight(repo_root, base, head)`: quando `head == "WORKTREE"`,
    rodar `git diff --numstat -z --no-renames --diff-filter=ACMRT <base>` (sem `head`) e somar os
    untracked via `git ls-files --others --exclude-standard -z`, contando linhas do arquivo como
    `added_lines`. Sem sentinela mágica espalhada: constante `WORKTREE_REF = "WORKTREE"` no módulo.
  - `plan_pr_review`: se `head_ref == WORKTREE_REF`, pular `resolve_commit(head)` e o bloco de
    `worktree add` (`mcp_server_standalone.py:471`); `runtime.worktree = None`,
    `runtime.workspace = repo_root`, `runtime.volatile = True`, `runtime.use_ratchet = False`.
  - Resposta do plano ganha `"volatile": bool` nos dois modos.

- [x] **Step 4: Rodar e ver passar** e conferir que `close_review_plan` num plano volátil não tenta
  remover worktree inexistente (`_cleanup_plan_runtime` já trata `worktree` nulo,
  `mcp_server_standalone.py:184`).

---

### Task 6: `get_task_context`

**Files:**
- Modify: `mcp_server_standalone.py`, `app/gates/git_diff.py`, `app/ai/prompts.py`
- Test: `tests/test_task_context.py` (criar)

- [x] **Step 1: Testes que falham**

```python
def test_context_returns_diff_per_file_with_worktree_path(plan_with_agent_task):
    ctx = json.loads(asyncio.run(get_task_context(plan_id=plan_with_agent_task, task_id="review-1")))

    assert ctx["worktree_path"].endswith("repo")
    assert [f["path"] for f in ctx["files"]] == ["app/gates/a.py", "app/gates/c.py"]
    assert ctx["files"][0]["diff"].startswith("@@") or "@@" in ctx["files"][0]["diff"]
    assert ctx["truncated"] == []


def test_context_carries_standards_and_finding_schema(plan_with_agent_task):
    ctx = json.loads(asyncio.run(get_task_context(plan_id=plan_with_agent_task, task_id="review-1")))

    assert ctx["standards"]
    assert set(ctx["finding_schema"]) == {"file", "line", "severity", "category", "message", "suggestion"}


def test_context_truncates_loudly_when_over_the_char_budget(plan_with_huge_diff):
    ctx = json.loads(asyncio.run(get_task_context(plan_id=plan_with_huge_diff, task_id="review-1")))

    assert ctx["truncated"]                                   # lista de paths cortados
    assert all(f["path"] in ctx["truncated"] or f["diff"] for f in ctx["files"])
    assert len(json.dumps(ctx)) <= _max_context_chars() * 1.1  # margem para o envelope


def test_context_includes_deterministic_results_already_computed(plan_with_finished_shard):
    ctx = json.loads(asyncio.run(get_task_context(plan_id=plan_with_finished_shard, task_id="review-1")))
    checks = {c["check"] for c in ctx["deterministic_checks"]}

    assert "file_size" in checks          # o subagente não repete o que a máquina já mediu


def test_context_rejects_a_deterministic_task():
    # get_task_context("global") -> erro claro apontando run_review_task
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar**

  - `git_diff.file_diffs(repo_root, base, head, paths) -> dict[str, str]`: um
    `git diff --unified=3 <base> [head] -- <paths…>` por lote, separado por arquivo. No modo
    `WORKTREE`, omitir `head`; arquivo untracked entra como diff sintético (`+` em todas as linhas)
    ou, se for grande demais, só o cabeçalho e o path (marcado em `truncated`).
  - `get_task_context(plan_id, task_id)` no MCP: carrega o plano, valida `kind == "agent"`
    (senão erro citando `run_review_task`), monta a resposta do contrato acima.
  - Orçamento: soma `len(diff)` na ordem dos arquivos; ao passar `max_context_chars`, corta o diff
    do arquivo atual e dos seguintes, mantendo `path`/`added_lines`, e acrescenta cada path em
    `truncated`. Nunca corta no meio sem registrar.
  - `deterministic_checks`: resultados já persistidos das tasks determinísticas que cobrem os
    mesmos arquivos (`repositories.load_review_tasks`), filtrados por path. Se ainda não rodaram,
    devolve `[]` — o subagente não bloqueia esperando.
  - `instructions`: texto curto e estável dizendo o escopo (só os arquivos da task), que
    `worktree_path` serve para ler o entorno, e que a resposta volta por `submit_task_findings`.

- [x] **Step 4: Rodar e ver passar** — `pytest tests/test_task_context.py -v`.

---

### Task 7: `submit_task_findings`

**Files:**
- Modify: `mcp_server_standalone.py`, `app/gates/review_plan.py`
- Test: `tests/test_agent_findings.py`

- [x] **Step 1: Testes que falham**

```python
def test_findings_become_an_agent_review_check_result(plan_with_agent_task):
    asyncio.run(submit_task_findings(plan_id=plan_with_agent_task, task_id="review-1", findings=[
        {"file": "app/gates/a.py", "line": 95, "severity": "high",
         "category": "correctness", "message": "…", "suggestion": "…"},
    ]))
    stored = repositories.load_review_tasks(plan_with_agent_task)
    check = [CheckResult(**c) for c in _result_of(stored, "review-1")][0]

    assert check.check == "agent_review"
    assert check.status == GateStatus.failed          # high com block_on=high
    assert check.violations[0].category == "correctness"
    assert check.metrics == {"high_issues": 1, "medium_issues": 0, "low_issues": 0, "findings_count": 1}


def test_block_on_none_caps_severity_at_warning(plan_with_block_on_none):
    # mesmo finding high -> status warning, veredito não reprova


def test_empty_findings_is_a_valid_pass(plan_with_agent_task):
    asyncio.run(submit_task_findings(plan_id=plan_with_agent_task, task_id="review-1", findings=[]))
    check = _agent_check(plan_with_agent_task, "review-1")

    assert check.status == GateStatus.passed          # revisado e limpo ≠ não revisado


def test_invalid_severity_is_rejected_with_a_clear_error(plan_with_agent_task):
    out = json.loads(asyncio.run(submit_task_findings(plan_id=plan_with_agent_task, task_id="review-1",
                                                      findings=[{"file": "a.py", "severity": "critical",
                                                                 "message": "…"}])))
    assert "severity" in out["error"]


def test_resubmitting_overwrites_instead_of_duplicating(plan_with_agent_task):
    # mesma garantia que run_review_task já dá (save_review_task_result é upsert)


def test_submit_rejects_a_deterministic_task():
    # submit_task_findings("files-1") -> erro claro
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar**

  - Em `app/gates/review_plan.py`, função pura
    `findings_to_check_result(findings: list[dict], block_on: str) -> CheckResult`:
    valida `severity ∈ {high, medium, low}` e campos obrigatórios (`file`, `message`); mapeia
    `high → failed`, `medium → warning`, `low → passed` (informativo), pega o pior; com
    `block_on == "none"`, teto em `warning`. Métricas:
    `high_issues`, `medium_issues`, `low_issues`, `findings_count`.
  - Tool `submit_task_findings(plan_id, task_id, findings)` no MCP: valida `kind == "agent"`,
    converte, grava com `repositories.save_review_task_result` (mesmo caminho de
    `run_review_task`, `mcp_server_standalone.py:539`), devolve o resumo do contrato.
  - `line` ausente vira `None`; `category`/`suggestion` ausentes viram `""`.

- [x] **Step 4: Rodar e ver passar** — `pytest tests/test_agent_findings.py -v`.

---

### Task 8: Agregação — baseline, `block_on` e `force`

**Files:**
- Modify: `app/gates/review_plan.py`, `mcp_server_standalone.py`
- Test: `tests/test_agent_findings.py`, `tests/test_review_plan_aggregation.py`

- [x] **Step 1: Testes que falham**

```python
def test_agent_metrics_never_reach_the_baseline():
    checks = [_check("file_size", metrics={"max_file_lines": 120}),
              _check("agent_review", metrics={"high_issues": 2, "findings_count": 5})]
    metrics = review_plan.baseline_metrics(checks)

    assert metrics == {"file_size.max_file_lines": 120}


def test_agent_findings_are_merged_and_deduped_across_tasks():
    parts = [[_agent_check([("a.py", 10, "X")])], [_agent_check([("a.py", 10, "X"), ("b.py", 3, "Y")])]]
    merged = review_plan.aggregate_checks(parts)

    assert len(merged) == 1 and merged[0].check == "agent_review"
    assert len(merged[0].violations) == 2                  # duplicata some
    assert merged[0].metrics["findings_count"] == 3        # contador soma o bruto


def test_force_consolidates_with_missing_agent_tasks(plan_with_pending_review):
    table = asyncio.run(get_review_plan(plan_id=plan_with_pending_review, force=True))

    assert "agent_review" in table and "skipped" in table
    assert "1 task sem retorno" in table


def test_force_never_yields_a_clean_pass(plan_with_pending_review):
    run = repositories.get_gate_run(plan_with_pending_review)
    assert run["status"] in ("warning", "failed")


def test_without_force_pending_agent_tasks_still_block(plan_with_pending_review):
    out = json.loads(asyncio.run(get_review_plan(plan_id=plan_with_pending_review)))
    assert out["pending_tasks"] == ["review-1"]
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar**

  - `review_plan.baseline_metrics(checks)` = `ratchet.extract_metrics(checks)` menos as chaves com
    prefixo `agent_review.`; usar essa função no ponto que grava baseline
    (`app/gates/review_plan.py:334`) e em `orchestrator.run_local_quality_gate` se `agent_review`
    puder chegar lá. Não alterar `ratchet.extract_metrics` (o `GateRun` continua registrando as
    métricas; o que muda é o que vira baseline).
  - `_SUM_METRIC_SUFFIXES` já cobre `_issues` e `count`
    (`app/gates/review_plan.py:262`): `high_issues`/`findings_count` somam entre tasks de graça.
    Confirmar com teste, sem código novo.
  - `finalize_plan(..., forced_tasks: list[str] = [])`: para cada task de agente sem resultado,
    injeta `CheckResult(check="agent_review", status=skipped, metrics={"skipped_tasks": N})`; se
    houver qualquer task pulada, o veredito consolidado não pode ser `passed` — rebaixa para
    `warning` (o `result.consolidate` cuida do pior status; a linha `skipped` sozinha não
    rebaixaria).
  - `get_review_plan(plan_id, force: bool = False)`: com `force=True` e tasks pendentes, consolida
    passando `forced_tasks`; sem `force`, mantém o retorno `{"status": "pending", …}` atual.

- [x] **Step 4: Rodar e ver passar** — `pytest tests/test_review_plan_aggregation.py tests/test_agent_findings.py -v`.

---

### Task 9: Saída — seção de findings na tabela consolidada

**Files:**
- Modify: `app/gates/mcp_table.py`
- Test: `tests/test_mcp_table.py`

`build_quality_gate_table` só imprime métricas numéricas (`app/gates/mcp_table.py:38`), então hoje
um check com 12 findings sai como uma linha vazia. A seção nova é o produto final da camada toda.

- [x] **Step 1: Testes que falham**

```python
def test_table_lists_agent_findings_below_the_table():
    table = build_quality_gate_table(_run_with_agent_findings(3))

    assert "| agent_review |" in table
    assert "### Findings" in table
    assert "app/gates/a.py:95 — high — correctness" in table
    assert "Sugestão:" in table


def test_findings_are_capped_at_twenty_with_a_remainder_line():
    table = build_quality_gate_table(_run_with_agent_findings(35))

    assert table.count("— high —") + table.count("— medium —") + table.count("— low —") == 20
    assert "mais 15 findings" in table
    assert "get_gate_run" in table              # o resto é recuperável


def test_findings_are_ordered_by_severity():
    table = build_quality_gate_table(_run_with_mixed_severities())
    assert table.index("— high —") < table.index("— medium —") < table.index("— low —")


def test_run_without_agent_findings_is_byte_identical_to_today():
    # regressão: a saída de run_local_gate/run_commit_gate não pode mudar
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar** — em `build_quality_gate_table`, depois das linhas da tabela, se
  existir check `agent_review` com violations: cabeçalho `### Findings (agent_review)` e, por
  finding, `- \`{file}:{line}\` — {severity} — {category} — {message}` mais, se houver,
  `  Sugestão: {suggestion}`. Ordenar por `high > medium > low` (estável dentro da severidade),
  cortar em 20 e fechar com
  `_(mais N findings; use `get_gate_run("<run_id>")` para a lista completa)_`. Sem check
  `agent_review`, a função devolve exatamente o que devolve hoje.

- [x] **Step 4: Rodar e ver passar** — `pytest tests/test_mcp_table.py -v` e a suíte inteira.

---

### Task 10: SQLite concorrente (WAL) e documentação

**Files:**
- Modify: `app/storage/database.py`, `README.md`
- Test: `tests/test_agent_findings.py`

Vários subagentes escrevendo resultado parcial ao mesmo tempo batem em `database is locked` no
SQLite padrão. WAL + `busy_timeout` resolve sem trocar de storage: as escritas são raras e curtas
(uma por task).

- [x] **Step 1: Teste que falha**

```python
def test_parallel_task_writes_do_not_lock_the_database(tmp_plan):
    async def _write(index: int):
        return await submit_task_findings(plan_id=tmp_plan, task_id=f"review-{index}",
                                          findings=[{"file": f"a{index}.py", "severity": "low",
                                                     "message": "…"}])

    results = asyncio.run(asyncio.gather(*[_write(i) for i in range(1, 9)]))
    assert all("error" not in json.loads(r) for r in results)


def test_engine_enables_wal():
    with database.get_session() as session:
        assert session.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
```

- [x] **Step 2: Rodar e ver falhar** (ou passar por sorte: em máquina rápida o lock não aparece
  sempre — o teste de PRAGMA é o determinístico dos dois).

- [x] **Step 3: Implementar** — em `app/storage/database.py:6`, listener
  `event.listens_for(engine, "connect")` executando `PRAGMA journal_mode=WAL`,
  `PRAGMA busy_timeout=5000` e `PRAGMA synchronous=NORMAL`. Aplicar só quando a URL é SQLite.

- [x] **Step 4: README** — nova seção "Review semântica com subagentes", depois de "Revisar um PR
  grande em partes":

  - o ciclo `plan_pr_review` → (`run_review_task` ‖ `get_task_context` → subagente →
    `submit_task_findings`) → `get_review_plan` → `close_review_plan`;
  - `mode: auto | inline | sharded` e `head_ref: "WORKTREE"` para review rápida;
  - config `agent_review.enabled` / `block_on` e os tetos de `pr_review`;
  - o que **não** muda: findings não entram no baseline; Ollama (`big_o`/`ai_review`) segue
    disponível e desligado por padrão para CI sem agente;
  - `get_review_plan(force=true)` e o que ele marca como `skipped`.

---

## Verificação final

- [x] `pytest -q` — suíte inteira verde.
- [x] `run_local_gate` e `run_commit_gate` com saída idêntica à de antes num repo sem
      `agent_review` habilitado (regressão de contrato).
- [x] Fluxo ponta a ponta manual: `plan_pr_review` num PR grande com `agent_review: true`,
      fan-out real de subagentes sobre as tasks `review-N`, `get_review_plan` consolidando
      determinístico + findings, `close_review_plan` sem deixar worktree órfão
      (`git worktree list` limpo).
- [x] Fluxo `head_ref: "WORKTREE"` com arquivo não commitado aparecendo no plano e nenhum
      `worktree add` executado.
