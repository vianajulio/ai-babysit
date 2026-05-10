from base64 import b64encode

import httpx
from fastapi import HTTPException

from settings import settings


def _headers() -> dict:
    if not settings.azure_pat:
        raise HTTPException(status_code=500, detail="AZURE_PAT não configurado")
    token = b64encode(f":{settings.azure_pat}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _base() -> str:
    if not all([settings.azure_org, settings.azure_project, settings.azure_repo]):
        raise HTTPException(
            status_code=500,
            detail="AZURE_ORG, AZURE_PROJECT e AZURE_REPO precisam estar configurados",
        )
    return (
        f"https://dev.azure.com/{settings.azure_org}/{settings.azure_project}"
        f"/_apis/git/repositories/{settings.azure_repo}"
    )


async def _get(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="AZURE_PAT inválido ou expirado")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Recurso não encontrado: {url}")
    resp.raise_for_status()
    return resp.json()


async def list_pull_requests() -> list[dict]:
    url = (
        f"{_base()}/pullrequests"
        f"?searchCriteria.status=active"
        f"&api-version={settings.azure_api_version}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        data = await _get(client, url)
    return data.get("value", [])


async def get_pull_request(pr_id: int) -> dict:
    url = f"{_base()}/pullrequests/{pr_id}?api-version={settings.azure_api_version}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await _get(client, url)


async def get_changed_files(pr_id: int) -> list[str]:
    pr = await get_pull_request(pr_id)
    source_commit = pr["lastMergeSourceCommit"]["commitId"]
    target_commit = pr["lastMergeTargetCommit"]["commitId"]

    diff_url = (
        f"{_base()}/diffs/commits"
        f"?baseVersionType=commit&baseVersion={target_commit}"
        f"&targetVersionType=commit&targetVersion={source_commit}"
        f"&api-version={settings.azure_api_version}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        diff_data = await _get(client, diff_url)

    return [
        c["item"]["path"]
        for c in diff_data.get("changes", [])
        if c.get("item", {}).get("gitObjectType") == "blob"
        and c.get("changeType") != "delete"
    ]


async def get_file_content(path: str, commit_id: str) -> str:
    url = (
        f"{_base()}/items"
        f"?path={path}"
        f"&versionDescriptor.versionType=commit"
        f"&versionDescriptor.version={commit_id}"
        f"&api-version={settings.azure_api_version}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_headers())
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        return resp.text


def get_clone_url() -> str:
    # Embed PAT in URL — never logged; used only by git clone subprocess
    return (
        f"https://{settings.azure_pat}@dev.azure.com"
        f"/{settings.azure_org}/{settings.azure_project}"
        f"/_git/{settings.azure_repo}"
    )


async def post_comment(pr_id: int, text: str) -> None:
    url = (
        f"{_base()}/pullrequests/{pr_id}/threads"
        f"?api-version={settings.azure_api_version}"
    )
    payload = {
        "comments": [{"parentCommentId": 0, "content": text, "commentType": 1}],
        "status": 1,
    }
    headers = {**_headers(), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
