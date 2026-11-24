"""Backups: executa ./mail.sh backup/restore e gerencia o agendamento no crontab."""
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BACKUP_DIR = BASE / "backups"
MARK = "# backup-email (painel)"


@dataclass
class BackupFile:
    path: Path
    quando: datetime
    tamanho_mb: float


def list_backups() -> list[BackupFile]:
    files = sorted(BACKUP_DIR.glob("backup-email-*.tar.gz"), reverse=True)
    out = []
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2}_\d{6})", f.name)
        when = datetime.strptime(m[1], "%Y-%m-%d_%H%M%S") if m else datetime.fromtimestamp(f.stat().st_mtime)
        out.append(BackupFile(f, when, f.stat().st_size / 1_048_576))
    return out


def _mail_sh(*args: str, timeout: int = 1800) -> tuple[bool, str]:
    proc = subprocess.run(["./mail.sh", *args], cwd=BASE, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def run_backup(keep: int, extra: str = "") -> tuple[bool, str]:
    return _mail_sh("backup", str(keep), *([extra] if extra else []))


def restore(path: Path) -> tuple[bool, str]:
    return _mail_sh("restore", str(path))


# ---------- agendamento (crontab do usuário) ----------

def _crontab() -> list[str]:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def schedule() -> dict | None:
    """Agendamento atual: {'hora': 3, 'manter': 7, 'extra': ''} ou None."""
    for line in _crontab():
        if line.endswith(MARK):
            m = re.match(r"(\d+) (\d+) \* \* \* .*mail\.sh backup (\d+)(?: '([^']*)')?", line)
            if m:
                return {"minuto": int(m[1]), "hora": int(m[2]), "manter": int(m[3]),
                        "extra": m[4] or ""}
    return None


def set_schedule(hora: int | None, manter: int = 7, extra: str = "") -> None:
    """Grava (ou remove, com hora=None) a linha de backup diário no crontab."""
    if "'" in extra:
        raise ValueError("O caminho não pode conter aspas simples.")
    lines = [l for l in _crontab() if not l.endswith(MARK)]
    if hora is not None:
        dest = f" '{extra}'" if extra else ""
        lines.append(f"0 {hora} * * * cd {BASE} && ./mail.sh backup {manter}{dest} "
                     f">> {BASE}/backup.log 2>&1 {MARK}")
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def last_log(lines: int = 20) -> str:
    log = BASE / "backup.log"
    return "\n".join(log.read_text().splitlines()[-lines:]) if log.exists() else ""
