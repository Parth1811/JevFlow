import os
import tempfile

# keep the per-user Jevflow home (project registry, judge config, seen
# sessions) out of the developer's real ~/.config during tests
os.environ["JEVFLOW_HOME"] = tempfile.mkdtemp(prefix="jevflow-home-")
os.environ.setdefault("JEVFLOW_SCAN", "")
for _k in ("JEVFLOW_JUDGE", "JEVFLOW_JUDGE_URL", "JEVFLOW_JUDGE_MODEL"):
    os.environ.pop(_k, None)
