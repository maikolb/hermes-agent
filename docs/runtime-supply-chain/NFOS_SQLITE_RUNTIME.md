# NFOS Python and SQLite runtime supply chain

Status: investigated locally only. Nothing was installed or built on the VPS.

## Pinned candidate

- Publisher: Astral `python-build-standalone`
- Immutable release tag: `20260901`
- Tagged commit: `4bb01f09aaf362c71e891be4a41cb6d6ddf830b3`
- Artifact: `cpython-3.13.15+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz`
- Artifact URL: <https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-x86_64-unknown-linux-gnu-install_only.tar.gz>
- Publisher API SHA-256: `0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461`
- Independently downloaded local SHA-256: `0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461`
- Build manifest source: <https://github.com/astral-sh/python-build-standalone/blob/20260901/pythonbuild/downloads.py>
- Build manifest blob: `6576fed5f0df684716d12ca48b6139db1c22bc1f`
- Bundled SQLite source: `sqlite-autoconf-3530100.tar.gz`, SQLite `3.53.1.0`
- Bundled SQLite source URL: <https://www.sqlite.org/2026/sqlite-autoconf-3530100.tar.gz>
- Bundled SQLite source SHA-256: `83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f`

Astral documents that uv managed CPython uses `python-build-standalone`: <https://docs.astral.sh/uv/concepts/python-versions/#managed-python-distributions>. SQLite 3.50.0 is the approved minimum; its authoritative release record is <https://www.sqlite.org/releaselog/3_50_0.html>. The pinned build declares SQLite 3.53.1.0, which is newer than that minimum.

## Reproduction and verification on Linux x86_64

Run in a new empty staging directory. These commands do not select or replace a Hermes release.

```bash
set -eu
artifact='cpython-3.13.15+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz'
url='https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.13.15%2B20260901-x86_64-unknown-linux-gnu-install_only.tar.gz'
expected='0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461'

test "$(pwd)" != '/'
test ! -e "$artifact"
curl --fail --location --proto '=https' --tlsv1.2 --output "$artifact" "$url"
printf '%s  %s\n' "$expected" "$artifact" | sha256sum --check --strict
tar -xzf "$artifact"
test -x python/bin/python3.13
python/bin/python3.13 - <<'PY'
import platform
import sqlite3
import sys

print(sys.version)
print(platform.machine())
print(sqlite3.sqlite_version)
print(sqlite3.sqlite_source_id())
assert sys.version_info[:2] == (3, 13)
assert platform.machine() in {"x86_64", "AMD64"}
assert tuple(map(int, sqlite3.sqlite_version.split(".")[:3])) >= (3, 50, 0)
PY
```

## Future release build controls

1. Download on a staging host, never directly into an active release.
2. Compare the downloaded SHA-256 to the immutable value above before extraction.
3. Record a second SHA-256 after transport to the target. Both hashes must match.
4. Create the virtual environment with the extracted interpreter's exact absolute path.
5. Install dependencies from the candidate commit's locked or hashed dependency surface.
6. Record `sys.executable`, `sys.version`, `sqlite3.sqlite_version`, `sqlite3.sqlite_source_id()` and the release Git SHA.
7. Reject the release if the imported `hermes_state.__file__` is outside the immutable candidate directory.
8. Keep the candidate profile-scoped. Do not repoint `/usr/local/bin/hermes`.

## Local evidence and limitation

On 2026-09-03, the GitHub release API returned the publisher digest above and a local HTTPS download produced the same digest. The artifact is Linux x86_64; the current host is Windows and has no WSL runtime, so its interpreter was not executed here. Execution and dependency installation remain a future staging gate, followed by a separate approved target gate. No target validation is claimed.
