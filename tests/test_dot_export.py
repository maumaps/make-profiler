import io
from make_profiler.dot_export import critical_path, export_dot
from make_profiler import parser

def build_sample():
    influences = {'b': {'a'}, 'a': set()}
    dependencies = {'a': [['b'], []], 'b': [[], []]}
    order_only = set()
    performance = {}
    indirect = {'a': set(), 'b': set()}
    docs = {'a': 'A doc', 'b': 'B doc'}
    return influences, dependencies, order_only, performance, indirect, docs

def test_export_contains_edge_and_clusters():
    inf, deps, order, perf, ind, docs = build_sample()
    f = io.StringIO()
    export_dot(f, inf, deps, order, perf, ind, docs)
    data = f.getvalue()
    assert 'b -> a' in data
    assert 'subgraph cluster_inputs' in data
    assert 'subgraph cluster_result' in data


def test_export_escapes_special_chars():
    inf = {'a"b': set()}
    deps = {'a"b': [[], []]}
    order = set()
    perf = {}
    ind = {'a"b': set()}
    docs = {'a"b': 'A "quote" doc'}
    f = io.StringIO()
    export_dot(f, inf, deps, order, perf, ind, docs)
    data = f.getvalue()
    assert '"a\\"b"' in data
    assert 'A \\"quote\\" doc' in data


def test_critical_path_handles_final_targets():
    """Ensure graph export works when a root target has no dependents."""
    inf = {'a': set()}
    deps = {'a': [[], []]}
    f = io.StringIO()
    export_dot(f, inf, deps, set(), {}, {'a': set()}, {'a': 'Doc'})
    data = f.getvalue()
    assert 'subgraph cluster_tools' in data


def test_critical_path_handles_disconnected_components():
    """Disconnected Makefile components retain their complete critical paths."""
    ast = parser.parse(io.StringIO('all: input\ninput:\norphan:\n'))
    dependencies, influences, _order_only, _indirect = parser.get_dependencies_influences(ast)

    critical, _ = critical_path(influences, dependencies, {'input', 'orphan'}, {})

    assert critical == {'all', 'input', 'orphan'}, (
        "Disconnected components must propagate late starts through their own "
        f"valid reverse edges; got {critical!r} from {dependencies!r}."
    )


def test_repeated_target_rules_merge_dependencies_for_critical_path():
    """Repeated GNU Make rules must preserve every dependency branch."""
    ast = parser.parse(io.StringIO('all: a\nall: b\na:\nb:\n'))
    dependencies, influences, _order_only, _indirect = parser.get_dependencies_influences(ast)

    critical, _ = critical_path(influences, dependencies, {'a', 'b'}, {})

    assert dependencies['all'] == [['a', 'b'], []], (
        "Repeated single-colon rules must combine normal prerequisites in the "
        f"reverse graph; got {dependencies['all']!r}."
    )
    assert critical == {'a', 'all', 'b'}, (
        "Both equally long branches of repeated target 'all' must stay "
        f"critical; got {critical!r} from {dependencies!r}."
    )


def test_repeated_target_rules_preserve_dependency_order_and_deduplication():
    """Repeated GNU Make rules must keep first-seen prerequisite order."""
    ast = parser.parse(
        io.StringIO(
            'all: c a | stamp\n'
            'all: b a | stamp cache\n'
            'a:\n'
            'b:\n'
            'c:\n'
            'stamp:\n'
            'cache:\n'
        )
    )
    dependencies, influences, order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['all'] == [['c', 'a', 'b'], ['stamp', 'cache']], (
        "Repeated target rules must deduplicate prerequisites while preserving "
        f"their first-seen source order; got {dependencies['all']!r}."
    )
    assert influences['a'] == {'all'}, (
        "Normal prerequisite 'a' should influence 'all' exactly once after "
        f"deduplication; got {influences['a']!r}."
    )
    assert influences['b'] == {'all'}, (
        "Normal prerequisite 'b' should remain connected to 'all'; got "
        f"{influences['b']!r}."
    )
    assert influences['c'] == {'all'}, (
        "Normal prerequisite 'c' should remain connected to 'all'; got "
        f"{influences['c']!r}."
    )
    assert 'all' not in influences['stamp'], (
        "Order-only prerequisite 'stamp' must not become a normal reverse "
        f"edge; got {influences['stamp']!r}."
    )
    assert order_only == {'stamp', 'cache'}, (
        "Order-only prerequisites must still be tracked separately; got "
        f"{order_only!r}."
    )


def test_repeated_target_rules_preserve_source_order():
    """Repeated rules retain GNU Make's prerequisite encounter order."""
    ast = parser.parse(io.StringIO('all: z a\nall: y\nz:\na:\ny:\n'))
    dependencies, _influences, _order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['all'] == [['z', 'a', 'y'], []], (
        "Repeated target rules must retain source prerequisite order, matching "
        f"GNU Make's z, a, y processing order; got {dependencies['all']!r}."
    )


def test_pattern_rule_alternatives_keep_graph_directions_consistent():
    """Every pattern alternative must have matching forward and reverse edges."""
    ast = parser.parse(io.StringIO('%.o: %.c\n%.o: %.s\n'))
    dependencies, influences, order_only, indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.o'] == [['%.c', '%.s'], []], (
        "Pattern alternatives must be preserved as a union so an applicable "
        f"earlier rule is never omitted; got {dependencies['%.o']!r}."
    )
    assert influences['%.c'] == {'%.o'}, (
        "The earlier pattern alternative must retain its reverse edge; got "
        f"{influences!r}."
    )
    assert influences['%.s'] == {'%.o'}, (
        "The later pattern alternative must retain its reverse edge; got "
        f"{influences!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, indirect, {})
    data = f.getvalue()

    assert '"%.s" -> "%.o"' in data and '"%.c" -> "%.o"' in data, (
        "DOT export must render every stored pattern alternative; got "
        f"{data!r}."
    )


def test_pattern_alternatives_preserve_earlier_applicable_rule():
    """An earlier applicable pattern rule must survive a later alternative."""
    ast = parser.parse(io.StringIO('%.out: %.first\n%.out: %.second\n'))
    dependencies, influences, _order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.out'] == [['%.first', '%.second'], []], (
        "GNU Make may select the earlier pattern rule through implicit-rule "
        f"search, so it must be preserved; got {dependencies['%.out']!r}."
    )
    assert influences['%.first'] == {'%.out'}, (
        "The earlier applicable alternative must keep its reverse edge; got "
        f"{influences!r}."
    )


def test_pattern_order_only_prerequisite_is_registered_for_export():
    """Every pattern alternative contributes order-only nodes."""
    ast = parser.parse(
        io.StringIO(
            '%.o: | obsolete\n'
            '%.o: | stamp\n'
            'all: %.o | stamp\n'
            'stamp:\n'
        )
    )
    dependencies, influences, order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.o'] == [[], ['obsolete', 'stamp']], (
        "Pattern order-only alternatives must be preserved as a union; "
        f"got {dependencies['%.o']!r}."
    )
    assert 'stamp' in influences and influences['stamp'] == set(), (
        "Pattern order-only prerequisites must be registered as graph nodes; "
        f"got {influences!r}."
    )
    assert 'obsolete' in influences and influences['obsolete'] == set(), (
        "An earlier pattern order-only alternative must remain in the graph; "
        f"got influences={influences!r}."
    )
    assert order_only == {'obsolete', 'stamp'}, (
        "Every live pattern order-only prerequisite must remain globally "
        f"classified; got {order_only!r} from {dependencies!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, _indirect, {})


def test_pattern_normal_prerequisite_wins_over_alternative_order_only():
    """A normal pattern alternative must dominate an order-only alternative."""
    ast = parser.parse(
        io.StringIO('%.o: shared\n%.o: | shared\nall: %.o\nshared:\n')
    )
    dependencies, influences, order_only, indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.o'] == [['shared'], []], (
        "A prerequisite seen normally in any pattern alternative is normal "
        f"everywhere; got {dependencies['%.o']!r}."
    )
    assert influences['shared'] == {'%.o'}, (
        "The normal pattern alternative must retain its reverse edge; got "
        f"{influences!r}."
    )
    assert order_only == set(), (
        "A normal pattern prerequisite cannot remain globally order-only; got "
        f"{order_only!r} from {dependencies!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, indirect, {})
    data = f.getvalue()

    assert 'shared -> "%.o"' in data, (
        "DOT must render the normal edge for the preserved pattern "
        f"alternative; got {data!r}."
    )


def test_pattern_normal_prerequisite_wins_over_same_rule_order_only_entry():
    """A prerequisite cannot be both normal and order-only for one pattern."""
    ast = parser.parse(io.StringIO('%.o: shared | shared\nall: %.o\nshared:\n'))
    dependencies, influences, order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.o'] == [['shared'], []], (
        "A normal pattern prerequisite must remove its duplicate order-only "
        f"entry; got dependencies={dependencies!r}."
    )
    assert influences['shared'] == {'%.o'}, (
        "The shared prerequisite must retain exactly its normal reverse edge; "
        f"got influences={influences!r}."
    )
    assert order_only == set(), (
        "A normal pattern prerequisite cannot remain globally order-only; got "
        f"order_only={order_only!r} from dependencies={dependencies!r}."
    )


def test_pattern_alternative_cleanup_keeps_cross_pattern_order_only_node():
    """Union alternatives must not erase another pattern's node."""
    ast = parser.parse(
        io.StringIO(
            '%.a: | shared\n'
            '%.b: shared\n'
            '%.b: other\n'
            'all: %.a %.b\n'
            'other:\n'
        )
    )
    dependencies, influences, order_only, indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.b'] == [['shared', 'other'], []], (
        "Pattern alternatives must be preserved as a union; got "
        f"dependencies={dependencies!r}."
    )
    assert dependencies['%.a'] == [[], ['shared']], (
        "The unrelated pattern's order-only prerequisite must survive the "
        f"union; got dependencies={dependencies!r}."
    )
    assert influences['shared'] == {'%.b'}, (
        "A prerequisite seen normally in any alternative is normal everywhere; "
        f"got influences={influences!r}."
    )
    assert order_only == set(), (
        "A normal pattern prerequisite cannot remain globally order-only; got "
        f"order_only={order_only!r}, dependencies={dependencies!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, indirect, {})
    data = f.getvalue()

    assert 'shared -> "%.b"' in data, (
        "The preserved normal pattern edge must be exported; got DOT={data!r}."
    )


def test_pattern_alternative_cleanup_keeps_declared_target_node():
    """Union alternatives must keep every declared prerequisite node."""
    ast = parser.parse(
        io.StringIO(
            'src:\n'
            'foo: src\n'
            '%.a: foo\n'
            '%.a: bar\n'
            'all: %.a\n'
            'bar:\n'
        )
    )
    dependencies, influences, order_only, indirect = parser.get_dependencies_influences(ast)

    assert dependencies['%.a'] == [['foo', 'bar'], []], (
        "Pattern alternatives must be preserved as a union; got "
        f"dependencies={dependencies!r}."
    )
    assert influences['foo'] == {'%.a'}, (
        "A preserved pattern alternative must retain its reverse edge; got "
        f"influences={influences!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, indirect, {})
    data = f.getvalue()

    assert 'foo -> "%.a"' in data, (
        "The preserved pattern edge must be exported; got DOT={data!r}."
    )
    assert 'foo [label=foo' in data, (
        "The declared target must remain exportable with the union; "
        f"got DOT={data!r}."
    )


def test_normal_pattern_prerequisite_dominates_explicit_order_only_export():
    """A normal pattern prerequisite must never be globally order-only."""
    ast = parser.parse(io.StringIO('%.o: shared\nall: %.o | shared\nshared:\n'))
    dependencies, influences, order_only, indirect = parser.get_dependencies_influences(ast)

    assert order_only == set(), (
        "A normal pattern prerequisite must promote the shared prerequisite "
        f"globally; got order_only={order_only!r} for {dependencies!r}."
    )

    f = io.StringIO()
    export_dot(f, influences, dependencies, order_only, {}, indirect, {})
    data = f.getvalue()

    assert 'subgraph cluster_order_only' not in data, (
        "export_dot must not emit an order-only cluster once 'shared' is "
        f"promoted by the pattern rule; got {data!r}."
    )
    assert 'shared [label=shared' in data, (
        "export_dot must retain the promoted prerequisite as a visible node; "
        f"got {data!r}."
    )


def test_normal_prerequisite_dominates_order_only_on_repeated_rules():
    """GNU Make treats an overlapping normal prerequisite as normal."""
    ast = parser.parse(io.StringIO('all: input | stamp\nall: stamp\ninput:\nstamp:\n'))
    dependencies, influences, order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['all'] == [['input', 'stamp'], []], (
        "Normal prerequisites must absorb an overlapping order-only entry; "
        f"got {dependencies['all']!r}."
    )
    assert order_only == set(), (
        "A prerequisite promoted to normal must not remain order-only; got "
        f"{order_only!r}."
    )
    assert influences['stamp'] == {'all'}, (
        "The promoted prerequisite must retain its normal reverse edge; got "
        f"{influences['stamp']!r}."
    )


def test_order_only_promotions_preserve_remaining_first_seen_order():
    """Promotion must filter order-only prerequisites without reordering survivors."""
    ast = parser.parse(
        io.StringIO(
            'all: | alpha beta gamma\n'
            'all: beta zeta | delta\n'
            'alpha:\n'
            'beta:\n'
            'gamma:\n'
            'zeta:\n'
            'delta:\n'
        )
    )
    dependencies, influences, order_only, _indirect = parser.get_dependencies_influences(ast)

    assert dependencies['all'] == [['beta', 'zeta'], ['alpha', 'gamma', 'delta']], (
        "Promoted 'beta' must move to normal prerequisites while remaining "
        "order-only prerequisites retain their first-seen order; got "
        f"{dependencies['all']!r}."
    )
    assert influences['beta'] == {'all'}, (
        "Promoted 'beta' must provide one normal reverse edge; got "
        f"{influences['beta']!r}."
    )
    assert order_only == {'alpha', 'gamma', 'delta'}, (
        "Only unpromoted prerequisites may remain globally order-only; got "
        f"{order_only!r}."
    )


def test_critical_path_keeps_shorter_branch_noncritical():
    """A complete reverse graph must not make genuinely shorter paths critical."""
    ast = parser.parse(io.StringIO('all: slow fast\nslow:\nfast:\n'))
    dependencies, influences, _order_only, _indirect = parser.get_dependencies_influences(ast)
    timing = {'slow': {'timing_sec': 5}, 'fast': {'timing_sec': 1}}

    critical, _ = critical_path(influences, dependencies, {'slow', 'fast'}, timing)

    assert critical == {'all', 'slow'}, (
        "Only the longest branch should be critical after reverse-edge "
        f"propagation; got {critical!r} with timing {timing!r}."
    )


def test_example_makefile_from_readme():
    with open('test/example.mk', encoding='utf-8') as fh:
        ast = parser.parse(fh)
    deps, influences, order_only, indirect = parser.get_dependencies_influences(ast)
    docs = {i[1]['target']: i[1]['docs'] for i in ast if i[0] == parser.Tokens.target}
    f = io.StringIO()
    export_dot(f, influences, deps, order_only, {}, indirect, docs)
    data = f.getvalue()
    assert 'target1 -> all' in data
    assert 'subgraph cluster_inputs' in data
    assert 'label=Input' in data


def test_current_targets_are_highlighted():
    inf, deps, order, _, ind, docs = build_sample()
    perf = {
        'a': {'done': True, 'failed': False, 'isdir': False, 'current': True},
        'b': {'done': True, 'failed': False, 'isdir': False, 'current': False},
    }
    f = io.StringIO()
    export_dot(f, inf, deps, order, perf, ind, docs)
    data = f.getvalue()
    assert 'fillcolor="#0969DA"' in data
    assert 'fontcolor="#fff"' in data


def test_current_run_critical_path_colored():
    inf, deps, order, _, ind, docs = build_sample()
    perf = {
        'a': {'done': True, 'failed': False, 'isdir': False, 'current': True, 'timing_sec': 2},
        'b': {'done': True, 'failed': False, 'isdir': False, 'current': True, 'timing_sec': 1},
    }
    f = io.StringIO()
    export_dot(f, inf, deps, order, perf, ind, docs)
    data = f.getvalue()
    assert 'color="#800080"' in data


def test_grouped_targets_render_once() -> None:
    """Grouped targets produce one node in the DOT output."""
    mk = (
        "a b &:\n"
        "\t@true\n"
        "c: a\n"
        "\t@true\n"
        "d: b\n"
        "\t@true\n"
    )
    ast = parser.parse(io.StringIO(mk))
    deps, inf, order, indirect = parser.get_dependencies_influences(ast)
    docs = {t[1]['target']: t[1]['docs'] for t in ast if t[0] == parser.Tokens.target}
    f = io.StringIO()
    export_dot(f, inf, deps, order, {}, indirect, docs)
    data = f.getvalue()
    assert 'a -> c' in data
    assert 'a -> d' in data
    assert 'b ->' not in data  # b is merged with a
