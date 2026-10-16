import collections
import io
import os
import re
import subprocess

from enum import Enum
from typing import Any, Dict, Generator, List, Tuple, TextIO

from more_itertools import peekable

from make_profiler.make_args import (  # noqa: F401  (re-exported for callers)
    has_makefile_argument,
    make_command,
    make_environment,
    make_include_directories,
    make_working_directory,
    makefile_arguments,
)


class Tokens(str, Enum):
    target = "target"
    command = "command"
    expression = "expression"


INCLUDE_DIRECTIVE = re.compile(r'^[ ]*(?:-?include|sinclude)\s+')


def documented_targets(makefile: str) -> Dict[str, str]:
    """Read self-documenting target descriptions from one Makefile source."""
    docs = {}
    with open(makefile) as source:
        for token, line in tokenizer(source):
            if token != Tokens.target or '##' not in line:
                continue
            target_part, description = line.split('##', 1)
            separator = '&:' if '&:' in target_part else ':'
            for target in target_part.split(separator, 1)[0].strip().split():
                docs[target] = description.strip()
    return docs


def included_makefiles(output: str, working_directory: str) -> List[str]:
    """Recover existing source paths from GNU Make's space-separated database value."""
    prefix = 'MAKEFILE_LIST := '
    value = next((line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)), '')
    makefiles = []
    # GNU Make escapes a space inside a name as ``\ ``; any other whitespace
    # separates entries.  Splitting once keeps this linear in the list length,
    # with one filesystem probe per entry.
    for entry in re.split(r'(?<!\\)\s+', value.strip()):
        candidate = entry.replace('\\ ', ' ')
        if not candidate:
            continue
        path = candidate if os.path.isabs(candidate) else os.path.join(working_directory, candidate)
        if os.path.isfile(path):
            makefiles.append(path)
    return makefiles


def tokenizer(fd: List[str]) -> Generator[Tuple[Tokens, str], None, None]:
    it = enumerate(fd)

    def glue_multiline(line: str) -> str:
        lines = []
        strip_line = line.strip()
        while strip_line[-1] == '\\':
            lines.append(strip_line.rstrip('\\').strip())
            line_num, line = next(it)
            strip_line = line.strip()
        lines.append(strip_line.rstrip('\\').strip())
        return ' '.join(lines)

    for line_num, line in it:
        strip_line = line.strip()

        # skip empty lines
        if not strip_line:
            continue

        # skip comments, don't skip docstrings
        if strip_line[0] == '#' and line[:2] != '##':
            continue
        elif line[0] == '\t':
            yield (Tokens.command, glue_multiline(line))
        elif ':' in line and '=' not in line:
            yield (Tokens.target, glue_multiline(line))
        else:
            yield (Tokens.expression, line.strip(' ;\t'))


def parse_makefile(filename: str, make_args=()) -> List[Tuple[Tokens, Dict[str, Any]]]:
    """Read GNU Make's final rule database for a named Makefile.

    GNU Make, rather than this parser, evaluates conditionals, variables and
    includes. ``--question`` keeps the database probe from running ordinary
    targets, but GNU Make may still remake stale included makefiles before
    printing the database (see docs/profiling-and-includes.md).  Parsing is
    therefore not side-effect-free; with goals the traced build already remade
    them, and ``--preprocess_only`` may remake them as well.
    """
    extra_args = [
        '--no-builtin-rules',
        '--no-builtin-variables',
        '--print-data-base',
        '--question',
    ]
    if not has_makefile_argument(make_args):
        extra_args += ['--file', filename]
    command = make_command(make_args, extra_args)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        # Same Makefile-visible locale as the traced build (profile_target uses
        # keep_recipe_locale=True): an include or conditional using $(LC_ALL)
        # must see the same environment in both Make processes.
        env=make_environment(keep_recipe_locale=True),
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )

    ast = []
    source_variable = False
    for line in result.stdout.splitlines():
        if line.startswith('# makefile ') or line.startswith('# command line'):
            source_variable = True
            continue
        if line.startswith('#'):
            source_variable = False
            continue
        if source_variable and line:
            ast.append((Tokens.expression, line))
            source_variable = False

    in_files = False
    skip_target = False
    current_target = None
    # Rules without a recipe carry no source annotation; they come from the
    # first Makefile Make was told to read (``-f``/``--file``/``--makefile``).
    default_source = next(iter(makefile_arguments(make_args)), filename)
    current_source = default_source
    source_docs = {}
    working_directory = make_working_directory(make_args)
    include_directories = make_include_directories(make_args, working_directory)

    def docs_for(source):
        paths = [source] if os.path.isabs(source) else [
            os.path.join(working_directory, source),
            *(os.path.join(directory, source) for directory in include_directories),
        ]
        path = next((candidate for candidate in paths if os.path.exists(candidate)), paths[0])
        if path not in source_docs:
            source_docs[path] = documented_targets(path)
        return source_docs[path]

    all_docs = {}
    grouped_entries = {}
    for makefile in included_makefiles(result.stdout, working_directory):
        all_docs.update(docs_for(makefile))

    def flush_target():
        nonlocal current_target
        if current_target is not None:
            docs = docs_for(current_source)
            current_target['docs'] = next(
                (
                    docs.get(target, all_docs.get(target, ''))
                    for target in current_target['all_targets']
                    if target in docs or target in all_docs
                ),
                '',
            )
            members = frozenset(current_target['all_targets'])
            if current_target['grouped'] and members in grouped_entries:
                # GNU Make prints a grouped rule once per member; the recipe
                # is attached to only one of those records, so merge them.
                entry = grouped_entries[members]
                entry['body'] = entry['body'] or current_target['body']
                entry['docs'] = entry['docs'] or current_target['docs']
            else:
                if current_target['grouped']:
                    grouped_entries[members] = current_target
                ast.append((Tokens.target, current_target))
            current_target = None

    for line in result.stdout.splitlines():
        if line == '# Files':
            in_files = True
            continue
        if not in_files:
            continue
        if line.startswith('# files hash-table stats:'):
            flush_target()
            break
        if line.startswith('# Not a target:'):
            flush_target()
            skip_target = True
            continue
        if line.startswith("#  recipe to execute (from '"):
            current_source = line[len("#  recipe to execute (from '"):].rsplit("', line ", 1)[0]
            continue
        if line.startswith('#  Also makes:'):
            # Grouped targets (``a b &: c``) are printed as one ``a: c`` record
            # per member, each followed by this comment listing the group.
            if current_target is not None:
                current_target['grouped'] = True
                current_target['all_targets'] = sorted(line.split(':', 1)[1].split())
                current_target['target'] = current_target['all_targets'][0]
            continue
        if line.startswith('#') or not line:
            continue
        if line.startswith('\t'):
            if current_target is not None:
                current_target['body'].append((Tokens.command, line.strip(' ;\t')))
            continue
        if ':' not in line or '=' in line:
            continue

        flush_target()
        if skip_target:
            skip_target = False
            continue

        sep = '&:' if '&:' in line else ':'
        target_part, rest = line.split(sep, 1)
        targets = target_part.strip().split()
        if not targets:
            continue
        rest = rest.strip()
        if '|' in rest:
            deps_part, order_part = rest.split('|', 1)
            order_deps = sorted(order_part.strip().split()) if order_part.strip() else []
        else:
            deps_part = rest
            order_deps = []
        current_target = {
            'target': targets[0],
            'all_targets': targets,
            'grouped': sep == '&:',
            'deps': [sorted(deps_part.split()), order_deps],
            'docs': '',
            'body': [],
        }
        current_source = default_source

    return ast


def parse(
    fd: TextIO,
    is_check_loop: bool = True,
    loop_check_depth: int = 20,
    make_args=(),
) -> List[Tuple[Tokens, Dict[str, Any]]]:
    ast = []

    make_full_text = fd.read()
    if any(INCLUDE_DIRECTIVE.match(line) for line in make_full_text.splitlines()):
        source_path = getattr(fd, 'name', None)
        if not isinstance(source_path, str):
            raise ValueError('A Makefile with include directives must be read from a named file.')
        return parse_makefile(source_path, make_args)

    it = peekable(tokenizer(io.StringIO(make_full_text)))

    def parse_target(token: Tuple[Tokens, str]):
        """Parse a Makefile rule and store it in the AST.

        Supports both traditional ``target:`` syntax and ``&:`` grouped
        targets introduced in GNU make 4.3. When ``&:`` is used all targets
        in the list are produced by a single recipe execution.
        """
        line = token[1]

        sep = '&:' if '&:' in line else ':'
        target_part, rest = line.split(sep, 1)
        targets = target_part.strip().split()

        docs = ''
        if '##' in rest:
            rest, docs = rest.split('##', 1)
            docs = docs.strip()

        rest = rest.strip()
        if '|' in rest:
            deps_part, order_part = rest.split('|', 1)
            order_deps = sorted(order_part.strip().split()) if order_part.strip() else []
        else:
            deps_part = rest
            order_deps = []
        deps = sorted(deps_part.strip().split()) if deps_part.strip() else []

        body = parse_body()
        ast.append(
            (
                token[0],
                {
                    'target': targets[0],
                    'all_targets': targets,
                    'grouped': sep == '&:',
                    'deps': [deps, order_deps],
                    'docs': docs,
                    'body': body
                }
            )
        )

    def next_belongs_to_target() -> bool:
        token, _ = it.peek()
        return token == Tokens.command

    def parse_body() -> List[Tuple[Tokens, str]]:
        body = []
        try:
            while next_belongs_to_target():
                body.append(next(it))
        except StopIteration:
            pass
        return body

    for token in it:
        if token[0] == Tokens.target:
            parse_target(token)
        else:
            # expression
            ast.append(token)

    return ast


def get_dependencies_influences(ast: List[Tuple[Tokens, Dict[str, Any]]]):
    dependencies = {}
    influences = collections.defaultdict(set)
    order_only = set()
    indirect_influences = collections.defaultdict(set)

    alias_map = {}
    for item_t, item in ast:
        if item_t != Tokens.target:
            continue
        targets = item.get('all_targets', [item['target']])
        canonical = targets[0]
        for alias_name in targets[1:]:
            alias_map[alias_name] = canonical

    def alias(name: str) -> str:
        return alias_map.get(name, name)

    for item_t, item in ast:
        if item_t != Tokens.target:
            continue
        target = alias(item['target'])
        deps, order_deps = item['deps']
        deps = [alias(d) for d in deps]
        order_deps = [alias(d) for d in order_deps]

        if target in ('.PHONY',):
            continue

        dependencies[target] = [deps, order_deps]

        # influences
        influences[target]
        for k in deps:
            influences[k].add(target)
        for k in order_deps:
            influences[k]
        order_only.update(order_deps)

    # Cache previously calculated descendants to avoid quadratic behaviour on
    # large graphs. ``descendants(target)`` returns a set of all nodes reachable
    # from ``target`` including direct children.
    cached_descendants = {}

    def descendants(target):
        if target in cached_descendants:
            return cached_descendants[target]
        result = set(influences[target])
        for child in influences[target]:
            result.update(descendants(child))
        cached_descendants[target] = result
        return result

    for original_target in influences:
        indirect_influences[original_target] = (
            descendants(original_target) - influences[original_target]
        )

    return dependencies, influences, order_only, indirect_influences
