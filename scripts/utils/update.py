import subprocess

from pathlib import Path


class Update:
    def __init__(self) -> None:
        self.root_dir: Path = Path.cwd().parent

    def available(self) -> bool:
        try:
            subprocess.run(
                ('git', 'fetch', 'origin', 'master'),
                cwd=self.root_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )

            result = subprocess.run(
                ('git', 'rev-list', '--count', 'master..origin/master'),
                cwd=self.root_dir,
                capture_output=True,
                timeout=5,
            )

            if result.returncode == 0:
                return int(result.stdout.decode().strip()) > 0
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

        return False

    def pull_latest(self) -> bool:
        return (
            subprocess.run(
                ('git', 'pull', '--ff-only', 'origin', 'master'),
                cwd=self.root_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
