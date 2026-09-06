"""Azure DevOps provider — prefers the Azure CLI (az) login, falls back to a configured PAT."""
from __future__ import annotations

import base64
import shutil
from dataclasses import dataclass, field
from urllib.parse import quote

from .base import (
    BearerHeaderAuth,
    CleanupCandidate,
    GitAuth,
    Provider,
    RemoteRepo,
    SyncError,
    TokenUrlAuth,
    _toml_str,
    http_json,
    run,
    toml_list,
)

AZDO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # well-known Azure DevOps resource id
API_VERSION = "7.1"


@dataclass
class AzureAccount:
    org: str = ""                                 # dev.azure.com/<org>
    pat: str = ""                                 # blank = use the Azure CLI login
    projects: list[str] = field(default_factory=list)  # blank = all projects in the org


class _AzCli:
    """REST + git via the Azure CLI's Entra token."""

    def __init__(self):
        out = run([
            "az", "account", "get-access-token",
            "--resource", AZDO_RESOURCE, "--query", "accessToken", "-o", "tsv",
        ])
        self.token = out.strip()
        if not self.token:
            raise SyncError("az returned an empty access token — run `az login`")

    def rest(self, url: str) -> dict:
        import json

        try:
            return json.loads(run(["az", "rest", "-m", "get", "-u", url]))
        except json.JSONDecodeError as e:
            raise SyncError(f"az rest returned invalid JSON: {e}")

    def git(self) -> GitAuth:
        return BearerHeaderAuth(self.token)


class _AzPat:
    """REST via basic auth; git via PAT embedded in the clone URL."""

    def __init__(self, pat: str):
        self.pat = pat

    def rest(self, url: str) -> dict:
        creds = base64.b64encode(f":{self.pat}".encode()).decode()
        return http_json(url, {"Authorization": f"Basic {creds}"})

    def git(self) -> GitAuth:
        return TokenUrlAuth(f"codehound:{quote(self.pat)}")


class AzureProvider(Provider):
    key = "azure"
    label = "Azure DevOps"

    # ----- config -----
    def load_accounts(self, raw: list[dict]) -> list[AzureAccount]:
        return [
            AzureAccount(
                org=a.get("org", ""),
                pat=a.get("pat", ""),
                projects=list(a.get("projects", [])),
            )
            for a in raw
        ]

    def dump_account(self, account: AzureAccount) -> str:
        return "\n".join([
            f"org = {_toml_str(account.org)}",
            f"pat = {_toml_str(account.pat)}",
            f"projects = {toml_list(account.projects)}",
        ])

    def account_label(self, account: AzureAccount) -> str:
        return f"{account.org} ({'PAT' if account.pat else 'az CLI'})"

    # ----- auth -----
    def _auth(self, account: AzureAccount):
        if shutil.which("az"):
            try:
                return _AzCli()
            except SyncError:
                if not account.pat:
                    raise
        if account.pat:
            return _AzPat(account.pat)
        raise SyncError("Azure DevOps account needs a PAT or the Azure CLI (az)")

    def git_auth(self, account: AzureAccount) -> GitAuth:
        return self._auth(account).git()

    # ----- listing -----
    def list_repos(self, account: AzureAccount, progress) -> list[RemoteRepo]:
        if not account.org:
            raise SyncError("Azure DevOps account has no org configured")
        auth = self._auth(account)
        base = f"https://dev.azure.com/{quote(account.org)}"
        projects = account.projects
        if not projects:
            data = auth.rest(f"{base}/_apis/projects?api-version={API_VERSION}")
            projects = [p["name"] for p in data.get("value", [])]
        repos: list[RemoteRepo] = []
        for project in projects:
            data = auth.rest(f"{base}/{quote(project)}/_apis/git/repositories?api-version={API_VERSION}")
            for r in data.get("value", []):
                url = r.get("remoteUrl") or f"{base}/{quote(project)}/_git/{quote(r['name'])}"
                # strip any credentials baked into remoteUrl
                url = "https://" + url.split("@", 1)[-1].removeprefix("https://")
                repos.append(RemoteRepo(["dev.azure.com", account.org, project, r["name"]], url, r["name"]))
        return sorted(repos, key=lambda r: r.host_path)

    # ----- janitor -----
    def cleanup_candidates(self, account: AzureAccount, progress) -> list[CleanupCandidate]:
        """Branches whose PRs are completed/abandoned, and repos that are disabled."""
        if not account.org:
            raise SyncError("Azure DevOps account has no org configured")
        auth = self._auth(account)
        base = f"https://dev.azure.com/{quote(account.org)}"
        projects = account.projects
        if not projects:
            data = auth.rest(f"{base}/_apis/projects?api-version={API_VERSION}")
            projects = [p["name"] for p in data.get("value", [])]

        out: list[CleanupCandidate] = []
        for project in projects:
            proj_base = f"{base}/{quote(project)}"
            repos = auth.rest(f"{proj_base}/_apis/git/repositories?api-version={API_VERSION}").get("value", [])
            repo_by_id = {r["id"]: r for r in repos}
            repo_by_name = {r["name"]: r for r in repos}

            # disabled repos (AzDO "archive" equivalent)
            for r in repos:
                if r.get("isDisabled"):
                    url = r.get("remoteUrl") or f"{proj_base}/_git/{quote(r['name'])}"
                    url = "https://" + url.split("@", 1)[-1].removeprefix("https://")
                    out.append(CleanupCandidate(
                        RemoteRepo(["dev.azure.com", account.org, project, r["name"]], url, r["name"]),
                        branch="",
                        reason="disabled",
                    ))

            # PRs: completed (merged) or abandoned → source branch is done
            prs = auth.rest(
                f"{proj_base}/_apis/git/pullrequests?searchCriteria.status=completed"
                f"&searchCriteria.status=abandoned&api-version={API_VERSION}"
            ).get("value", [])
            for pr in prs:
                repo = repo_by_id.get(pr["repository"]["id"])
                if not repo:
                    continue
                branch = pr["sourceRefName"].removeprefix("refs/heads/")
                status = pr["status"]  # completed | abandoned
                reason = f"PR #{pr['pullRequestId']} {status}"
                url = repo.get("remoteUrl") or f"{proj_base}/_git/{quote(repo['name'])}"
                url = "https://" + url.split("@", 1)[-1].removeprefix("https://")
                out.append(CleanupCandidate(
                    RemoteRepo(["dev.azure.com", account.org, project, repo["name"]], url, repo["name"]),
                    branch=branch,
                    reason=reason,
                ))
        return out
