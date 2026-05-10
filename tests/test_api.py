from fastapi.testclient import TestClient

from app.api import azure, baselines, gate_runs, local, review
from main import app


client = TestClient(app)


def test_health_returns_status_and_model(monkeypatch):
    monkeypatch.setattr("app.api.health.settings.ollama_model", "test-model")

    response = client.get("/health")


    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model": "test-model"}


def test_get_gate_run_returns_run(monkeypatch):
    run = {"run_id": "run-1", "status": "passed", "checks": []}
    monkeypatch.setattr(gate_runs.repositories, "get_gate_run", lambda run_id: run if run_id == "run-1" else None)

    response = client.get("/gate-runs/run-1")

    assert response.status_code == 200
    assert response.json() == run


def test_get_gate_run_returns_404_when_missing(monkeypatch):
    monkeypatch.setattr(gate_runs.repositories, "get_gate_run", lambda run_id: None)

    response = client.get("/gate-runs/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}


def test_get_gate_run_summary_counts_failures(monkeypatch):
    monkeypatch.setattr(
        gate_runs.repositories,
        "get_gate_run",
        lambda run_id: {
            "run_id": run_id,
            "status": "failed",
            "checks": [
                {"check": "file_size", "status": "passed"},
                {"check": "complexity", "status": "failed"},
            ],
        },
    )

    response = client.get("/gate-runs/run-1/summary")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "status": "failed",
        "total_checks": 2,
        "failed_checks": 1,
        "failed": ["complexity"],
    }


def test_get_baseline_returns_metrics(monkeypatch):
    baseline = {"complexity": {"average": 3.2}}
    monkeypatch.setattr(
        baselines.repositories,
        "load_baseline",
        lambda repository, branch: baseline if (repository, branch) == ("repo", "develop") else None,
    )

    response = client.get("/baselines/repo?branch=develop")

    assert response.status_code == 200
    assert response.json() == baseline


def test_get_baseline_returns_404_when_missing(monkeypatch):
    monkeypatch.setattr(baselines.repositories, "load_baseline", lambda repository, branch: None)

    response = client.get("/baselines/repo")

    assert response.status_code == 404
    assert response.json() == {"detail": "Baseline not found"}


def test_refresh_baseline_saves_metrics(monkeypatch):
    saved = {}

    def save_baseline(repository, branch, metrics):
        saved.update(repository=repository, branch=branch, metrics=metrics)

    monkeypatch.setattr(baselines.repositories, "save_baseline", save_baseline)

    response = client.post("/baselines/repo/refresh?branch=develop", json={"lines": 10})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "repository": "repo", "branch": "develop"}
    assert saved == {"repository": "repo", "branch": "develop", "metrics": {"lines": 10}}


def test_list_azure_prs_maps_provider_payload(monkeypatch):
    async def list_pull_requests():
        return [
            {
                "pullRequestId": 123,
                "title": "Add export",
                "createdBy": {"displayName": "Ana"},
                "sourceRefName": "refs/heads/feature/export",
                "targetRefName": "refs/heads/main",
                "status": "active",
            }
        ]

    monkeypatch.setattr(azure.azure_devops, "list_pull_requests", list_pull_requests)
    response = client.get("/providers/azure/prs")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 123,
            "title": "Add export",
            "author": "Ana",
            "source_branch": "feature/export",
            "target_branch": "main",
            "status": "active",
        }
    ]


def test_post_azure_comment(monkeypatch):
    posted = {}

    async def post_comment(pr_id, text):
        posted.update(pr_id=pr_id, text=text)

    monkeypatch.setattr(azure.azure_devops, "post_comment", post_comment)
    response = client.post("/providers/azure/prs/123/comment", json={"text": "ok"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert posted == {"pr_id": 123, "text": "ok"}


def test_run_local_gate_resolves_files(monkeypatch, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "Foo.cs").write_text("public class Foo {}", encoding="utf-8")
    called = {}

    async def run_local_quality_gate(workspace_arg, files, repository, branch, use_ratchet):
        called.update(
            workspace=str(workspace_arg),
            files=files,
            repository=repository,
            branch=branch,
            use_ratchet=use_ratchet,
        )
        return {"run_id": "run-local", "pr_id": 0, "status": "passed", "checks": []}

    monkeypatch.setattr(local, "run_local_quality_gate", run_local_quality_gate)

    response = client.post(
        "/local/gate",
        json={
            "workspace": str(workspace),
            "files": ["Foo.cs"],
            "repository": "energia",
            "branch": "local",
            "use_ratchet": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-local"
    assert called == {
        "workspace": str(workspace.resolve()),
        "files": ["Foo.cs"],
        "repository": "energia",
        "branch": "local",
        "use_ratchet": False,
    }


def test_run_local_gate_rejects_file_outside_workspace(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "Outside.cs"
    outside.write_text("public class Outside {}", encoding="utf-8")

    response = client.post(
        "/local/gate",
        json={"workspace": str(workspace), "files": [str(outside)]},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": f"Arquivo fora do workspace: {outside}"}


def test_review_file_calls_ollama(monkeypatch):
    captured = {}

    def load_standards():
        return "standards"

    async def generate_json(prompt):
        captured["prompt"] = prompt
        return {"summary": "ok", "severity": "low", "issues": [], "approved": True}

    monkeypatch.setattr(review, "load_standards", load_standards)
    monkeypatch.setattr(review.ollama, "generate_json", generate_json)

    response = client.post(
        "/review",
        json={
            "language": "csharp",
            "filePath": "Foo.cs",
            "code": "public class Foo {}",
            "diff": None,
            "reviewMode": "strict",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"summary": "ok", "severity": "low", "issues": [], "approved": True}
    assert "standards" in captured["prompt"]
    assert "Foo.cs" in captured["prompt"]
