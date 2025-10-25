# github_codebase.py
from __future__ import annotations
import base64, mimetypes
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, List, Optional, Tuple, Dict, Any

import requests
from .github_auth import GitHubAuth, TokenAuth

class GitHubError(RuntimeError): ...

@dataclass(frozen=True)
class TreeEntry:
    path: str
    type: str   # 'blob'|'tree'|'commit'
    mode: str
    size: Optional[int]
    sha: str

class GitHubCodebase:
    GITHUB_API = "https://api.github.com"

    def __init__(self, owner: str, repo: str, auth: GitHubAuth, base_url: str = GITHUB_API, session: Optional[requests.Session]=None):
        self.owner, self.repo = owner, repo
        self.base_url = base_url.rstrip("/")
        self.sess = session or requests.Session()
        self.sess.headers.update({"Accept": "application/vnd.github+json","User-Agent":"codebase-reader/0.2"})
        # store auth (TokenAuth or AppAuth)
        self.auth = auth

    # ------------ reads ------------
    def tree(self, ref: str="main", *, recursive: bool=True, include: Optional[Iterable[str]]=None,
             exclude: Optional[Iterable[str]]=None, as_text_tree: bool=False, max_depth: Optional[int]=None) -> List[TreeEntry] | str:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/trees/{ref}"
        resp = self.sess.get(url, params={"recursive": 1 if recursive else 0}, headers=self.auth.auth_header(), timeout=60)
        self._raise(resp, url)
        data = resp.json()
        entries = [TreeEntry(t["path"], t["type"], t.get("mode",""), t.get("size"), t["sha"]) for t in data.get("tree",[])]
        entries = self._apply_filters(entries, include, exclude)
        if as_text_tree: return self._format_tree(entries, max_depth=max_depth)
        return entries

    def read_file(self, path: str, ref: str="main", *, as_text: bool=False, encoding: str="utf-8",
                  allow_large_via_raw: bool=True, size_limit_bytes: int=1_000_000) -> bytes | str:
        c_url = f"{self.base_url}/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = self.sess.get(c_url, params={"ref": ref}, headers=self.auth.auth_header(), timeout=60)
        self._raise(resp, c_url)
        meta = resp.json()
        if meta.get("encoding") == "base64" and "content" in meta:
            content = base64.b64decode(meta["content"])
        elif meta.get("download_url") and (meta.get("size", 0) > size_limit_bytes or allow_large_via_raw):
            raw = self.sess.get(meta["download_url"], headers=self.auth.auth_header(), timeout=120)
            self._raise(raw, meta["download_url"])
            content = raw.content
        else:
            raise GitHubError(f"Unsupported path type or missing content for {path!r}")
        if as_text:
            try: return content.decode(encoding)
            except UnicodeDecodeError:
                for enc in ("utf-8-sig","latin-1"):
                    try: return content.decode(enc)
                    except UnicodeDecodeError: pass
                mt,_ = mimetypes.guess_type(path)
                raise GitHubError(f"Binary or unknown text encoding (mime={mt}). Use as_text=False.")
        return content

    # ------------ writes (via Git Data API) ------------
    def create_branch(self, new_branch: str, base_ref: str="main") -> str:
        """Returns new branch ref name."""
        base_sha = self._get_ref_sha(base_ref)
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/refs"
        body = {"ref": f"refs/heads/{new_branch}", "sha": base_sha}
        resp = self.sess.post(url, json=body, headers=self.auth.auth_header(), timeout=30)
        # If already exists, just return
        if resp.status_code == 422 and "Reference already exists" in resp.text:
            return new_branch
        self._raise(resp, url)
        return new_branch

    def commit_files(self, branch: str, message: str, files: Dict[str, bytes]) -> str:
        """
        Commit multiple files to `branch`. Returns commit SHA.
        Steps: get base commit -> create blobs -> create tree -> create commit -> update ref.
        """
        head_sha, base_tree = self._get_head_and_tree(branch)
        blob_entries = []
        for path, data in files.items():
            blob_sha = self._create_blob(data)
            blob_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        tree_sha = self._create_tree(base_tree, blob_entries)
        commit_sha = self._create_commit(message, tree_sha, [head_sha])
        self._update_ref(branch, commit_sha)
        return commit_sha

    def open_pr(self, head_branch: str, base_branch: str, title: str, body: str="") -> int:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/pulls"
        payload = {"title": title, "head": head_branch, "base": base_branch, "body": body}
        resp = self.sess.post(url, json=payload, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        return resp.json()["number"]

    # ---- internals for Git Data API ----
    def _get_ref_sha(self, ref: str) -> str:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/refs/heads/{ref}"
        resp = self.sess.get(url, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        return resp.json()["object"]["sha"]

    def _get_head_and_tree(self, branch: str) -> Tuple[str,str]:
        commit_sha = self._get_ref_sha(branch)
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/commits/{commit_sha}"
        resp = self.sess.get(url, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        data = resp.json()
        return commit_sha, data["tree"]["sha"]

    def _create_blob(self, content: bytes) -> str:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/blobs"
        payload = {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
        resp = self.sess.post(url, json=payload, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        return resp.json()["sha"]

    def _create_tree(self, base_tree_sha: str, entries: List[Dict[str, str]]) -> str:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/trees"
        payload = {"base_tree": base_tree_sha, "tree": entries}
        resp = self.sess.post(url, json=payload, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        return resp.json()["sha"]

    def _create_commit(self, message: str, tree_sha: str, parent_shas: List[str]) -> str:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/commits"
        payload = {"message": message, "tree": tree_sha, "parents": parent_shas}
        resp = self.sess.post(url, json=payload, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)
        return resp.json()["sha"]

    def _update_ref(self, branch: str, commit_sha: str) -> None:
        url = f"{self.base_url}/repos/{self.owner}/{self.repo}/git/refs/heads/{branch}"
        resp = self.sess.patch(url, json={"sha": commit_sha, "force": False}, headers=self.auth.auth_header(), timeout=30)
        self._raise(resp, url)

    # ------------ helpers ------------
    @staticmethod
    def _apply_filters(entries: List[TreeEntry], include, exclude) -> List[TreeEntry]:
        if include:
            inc = [s.lower() for s in include]
            entries = [e for e in entries if any(s in e.path.lower() for s in inc)]
        if exclude:
            exc = [s.lower() for s in exclude]
            entries = [e for e in entries if not any(s in e.path.lower() for s in exc)]
        return entries

    @staticmethod
    def _format_tree(entries: List[TreeEntry], *, max_depth: Optional[int]) -> str:
        from collections import defaultdict
        Node = lambda: {"dirs": defaultdict(Node), "files": []}
        root = Node()
        for e in entries:
            parts = PurePosixPath(e.path).parts
            if e.type == "tree":
                cur = root
                for p in parts: cur = cur["dirs"][p]
            else:
                cur = root
                for p in parts[:-1]: cur = cur["dirs"][p]
                cur["files"].append(parts[-1])

        def walk(node, prefix="", depth=0):
            if max_depth is not None and depth > max_depth: return []
            lines, dir_names, file_names = [], sorted(node["dirs"].keys()), sorted(node["files"])
            total, idx = len(dir_names)+len(file_names), 0
            for d in dir_names:
                idx += 1; last = idx==total; branch = "└── " if last else "├── "
                lines.append(prefix+branch+d+"/")
                lines += walk(node["dirs"][d], prefix+("    " if last else "│   "), depth+1)
            for f in file_names:
                idx += 1; last = idx==total; branch = "└── " if last else "├── "
                lines.append(prefix+branch+f)
            return lines

        rendered = ["."]
        rendered += walk(root)
        return "\n".join(rendered)

    @staticmethod
    def _raise(resp: requests.Response, url: str) -> None:
        if 200 <= resp.status_code < 300: return
        raise GitHubError(f"GitHub API error {resp.status_code} for {url}\n{resp.text[:500]}")
