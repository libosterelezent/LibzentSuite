# RETIRED 2026-11-15 from core/names.py (verbatim): the sync-group name
# validator — zero importers since the W5 workspace unification.

#: contain ':'/'/' (the limiter paces groups under ``group:<name>`` keys)
GROUP_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
