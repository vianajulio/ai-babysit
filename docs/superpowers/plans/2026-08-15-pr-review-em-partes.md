# Plano: revisão de PR em partes (multi-agente) com fallback one-shot

> **Para agentes:** implemente task a task, na ordem. Cada step tem checkbox (`- [ ]`).
> Fluxo por task: escrever teste que falha → rodar e ver falhar → implementar → rodar e ver passar.

**Goal:** permitir que um cliente MCP com múltiplos agentes revise um PR grande em partes
paralelas, mantendo um único resultado consolidado — e, quando o PR é pequeno, rodar uma
única revisão one-shot (comportamento atual, sem overhead de plano).

**Architecture:** o servidor MCP passa a expor um *plano de revisão*. `plan_pr_review` calcula
os arquivos alterados, decide entre `one_shot` e `sharded` e devolve uma lista de tasks já
prontas para fan-out (cada task com os argumentos exatos da chamada MCP seguinte). Cada agente
chama `run_review_task` para a sua task; os resultados parciais são persistidos por `plan_id`.
`get_review_plan` agrega tudo em um único `GateRun`, aplica ratchet **uma vez** e devolve a
tabela markdown já existente.

**Restrição que define o particionamento** (verificada no código atual):

| Check | Escopo real hoje | Pode ser shardado? |
|---|---|---|
| `duplication` | roda `jscpd` na **workspace inteira**, filtra violations por arquivo alterado (`app/runners/duplication.py:24`) | ❌ — shardar repete o scan completo N vezes e `duplication_percent` é global |
| `secrets` | roda `gitleaks detect --source <workspace>` (`app/runners/secrets.py:14`) | ❌ — mesmo motivo |
| `complexity` | roda `lizard <workspace>` (`app/runners/complexity.py:24`) | ❌ — mesmo motivo |
| `file_size` | lê apenas os arquivos de `changed_files` | ✅ |
| `big_o` | 1 chamada Ollama por arquivo candidato, com teto `max_files_per_run: 20` (`app/runners/big_o.py:55`) | ✅ — é o gargalo real |

Portanto o modo `sharded` gera **1 task `global`** (checks de workspace, executada uma vez) e
**N tasks `files-i`** (checks por arquivo, paralelizáveis). Isso é o oposto de "dividir tudo em N":
dividir os checks de workspace multiplicaria custo sem ganhar cobertura.

**Tech Stack:** Python 3.11, pytest, Pydantic, SQLAlchemy (SQLite), YAML de config, MCP stdio.

---

## File Structure

- Criar `app/gates/review_plan.py`: função pura de particionamento + regras de agregação.
- Criar `app/gates/git_diff.py`: extração de arquivos alterados e peso (linhas) via git, reutilizada pelo `run_commit_gate`.
- Criar `tests/test_review_plan.py`: particionamento e decisão one-shot/sharded.
- Criar `tests/test_review_plan_aggregation.py`: merge de checks parciais.
- Criar `tests/test_review_plan_mcp.py`: contrato das novas tools MCP.
- Modificar `app/gates/orchestrator.py`: `_build_runners`/`_run_checks` aceitam filtro de checks; nova `run_review_task_gate`.
- Modificar `app/storage/database.py`: tabelas `review_plans` e `review_tasks`.
- Modificar `app/storage/repositories.py`: save/load de plano e de resultado de task.
- Modificar `app/runners/duplication.py` e `app/runners/secrets.py`: caminho de relatório único por execução.
- Modificar `mcp_server_standalone.py`: tools `plan_pr_review`, `run_review_task`, `get_review_plan`, `close_review_plan`; `_commit_files` passa a delegar para `app/gates/git_diff.py`.
- Modificar `quality_gate.yaml`: bloco `quality_gate.pr_review`.
- Modificar `settings.py` (se necessário) e `tests/test_quality_gate_config.py`: defaults e merge do novo bloco.
- Modificar `README.md`: seção "Revisar um PR grande em partes".

---

## Contrato das tools (alvo)

```jsonc
// 1) plan_pr_review
{ "workspace": "C:\\Projetos\\meu-projeto", "base_ref": "origin/main", "head_ref": "HEAD" }
// -> resposta
{
  "plan_id": "…uuid…",
  "mode": "sharded",              // ou "one_shot"
  "files_total": 63,
  "reason": "63 arquivos / 4120 linhas acima do limite one-shot (10 / 800)",
  "parallel_hint": 4,
  "tasks": [
    { "task_id": "global",  "checks": ["duplication", "secrets", "complexity"],
      "files": ["…todos os alterados…"], "call": { "tool": "run_review_task", "args": {"plan_id": "…", "task_id": "global"} } },
    { "task_id": "files-1", "checks": ["file_size", "big_o"], "files": ["…15 arquivos…"],
      "call": { "tool": "run_review_task", "args": {"plan_id": "…", "task_id": "files-1"} } }
  ]
}

// modo one_shot: uma única task apontando para o comportamento atual
{ "plan_id": "…", "mode": "one_shot", "files_total": 4,
  "tasks": [{ "task_id": "single", "checks": ["*"], "files": ["…"],
              "call": { "tool": "run_local_gate", "args": { "workspace": "…", "files": ["…"] } } }] }
```

O cliente multi-agente faz fan-out sobre `tasks` (respeitando `parallel_hint`), e no fim chama
`get_review_plan` para a tabela consolidada. Em `one_shot` não há fan-out nem agregação.

---

### Task 1: Config `pr_review`

**Files:**
- Modify: `quality_gate.yaml`
- Test: `tests/test_quality_gate_config.py`

- [ ] **Step 1: Teste que falha**

```python
def test_quality_gate_config_exposes_pr_review_defaults():
    cfg = Settings().load_quality_gate_config()
    pr = cfg["quality_gate"]["pr_review"]

    assert pr["one_shot_max_files"] == 10
    assert pr["one_shot_max_diff_lines"] == 800
    assert pr["max_files_per_shard"] == 15
    assert pr["parallel_hint"] == 4
```

- [ ] **Step 2: Rodar e ver falhar** — `pytest tests/test_quality_gate_config.py -v` (KeyError `pr_review`).

- [ ] **Step 3: Adicionar em `quality_gate.yaml`, dentro de `quality_gate:`**

```yaml
  pr_review:
    one_shot_max_files: 10
    one_shot_max_diff_lines: 800
    max_files_per_shard: 15
    max_diff_lines_per_shard: 1200
    parallel_hint: 4
    plan_ttl_minutes: 60
```

- [ ] **Step 4: Teste de override por projeto** — `.babysit.yml` com `pr_review.one_shot_max_files: 30` deve sobrescrever só essa chave (o `_deep_merge` de `settings.py` já cobre; o teste garante que continue coberto).

---

### Task 2: Extração de arquivos alterados e peso (`git_diff`)

**Files:**
- Create: `app/gates/git_diff.py`
- Modify: `mcp_server_standalone.py` (`_commit_files` delega)
- Test: `tests/test_git_diff.py`

Hoje `_commit_files` (`mcp_server_standalone.py:191`) só resolve commit↔pai. O plano precisa de
`base_ref..head_ref` e do **peso por arquivo** (linhas adicionadas) para balancear os shards.

- [ ] **Step 1: Testes que falham** (repositório git real criado em `tmp_path`)

```python
def test_changed_files_with_weight_between_refs(tmp_path):
    repo = _init_repo(tmp_path)          # helper local: git init + commits
    files = git_diff.changed_files_with_weight(repo, base="HEAD~1", head="HEAD")

    assert files == [git_diff.ChangedFile(path="src/foo.py", added_lines=12)]

def test_changed_files_rejects_paths_outside_repo(tmp_path):
    # mesma validação de `_commit_files`: nada absoluto, nada com ".."
```

- [ ] **Step 2: Rodar e ver falhar.**

- [ ] **Step 3: Implementar `app/gates/git_diff.py`**

  - `run_git(args, cwd)` — mover a função `_git` de `mcp_server_standalone.py` para cá (mesmos timeouts, `stdin=DEVNULL`, mensagens de erro em pt-BR).
  - `resolve_commit(repo_root, ref)` — `rev-parse --verify <ref>^{commit}`, com o fallback de árvore vazia já existente para commit raiz.
  - `changed_files_with_weight(repo_root, base, head) -> list[ChangedFile]` — `git diff --numstat -z --no-renames --diff-filter=ACMRT base head`; binários (`-`) recebem `added_lines=0`; mantém a rejeição de caminho absoluto / `..`.
  - `ChangedFile` = `pydantic.BaseModel` com `path: str` e `added_lines: int`.

- [ ] **Step 4: `_commit_files` do MCP passa a chamar `git_diff`** — sem duplicar lógica (o próprio gate reprova duplicação). Rodar `pytest tests/test_standalone_validation.py -v` para garantir que `run_commit_gate` não regrediu.

---

### Task 3: Particionador (função pura)

**Files:**
- Create: `app/gates/review_plan.py`
- Test: `tests/test_review_plan.py`

- [ ] **Step 1: Testes que falham**

```python
def test_small_pr_becomes_one_shot():
    plan = review_plan.build_plan(files=_files(4, added=30), config=_cfg())

    assert plan["mode"] == "one_shot"
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["call"]["tool"] == "run_local_gate"

def test_big_pr_splits_into_global_task_plus_file_shards():
    plan = review_plan.build_plan(files=_files(63, added=60), config=_cfg())

    assert plan["mode"] == "sharded"
    assert plan["tasks"][0]["task_id"] == "global"
    assert plan["tasks"][0]["checks"] == ["duplication", "secrets", "complexity"]
    assert len(plan["tasks"][0]["files"]) == 63          # escopo global recebe tudo
    shards = plan["tasks"][1:]
    assert all(len(t["files"]) <= 15 for t in shards)
    assert sum(len(t["files"]) for t in shards) == 63    # cobertura total, sem overlap

def test_shards_balance_by_added_lines_not_only_by_count():
    files = [_file("huge.cs", 4000), *_files(10, added=5)]
    shards = _shards(review_plan.build_plan(files=files, config=_cfg()))

    assert ["huge.cs"] == shards[0]["files"]             # arquivo pesado isolado

def test_one_shot_when_few_files_but_forced_sharded_by_diff_lines():
    plan = review_plan.build_plan(files=_files(6, added=400), config=_cfg())
    assert plan["mode"] == "sharded"                     # 2400 linhas > one_shot_max_diff_lines

def test_disabled_checks_are_not_scheduled():
    cfg = _cfg(checks={"duplication": {"enabled": False}, "big_o": {"enabled": False}})
    plan = review_plan.build_plan(files=_files(40, added=10), config=cfg)

    assert "duplication" not in plan["tasks"][0]["checks"]
    assert all("big_o" not in t["checks"] for t in plan["tasks"])
```

- [ ] **Step 2: Rodar e ver falhar.**

- [ ] **Step 3: Implementar**

```python
WORKSPACE_SCOPED = ("duplication", "secrets", "complexity")
FILE_SCOPED = ("file_size", "big_o")


def build_plan(files: list[ChangedFile], config: dict, *, plan_id: str = "") -> dict:
    """Decide one_shot vs sharded e devolve as tasks já prontas para fan-out.

    Função pura: não toca em git, disco ou banco — só decide.
    """
```

  Regras:
  - `enabled_checks(config)` respeita `quality_gate.checks.<nome>.enabled` (mesmos defaults de `_build_runners`).
  - one-shot quando `len(files) <= one_shot_max_files` **e** `sum(added_lines) <= one_shot_max_diff_lines`.
  - sharding: ordena por `added_lines` desc e usa *greedy bin packing* — abre novo shard quando o próximo arquivo estourar `max_files_per_shard` ou `max_diff_lines_per_shard`; um único arquivo acima do limite ocupa um shard sozinho (nunca é dividido nem descartado).
  - task `global` só existe se houver check de workspace habilitado; shards só existem se houver check por arquivo habilitado. Se sobrar zero task, devolve `mode: "one_shot"`.
  - a ordem das tasks é determinística (mesma entrada ⇒ mesmo plano), para que retry/resume seja idempotente.

---

### Task 4: Orchestrator executa um subconjunto de checks

**Files:**
- Modify: `app/gates/orchestrator.py`
- Test: `tests/test_local_orchestrator.py`

- [ ] **Step 1: Testes que falham**

```python
def test_build_runners_filters_by_requested_checks():
    runners = orchestrator._build_runners(_full_config(), only=["file_size"])
    assert [r.name for r in runners] == ["file_size"]

def test_run_review_task_gate_does_not_touch_baseline(monkeypatch, tmp_path):
    # save_baseline monkeypatchado para explodir; resultado parcial nunca pode gravar baseline
```

- [ ] **Step 2: Rodar e ver falhar.**

- [ ] **Step 3: Implementar**

  - `_build_runners(config, only: list[str] | None = None)` — mantém a assinatura atual compatível (`only=None` ⇒ comportamento de hoje) e filtra por `runner.name`.
  - `_run_checks(workspace, changed_files, only=None)` — repassa o filtro.
  - `run_review_task_gate(workspace, changed_files, checks) -> list[CheckResult]` — executa só os runners pedidos e **retorna os `CheckResult` crus**: sem `ratchet.apply`, sem `save_baseline`, sem `consolidate`. Ratchet e baseline são responsabilidade exclusiva da agregação (Task 6); aplicar ratchet em resultado parcial gravaria um baseline com métricas de uma fatia do PR.
  - AI annotation (`ai_review`) também fica só na agregação — anotar por shard geraria N sugestões desconexas.

---

### Task 5: Persistência do plano e das tasks

**Files:**
- Modify: `app/storage/database.py`, `app/storage/repositories.py`
- Test: `tests/test_review_plan_storage.py`

- [ ] **Step 1: Testes que falham**

```python
def test_task_result_is_saved_and_plan_reports_pending_tasks(): ...
def test_saving_the_same_task_twice_overwrites_instead_of_duplicating(): ...  # retry de agente
def test_plan_is_complete_only_when_every_task_has_a_result(): ...
```

- [ ] **Step 2: Rodar e ver falhar.**

- [ ] **Step 3: Implementar**

```python
class ReviewPlanRecord(Base):
    __tablename__ = "review_plans"
    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String(64), unique=True, nullable=False, index=True)
    workspace = Column(String(1024), nullable=False)
    repository = Column(String(256), nullable=False)
    branch = Column(String(256), nullable=False)
    status = Column(String(16), nullable=False)      # pending | complete
    created_at = Column(DateTime, nullable=False)
    payload_json = Column(Text, nullable=False)      # plano completo devolvido ao cliente


class ReviewTaskRecord(Base):
    __tablename__ = "review_tasks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String(64), nullable=False, index=True)
    task_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)      # pending | done | error
    result_json = Column(Text, nullable=True)        # list[CheckResult]
    updated_at = Column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("plan_id", "task_id", name="uq_review_task"),)
```

  Repositories: `save_review_plan`, `load_review_plan`, `save_review_task_result` (upsert por `(plan_id, task_id)`), `load_review_tasks`, `mark_plan_complete`.
  `init_db()` já usa `create_all`, então as tabelas novas nascem junto — nenhuma migração manual.

---

### Task 6: Agregação dos resultados parciais

**Files:**
- Modify: `app/gates/review_plan.py`
- Test: `tests/test_review_plan_aggregation.py`

O ponto delicado: métricas **não** se somam de forma uniforme.

- [ ] **Step 1: Testes que falham**

```python
def test_aggregate_unions_violations_of_the_same_check():
    checks = aggregate([_fs(["a.py"]), _fs(["b.py"])])
    assert [v.file for v in checks[0].violations] == ["a.py", "b.py"]

def test_aggregate_sums_counters_but_keeps_max_for_max_metrics():
    checks = aggregate([_fs(max_file_lines=420, violations_count=1),
                        _fs(max_file_lines=900, violations_count=2)])
    assert checks[0].metrics["max_file_lines"] == 900
    assert checks[0].metrics["violations_count"] == 3

def test_aggregate_keeps_global_metric_untouched():
    # duplication_percent vem só da task global; nunca soma
    assert aggregate([_dup(7.5)])[0].metrics["duplication_percent"] == 7.5

def test_worst_status_wins():
    assert aggregate([_fs(status="passed"), _fs(status="failed")])[0].status is GateStatus.failed

def test_error_in_one_shard_is_surfaced_not_swallowed():
    checks = aggregate([_fs(status="passed"), _fs(status="error", error="lizard timeout")])
    assert checks[0].status is GateStatus.error
    assert "lizard timeout" in str(checks[0].metrics)
```

- [ ] **Step 2: Rodar e ver falhar.**

- [ ] **Step 3: Implementar `aggregate_checks(parts: list[list[CheckResult]]) -> list[CheckResult]`**

  - agrupa por `check`; violations = concatenação estável (ordem de task, depois ordem original), deduplicadas por `(file, line, message)`;
  - métricas: prefixo `max_*` ⇒ `max`; sufixo `_count`/`count`/`_found`/`_issues` ⇒ soma; `duplication_percent` e demais percentuais/médias ⇒ valor da task que produziu (task global é a única fonte, então "primeiro valor não nulo");
  - status: pior status vence, na ordem `error > failed > warning > passed > skipped`;
  - `skipped` de um shard não rebaixa um `passed` de outro.

- [ ] **Step 4: Implementar `finalize_plan(plan, parts, ...)`** — chama `aggregate_checks`, depois `ratchet.apply` **uma vez**, depois `_apply_ai_annotation`, `result.consolidate(pr_id=0, …)`, `save_baseline` (só se `use_ratchet` e status `passed`) e `repositories.save_gate_run`. Devolve `build_quality_gate_table(gate_run, before_metrics)` — mesmo formato de saída de hoje, para o cliente não precisar aprender um segundo formato.

---

### Task 7: Tools MCP

**Files:**
- Modify: `mcp_server_standalone.py`
- Test: `tests/test_review_plan_mcp.py`

- [x] **Step 1: Testes que falham**

```python
def test_plan_pr_review_rejects_workspace_that_is_not_a_directory(): ...
def test_plan_pr_review_rejects_workspace_that_is_not_a_git_repo(): ...
def test_plan_pr_review_returns_one_shot_for_small_diff(): ...
def test_run_review_task_rejects_unknown_plan_or_task(): ...
def test_run_review_task_is_idempotent_on_retry(): ...
def test_get_review_plan_reports_pending_tasks_before_aggregating(): ...
def test_get_review_plan_returns_the_markdown_table_when_complete(): ...
```

- [x] **Step 2: Rodar e ver falhar.**

- [x] **Step 3: Implementar as tools** (mesmo estilo das existentes: `Annotated[...]` em todo parâmetro, docstring que ensina o cliente, erro sempre como `_exc_err`, nunca exceção crua)

```python
@mcp.tool()
async def plan_pr_review(
    workspace: Annotated[str, "Raiz do repositório Git a revisar"],
    base_ref: Annotated[str, "Referência base do PR (ex.: origin/main)"],
    head_ref: Annotated[str, "Referência de topo; padrão HEAD"] = "HEAD",
    repository: Annotated[str, "Identificador opcional para baseline"] = "",
    branch: Annotated[str, "Branch opcional para baseline"] = "",
) -> str:
    """Planeja a revisão de um PR. Se o diff for pequeno, devolve mode=one_shot com uma única
    chamada a run_local_gate. Se for grande, devolve mode=sharded com tasks paralelizáveis:
    execute cada task com run_review_task (em agentes distintos, respeitando parallel_hint) e
    depois chame get_review_plan para o resultado consolidado."""
```

```python
@mcp.tool()
async def run_review_task(plan_id, task_id) -> str:      # executa só os checks da task
@mcp.tool()
async def get_review_plan(plan_id) -> str:               # status ou tabela consolidada
@mcp.tool()
async def close_review_plan(plan_id) -> str:             # libera worktree/registro
```

  Detalhes de implementação:
  - `plan_pr_review` valida workspace (mesmas checagens de `run_local_gate`), resolve `repo_root`, chama `git_diff.changed_files_with_weight`, `_ensure_db()`, `review_plan.build_plan`, persiste e devolve JSON. Diff vazio ⇒ erro claro ("nenhum arquivo alterado entre as referências").
  - `run_review_task` carrega o plano, valida `task_id`, roda `run_review_task_gate` sobre `task["files"]` e salva o resultado. Retorno enxuto (`{"plan_id", "task_id", "status", "violations": n, "remaining_tasks": k}`) — a tabela completa só sai na agregação, para não estourar contexto de N agentes.
  - `get_review_plan` devolve `{"status": "pending", "pending_tasks": [...]}` enquanto faltar task, e a tabela markdown quando tudo estiver `done`. Chamadas repetidas depois de completo devolvem o mesmo `run_id` (não reprocessa).
  - **Revisão sobre commit/branch que não é o checkout atual:** o plano cria **um** worktree detached (como `run_commit_gate` faz hoje) e todas as tasks reusam esse caminho — criar um worktree por task multiplicaria o custo. `close_review_plan` remove worktree + diretório temporário; `plan_pr_review` também limpa planos expirados por `plan_ttl_minutes` no início, para não deixar worktree órfão se o cliente sumir.

---

### Task 8: Isolar relatórios concorrentes

**Files:**
- Modify: `app/runners/duplication.py`, `app/runners/secrets.py`
- Test: `tests/test_external_runners.py`

Hoje ambos escrevem em caminho fixo: `workspace.parent / "jscpd-report.json"` e
`workspace.parent / "gitleaks-report.json"`. Com dois planos rodando na mesma workspace, um
sobrescreve o relatório do outro e o resultado sai trocado.

- [x] **Step 1: Teste que falha** — duas execuções concorrentes do runner na mesma workspace produzem relatórios em caminhos distintos e cada uma lê o seu.
- [x] **Step 2: Rodar e ver falhar.**
- [x] **Step 3:** aceitar `report_dir: Path | None = None` no construtor (default = comportamento atual) e, quando ausente, gerar um subdiretório temporário único por execução, removido no `finally`. O orchestrator passa um diretório derivado do `run_id`/`plan_id`.

---

### Task 9: Documentação

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** nova seção **"Revisar um PR grande em partes"** depois de "Analisar um commit", cobrindo:
  - quando o modo one-shot é escolhido (limites e como sobrescrevê-los no `.babysit.yml`);
  - o ciclo `plan_pr_review` → fan-out de `run_review_task` → `get_review_plan` → `close_review_plan`, com um exemplo de JSON real;
  - o aviso de que `duplication`/`secrets`/`complexity` rodam **uma vez** na task `global` — não é bug, é o escopo real dessas ferramentas;
  - que ratchet e baseline só são aplicados na agregação.
- [ ] **Step 2:** adicionar `pr_review` ao exemplo de `.babysit.yml` do README.

---

## Verificação final

- [ ] `pytest -q` — suíte inteira verde.
- [ ] Fumaça one-shot: `plan_pr_review` em um diff de 3 arquivos ⇒ `mode: one_shot`, e `run_local_gate` com os mesmos arquivos produz a mesma tabela de antes (sem regressão).
- [ ] Fumaça sharded: diff de 60+ arquivos ⇒ tasks cobrindo 100% dos arquivos sem overlap; rodar as tasks fora de ordem e ainda assim obter uma tabela consolidada correta.
- [ ] Rodar o próprio gate do Babysit sobre os arquivos novos (`run_local_gate`) — o plano introduz `review_plan.py` e `git_diff.py`, que precisam respeitar `max_lines_per_file: 400` e complexidade ≤ 10.
