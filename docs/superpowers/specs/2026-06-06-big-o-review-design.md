# Big O Review Design

## Context

The project provides a quality gate for Azure DevOps pull requests and local files. Existing checks are implemented as runners that return `CheckResult` objects, are instantiated by `app/gates/orchestrator.py`, can be ratcheted through numeric metrics, and can receive AI suggestions when they fail.

The new feature adds a configurable Big O review to detect algorithmic complexity risks in changed C# files. The first version should avoid broad language support and avoid blocking pull requests on uncertain findings.

## Decisions

- The check is configurable.
- Initial language support is C# only.
- The detection approach is hybrid: deterministic pre-filtering plus AI classification.
- High severity findings block the quality gate when configured to do so.
- Medium severity findings produce a warning when there are no high severity findings.
- Low severity findings do not affect gate status in the first version.

## Configuration

Add a `big_o` check under `quality_gate.checks`:

```yaml
big_o:
  enabled: true
  languages: ["csharp"]
  mode: hybrid
  fail_on_high: true
  warn_on_medium: true
  max_files_per_run: 20
```

The defaults should be conservative. If the check is enabled but Ollama is unavailable, the runner should return `skipped` with an explanatory metric so unreviewed code is not reported as clean. Malformed AI responses should return `error` because analysis was attempted but could not be interpreted safely.

## Architecture

Add a `BigORunner` in `app/runners/big_o.py` and register it in `_build_runners()` in `app/gates/orchestrator.py` when `quality_gate.checks.big_o.enabled` is true.

The runner follows the existing runner contract:

```python
async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult
```

The runner reads changed `.cs` files from the workspace, filters out missing files, generated files, and files outside the configured language set, then limits the number of analyzed files with `max_files_per_run`.

## Candidate Filtering

The deterministic pre-filter should identify files that are likely to contain Big O risks before sending them to the model. Candidate signals include:

- Nested loops.
- LINQ chains with multiple `Where`, `Select`, `GroupBy`, `OrderBy`, `Any`, `First`, `Single`, or `Count` operations.
- LINQ operations inside loops.
- Repository, database, or EF Core calls inside loops.
- Repeated sorting or materialization inside loops, such as `OrderBy`, `ToList`, or `ToArray`.
- Repeated membership checks on list-like collections where a set or dictionary would be more appropriate.

Filtering is an optimization, not the source of truth. The AI classifies whether a candidate is a real Big O issue.

## AI Analysis

Add a Big O-specific prompt builder in `app/ai/prompts.py`. The prompt should ask Ollama to analyze only algorithmic complexity, not general style or architecture.

The response must be JSON only:

```json
{
  "summary": "short summary",
  "issues": [
    {
      "severity": "low|medium|high",
      "line": 0,
      "current_complexity": "O(n^2)",
      "suggested_complexity": "O(n)",
      "problem": "description",
      "suggestion": "practical fix"
    }
  ]
}
```

The prompt should treat high severity as clear scalability risk, such as nested database calls, avoidable quadratic scans over likely large collections, or repeated expensive operations inside loops. Medium severity should cover likely but context-dependent inefficiencies. Low severity should cover minor opportunities that should not affect the gate.

## Result Mapping

The runner maps AI issues to the existing `Violation` model:

- `file`: changed file path.
- `line`: issue line returned by the model.
- `severity`: issue severity.
- `message`: include current and suggested complexity plus the problem summary.
- `current_value` and `allowed_value`: left unset because Big O strings are not numeric.

Metrics should include:

- `files_considered`.
- `files_analyzed`.
- `high_issues`.
- `medium_issues`.
- `low_issues`.
- `violations_count`, counting high and medium issues.

Status rules:

- `failed` when at least one high issue exists and `fail_on_high=true`.
- `warning` when at least one high issue exists and `fail_on_high=false`.
- `warning` when no high issue blocks the gate and at least one medium issue exists with `warn_on_medium=true`.
- `passed` when no high or medium issue is found.
- `skipped` when there are no eligible C# files or no candidate files.
- `error` for malformed AI responses or unexpected runner failures that prevent completed analysis.

## Ratchet

The ratchet should support Big O numeric metrics by including the `big_o` prefix in metric comparison. Useful metrics are `high_issues`, `medium_issues`, `low_issues`, `violations_count`, and `files_analyzed`.

The ratchet should not compare Big O notation strings directly.

## User-Facing Output

The existing PR comment and MCP table can display the `big_o` check without a new output format because they already render check status, violations, metrics, and AI suggestions.

Violation messages should be concise and actionable, for example:

`Possivel O(n^2): chamada Any dentro de loop. Sugestao: materializar IDs em HashSet para consulta O(1).`

## Tests

Add tests for:

- Runner skips when there are no changed C# files.
- Runner skips when there are no candidate files after filtering.
- High severity issue fails when `fail_on_high=true`.
- Medium severity issue produces `warning` when there are no high issues.
- High severity issue produces `warning` when `fail_on_high=false`.
- Malformed AI JSON returns `error`.
- Orchestrator registers `BigORunner` from config.
- Config merge preserves Big O defaults and project overrides.
- Ratchet compares `big_o` numeric metrics.

## Non-Goals

- Supporting non-C# languages in the first version.
- Proving exact runtime complexity deterministically.
- Rewriting code automatically.
- Blocking on medium or low findings in the initial design.
