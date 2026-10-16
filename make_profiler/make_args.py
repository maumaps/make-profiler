"""Helpers that interpret GNU Make's own command line.

``profile_make`` hands its arguments to GNU Make unchanged, so the profiler
has to understand just enough of Make's option syntax to find the effective
working directory, Makefile, include search path and goals.
"""
import os
from typing import Dict, List, Optional

# Short options that always take a value (``-C dir`` or ``-Cdir``).
SHORT_VALUE_OPTIONS = 'CfIoW'
# Short options whose value is optional and, when separate, must be numeric.
SHORT_OPTIONAL_NUMBER_OPTIONS = 'jl'
LONG_VALUE_OPTIONS = (
    '--directory', '--file', '--makefile', '--include-dir', '--eval',
    '--old-file', '--assume-old', '--new-file', '--assume-new', '--what-if',
)
LONG_OPTIONAL_NUMBER_OPTIONS = ('--jobs', '--load-average', '--max-load')
DRY_RUN_LONG_OPTIONS = ('--just-print', '--dry-run', '--recon', '--question', '--touch')
DRY_RUN_SHORT_OPTIONS = 'nqt'


def make_command(make_args, extra_args) -> List[str]:
    """Insert internal GNU Make options before its optional ``--`` separator."""
    try:
        separator = make_args.index('--')
    except ValueError:
        return ['make', *make_args, *extra_args]
    return ['make', *make_args[:separator], *extra_args, *make_args[separator:]]


def make_environment(keep_recipe_locale: bool = False) -> Dict[str, str]:
    """Environment forcing Make's untranslated messages.

    The profiler parses Make's ``--print-data-base`` and ``--trace`` text,
    which GNU Make localizes, so a non-English locale would hide every rule.
    ``LC_ALL=C`` is right for the database probe, which runs no recipes.  For
    a profiled build only Make's message catalog is switched to ``C``
    (``LC_MESSAGES`` plus no ``LANGUAGE``) so recipes keep the user's locale.
    """
    environment = os.environ.copy()
    if not keep_recipe_locale:
        environment['LC_ALL'] = 'C'
        return environment
    user_locale = environment.pop('LC_ALL', None)
    if user_locale:
        # LC_ALL outranked the per-category variables; keep its effect for
        # everything except messages.
        for category in ('LC_CTYPE', 'LC_COLLATE', 'LC_NUMERIC', 'LC_TIME', 'LC_MONETARY'):
            environment[category] = user_locale
    environment.pop('LANGUAGE', None)
    environment['LC_MESSAGES'] = 'C'
    return environment


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def scan_make_options(make_args) -> Dict[str, object]:
    """Classify GNU Make arguments.

    Returns ``goals`` (ordinary targets, including everything after ``--``),
    ``dry_run`` (Make prints or tests but does not run recipes: ``-n``, ``-q``,
    ``-t``) and ``jobs`` (parallel builds, which make ``--trace`` timings
    approximate).
    """
    goals: List[str] = []
    dry_run = False
    jobs = False
    arguments = iter(make_args)
    pending: Optional[str] = None  # an argument pulled ahead for a numeric-value check

    def next_argument():
        nonlocal pending
        if pending is not None:
            value, pending = pending, None
            return value
        return next(arguments, None)

    while True:
        argument = next_argument()
        if argument is None:
            break
        if argument == '--':
            goals.extend(arguments)
            break
        if argument.startswith('--'):
            name, has_value, value = argument.partition('=')
            if name in DRY_RUN_LONG_OPTIONS:
                dry_run = True
            if name in LONG_OPTIONAL_NUMBER_OPTIONS:
                # ``--jobs`` without a number still means unlimited parallelism.
                jobs = jobs or name == '--jobs' and (not has_value or value != '1')
                if not has_value:
                    following = next_argument()
                    if following is not None and not _is_number(following):
                        pending = following
            elif name in LONG_VALUE_OPTIONS and not has_value:
                next_argument()
            continue
        if argument.startswith('-') and len(argument) > 1:
            cluster = argument[1:]
            for position, letter in enumerate(cluster):
                rest = cluster[position + 1:]
                if letter in DRY_RUN_SHORT_OPTIONS:
                    dry_run = True
                if letter in SHORT_VALUE_OPTIONS:
                    if not rest:
                        next_argument()
                    break
                if letter in SHORT_OPTIONAL_NUMBER_OPTIONS:
                    if letter == 'j':
                        jobs = jobs or rest != '1'
                    if not rest:
                        following = next_argument()
                        if following is not None and not _is_number(following):
                            pending = following
                    break
            continue
        if '=' in argument:
            continue  # variable assignment such as CC=gcc
        goals.append(argument)
    return {'goals': goals, 'dry_run': dry_run, 'jobs': jobs}


def make_goals(make_args) -> List[str]:
    """Return ordinary GNU Make goals without mistaking option values for goals."""
    return scan_make_options(make_args)['goals']


def _consume_short_cluster(cluster, position, args, index):
    """Return (value, consumes_next) for a value-taking letter in a cluster.

    ``cluster`` is the text after ``-``, ``position`` is the letter's index,
    ``args`` is the full argument list and ``index`` points at the current
    argument.  A letter with attached text takes it; otherwise it takes the
    next argument when one exists.
    """
    rest = cluster[position + 1:]
    if rest:
        return rest, False
    if index + 1 < len(args):
        return args[index + 1], True
    return None, False


def make_working_directory(make_args) -> str:
    """Return GNU Make's effective working directory after its ``-C`` options.

    Clustered short options (``-kC dir``, ``-kCdir``) are classified the same
    way as in :func:`scan_make_options`.
    """
    directory = os.getcwd()
    args = list(make_args)
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == '--':
            break
        if argument.startswith('--'):
            name, has_value, value = argument.partition('=')
            if name == '--directory':
                if has_value:
                    directory = os.path.abspath(os.path.join(directory, value))
                    index += 1
                elif index + 1 < len(args):
                    directory = os.path.abspath(os.path.join(directory, args[index + 1]))
                    index += 2
                else:
                    index += 1
            elif name in LONG_VALUE_OPTIONS and not has_value:
                index += 2 if index + 1 < len(args) else 1
            elif name in LONG_OPTIONAL_NUMBER_OPTIONS and not has_value:
                if index + 1 < len(args) and _is_number(args[index + 1]):
                    index += 2
                else:
                    index += 1
            else:
                index += 1
            continue
        if argument.startswith('-') and len(argument) > 1:
            cluster = argument[1:]
            consumed_next = False
            for position, character in enumerate(cluster):
                if character in SHORT_VALUE_OPTIONS:
                    if character == 'C':
                        value, consumes_next = _consume_short_cluster(cluster, position, args, index)
                        if value is not None:
                            directory = os.path.abspath(os.path.join(directory, value))
                        consumed_next = consumes_next
                    elif not cluster[position + 1:] and index + 1 < len(args):
                        consumed_next = True
                    break
                if character in SHORT_OPTIONAL_NUMBER_OPTIONS:
                    if not cluster[position + 1:] and index + 1 < len(args) and _is_number(args[index + 1]):
                        consumed_next = True
                    break
            index += 2 if consumed_next else 1
            continue
        index += 1
    return directory


def makefile_arguments(make_args) -> List[str]:
    """Return the Makefiles selected with ``-f``, ``--file`` or ``--makefile``.

    Clustered short options (``-kf Alt.mk``, ``-kfAlt.mk``) are classified the
    same way as in :func:`scan_make_options`.
    """
    files = []
    args = list(make_args)
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == '--':
            break
        if argument.startswith('--'):
            name, has_value, value = argument.partition('=')
            if name in ('--file', '--makefile'):
                if has_value:
                    files.append(value)
                    index += 1
                elif index + 1 < len(args):
                    files.append(args[index + 1])
                    index += 2
                else:
                    index += 1
            elif name in LONG_VALUE_OPTIONS and not has_value:
                index += 2 if index + 1 < len(args) else 1
            elif name in LONG_OPTIONAL_NUMBER_OPTIONS and not has_value:
                if index + 1 < len(args) and _is_number(args[index + 1]):
                    index += 2
                else:
                    index += 1
            else:
                index += 1
            continue
        if argument.startswith('-') and len(argument) > 1:
            cluster = argument[1:]
            consumed_next = False
            for position, character in enumerate(cluster):
                if character in SHORT_VALUE_OPTIONS:
                    if character == 'f':
                        value, consumes_next = _consume_short_cluster(cluster, position, args, index)
                        if value is not None:
                            files.append(value)
                        consumed_next = consumes_next
                    elif not cluster[position + 1:] and index + 1 < len(args):
                        consumed_next = True
                    break
                if character in SHORT_OPTIONAL_NUMBER_OPTIONS:
                    if not cluster[position + 1:] and index + 1 < len(args) and _is_number(args[index + 1]):
                        consumed_next = True
                    break
            index += 2 if consumed_next else 1
            continue
        index += 1
    return files


def has_makefile_argument(make_args) -> bool:
    """Return whether GNU Make receives an explicit Makefile selection."""
    return bool(makefile_arguments(make_args))


def make_include_directories(make_args, working_directory) -> List[str]:
    """Return GNU Make's include search directories in command-line order.

    Clustered short options (``-kI dir``, ``-kIdir``) are classified the same
    way as in :func:`scan_make_options`.
    """
    directories = []
    args = list(make_args)
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == '--':
            break
        if argument.startswith('--'):
            name, has_value, value = argument.partition('=')
            if name == '--include-dir':
                if has_value:
                    directory = value
                    index += 1
                elif index + 1 < len(args):
                    directory = args[index + 1]
                    index += 2
                else:
                    index += 1
                    continue
                directories.append(
                    directory if os.path.isabs(directory) else os.path.join(working_directory, directory)
                )
            elif name in LONG_VALUE_OPTIONS and not has_value:
                index += 2 if index + 1 < len(args) else 1
            elif name in LONG_OPTIONAL_NUMBER_OPTIONS and not has_value:
                if index + 1 < len(args) and _is_number(args[index + 1]):
                    index += 2
                else:
                    index += 1
            else:
                index += 1
            continue
        if argument.startswith('-') and len(argument) > 1:
            cluster = argument[1:]
            consumed_next = False
            picked = None
            for position, character in enumerate(cluster):
                if character in SHORT_VALUE_OPTIONS:
                    if character == 'I':
                        value, consumes_next = _consume_short_cluster(cluster, position, args, index)
                        picked = value
                        consumed_next = consumes_next
                    elif not cluster[position + 1:] and index + 1 < len(args):
                        consumed_next = True
                    break
                if character in SHORT_OPTIONAL_NUMBER_OPTIONS:
                    if not cluster[position + 1:] and index + 1 < len(args) and _is_number(args[index + 1]):
                        consumed_next = True
                    break
            if picked is not None:
                directories.append(
                    picked if os.path.isabs(picked) else os.path.join(working_directory, picked)
                )
            index += 2 if consumed_next else 1
            continue
        index += 1
    return directories
