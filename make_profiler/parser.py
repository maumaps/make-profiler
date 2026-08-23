import collections
import io
import os
import re
import subprocess

from enum import Enum
from typing import Any, Dict, Generator, List, Tuple, TextIO

from more_itertools import peekable


class Tokens(str, Enum):
    target = "target"
    command = "command"
    expression = "expression"


INCLUDE_DIRECTIVE = re.compile(r'^[ ]*(?:-?include|sinclude)\s+')


def make_command(make_args, extra_args):
    """Insert internal GNU Make options before its optional ``--`` separator."""
    try:
        separator = make_args.index('--')
    except ValueError:
        return ['make', *make_args, *extra_args]
    return ['make', *make_args[:separator], *extra_args, *make_args[separator:]]


def make_working_directory(make_args) -> str:
    """Return GNU Make's effective working directory after its ``-C`` options."""
    directory = os.getcwd()
    arguments = iter(make_args)
    for argument in arguments:
        if argument == '--':
            break
        if argument in ('-C', '--directory'):
            directory = os.path.abspath(os.path.join(directory, next(arguments)))
        elif argument.startswith('-C') and len(argument) > 2:
            directory = os.path.abspath(os.path.join(directory, argument[2:]))
        elif argument.startswith('--directory='):
            directory = os.path.abspath(os.path.join(directory, argument.split('=', 1)[1]))
    return directory


def make_include_directories(make_args, working_directory) -> List[str]:
    """Return GNU Make's include search directories in command-line order."""
    directories = []
    arguments = iter(make_args)
    for argument in arguments:
        if argument == '--':
            break
        if argument in ('-I', '--include-dir'):
            directory = next(arguments)
        elif argument.startswith('-I') and len(argument) > 2:
            directory = argument[2:]
        elif argument.startswith('--include-dir='):
            directory = argument.split('=', 1)[1]
        else:
            continue
        directories.append(
            directory if os.path.isabs(directory) else os.path.join(working_directory, directory)
        )
    return directories


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
    while value:
        boundaries = [index for index, character in enumerate(value) if character.isspace()]
        boundaries.append(len(value))
        candidates = []
        for boundary in boundaries:
            candidate = value[:boundary].replace('\\ ', ' ')
            path = candidate if os.path.isabs(candidate) else os.path.join(working_directory, candidate)
            if os.path.isfile(path):
                candidates.append((boundary, path))
        if not candidates:
            break
        boundary, path = candidates[-1]
        makefiles.append(path)
        value = value[boundary:].lstrip()
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
    includes. ``--question`` keeps the database probe from running targets.
    """
    command = make_command(make_args, [
        '--no-builtin-rules',
        '--no-builtin-variables',
        '--print-data-base',
        '--question',
    ])
    has_makefile_argument = False
    for argument in make_args:
        if argument == '--':
            break
        if argument in ('-f', '--file', '--makefile') or argument.startswith('-f'):
            has_makefile_argument = True
            break
    if not has_makefile_argument:
        command = make_command(
            make_args,
            [
                '--no-builtin-rules',
                '--no-builtin-variables',
                '--print-data-base',
                '--question',
                '--file',
                filename,
            ],
        )
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
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
    current_source = filename
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
        current_source = filename

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
