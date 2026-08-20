# Babysit — MCP local reutilizável

O Babysit é um quality gate que pode ser usado por qualquer projeto por meio de
um servidor [MCP](https://modelcontextprotocol.io/) local. O caminho recomendado
é iniciar `mcp_server_standalone.py` diretamente por **stdio**. Assim, o cliente
MCP conversa com um processo Python local e não precisa de uma API HTTP, de
Azure DevOps ou de Ollama.

## Caminho recomendado: MCP por stdio

Use este entrypoint:

```text
mcp_server_standalone.py
```

O cliente MCP deve iniciar o processo e manter stdin/stdout reservados para o
protocolo. Para o gate local padrão:

- **não** inicie Uvicorn ou `main:app`;
- **não** configure Azure DevOps;
- **não** instale ou inicie Ollama.

O processo HTTP e os providers de serviços de nuvem são integrações opcionais;
não são pré-requisitos do uso local.

## Instalação

### Windows PowerShell

```powershell
git clone <URL_DO_REPOSITORIO> .\babysit
Set-Location .\babysit

py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

O cliente MCP inicia o servidor com este comando (não o execute com Uvicorn):

```powershell
& .\.venv\Scripts\python.exe .\mcp_server_standalone.py
```

Esse processo fica aguardando mensagens MCP em stdio. Normalmente ele será
iniciado automaticamente pelo cliente, conforme a configuração abaixo.

No POSIX, os comandos equivalentes são:

```bash
python3.11 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python mcp_server_standalone.py
```

### Configuração genérica do cliente MCP

O formato abaixo usa caminhos Windows **somente como exemplo**. Troque todos os
caminhos `C:\\CAMINHO\\...` pelo local real do checkout do Babysit; não use esses
valores literalmente.

```json
{
  "mcpServers": {
    "babysit": {
      "command": "C:\\CAMINHO\\babysit\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\CAMINHO\\babysit\\mcp_server_standalone.py"
      ],
      "env": {
        "BABYSIT_DATABASE_URL": "sqlite:///C:/CAMINHO/babysit/babysit.db"
      }
    }
  }
}
```

O `env` é opcional; ele apenas fixa o local do SQLite. Não coloque tokens,
PATs ou segredos nesse JSON versionado. Para POSIX, substitua `command` e o
item de `args` por caminhos como `/opt/babysit/.venv/bin/python` e
`/opt/babysit/mcp_server_standalone.py`.

## Usar em outro projeto

1. Instale o Babysit uma vez em um diretório separado.
2. Cadastre esse servidor no cliente MCP do projeto.
3. Ao chamar `run_local_gate`, informe a raiz absoluta em `workspace` e os
   arquivos em `files`.

Exemplo de chamada MCP:

```json
{
  "workspace": "C:\\Projetos\\meu-projeto",
  "files": [
    "src\\servico.py",
    "tests\\test_servico.py"
  ],
  "repository": "",
  "branch": "",
  "use_ratchet": false
}
```

Prefira paths relativos a `workspace`. Paths absolutos também são aceitos,
desde que permaneçam dentro dele; arquivos inexistentes ou fora da raiz são
rejeitados. `repository` e `branch` podem ficar vazios para serem derivados
localmente. Use `use_ratchet: true` somente quando quiser comparar com um
baseline salvo.

### Configuração por projeto

Crie `.babysit.yml` na raiz do projeto analisado. Ela é mesclada à configuração
global e permite definir limites sem editar o checkout do Babysit. Este exemplo
mantém os checks determinísticos e desativa toda análise que depende de IA:

```yaml
quality_gate:
  ratchet: false
  checks:
    file_size:
      enabled: true
      max_lines_per_file: 300
      max_lines_per_function: 80

    complexity:
      enabled: true
      max_cyclomatic_complexity: 10

    duplication:
      enabled: true
      max_percent: 5

    secrets:
      enabled: true

    big_o:
      enabled: false

    ai_review:
      enabled: false

  pr_review:
    one_shot_max_files: 10
    one_shot_max_diff_lines: 800
    max_files_per_shard: 15
    max_diff_lines_per_shard: 1200
    parallel_hint: 4
    plan_ttl_minutes: 60
```

O gate local analisa somente os arquivos informados. Ele não faz checkout,
commit, push ou alteração de código do projeto consumidor.

## Analisar um commit: `run_commit_gate`

`run_commit_gate` é a ferramenta MCP disponível para analisar um commit sem
exigir que o usuário troque a branch atual. O contrato da chamada é:

```json
{
  "workspace": "C:\\Projetos\\meu-projeto",
  "sha": "<SHA_DO_COMMIT>",
  "base_commit": null
}
```

`workspace` deve ser a raiz de um repositório Git e `sha` deve identificar o
commit a analisar. `base_commit` é opcional: quando omitido ou definido como
`null`, a comparação usa o pai de `sha`. A ferramenta analisa os arquivos
adicionados, modificados ou renomeados entre a base e o commit; arquivos
deletados ficam fora da análise.

O gate é executado em um worktree detached temporário, sem executar
`checkout`, `reset`, `stash`, `pull` ou qualquer operação que altere o checkout
do usuário. O worktree temporário e seus metadados são removidos ao final, e o
diretório informado em `workspace` permanece intacto.

## Revisar um PR grande em partes

Para PRs grandes, um cliente MCP com múltiplos agentes pode revisar o diff em
paralelo em vez de rodar um único `run_local_gate` sequencial. O servidor
expõe um *plano de revisão*: `plan_pr_review` decide entre `one_shot` (PR
pequeno, comportamento de hoje, sem overhead) e `sharded` (PR grande, tasks
paralelizáveis), e o cliente executa o fan-out chamando `run_review_task` para
cada task antes de agregar o resultado com `get_review_plan`.

### Quando o modo é `one_shot`

O plano é `one_shot` quando **ambos** os limites abaixo são respeitados; basta
um deles estourar para o plano virar `sharded`:

- número de arquivos alterados ≤ `one_shot_max_files` (padrão `10`);
- soma de linhas adicionadas no diff ≤ `one_shot_max_diff_lines` (padrão
  `800`).

Esses limites, junto com os do particionamento em shards, vêm do bloco
`quality_gate.pr_review` de `quality_gate.yaml` e podem ser sobrescritos por
projeto em `.babysit.yml`:

```yaml
quality_gate:
  pr_review:
    one_shot_max_files: 10
    one_shot_max_diff_lines: 800
    max_files_per_shard: 15
    max_diff_lines_per_shard: 1200
    parallel_hint: 4
    plan_ttl_minutes: 60
```

- `max_files_per_shard` / `max_diff_lines_per_shard`: tamanho máximo de cada
  shard de arquivos no modo `sharded` (um arquivo isolado que já estoure o
  limite ocupa um shard sozinho, nunca é dividido ou descartado);
- `parallel_hint`: quantos agentes o cliente deve disparar em paralelo;
- `plan_ttl_minutes`: por quanto tempo um plano não finalizado fica retido
  antes de ser expirado (e seu worktree removido) na próxima chamada a
  `plan_pr_review`.

### O ciclo: `plan_pr_review` → `run_review_task` → `get_review_plan` → `close_review_plan`

1. **`plan_pr_review`** recebe `workspace`, `base_ref` e `head_ref`, calcula o
   diff e devolve o plano. Exemplo de resposta no modo `sharded`:

   ```json
   {
     "mode": "sharded",
     "tasks": [
       {
         "task_id": "global",
         "checks": ["duplication", "secrets", "complexity"],
         "files": ["…todos os arquivos alterados…"],
         "call": {
           "tool": "run_review_task",
           "args": { "plan_id": "b7e2…", "task_id": "global" }
         }
       },
       {
         "task_id": "files-1",
         "checks": ["file_size", "big_o"],
         "files": ["src/a.py", "src/b.py"],
         "call": {
           "tool": "run_review_task",
           "args": { "plan_id": "b7e2…", "task_id": "files-1" }
         }
       }
     ]
   }
   ```

   Quando o diff é pequeno, a resposta é `mode: "one_shot"` com uma única task
   cujo `call.tool` é `run_local_gate` — não há fan-out nem agregação; o
   cliente só executa essa chamada e usa a tabela devolvida diretamente.

2. **`run_review_task(plan_id, task_id)`** — o cliente faz fan-out chamando
   esta tool para cada `task_id` do plano (em agentes distintos, respeitando
   `parallel_hint`). Cada chamada roda só os `checks` daquela task sobre os
   `files` dela e persiste um resultado parcial. Repetir a chamada para a
   mesma task é seguro: o resultado anterior é sobrescrito, não duplicado.
   Retorno enxuto, sem a tabela completa:

   ```json
   {
     "plan_id": "b7e2…",
     "task_id": "files-1",
     "status": "failed",
     "violations": 3,
     "remaining_tasks": 1
   }
   ```

3. **`get_review_plan(plan_id)`** — enquanto faltar alguma task, devolve
   `{"status": "pending", "pending_tasks": [...]}`. Quando todas as tasks do
   plano estiverem concluídas, agrega os resultados parciais e devolve a
   mesma tabela markdown de `run_local_gate`/`run_commit_gate`. Chamadas
   repetidas depois de completo devolvem a tabela já calculada, sem
   reprocessar.

4. **`close_review_plan(plan_id)`** — remove o worktree e os registros do
   plano. Chame sempre ao final do fluxo, mesmo que nem todas as tasks tenham
   sido concluídas, para não deixar worktree órfão.

### `duplication`, `secrets` e `complexity` rodam uma vez

Esses três checks analisam a workspace inteira (`jscpd`, `gitleaks` e
`lizard` não têm modo "só este arquivo"), então só aparecem na task `global`,
que roda uma única vez com todos os arquivos alterados como contexto — nunca
são repetidos por shard. Isso não é um bug: shardar esses checks multiplicaria
o custo do scan sem ganhar cobertura, já que `duplication_percent` e as demais
métricas desses checks são globais por natureza.

### Ratchet e baseline só na agregação

Os resultados parciais devolvidos por `run_review_task` são `CheckResult`
crus: nenhuma task individual aplica ratchet, salva baseline ou roda a
anotação de IA. Essas etapas rodam **uma única vez**, dentro de
`get_review_plan`, sobre o resultado já agregado de todas as tasks — aplicar
ratchet a uma fatia do PR gravaria um baseline com métricas incompletas, e
anotar cada shard isoladamente geraria sugestões de IA desconexas entre si.

## Review semântica com subagentes

Os checks determinísticos (tamanho, complexidade, duplicação, secrets) rodam no
servidor. A revisão *semântica* — a que precisa de julgamento — roda fora dele:
o servidor entrega o contexto fatiado, um subagente do cliente MCP julga com o
próprio LLM e devolve findings estruturados, e o servidor consolida tudo em um
único veredito.

Ligue por projeto em `.babysit.yml`:

```yaml
quality_gate:
  checks:
    agent_review:
      enabled: true      # padrão: false
      block_on: high     # high (findings high reprovam) | none (teto em warning)
  pr_review:
    max_context_chars: 120000
    max_files_per_review_task: 6
    max_diff_lines_per_review_task: 400
```

Com `agent_review.enabled: true`, `plan_pr_review` passa a emitir, além das
tasks determinísticas, tasks `review-N` (`kind: "agent"`). Elas são particionadas
por diretório — arquivos do mesmo módulo caem na mesma task — com tetos menores
que os das shards determinísticas, porque um subagente revisa melhor seis
arquivos relacionados do que quinze sem relação.

### O ciclo

1. **`plan_pr_review`** — devolve as tasks. Cada task `review-N` traz duas
   chamadas: `call` (`get_task_context`) e `submit` (`submit_task_findings`).
2. **`run_review_task`** — fan-out determinístico, como antes. Aceita
   `detail: "full"` quando o subagente precisa dos `CheckResult` completos da
   fatia como insumo; o padrão (`summary`) segue enxuto.
3. **`get_task_context(plan_id, task_id)`** — contexto da fatia: diff por
   arquivo, `worktree_path` para ler o entorno, `standards` (o
   `docs/coding-standards.md` do projeto revisado), `finding_schema` e os
   `deterministic_checks` já calculados para aqueles arquivos. Se o diff não
   couber em `max_context_chars`, os arquivos cortados vêm em `truncated` —
   nunca há truncamento silencioso.
4. **`submit_task_findings(plan_id, task_id, findings)`** — o subagente devolve
   a lista de findings. Lista vazia é um resultado válido: significa "revisado e
   limpo". Reenviar a mesma task sobrescreve, não duplica.
5. **`get_review_plan(plan_id)`** — consolida determinístico + findings na mesma
   tabela markdown, agora com uma seção `### Findings (agent_review)` abaixo
   dela (os 20 mais graves; o resto sai em `get_gate_run(run_id)`).
6. **`close_review_plan(plan_id)`** — limpeza, como antes.

Formato de um finding:

```json
{
  "file": "app/gates/review_plan.py",
  "line": 95,
  "severity": "high",
  "category": "correctness",
  "message": "o problema, uma frase",
  "suggestion": "a correção concreta"
}
```

`high` reprova o gate quando `block_on: high`; `medium` vira `warning`; `low` é
informativo. `block_on` aceita apenas `high` ou `none` (a comparação é
normalizada): um valor inventado é recusado em vez de desligar o bloqueio em
silêncio. Um finding cujo `file` não pertence à fatia da task é rejeitado, e um
`submit` em plano já consolidado também — nos dois casos o finding não chegaria
ao veredito. **Nada vindo de `agent_review` entra no baseline do ratchet**:
findings não são reprodutíveis entre execuções, e promovê-los a referência faria
o ratchet oscilar. Eles continuam no `GateRun` — o que muda é só o que vira
baseline.

### Task de agente que não volta

Se um subagente morrer ou o cliente desistir da revisão semântica, o plano ficaria
travado esperando. `get_review_plan(plan_id, force=true)` consolida com o que
existe, registra `skipped` com a contagem de tasks sem retorno e **nunca**
devolve `passed` limpo — um PR sem a review que foi pedida não pode parecer
aprovado. Task de agente faltando vira `agent_review: skipped`; task
determinística faltando vira `skipped` no próprio check (`duplication`,
`secrets`…), para nenhuma medição sumir da tabela sem aviso.

Consolidação forçada **não** encerra o plano: se uma task atrasada chegar
depois, ela ainda entra no veredito da próxima chamada. Planos inativos há mais
de `plan_ttl_minutes` — contados pela última atividade das tasks, não pela
criação — são expirados na próxima chamada a `plan_pr_review`, inclusive os já
consolidados que ninguém fechou.

### `mode`: forçar inline ou fan-out

`plan_pr_review` aceita `mode`:

- `auto` (padrão) — decide por `one_shot_max_files` / `one_shot_max_diff_lines`;
- `inline` — força `one_shot` mesmo num PR grande;
- `sharded` — força o fan-out mesmo num PR pequeno.

No modo `one_shot` não há tasks `review-N`: o próprio agente principal revisa,
usando `standards` e `finding_schema`, que também vêm no plano. A régua é a
mesma nos dois caminhos.

### Review rápida da árvore de trabalho

`head_ref: "WORKTREE"` compara `base_ref` com o checkout atual, incluindo
arquivos não rastreados, sem criar worktree temporário e sem tocar no seu
checkout. O plano vem com `"volatile": true`: como você pode editar durante o
fan-out, o resultado nunca vira baseline. Para revisão de PR de verdade, use uma
referência commitada.

### O caminho Ollama continua

`big_o` e `ai_review` (que chamam Ollama) seguem disponíveis e desligados por
padrão. Eles são a alternativa para CI headless, onde não há agente para fazer a
revisão semântica.

## Estrutura do código

```text
mcp_server_standalone.py     fachada: registra as tools e roda o stdio
app/mcp/
  runtime.py                 instância MCP, init do banco, erro serializado
  workspace.py               resolução de workspace/arquivos, worktree temporário
  plan_state.py              estado do plano: expiração, limpeza, leitura de task
  gate_tools.py              run_local_gate, run_commit_gate, review_file, consultas
  review_tools.py            plan_pr_review, run_review_task, get_review_plan, close_review_plan
  agent_review_tools.py      get_task_context, submit_task_findings
app/gates/
  review_plan.py             particionamento puro (one_shot/sharded/review-N)
  review_aggregate.py        agregação, ratchet e fechamento do plano
  agent_findings.py          findings do subagente -> CheckResult
  git_diff.py                arquivos alterados, peso e diff (inclui modo WORKTREE)
  mcp_table.py               tabela markdown + seção de findings
```

O entrypoint não muda: o cliente MCP continua iniciando
`mcp_server_standalone.py`.

## Dependências opcionais

- **gitleaks**: habilita a detecção de secrets. Instale o executável e deixe-o
  no `PATH` (ou configure `BABYSIT_GITLEAKS_CMD`).
- **jscpd**: habilita a detecção de duplicação. No Windows PowerShell, a partir
  do checkout do Babysit, execute `npm install`; o runner usa
  `node_modules/.bin/jscpd`.
- **Ollama**: necessário somente se o projeto habilitar `big_o` ou
  `ai_review`. Nesse caso, configure `OLLAMA_URL` e `OLLAMA_MODEL` e instale o
  modelo desejado. Ele não participa do gate padrão acima.

Python 3.11+ e `git` são os requisitos básicos. `lizard` e o SDK MCP são
instalados pelo `requirements.txt`; a ausência de uma ferramenta opcional deve
ser reportada pelo respectivo check, não tratada como requisito de transporte.

## Integrações de nuvem

Azure DevOps é uma integração futura/opcional para cenários de PR. Ela não é
necessária para o MCP local e não deve ser configurada para o gate padrão. A
documentação de um provider Bitbucket Cloud futuro está em
[`docs/BITBUCKET_FUTURE.md`](docs/BITBUCKET_FUTURE.md).
