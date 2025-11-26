import argparse
import collections
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from make_profiler.parser import parse


@dataclass
class LintError:
    """A machine-readable lint error with useful context."""

    error_type: str
    message: str
    line_number: int | None = None
    line_text: str | None = None


def parse_args():
    parser = argparse.ArgumentParser(description="Makefile linter")
    parser.add_argument(
        "--in_filename",
        type=str,
        default="Makefile",
        help="Makefile to read (default %(default)s)",
    )

    return parser.parse_args()


@dataclass
class TargetData:
    name: str
    doc: str
    line_number: int | None = None
    line_text: str | None = None
    grouped: bool = False
    deps: list[str] = field(default_factory=list)
    order_only_deps: list[str] = field(default_factory=list)


def _create_error(
    error_type: str,
    message: str,
    line_number: int | None = None,
    line_text: str | None = None,
) -> LintError:
    """Construct a LintError with consistent arguments."""

    return LintError(
        error_type=error_type,
        message=message,
        line_number=line_number,
        line_text=line_text,
    )


def _compute_target_lines(lines: list[str]) -> dict[str, tuple[int, str]]:
    mapping: dict[str, tuple[int, str]] = {}
    i = 0
    n = len(lines)
    target_re = re.compile(r"(?P<target>.+?)(?:&:|:)")

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue
        if stripped[0] == "#" and not line.startswith("##"):
            i += 1
            continue
        if line.startswith("\t"):
            i += 1
            continue

        if ":" in line and "=" not in line:
            m = target_re.match(line)
            if m:
                names = m.group("target").split()
                for name in names:
                    mapping.setdefault(name, (i, line))
            while stripped.rstrip().endswith("\\"):
                i += 1
                if i >= n:
                    return mapping
                line = lines[i]
                stripped = line.strip()
            i += 1
            continue

        i += 1

    return mapping


def parse_targets(
    ast: list[tuple[str, dict]],
    lines: list[str] | None = None,
) -> tuple[list[TargetData], set[str], dict[str, set[str]]]:
    line_map = _compute_target_lines(lines) if lines else {}
    target_data = []
    deps_targets = set()
    deps_map = collections.defaultdict(set)

    for token_type, data in ast:
        if token_type != "target":
            continue

        names = data.get("all_targets", [data["target"]])
        deps_list, order_only_list = data.get("deps", ([], []))
        for name in names:
            line_number, line_text = line_map.get(name, (None, None))
            target_data.append(
                TargetData(
                    name=name,
                    doc=data["docs"],
                    line_number=line_number,
                    line_text=line_text,
                    grouped=data.get("grouped", False),
                    deps=list(deps_list),
                    order_only_deps=list(order_only_list),
                ),
            )

        for dep_arr in data["deps"]:
            for item in dep_arr:
                deps_targets.add(item)
                for name in names:
                    deps_map[item].add(name)

    return target_data, deps_targets, deps_map


def validate_target_comments(
    targets: list[TargetData], *, errors: list[LintError] | None = None
) -> bool:
    """Ensure that every target has documentation."""
    is_valid = True

    for t in targets:
        if not t.doc:
            msg = f"Target without comments: {t.name}"
            print(msg, file=sys.stderr)
            if errors is not None:
                errors.append(
                    _create_error(
                        "target without comments",
                        msg,
                        line_number=t.line_number,
                        line_text=t.line_text,
                    ),
                )
            is_valid = False

    return is_valid


def validate_orphan_targets(
    targets: list[TargetData],
    deps: set[str],
    *,
    errors: list[LintError] | None = None,
) -> bool:
    """Check that every target is used or explicitly marked as FINAL."""
    is_valid = True

    for t in targets:
        if t.name not in deps and "[FINAL]" not in t.doc:
            msg = f"{t.name}, is orphan - not marked as [FINAL] and no other target depends on it"
            print(msg, file=sys.stderr)
            if errors is not None:
                errors.append(
                    _create_error(
                        "orphan target",
                        msg,
                        line_number=t.line_number,
                        line_text=t.line_text,
                    ),
                )
            is_valid = False

    return is_valid


def validate_missing_rules(
    targets: list[TargetData],
    deps: set[str],
    deps_map: dict[str, set[str]],
    *,
    root_dir: str,
    errors: list[LintError] | None = None,
) -> bool:
    """Report dependencies that do not have a rule or a file backing them."""
    is_valid = True

    target_map = {t.name: t for t in targets}
    target_names = set(target_map)
    for dep in deps:
        candidate = _resolve_filesystem_path(dep, root_dir)
        if dep not in target_names and not os.path.exists(candidate):
            for parent in sorted(deps_map.get(dep, [])):
                msg = f"No rule to make target '{dep}', needed by '{parent}'"
                print(msg, file=sys.stderr)
                if errors is not None:
                    t = target_map.get(parent)
                    errors.append(
                        _create_error(
                            "missing rule",
                            msg,
                            line_number=t.line_number if t else None,
                            line_text=t.line_text if t else None,
                        ),
                    )
            is_valid = False

    return is_valid


def validate_spaces(lines: list[str], *, errors: list[LintError] | None = None) -> bool:
    """Validate that there are no unwanted spaces in Makefile lines.

    Spaces at the beginning of a line are normally disallowed.  However
    Make allows a backslash (``\``) at the end of a line to indicate that
    the statement continues on the next line.  In such cases the
    following line usually begins with spaces for readability.  These
    spaces should not be considered an error.
    """

    is_valid = True
    prev_line = ""

    for i, line in enumerate(lines):
        if line.rstrip() != line:
            msg = f"Trailing spaces ({i}): {line}"
            print(msg, file=sys.stderr)
            if errors is not None:
                errors.append(
                    _create_error(
                        "trailing spaces",
                        msg,
                        line_number=i,
                        line_text=line,
                    ),
                )
            is_valid = False

        if prev_line.rstrip().endswith("\\"):
            prev_line = line
            continue

        if line.startswith(" ") and not line.startswith("\t"):
            msg = f"Space instead of tab ({i}): {line}"
            print(msg, file=sys.stderr)
            if errors is not None:
                errors.append(
                    _create_error(
                        "space instead of tab",
                        msg,
                        line_number=i,
                        line_text=line,
                    ),
                )
            is_valid = False

        prev_line = line

    return is_valid


def validate_multiple_targets_colon(
    targets: list[TargetData],
    _deps: set[str],
    _dep_map: dict[str, set[str]],
    *,
    errors: list[LintError] | None = None,
) -> bool:
    """Warn when multiple targets share a rule without '&:' grouping."""
    is_valid = True

    for t in targets:
        if t.grouped or t.line_text is None:
            continue
        m = re.match(r"(?P<target>.+?)(?:&:|:)", t.line_text)
        if m:
            names = m.group("target").split()
            if len(names) > 1:
                msg = (
                    f"Multiple targets defined with ':' may run several times in parallel: "
                    f"{m.group('target')}. Use '&:' to group them"
                )
                print(msg, file=sys.stderr)
                if errors is not None:
                    errors.append(
                        _create_error(
                            "multiple targets with colon",
                            msg,
                            line_number=t.line_number,
                            line_text=t.line_text,
                        ),
                    )
                is_valid = False

    return is_valid


def _resolve_filesystem_path(path: str, root_dir: str) -> str:
    """Return an absolute path for dependency lookups."""

    if not root_dir or os.path.isabs(path):
        return path
    return os.path.join(root_dir, path)


def _looks_like_directory(path: str, root_dir: str) -> bool:
    """Best-effort detection whether a dependency refers to an actual directory."""

    if not path or any(char in path for char in ("$", "*", "?", "[", "]", "%", "(", ")")):
        return False

    candidate = _resolve_filesystem_path(path, root_dir)
    return os.path.isdir(candidate)


def validate_directory_order_only_dependencies(
    targets: list[TargetData],
    *,
    root_dir: str,
    errors: list[LintError] | None = None,
) -> bool:
    """Ensure directories are only listed as order-only prerequisites."""

    is_valid = True
    for t in targets:
        for dep in t.deps:
            if dep in t.order_only_deps:
                # Already explicitly marked as order-only, nothing to report.
                continue
            if not _looks_like_directory(dep, root_dir):
                continue
            msg = (
                f"Directory dependency '{dep}' on target '{t.name}' must be "
                "order-only (list it after '|')."
            )
            print(msg, file=sys.stderr)
            if errors is not None:
                errors.append(
                    _create_error(
                        "directory dependency not order-only",
                        msg,
                        line_number=t.line_number,
                        line_text=t.line_text,
                    ),
                )
            is_valid = False

    return is_valid


TARGET_VALIDATORS: list[Callable[..., bool]] = [
    validate_orphan_targets,
    validate_target_comments,
    validate_missing_rules,
    validate_multiple_targets_colon,
]
# The list holds validators with varying signatures, so use a generic Callable.
TEXT_VALIDATORS: list[Callable[..., bool]] = [validate_spaces]


def validate(
    makefile_lines: list[str],
    targets: list[TargetData],
    deps: set[str],
    deps_map: dict[str, set[str]],
    *,
    root_dir: str | None = None,
    errors: list[LintError] | None = None,
) -> bool:
    """Run all validators and collect error messages."""

    if root_dir is None:
        root_dir = os.getcwd()

    is_valid = True

    for validator in TEXT_VALIDATORS:
        is_valid = validator(makefile_lines, errors=errors) and is_valid

    for validator in TARGET_VALIDATORS:
        if validator is validate_target_comments:
            is_valid = validator(targets, errors=errors) and is_valid
        elif validator is validate_orphan_targets:
            is_valid = validator(targets, deps, errors=errors) and is_valid
        elif validator is validate_missing_rules:
            is_valid = validator(
                targets,
                deps,
                deps_map,
                root_dir=root_dir,
                errors=errors,
            ) and is_valid
        else:
            is_valid = validator(targets, deps, deps_map, errors=errors) and is_valid

    is_valid = (
        validate_directory_order_only_dependencies(
            targets,
            root_dir=root_dir,
            errors=errors,
        )
        and is_valid
    )

    return is_valid


def summarize_errors(errors: list[LintError]) -> str:
    """Return a short summary of lint errors."""

    counts = collections.Counter(err.error_type for err in errors)
    first_seen: dict[str, int] = {}
    for err in errors:
        if err.error_type not in first_seen and err.line_number is not None:
            first_seen[err.error_type] = err.line_number

    def order(key: str) -> int:
        return first_seen.get(key, sys.maxsize)

    parts = [
        f"{name}: {counts[name]}" for name in sorted(counts.keys(), key=order)
    ]
    return ", ".join(parts)


def main():
    args = parse_args()
    
    with open(args.in_filename, "r") as f:
        makefile_lines = f.read().split("\n")

    # file_object is the stream of data and if once the data is consumed, you can't ask the source to give you the same data again.
    # so it's the reason why we should open in_file twice
    with open(args.in_filename, "r") as file:
        ast = parse(file)

    targets, deps, deps_map = parse_targets(ast, makefile_lines)

    root_dir = os.path.dirname(os.path.abspath(args.in_filename)) or "."
    errors: list[LintError] = []
    if not validate(
        makefile_lines,
        targets,
        deps,
        deps_map,
        root_dir=root_dir,
        errors=errors,
    ):
        summary = summarize_errors(errors)
        print(f"Makefile validation failed: {summary}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
