# Terminology

This project parses a Makefile into a dependency graph and exports it as an SVG report. The following terms are used throughout the code base.

## Target
A `target` is the left part of a Makefile rule before the colon. Each target may have commands associated with it and can depend on other targets.

## Dependencies
`dependencies` are the direct prerequisites of a target listed after the colon. They are returned by `parser.get_dependencies_influences` as a mapping of target name to a pair of lists. The first list contains normal dependencies; the second list contains order-only dependencies.

## Order-only dependencies
Order-only dependencies are written after `|` in a Makefile rule.
They enforce build order but do not trigger a rebuild when their timestamps change.
They are collected separately in the returned `order_only` set and displayed in a dedicated cluster in the generated graph.
The linter reports a violation when a target lists an existing directory as a normal dependency, because directories should only establish ordering guarantees and belong after the `|`.

## Influences
`influences` is a mapping from a dependency to all targets that directly depend on it. If `B` depends on `A`, then `A` influences `B`. This structure is used for traversing the dependency graph in the forward direction.

## Indirect influences
`indirect influences` describe transitive relationships. For a dependency `A`, the indirect influences set contains all targets reachable from `A` via more than one edge, excluding direct dependents. They are drawn as dashed arrows in the SVG to avoid clutter.

## Inputs
`inputs` are nodes with no incoming edges. They typically represent external files or sources provided to the pipeline.

## Critical path
The `critical path` is the longest sequence of dependent targets determined by `dot_export.critical_path`. These nodes are highlighted in the graph to show which steps limit overall execution time.

## Tools and results
Targets that nobody depends on are classified as results. Targets that have no dependencies and no dependents are classified as tools (for example, `clean`). These groupings control the visual clusters in the SVG output.

## Grouped targets

Using `&:` in a rule declares that several targets are produced by the same
command block. For example:

```make
foo bar &: deps
@echo building both
```

The profiler merges `foo` and `bar` into one node so the recipe appears only
once in the call graph. When multiple targets are listed with a plain `:`, Make
may run the recipe more than once in parallel; the linter reports this pattern
and suggests using `&:` instead.
