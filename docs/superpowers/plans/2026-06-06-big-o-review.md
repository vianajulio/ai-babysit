# Big O Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a configurable hybrid Big O review check for changed C# files in the quality gate.

**Architecture:** Add a focused `BigORunner` that filters changed C# files, uses simple candidate heuristics, asks Ollama for Big O classification, and maps high/medium findings to existing `CheckResult` violations. Register the runner through the existing orchestrator config path and extend ratchet support for `big_o` numeric metrics.

**Tech Stack:** Python 3.11, pytest, Pydantic models, existing Ollama client, YAML quality gate config.

---

## File Structure

- Create `app/runners/big_o.py`: Big O candidate filtering, Ollama call, JSON parsing, status/metrics mapping.
- Modify `app/ai/prompts.py`: add `build_big_o_prompt()`.
- Modify `app/gates/orchestrator.py`: register `BigORunner` from `quality_gate.checks.big_o`.
- Modify `app/gates/ratchet.py`: include `big_o` prefix in ratcheted metrics.
- Modify `quality_gate.yaml`: add default `big_o` config.
- Modify `README.md`: document `big_o` in the check list and config sample.
- Modify `tests/test_external_runners.py`: add runner behavior tests.
- Modify `tests/test_quality_gate_config.py`: assert config merge preserves Big O defaults and overrides.
- Add `tests/test_big_o_orchestrator.py`: assert runner registration from config.
- Add `tests/test_ratchet.py`: assert ratchet compares `big_o` metrics.

### Task 1: Prompt Builder

**Files:**
- Modify: `app/ai/prompts.py`
- Test: `tests/test_big_o_prompt.py`

- [ ] **Step 1: Write failing prompt test**

```python
from app.ai.prompts import build_big_o_prompt


def test_build_big_o_prompt_requires_json_and_big_o_focus():
    prompt = build_big_o_prompt(
        language="csharp",
        file_path="src/Foo.cs",
        code="foreach (var item in items) { users.Any(u => u.Id == item.Id); }",
    )

    assert "Big O" in prompt
    assert "APENAS com JSON" in prompt
    assert "current_complexity" in prompt
    assert "suggested_complexity" in prompt
    assert "src/Foo.cs" in prompt
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_big_o_prompt.py -v`
Expected: FAIL with import error for `build_big_o_prompt`.

- [ ] **Step 3: Implement prompt builder**

Add this function to `app/ai/prompts.py`:

```python
def build_big_o_prompt(language: str, file_path: str, code: str) -> str:
    return f"""Voce e um revisor especializado em complexidade algoritmica e Big O.

Analise somente riscos de complexidade algoritmica. Ignore estilo, arquitetura, nomenclatura, logs e validacoes, a menos que afetem diretamente Big O.

Classificacao:
- high: risco claro de escalabilidade, como consulta a banco dentro de loop, varredura quadratica evitavel em colecoes grandes, ordenacao/materializacao repetida dentro de loop.
- medium: ineficiencia provavel, mas dependente de contexto ou tamanho dos dados.
- low: oportunidade menor que nao deve afetar o quality gate.

Contexto:
- language: {language}
- filePath: {file_path}

[CODIGO]
{code}

Responda APENAS com JSON valido, sem texto adicional fora do JSON:
{{
  "summary": "resumo curto",
  "issues": [
    {{
      "severity": "low|medium|high",
      "line": 0,
      "current_complexity": "O(n^2)",
      "suggested_complexity": "O(n)",
      "problem": "descricao do problema",
      "suggestion": "correcao pratica"
    }}
  ]
}}"""
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_big_o_prompt.py -v`
Expected: PASS.

### Task 2: BigORunner Core Behavior

**Files:**
- Create: `app/runners/big_o.py`
- Modify: `tests/test_external_runners.py`

- [ ] **Step 1: Write failing runner tests**

Add tests covering skip, high failure, medium warning, high warning with `fail_on_high=false`, and malformed JSON.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_external_runners.py -v`
Expected: FAIL with missing `app.runners.big_o`.

- [ ] **Step 3: Implement `BigORunner`**

Create `app/runners/big_o.py` with:

- `name = "big_o"`.
- constructor parameters `languages`, `fail_on_high`, `warn_on_medium`, `max_files_per_run`.
- changed file filtering for `.cs` when language includes `csharp`.
- candidate detection for loops, LINQ, repository/database calls, sorting/materialization, and membership checks.
- Ollama call using `build_big_o_prompt()`.
- JSON parsing and mapping to `Violation`.
- status and metrics according to spec.

- [ ] **Step 4: Run runner tests to verify pass**

Run: `pytest tests/test_external_runners.py -v`
Expected: PASS.

### Task 3: Registration, Config, Ratchet

**Files:**
- Modify: `app/gates/orchestrator.py`
- Modify: `app/gates/ratchet.py`
- Modify: `quality_gate.yaml`
- Modify: `tests/test_quality_gate_config.py`
- Add: `tests/test_big_o_orchestrator.py`
- Add: `tests/test_ratchet.py`

- [ ] **Step 1: Write failing integration tests**

Test that `_build_runners()` includes `BigORunner` when enabled and excludes it when disabled. Test that project `.babysit.yml` can override `big_o.max_files_per_run`. Test that `ratchet.apply()` marks passed `big_o` as failed when baseline metrics worsen.

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_big_o_orchestrator.py tests/test_quality_gate_config.py tests/test_ratchet.py -v`
Expected: FAIL until registration/config/ratchet are implemented.

- [ ] **Step 3: Implement registration/config/ratchet**

Register `BigORunner` in `_build_runners()`, add default `big_o` config to `quality_gate.yaml`, and include `big_o` in ratchet metric prefixes.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_big_o_orchestrator.py tests/test_quality_gate_config.py tests/test_ratchet.py -v`
Expected: PASS.

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Document `big_o` in the runner list and config sample.

- [ ] **Step 2: Run full test suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 3: Run local quality gate on changed code files**

Run the babysit local gate for changed Python files if the MCP server is available, or report why it cannot be run.

---

## Self-Review

- Spec coverage: prompt, runner, config, status rules, ratchet, output reuse, tests, and docs are covered.
- Placeholder scan: no unresolved TBD/TODO items.
- Type consistency: `BigORunner`, `build_big_o_prompt`, `big_o`, `high_issues`, `medium_issues`, `low_issues`, `violations_count` are consistent across tasks.
