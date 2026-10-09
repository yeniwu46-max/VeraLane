"""Build a safe, reproducible source archive from the current worktree."""
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZIP_DEFLATED
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'release' / 'VeraLane-source-2026-10-09.zip'
EXCLUDED_PARTS = {
    '.git', '.venv', 'venv', 'node_modules', 'dist', 'build', '__pycache__',
    '.pytest_cache', '.ruff_cache', '.mypy_cache', '.cache', 'release', 'data',
}
EXCLUDED_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.log', '.pem', '.key', '.p12'}


def allowed(rel: str) -> bool:
    p = PurePosixPath(rel)
    if p.name.startswith('~$'):
        return False
    if any(part in EXCLUDED_PARTS for part in p.parts):
        return False
    if p.name == '.env' or (p.name.startswith('.env.') and p.name != '.env.example'):
        return False
    if p.suffix.lower() in EXCLUDED_SUFFIXES or p.name.endswith('.htpasswd'):
        return False
    return True


tracked = subprocess.check_output(
    ['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=ROOT
).decode('utf-8').split('\0')
files = sorted({rel.replace('\\', '/') for rel in tracked if rel and allowed(rel) and (ROOT / rel).is_file()})
OUT.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(OUT, 'w', ZIP_DEFLATED, compresslevel=8) as zf:
    for rel in files:
        zf.write(ROOT / rel, f'VeraLane/{rel}')

# Verify archive paths and scan textual content for common credential leakage.
secret = re.compile(rb'(?i)(?:sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|Authorization:\s*Basic\s+[A-Za-z0-9+/=]{16,})')
with ZipFile(OUT) as zf:
    bad_paths = [n for n in zf.namelist() if any(part in EXCLUDED_PARTS for part in PurePosixPath(n).parts)]
    leaked = []
    for name in zf.namelist():
        if name.lower().endswith(('.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.py', '.ts', '.tsx', '.js', '.ps1', '.conf', '.example')):
            if secret.search(zf.read(name)):
                leaked.append(name)
    if bad_paths or leaked:
        raise SystemExit(f'Archive safety check failed: excluded paths={bad_paths}, secret-like content={leaked}')
    print(f'Created {OUT} with {len(files)} files ({OUT.stat().st_size:,} bytes)')
    print('Archive path and secret scan passed.')
