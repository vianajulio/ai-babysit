## ✅ Task 3 — `build_plan` (função pura)

**Files**
- Create: `app/gates/review_plan.py`
- Test: `tests/test_review_plan.py`

**Assinatura**

```python
WORKSPACE_SCOPED = ("duplication", "secrets", "complexity")
FILE_SCOPED      = ("file_size", "big_o")

def build_plan(files: list[ChangedFile], config: dict, *, plan_id: str = "") -> dict:
    """Decide one_shot vs sharded e devolve as tasks prontas para fan-out.
    Pura: não toca git, disco nem banco — só decide."""
```

**Regras**

1. `enabled_checks(config)` respeita `quality_gate.checks.<nome>.enabled` (mesmos defaults de `_build_runners`)
2. one-shot ⇔ `len(files) ≤ one_shot_max_files` **E** `Σ added_lines ≤ one_shot_max_diff_lines`
3. sharding: ordena por `added_lines` desc, *greedy bin packing*; abre shard novo ao estourar `max_files_per_shard` ou `max_diff_lines_per_shard`
4. arquivo único acima do limite ⇒ shard sozinho (nunca dividido, nunca descartado)
5. task `global` só existe se houver check de workspace habilitado; shards só se houver check por arquivo
6. zero task ⇒ devolve `mode: "one_shot"`
7. ordem determinística: mesma entrada ⇒ mesmo plano (retry/resume idempotente)

**Testes (escrever antes)**

- [ ] `test_small_pr_becomes_one_shot`
- [ ] `test_big_pr_splits_into_global_task_plus_file_shards` — cobertura 100%, sem overlap
- [ ] `test_shards_balance_by_added_lines_not_only_by_count`
- [ ] `test_one_shot_when_few_files_but_forced_sharded_by_diff_lines`
- [ ] `test_disabled_checks_are_not_scheduled`

**Depende de:** Task 1 (config `pr_review`), Task 2 (`ChangedFile`)

---

_Nota extraída de `docs/superpowers/plans/2026-08-15-pr-review-em-partes.md` para colar como node de note no Canvas._
