import asyncio
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from app.config import Settings
from app.repository import RepositoryManager, WorkspaceError


class RepositoryManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.repos_root = self.tmp_dir / "repositories"
        self.runs_root = self.tmp_dir / "runs"
        self.repos_root.mkdir(parents=True)
        self.runs_root.mkdir(parents=True)
        self.settings = Settings(
            agentharness_repos_root=str(self.repos_root),
            agentharness_runs_root=str(self.runs_root),
        )
        self.manager = RepositoryManager(self.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_import_local_folder_creates_git_baseline(self):
        """Importing a local folder outside repos_root copies it and creates an initial Git commit."""
        source_dir = self.tmp_dir / "external_project"
        source_dir.mkdir()
        (source_dir / "main.py").write_text("print('hello world')\n")
        (source_dir / "README.md").write_text("# External Project\n")

        # Also add a .venv to ensure it gets ignored
        (source_dir / ".venv").mkdir()
        (source_dir / ".venv" / "bad.txt").write_text("should be ignored")

        item = await self.manager.import_local_folder(str(source_dir), custom_name="my_imported_app")
        self.assertEqual(item["name"], "my_imported_app")

        target_path = Path(item["path"])
        self.assertTrue(target_path.exists())
        self.assertTrue((target_path / "main.py").exists())
        self.assertFalse((target_path / ".venv").exists())
        self.assertTrue((target_path / ".git").exists())

        # Check repository listed
        listed = self.manager.list_repositories()
        self.assertTrue(any(r["name"] == "my_imported_app" for r in listed))

    async def test_import_zip_archive_safely_unwraps(self):
        """Importing a .zip archive unpacks files and initializes a Git repository."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("my-repo-main/app.py", "def app(): return True\n")
            zf.writestr("my-repo-main/tests/test_app.py", "def test_app(): assert True\n")
            zf.writestr("my-repo-main/requirements.txt", "pytest\n")

        item = await self.manager.import_zip(buf.getvalue(), "my-repo-main.zip")
        self.assertEqual(item["name"], "my-repo-main")

        target_path = Path(item["path"])
        self.assertTrue(target_path.exists())
        # Top-level directory was unwrapped
        self.assertTrue((target_path / "app.py").exists())
        self.assertTrue((target_path / "tests" / "test_app.py").exists())
        self.assertTrue((target_path / ".git").exists())

    async def test_zipslip_traversal_is_blocked(self):
        """Zip archive with path traversal paths must be rejected."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("../../../evil.sh", "echo bad")

        with self.assertRaises(WorkspaceError):
            await self.manager.import_zip(buf.getvalue(), "evil.zip")

    def test_normalize_git_url(self):
        """URL normalizer handles various GitHub and Git URL formats."""
        url, branch, name = self.manager._normalize_git_url("octocat/Hello-World")
        self.assertEqual(url, "https://github.com/octocat/Hello-World")
        self.assertIsNone(branch)
        self.assertEqual(name, "Hello-World")

        url, branch, name = self.manager._normalize_git_url("github.com/psf/requests.git")
        self.assertEqual(url, "https://github.com/psf/requests.git")
        self.assertEqual(name, "requests")

        url, branch, name = self.manager._normalize_git_url("https://github.com/torvalds/linux/tree/v6.1")
        self.assertEqual(url, "https://github.com/torvalds/linux")
        self.assertEqual(branch, "v6.1")
        self.assertEqual(name, "linux")

        url, branch, name = self.manager._normalize_git_url("git clone https://github.com/pallets/flask.git")
        self.assertEqual(url, "https://github.com/pallets/flask.git")
        self.assertEqual(name, "flask")

    async def test_clone_repository(self):
        """Cloning a repository via git URL succeeds and sets up the workspace."""
        origin = self.tmp_dir / "remote_origin"
        origin.mkdir()
        (origin / "hello.py").write_text("print('clone me')\n")
        self.manager._ensure_git_repository(origin)

        # Clone from origin using file:// URL
        origin_url = f"file://{origin.resolve()}"
        item = await self.manager.clone_repository(origin_url, custom_name="cloned_repo")
        self.assertEqual(item["name"], "cloned_repo")

        target = Path(item["path"])
        self.assertTrue(target.exists())
        self.assertTrue((target / "hello.py").exists())
        self.assertTrue((target / ".git").exists())

    async def test_delete_repository(self):
        """Deleting a repository safely removes it from workspace."""
        repo_dir = self.repos_root / "doomed_repo"
        repo_dir.mkdir()
        (repo_dir / "index.js").write_text("console.log('bye');")
        self.manager._ensure_git_repository(repo_dir)

        self.assertTrue(any(r["name"] == "doomed_repo" for r in self.manager.list_repositories()))

        self.manager.delete_repository("doomed_repo")
        self.assertFalse(repo_dir.exists())
        self.assertFalse(any(r["name"] == "doomed_repo" for r in self.manager.list_repositories()))

    async def test_resolve_repository_auto_initializes_plain_folder(self):
        """A plain folder created in repos_root is auto-initialized on resolution."""
        plain_dir = self.repos_root / "plain_folder"
        plain_dir.mkdir()
        (plain_dir / "code.py").write_text("x = 1\n")

        # Prior to resolve, .git doesn't exist
        self.assertFalse((plain_dir / ".git").exists())

        resolved = self.manager.resolve_repository("plain_folder")
        self.assertEqual(resolved, plain_dir.resolve())
        self.assertTrue((plain_dir / ".git").exists())


if __name__ == "__main__":
    unittest.main()
