"""pg_dump/pg_restore via local container socket; binary I/O is safe on PowerShell 5.

Run from the repository: python -m scripts.backup create|verify|restore ...
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def pg(script, *arguments, source=None, destination=None):
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "--user", "postgres", "postgres",
         "sh", "-eu", "-c", script, "seps-backup", *arguments],
        stdin=source if source is not None else subprocess.DEVNULL,
        stdout=destination if destination is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=180, check=True,
    )
    return result.stdout.decode("utf-8").strip() if result.stdout is not None else ""


def source_database():
    return pg('printf "%s" "$POSTGRES_DB"')


def query(database, sql):
    return pg('exec psql -X -w -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -Atc "$2"',
              database, sql)


@contextmanager
def directory_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".backup.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("Backup directory is locked; check for another running job") from None
    try:
        yield
    finally:
        lock.rmdir()


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune(directory, keep):
    # Only our completed archive/manifest pairs; unrelated files stay untouched.
    pattern = r"seps-\d{8}T\d{12}Z-[0-9a-f]{8}\.dump\.json"
    manifests = sorted(p for p in directory.glob("seps-*.dump.json")
                       if re.fullmatch(pattern, p.name) and p.with_suffix("").is_file())
    for manifest in manifests[:-keep]:
        manifest.with_suffix("").unlink()
        manifest.unlink()


def create(directory, keep=7):
    if keep < 1:
        raise ValueError("keep must be positive")
    directory = Path(directory).resolve()
    with directory_lock(directory):
        filename = "seps-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = directory / f"{filename}-{uuid4().hex[:8]}.dump"
        temporary = archive.with_suffix(".dump.partial")
        manifest = archive.with_suffix(".dump.json")
        metadata = {"format": 1, "archive": archive.name,
                    "database": source_database(), "started_at": utc_now()}
        try:
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                pg('exec pg_dump -w -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                   '--format=custom --no-owner --no-acl', destination=output)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size == 0:
                raise RuntimeError("Empty backup")
            with temporary.open("rb") as source:
                pg("exec pg_restore --list", source=source)
            metadata.update(bytes=temporary.stat().st_size, sha256=checksum(temporary),
                            completed_at=utc_now())
            temporary.replace(archive)
            manifest_temporary = manifest.with_suffix(".json.partial")
            try:
                with manifest_temporary.open("x", encoding="utf-8") as output:
                    os.chmod(manifest_temporary, 0o600)
                    json.dump(metadata, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                manifest_temporary.replace(manifest)
            finally:
                manifest_temporary.unlink(missing_ok=True)
            prune(directory, keep)
            return archive
        finally:
            temporary.unlink(missing_ok=True)
            if not manifest.exists():
                archive.unlink(missing_ok=True)


def verify(archive):
    archive = Path(archive).resolve()
    metadata = json.loads(archive.with_suffix(".dump.json").read_text(encoding="utf-8"))
    if (metadata.get("format") != 1 or metadata.get("archive") != archive.name
            or metadata.get("bytes") != archive.stat().st_size
            or metadata.get("sha256") != checksum(archive)):
        raise ValueError("Backup checksum/manifest mismatch")
    with archive.open("rb") as source:
        pg("exec pg_restore --list", source=source)
    return metadata


def validate_restore_database(database, production_database):
    if (not re.fullmatch(r"seps_restore_[a-z0-9_]+", database)
            or len(database) > 63 or database == production_database):
        raise ValueError("Restore requires a NEW seps_restore_* database, distinct from production")


def restore(archive, database):
    archive = Path(archive).resolve()
    with directory_lock(archive.parent):
        validate_restore_database(database, source_database())
        verify(archive)
        # createdb refuses an existing database. Never --clean, DROP, or overwrite.
        pg('exec createdb -w -U "$POSTGRES_USER" --maintenance-db=postgres '
           '--template=template0 -- "$1"', database)
        with archive.open("rb") as source:
            pg('exec pg_restore -w -U "$POSTGRES_USER" -d "$1" --exit-on-error '
               '--single-transaction --no-owner --no-acl', database, source=source)
        count = int(query(database, "SELECT count(*) FROM events"))
        return {"database": database, "event_count": count, "archive": str(archive)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--directory", type=Path, default=Path("backups"))
    create_parser.add_argument("--keep", type=int, default=7)
    for command in ("verify", "restore"):
        subparser = commands.add_parser(command)
        subparser.add_argument("archive", type=Path)
        if command == "restore":
            subparser.add_argument("--database", required=True)
    args = parser.parse_args()
    if args.command == "create":
        result = {"archive": str(create(args.directory, args.keep))}
    elif args.command == "verify":
        result = verify(args.archive)
    else:
        result = restore(args.archive, args.database)
    # ASCII JSON avoids native-pipeline code-page mismatches in Windows PowerShell 5;
    # ConvertFrom-Json restores Unicode paths (e.g. a Russian project directory).
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
