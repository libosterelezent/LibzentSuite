"""Verify direct runtime requirements are represented in the exact-version lock.

The lock is intentionally a pinned requirements file: this check prevents a
new direct dependency from being added to requirements.txt without updating
requirements.lock.txt. CI's pip check independently validates installed
dependency consistency.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def read_package_names(path: Path, *, require_exact_pins: bool = False) -> set[str]:
    """Read normalized package names from a simple requirements file."""
    package_names: set[str] = set()

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        requirement = raw_line.split("#", maxsplit=1)[0].strip()
        if not requirement:
            continue
        if requirement.startswith(("-", "\\")):
            raise ValueError(
                f"{path.relative_to(ROOT)}:{line_number}: "
                "requirement includes/options are unsupported by this lock check"
            )

        match = _PACKAGE_NAME.match(requirement)
        if match is None:
            raise ValueError(
                f"{path.relative_to(ROOT)}:{line_number}: "
                f"cannot parse requirement {requirement!r}"
            )

        normalized_name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if require_exact_pins and not requirement[match.end():].lstrip().startswith("=="):
            raise ValueError(
                f"{path.relative_to(ROOT)}:{line_number}: "
                f"{normalized_name} must use an exact == version pin"
            )
        if normalized_name in package_names:
            raise ValueError(
                f"{path.relative_to(ROOT)}:{line_number}: "
                f"duplicate normalized package {normalized_name}"
            )
        package_names.add(normalized_name)

    return package_names


def main() -> int:
    """Exit non-zero when a direct runtime requirement is absent from the lock."""
    direct = read_package_names(ROOT / "requirements.txt")
    locked = read_package_names(ROOT / "requirements.lock.txt", require_exact_pins=True)
    missing = sorted(direct - locked)

    if missing:
        print(
            "requirements.lock.txt is missing direct dependencies: "
            + ", ".join(missing)
            + ". Regenerate the lock after changing requirements.txt.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(direct)} direct runtime dependencies have exact lock entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
