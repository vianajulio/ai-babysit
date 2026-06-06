# babysit

API de Quality Gate para Pull Requests do Azure DevOps e arquivos locais.

Em PRs, clona o repositório, executa verificações estáticas, compara métricas com baseline via ratchet e publica o resultado como comentário. Em modo local, executa os mesmos runners em arquivos informados sem depender de PR. A IA é usada apenas para sugerir correções ou revisar um arquivo sob demanda, nunca altera código.

---

## Como funciona

```
POST /providers/azure/prs/{pr_id}/gate
        │
        ├─ Busca metadados do PR no Azure DevOps
        ├─ Clona o repositório em /tmp/babysit/{run_id}/repo
        ├─ Checkout da branch do PR
        ├─ Executa runners configurados
        │     ├─ file_size    — linhas por arquivo e por função
        │     ├─ complexity   — complexidade ciclomática (lizard)
        │     ├─ duplication  — duplicação de código (jscpd)
        │     ├─ secrets      — credenciais expostas (gitleaks)
        │     └─ big_o        — riscos de complexidade algorítmica em C# (Ollama)
        ├─ Compara métricas com baseline da target branch (ratchet)
        ├─ Chama Ollama para sugestões nos checks com falha
        ├─ Publica comentário markdown no PR
        ├─ Persiste execução no SQLite
        └─ Retorna GateRun em JSON
```

Para arquivos locais:

```
POST /local/gate
        │
        ├─ Recebe workspace local e lista de arquivos
        ├─ Valida que todos os arquivos existem dentro do workspace
        ├─ Executa runners configurados
        ├─ Opcionalmente compara com baseline local quando use_ratchet=true
        ├─ Chama Ollama para sugestões nos checks com falha
        ├─ Persiste execução no SQLite com pr_id=0
        └─ Retorna GateRun em JSON
```

### Regra de ratchet

Um PR pode **melhorar ou empatar** métricas. Nunca pode piorá-las.

- Se não existe baseline: cria com as métricas atuais e aprova.
- Se existe baseline e uma métrica piorou: o check é marcado como `failed`.
- Se o gate passa: o baseline é atualizado automaticamente.

---

## Pré-requisitos

**Python 3.11+**, `git`, `npm` e `curl`.

| Ferramenta | Uso |
|------------|-----|
| `git` | Clone e checkout do repositório |
| `lizard` | Complexidade ciclomática |
| `jscpd` | Detecção de duplicação |
| `gitleaks` | Detecção de secrets |
| `ollama` | Sugestões de correção via IA |

```bash
# ollama: https://ollama.com
ollama pull qwen2.5-coder:7b
```

O `lizard` é instalado no `.venv` pelo `pip install -r requirements.txt`. O `jscpd` deve ficar local em `node_modules/.bin/jscpd`, instalado por `npm install`. O `gitleaks` pode ficar local em `.venv/bin/gitleaks` ou `tools/bin/gitleaks`.

---

## Instalação

```bash
git clone <repo-url>
cd babysit
python -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
npm install
cp .env.example .env  # edite com suas credenciais
```

Para instalar o `gitleaks` localmente no `.venv` em Linux x64:

```bash
curl -L "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz" -o /tmp/gitleaks.tar.gz
tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks
cp /tmp/gitleaks ./.venv/bin/gitleaks
chmod +x ./.venv/bin/gitleaks
```

### Variáveis de ambiente (`.env`)

```env
AZURE_PAT=seu_personal_access_token
AZURE_ORG=sua_organizacao
AZURE_PROJECT=seu_projeto
AZURE_REPO=seu_repositorio

OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=qwen2.5-coder:7b

BABYSIT_TEMP_DIR=/tmp/babysit
BABYSIT_DATABASE_URL=sqlite:///./babysit.db
BABYSIT_CLEANUP_AFTER_RUN=true
BABYSIT_WORKSPACE_TIMEOUT=300
```

O PAT precisa das permissões: **Code (Read)** e **Pull Request Threads (Read & Write)**.

---

## Configuração dos checks (`quality_gate.yaml`)

```yaml
quality_gate:
  ratchet: true

  checks:
    file_size:
      enabled: true
      max_lines_per_file: 400
      max_lines_per_function: 80

    complexity:
      enabled: true
      max_cyclomatic_complexity: 10
      cannot_increase_average: true

    duplication:
      enabled: true
      max_percent: 5
      cannot_increase: true

    secrets:
      enabled: true
      block_on_detection: true

    big_o:
      enabled: true
      languages: ["csharp"]
      mode: hybrid
      fail_on_high: true
      warn_on_medium: true
      max_files_per_run: 20
      max_code_chars: 1000

    ai_review:
      enabled: true
      mode: suggest_only
```

---

## Executando

```bash
./.venv/bin/uvicorn main:app --reload
`
``

A API fica disponível em `http://localhost:8000`. Documentação interativa em `/docs`.

---

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/health` | Status da API |
| `GET` | `/providers/azure/prs` | Lista PRs ativos |
| `POST` | `/providers/azure/prs/{pr_id}/gate` | Executa o Quality Gate |
| `POST` | `/providers/azure/prs/{pr_id}/comment` | Publica comentário manual |
| `POST` | `/local/gate` | Executa o Quality Gate em arquivos locais |
| `POST` | `/review` | Revisa um arquivo via Ollama e coding standards |
| `GET` | `/gate-runs/{run_id}` | Consulta execução |
| `GET` | `/gate-runs/{run_id}/summary` | Resumo da execução |
| `GET` | `/baselines/{repository}?branch=main` | Consulta baseline |
| `POST` | `/baselines/{repository}/refresh` | Atualiza baseline manualmente |

### Executando em arquivos locais

```bash
curl -s -X POST http://localhost:8000/local/gate \
  -H "Content-Type: application/json" \
  -d '{
    "workspace": "/mnt/jogos/Plus/energia/backend",
    "files": ["src/Application/UseCases/Foo.cs"],
    "repository": "energia",
    "branch": "local",
    "use_ratchet": false
  }'
```

`files` aceita paths relativos ao `workspace` ou paths absolutos dentro dele. Arquivos fora do `workspace` são rejeitados.

### Revisando um arquivo via IA

```bash
curl -s -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{
    "language": "csharp",
    "filePath": "src/Application/UseCases/Foo.cs",
    "code": "public class Foo {}",
    "diff": null,
    "reviewMode": "strict"
  }'
```

### Exemplo de resposta do gate

```json
{
  "run_id": "3f2a1b...",
  "pr_id": 42,
  "status": "failed",
  "source_branch": "feature/novo-servico",
  "target_branch": "main",
  "comment_posted": true,
  "checks": [
    {
      "check": "complexity",
      "status": "failed",
      "metrics": { "average_complexity": 8.4, "violations_count": 2 },
      "violations": [
        {
          "file": "src/Services/PagamentoService.cs",
          "line": 87,
          "severity": "high",
          "message": "Função `ProcessarPagamento` com complexidade ciclomática 14",
          "current_value": 14,
          "allowed_value": 10
        }
      ],
      "suggestion": "Divida a função separando validação, cálculo e persistência em métodos menores."
    },
    {
      "check": "secrets",
      "status": "passed",
      "metrics": { "secrets_found": 0 },
      "violations": []
    }
  ]
}
```

### Exemplo de comentário no PR

```markdown
## Quality Gate ❌ FAILED

### file_size — passed
### complexity — failed
- `src/Services/PagamentoService.cs:87` — Função `ProcessarPagamento` com complexidade 14
  - Atual: **14** | Limite: **10**

> **Sugestão da IA:** Divida a função separando validação, cálculo e persistência em métodos menores.

### duplication — passed
### secrets — passed
```

---

## Estrutura do projeto

```
babysit/
├── main.py                  # Entry point FastAPI
├── settings.py              # Configuração via pydantic-settings
├── quality_gate.yaml        # Configuração dos checks
├── requirements.txt
└── app/
    ├── api/                 # Routers FastAPI
    │   ├── health.py
    │   ├── azure.py
    │   ├── gate_runs.py
    │   └── baselines.py
    ├── providers/
    │   └── azure_devops.py  # Cliente Azure DevOps
    ├── workspaces/
    │   └── manager.py       # Clone, checkout e cleanup
    ├── gates/
    │   ├── models.py        # GateRun, CheckResult, Violation
    │   ├── result.py        # Consolidação e geração do comentário
    │   ├── ratchet.py       # Engine de comparação com baseline
    │   └── orchestrator.py  # Fluxo principal do gate
    ├── runners/
    │   ├── file_size.py
    │   ├── complexity.py    # lizard
    │   ├── duplication.py   # jscpd
    │   ├── secrets.py       # gitleaks
    │   └── ai_review.py     # Sugestões via Ollama
    ├── languages/
    │   └── detection.py
    ├── ai/
    │   ├── ollama.py
    │   └── prompts.py
    └── storage/
        ├── database.py      # SQLAlchemy + SQLite
        └── repositories.py  # CRUD gate_runs e baselines
```

---

## Adicionando um runner

Crie `app/runners/meu_check.py` implementando o protocolo `Runner`:

```python
from pathlib import Path
from app.gates.models import CheckResult, GateStatus, Violation

class MeuCheckRunner:
    name = "meu_check"

    async def run(self, workspace: Path, changed_files: list[str]) -> CheckResult:
        violations = []
        # ... sua lógica aqui ...
        return CheckResult(
            check=self.name,
            status=GateStatus.failed if violations else GateStatus.passed,
            metrics={"violations_count": len(violations)},
            violations=violations,
        )
```

Registre em `app/gates/orchestrator.py` dentro de `_build_runners()`.

---

## Status possíveis de um check

| Status | Significado |
|--------|-------------|
| `passed` | Sem problemas |
| `failed` | Violação detectada ou regressão de métrica |
| `warning` | Atenção, mas não bloqueia |
| `skipped` | Ferramenta não instalada ou check desabilitado |
| `error` | Falha técnica na execução do runner |

---

## Roadmap

- Suporte a GitHub
- Lint por linguagem (pylint, eslint, dotnet format)
- Cobertura a partir de relatórios existentes
- Status check no Azure DevOps além de comentário
- Execução assíncrona com fila
- PostgreSQL
- Dashboard web
