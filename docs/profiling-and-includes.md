# Includes and GNU Make arguments

`profile_make` does not parse Makefiles itself.
GNU Make evaluates the Makefile, so everything Make understands is profiled and drawn as Make sees it.

## What is supported

- `include`, `-include`, `sinclude`, including paths built from `$(MAKEFILE_LIST)`, variables and conditionals.
- Makefiles that Make remakes through an include.
- Grouped targets (`a b &: c`) and order-only prerequisites (`|`).
- Make options after the profiler's own options are passed through unchanged:
  `-C`/`--directory`, `-f`/`--file`/`--makefile`, `-I`/`--include-dir`, `-j`, `-k`, `-n`, variable assignments (`CC=gcc`) and `--eval`.
  A `--` ends option parsing; everything after it is a goal, even `-db` or `--foo`.

## How a run works

1. With no goal (`profile_make`) only the graph is drawn from `make --print-data-base --question`.
2. With goals, `make --trace` runs them and each `update target 'x'` line starts a record in `make_profile.db`.
   Output between two trace lines goes to `logs/<run>/<target>/log.txt` with timestamps.
   `logs/latest` points to the newest run.
3. When Make fails, the targets still running get a `failed.touch` marker next to their log
   and `profile_make` exits with Make's own status (usually 2).
4. The report pass reads the rule database again with the same arguments.

## Paths

With `-C dir`, `make_profile.db`, `logs/` and target files are looked up in `dir`, as Make does.
A relative `-db` path is relative to that directory too.
Descriptions (`## text`) are taken from the Makefile that Make reports as the source of each rule,
searched in the `-C` directory and then in `-I` directories.

## Limitations

- `--trace` announces only where a target starts.
  A target is finished when the next one starts or Make exits, so time spent in Make between recipes is counted in the previous target.
- With `-j` several targets run at once, so no finish can be inferred from the next start.
  All targets are closed when Make exits and a warning says timings are approximate (upper bounds).
- Dry runs (`-n`, `-q`, `-t`) execute nothing and write no records or logs.
- Make's messages are parsed in the `C` locale: the profiler sets `LC_ALL=C` for the database probe
  and `LC_MESSAGES=C` for profiled builds, so recipes keep your locale.
- `--disable_loop_detection` and `--include_depth` are accepted for compatibility and ignored.
