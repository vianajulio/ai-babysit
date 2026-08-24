# Plano futuro: provider Bitbucket Cloud

Este arquivo descreve uma integração futura. Não há um MCP Bitbucket
configurado nesta sessão, e nenhum provider Cloud deve ser tratado como já
implementado. **Nunca cole tokens, secrets ou credenciais no chat**; use um
gerenciador de segredos ou variáveis protegidas do ambiente.

## Fase 1 — provider somente leitura

O primeiro incremento deve apenas obter contexto do Bitbucket Cloud e entregar
arquivos/diffs ao pipeline local do Babysit. Ele não deve publicar comentários,
criar webhooks, fazer push nem modificar o repositório.

### Configuração e escopos

Variáveis sugeridas:

```text
BITBUCKET_BASE_URL=https://api.bitbucket.org/2.0
BITBUCKET_WORKSPACE=<workspace>
BITBUCKET_REPO_SLUG=<repo-slug>
BITBUCKET_AUTH_METHOD=oauth2
BITBUCKET_CLIENT_ID=<secret fora do repositório>
BITBUCKET_CLIENT_SECRET=<secret fora do repositório>
BITBUCKET_ACCESS_TOKEN=<injetado pelo ambiente de execução>
```

O token de acesso deve ser obtido pelo mecanismo de autenticação escolhido e
armazenado fora do código. Solicitar somente os escopos de leitura de
repositório; adicionar leitura de pull requests apenas quando o fluxo precisar
de contexto de PR. Não solicitar escopos de escrita, administração, webhook ou
comentários na primeira fase. Os nomes exatos dos escopos devem ser validados
contra o tipo de credencial adotado (OAuth ou API token) durante a
implementação; como referência, considerar `repository`/`pullrequest` de
leitura no OAuth e `read:repository`/`read:pullrequest` em API tokens.

### Endpoints necessários

O provider futuro deve encapsular, no mínimo, estes endpoints v2 e sua
paginação:

| Operação | Endpoint HTTP |
|---|---|
| Metadados do repositório | `GET /repositories/{workspace}/{repo_slug}` |
| Metadados do commit | `GET /repositories/{workspace}/{repo_slug}/commits/{commit}` |
| Diff de um commit/spec | `GET /repositories/{workspace}/{repo_slug}/diff/{spec}` |
| Estatísticas e arquivos alterados | `GET /repositories/{workspace}/{repo_slug}/diffstat/{spec}` |
| Conteúdo de um arquivo no commit | `GET /repositories/{workspace}/{repo_slug}/src/{commit}/{path}` |
| Contexto opcional de PR | `GET /repositories/{workspace}/{repo_slug}/pullrequests/{id}` |
| Diff de um PR, quando aplicável | `GET /repositories/{workspace}/{repo_slug}/pullrequests/{id}/diff` |

`{spec}` deve ser validado como uma revisão/ref aceita pelo provider. Respostas
401, 403, 404, rate limit e páginas incompletas precisam virar erros explícitos
do provider, sem expor o token nos logs.

## Testes da primeira fase

- testes unitários com respostas HTTP simuladas para cada endpoint, paginação,
  erros 401/403/404 e rate limit;
- teste de mapeamento de commit/diffstat/diff para os arquivos aceitos pelo
  gate, incluindo commit sem alterações e renome de arquivo;
- teste que verifica que somente métodos `GET` são chamados e que nenhum token
  aparece em logs, exceções ou resultados MCP;
- teste de integração opt-in, usando credencial fornecida pelo ambiente de CI,
  nunca escrita em fixture ou no repositório;
- teste do fluxo de análise garantindo que o checkout local do usuário não
  sofre `checkout`, `reset`, `stash`, `pull`, push ou alteração de arquivos;
- testes do adapter MCP somente depois de o provider existir, mantendo
  `workspace`, repositório e commit como entradas explícitas.

## Evolução posterior

### Comentários em pull requests

Após validar a leitura, uma fase separada pode adicionar publicação idempotente
de resultado. Ela exigirá escopo de escrita de pull request e, no mínimo:

```text
POST /repositories/{workspace}/{repo_slug}/pullrequests/{id}/comments
```

O plano deve definir identificação/idempotência do comentário, atualização em
vez de duplicação e tratamento de permissões antes de habilitar essa operação.

### Webhooks

Por último, pode-se avaliar automação por eventos. A configuração de webhook
exigirá escopo administrativo apropriado e um endpoint público controlado:

```text
POST /repositories/{workspace}/{repo_slug}/hooks
```

Devem ser definidos validação de assinatura, allowlist de eventos (por exemplo,
push e pull request), replay/idempotência, rotação de credenciais e remoção do
webhook. Comentários e webhooks continuam opcionais e não fazem parte do MCP
local por stdio.
