import collections
import re
import tempfile

from enum import Enum
from typing import Any, Dict, Generator, List, Tuple, TextIO, Pattern

from more_itertools import peekable


class Tokens(str, Enum):
    target = "target"
    command = "command"
    expression = "expression"


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


def parse(fd: TextIO, is_check_loop: bool = True, loop_check_depth: int = 20) -> List[Tuple[Tokens, Dict[str, Any]]]:
    ast = []

    def insert_included_files(open_file: TextIO, is_check_include_loop: bool = True, loop_check_depth: int = 20) -> TextIO:
        
        # create nested function to check if list of rows cointains include instructions
        def check_for_includes(input_make: str, regular_expression: Pattern[str]) -> bool:
            # create list of rows from input make
            list_of_rows = input_make.split('\n')
            # find rows which consist include instruction and replace multiple spaces with one
            matches = [re.sub(' +', ' ', string) for string in list_of_rows if re.match(regular_expression, string)]

            if len(matches) != 0:
                return True
            else:
                return False

        # create nested function to be able insert included makes recursively
        def replace_include_with_file(
            input_make: str, regular_expression: Pattern[str]
        ) -> str:
            output_make = input_make
            # create list of rows from input make
            list_of_rows = input_make.split('\n')
            # iterate through input makefile
            for x in range(0,len(list_of_rows)):
                # check if string contains include instruction
                if re.match(regular_expression, list_of_rows[x]):
                    # get names of makefiles from instruction
                    instruction = re.sub(' +', ' ', list_of_rows[x].split('include ')[1])
                    # split list of included makes to support multiple entrance
                    makes = instruction.split(' ')
        
                    # initialize empty list to store included instructions as string
                    included_instructions = []   
                    # iterate through files from instruction add write them to list
                    for make in makes:
                        with open(make, 'r') as fp:
                            included_instructions.append(fp.read())
        
                    # transform list into single string
                    included_string = '\n'.join(included_instructions)
        
                    # replace include with included instruction
                    output_make = input_make.replace(
                        list_of_rows[x], included_string, 1
                    )

            return output_make

        make_full_text = open_file.read()

        # compile regex to find include instructions
        regex = re.compile(' *s?-?include +')
        
        loop_detector = 0
        
        # recursively check makefile to check include instruction in included files
        while check_for_includes(make_full_text, regex):
            make_full_text = replace_include_with_file(make_full_text, regex)
            loop_detector += 1
            if loop_detector > loop_check_depth and is_check_include_loop:
                raise Exception('Your make chain is looped or too deep (default depth = 20). if you have nesting depth > 20 please set your value with --include_depth option or turn off loop checking with --disable_loop_detection')

        # create temporary file
        tmp = tempfile.NamedTemporaryFile(mode = 'w+t')
        tmp_name=tmp.name
        tmp.close() #on windows it is not possible to open file for writing twice. So file needs to be closed.

        # open temporary file and write composed make to them
        with open(tmp_name, 'w') as temp_input_file:
            temp_input_file.write(make_full_text)

        # open temporary file as <class '_io.TextIOWrapper'> to support type compatibiluty with tokenizer() 
        temp_make_file = open(tmp_name, 'r')
        
        return temp_make_file


    it = peekable(tokenizer(insert_included_files(fd, is_check_loop, loop_check_depth)))

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
            order_deps = order_part.strip().split() if order_part.strip() else []
        else:
            deps_part = rest
            order_deps = []
        deps = deps_part.strip().split() if deps_part.strip() else []

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
    """Build the dependency graph consumed by :func:`export_dot`.

    Repeated explicit single-colon rules merge and deduplicate their
    prerequisites in first-seen order.  A prerequisite seen normally is
    normal everywhere, even if another rule lists it as order-only.  Pattern
    rules remain alternatives, so their latest definition overwrites the
    stored prerequisite lists rather than merging them.  Both graph directions
    describe that same stored alternative.

    Returns ``(dependencies, influences, order_only, indirect_influences)``.
    ``order_only`` contains only prerequisites that were never normal;
    ``export_dot`` uses it to hide otherwise-unconnected order-only nodes.
    """
    dependencies = {}
    dependency_membership = {}
    influences = collections.defaultdict(set)
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

    # A declared target remains a graph node even when a later pattern-rule
    # alternative drops its last incoming edge.  ``descendants`` below walks
    # every node, so deleting such a node can mutate ``influences`` mid-walk.
    declared_targets = {
        alias(item['target'])
        for item_t, item in ast
        if item_t == Tokens.target and item['target'] != '.PHONY'
    }

    for item_t, item in ast:
        if item_t != Tokens.target:
            continue
        target = alias(item['target'])
        deps, order_deps = item['deps']
        deps = [alias(d) for d in deps]
        order_deps = [alias(d) for d in order_deps]

        if target in ('.PHONY',):
            continue

        # Pattern rules are alternatives, not repeated explicit rules.  Keep
        # the base overwrite representation instead of applying explicit-rule
        # prerequisite accumulation to them.  Remove reverse edges from the
        # superseded alternative so critical-path traversal never reaches a
        # dependency absent from the forward graph.
        is_pattern_rule = any('%' in name for name in item.get('all_targets', []))
        if is_pattern_rule:
            previous_deps, _previous_order_deps = dependencies.get(target, [[], []])
            for dep in set(previous_deps) - set(deps):
                influences[dep].discard(target)

            normal_dep_names = set(deps)
            order_deps = [dep for dep in order_deps if dep not in normal_dep_names]
            dependencies[target] = [deps, order_deps]
            influences[target]
            for dep in deps:
                influences[dep].add(target)
            for dep in order_deps:
                influences[dep]
            continue

        # GNU Make combines prerequisites from repeated single-colon rules.
        # Keep the reverse graph aligned with the forward influences below so
        # critical-path traversal can return through every incoming edge.
        existing_deps, existing_order_deps = dependencies.setdefault(target, [[], []])
        if target not in dependency_membership:
            dependency_membership[target] = (set(existing_deps), set(existing_order_deps))
        known_dep_names, known_order_dep_names = dependency_membership[target]
        for dep in deps:
            if dep in known_order_dep_names:
                known_order_dep_names.remove(dep)
            if dep not in known_dep_names:
                existing_deps.append(dep)
                known_dep_names.add(dep)
        for dep in order_deps:
            if dep in known_dep_names or dep in known_order_dep_names:
                continue
            existing_order_deps.append(dep)
            known_order_dep_names.add(dep)

        # influences
        influences[target]
        for k in deps:
            influences[k].add(target)
        for k in order_deps:
            influences[k]

    # Promotions above only update membership sets: removing an item from the
    # ordered list for every promotion would repeatedly scan that list.  Filter
    # each target once instead, retaining the first-seen order of surviving
    # order-only prerequisites.
    for target, (_known_deps, known_order_deps) in dependency_membership.items():
        existing_order_deps = dependencies[target][1]
        dependencies[target][1] = [
            dep for dep in existing_order_deps if dep in known_order_deps
        ]

    # Classify prerequisites from the completed forward graph so a superseded
    # pattern alternative cannot keep a stale normal or order-only node.
    normal_dependencies = {
        dependency
        for normal_deps, _order_deps in dependencies.values()
        for dependency in normal_deps
    }
    order_only = {
        dependency
        for _normal_deps, order_deps in dependencies.values()
        for dependency in order_deps
    }
    order_only.difference_update(normal_dependencies)

    # Pattern replacements leave empty reverse nodes behind until the complete
    # forward graph is known.  Clean them once here instead of repeatedly
    # rescanning all dependencies for every replacement.  Declared targets and
    # live normal/order-only prerequisites must remain graph nodes even if they
    # have no outgoing influence edges.
    live_nodes = declared_targets | normal_dependencies | order_only
    for node in list(influences):
        if not influences[node] and node not in live_nodes:
            del influences[node]

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
