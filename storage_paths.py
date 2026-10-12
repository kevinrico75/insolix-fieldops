"""Single resolver for persistent INSOLIX files.

Every module that stores data must use these paths. Geometry page images are
rendered in memory and are not a directory on disk.
"""
import os
import sys


class StorageError(RuntimeError):
    pass


class StoragePaths:
    def __init__(self, data_dir, db, uploads, estimator_uploads, backups, railway, explicit):
        self.data_dir = data_dir
        self.db = db
        self.uploads = uploads
        self.estimator_uploads = estimator_uploads
        self.backups = backups
        self.railway = railway
        self.explicit = explicit


def _within(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def resolve_storage(base, environ=None):
    """Resolve persistent paths from INSOLIX_DATA_DIR.

    Relative values are joined to `base` (the application directory), never
    the process working directory. On Railway, an unset or in-app data
    directory is rejected so files cannot land on the ephemeral container disk.
    """
    environ = os.environ if environ is None else environ
    raw = str(environ.get("INSOLIX_DATA_DIR", "") or "").strip()
    railway_name = str(environ.get("RAILWAY_ENVIRONMENT", "") or "").strip()
    railway = bool(railway_name)
    explicit = bool(raw)
    base = os.path.abspath(base)
    if explicit:
        data_dir = raw if os.path.isabs(raw) else os.path.abspath(os.path.join(base, raw))
    else:
        data_dir = base
    if railway and not explicit:
        raise StorageError(
            "INSOLIX_DATA_DIR is unset while RAILWAY_ENVIRONMENT is set. "
            "Persistent files would be written to the ephemeral container disk. "
            "Set INSOLIX_DATA_DIR to the volume mount (production uses /data)."
        )
    if railway and _within(data_dir, base):
        raise StorageError(
            "INSOLIX_DATA_DIR resolves inside the application directory ("
            + data_dir
            + "), which is ephemeral on Railway. Set it to the volume mount (production uses /data)."
        )
    return StoragePaths(
        data_dir=data_dir,
        db=os.path.join(data_dir, "fieldops.db"),
        uploads=os.path.join(data_dir, "uploads"),
        estimator_uploads=os.path.join(data_dir, "estimator_uploads"),
        backups=os.path.join(data_dir, "backups"),
        railway=railway,
        explicit=explicit,
    )


def apply_storage(paths):
    for directory in (paths.data_dir, paths.uploads, paths.estimator_uploads, paths.backups):
        os.makedirs(directory, exist_ok=True)
    return paths


def assert_under_data_dir(data_dir, labeled_paths):
    problems = []
    root = os.path.realpath(data_dir)
    for label, path in labeled_paths:
        real = os.path.realpath(path)
        if not _within(real, root):
            problems.append(f"{label}={path}")
    if problems:
        raise StorageError(
            "Persistent path is outside INSOLIX_DATA_DIR: " + ", ".join(problems)
        )


def audit_runtime_paths(paths):
    """Import feature modules and reject any DB or upload path outside the data dir."""
    import agent_feature
    import estimator_feature
    import geometry_feature
    import opportunity_feature

    labeled = [
        ("db", paths.db),
        ("uploads", paths.uploads),
        ("estimator_uploads", paths.estimator_uploads),
        ("backups", paths.backups),
        ("estimator.db", estimator_feature.DB),
        ("estimator.uploads", estimator_feature.UPLOAD_ROOT),
        ("geometry.db", geometry_feature.DB),
        ("geometry.uploads", geometry_feature.UPLOAD_ROOT),
        ("opportunity.db", opportunity_feature.DB),
        ("agent.db", agent_feature.DB),
    ]
    assert_under_data_dir(paths.data_dir, labeled)
    expected_db = os.path.realpath(paths.db)
    expected_uploads = os.path.realpath(paths.estimator_uploads)
    mismatches = []
    for label, path in (
        ("estimator.db", estimator_feature.DB),
        ("geometry.db", geometry_feature.DB),
        ("opportunity.db", opportunity_feature.DB),
        ("agent.db", agent_feature.DB),
    ):
        if os.path.realpath(path) != expected_db:
            mismatches.append(label)
    for label, path in (
        ("estimator.uploads", estimator_feature.UPLOAD_ROOT),
        ("geometry.uploads", geometry_feature.UPLOAD_ROOT),
    ):
        if os.path.realpath(path) != expected_uploads:
            mismatches.append(label)
    if mismatches:
        raise StorageError("Storage paths disagree across modules: " + ", ".join(mismatches))
    return labeled


def log_storage_paths(paths):
    """One startup line. Paths only; no secrets and no file contents."""
    print(
        "INSOLIX storage "
        f"data_dir={paths.data_dir} "
        f"db={paths.db} "
        f"uploads={paths.uploads} "
        f"estimator_uploads={paths.estimator_uploads} "
        f"backups={paths.backups} "
        "geometry_renders=memory "
        f"railway={'yes' if paths.railway else 'no'} "
        f"data_dir_explicit={'yes' if paths.explicit else 'no'}",
        flush=True,
    )
    if not paths.explicit:
        print(
            "INSOLIX WARNING: INSOLIX_DATA_DIR is unset. Persistent files use the application "
            "directory. On Railway this process refuses to start until INSOLIX_DATA_DIR is the volume mount.",
            file=sys.stderr,
            flush=True,
        )
