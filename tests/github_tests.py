import os
from dotenv import load_dotenv
from src.github.github_auth import AppAuth
from src.github.github_codebase import GitHubCodebase

load_dotenv()

# App credentials (store in secrets!)
APP_ID = os.getenv("GITHUB_APP_ID")
INSTALLATION_ID = os.getenv("GITHUB_APP_INSTALLATION_ID")
PRIVATE_KEY_PEM = os.getenv("GITHUB_APP_PRIVATE_KEY_PEM")  # full PEM string

auth = AppAuth(APP_ID, INSTALLATION_ID, PRIVATE_KEY_PEM)
gh = GitHubCodebase("ToasterTechHelp", "CannyEdgeDetection", auth)

# 1) View structure
print(gh.tree(ref="main", as_text_tree=True, max_depth=3))

# 2) Read any file
readme = gh.read_file("README.md", ref="main", as_text=True)

# 3) Create a branch, commit multiple files, open a PR
branch = gh.create_branch(new_branch="chore/williamdouglasthird", base_ref="main")
commit_sha = gh.commit_files(
    branch=branch,
    message="Initial boot scan artifacts",
    files={
        ".github/scan/semgrep.json": b"...json...",
        ".github/scan/report.md": "# Findings\n...".encode(),
    },
)
pr_num = gh.open_pr(head_branch=branch, base_branch="main", title="Initial security scan", body="Auto-generated report.")
print("PR #", pr_num)
