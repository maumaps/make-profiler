import argparse
import io
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime

from make_profiler.dot_export import export_dot, render_dot
from make_profiler.make_args import (
    has_makefile_argument,
    make_command,
    make_environment,
    make_goals,
    make_working_directory,
    scan_make_options,
)
from make_profiler.parser import get_dependencies_influences, parse_makefile
from make_profiler.preprocess import generate_makefile
from make_profiler.timing import parse_timing_db, analyze_target
from make_profiler.report_export import export_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('make_profiler')
# GNU Make 4.4 reports every rebuilt target as ``update target 'x' due to: ...``;
# 4.3 reports file-less targets (``.PHONY``, never created files) as
# ``target 'x' does not exist``.  Both mark the start of a target's recipes.
TRACE_TARGET = re.compile(r"^.*?:\d+: (?:update )?target '([^']+)'(?: due to:.*| does not exist)?$")
LOG_TIMESTAMP = '%Y-%m-%d %H:%M:%S'



def database_filename(filename, make_args):
    """Resolve relative profiling data paths like GNU Make resolves ``-C``."""
    if os.path.isabs(filename):
        return filename
    return os.path.join(make_working_directory(make_args), filename)


def make_arguments(argv):
    """Keep GNU Make arguments intact while removing profiler-only options."""
    profiler_options = {
        '--preprocess_only': 0,
        '-db': 1,
        '-p': 1,
        '-a': 1,
        '--analyze': 1,
        '--disable_loop_detection': 0,
        '--include_depth': 1,
    }
    result = []
    arguments = iter(argv)
    after_separator = False
    for argument in arguments:
        if after_separator:
            result.append(argument)
            continue
        if argument == '--':
            result.append(argument)
            after_separator = True
            continue
        option, separator, _ = argument.partition('=')
        if option not in profiler_options:
            result.append(argument)
            continue
        if separator:
            if profiler_options[option] == 0:
                raise ValueError(f'{option} does not accept a value.')
            continue
        for _ in range(profiler_options[option]):
            next(arguments)
    return result


def with_default_makefile(make_args, filename):
    """Preserve profile_make's historical ``Makefile`` default for every Make pass."""
    if has_makefile_argument(make_args):
        return make_args
    try:
        separator = make_args.index('--')
    except ValueError:
        return [*make_args, '--file', filename]
    return [*make_args[:separator], '--file', filename, *make_args[separator:]]


def profile_target(make_args, db_filename):
    """Run the requested target in GNU Make and record its traced recipes.

    ``--trace`` is emitted by the same Make process that evaluates and runs
    the target, so include expansion, makefile remakes and command-line
    options cannot diverge from the profiled build.

    Per target this writes ``start``/``finish`` records to the timing database
    and, like the former instrumented Makefile, ``logs/<run>/<target>/log.txt``
    (timestamped output) plus ``failed.touch`` for targets that were running
    when Make failed.  ``--trace`` only announces where a target's recipes
    begin, so a target ends when the next one begins.  With ``-j`` several
    targets run at once and that inference is impossible: each target is then
    closed when Make exits and the timings are approximate.  Dry runs
    (``-n``, ``-q``, ``-t``) execute no recipes and are never recorded.
    """
    options = scan_make_options(make_args)
    record_events = not options['dry_run']
    parallel = bool(options['jobs'])
    command = make_command(make_args, ['--trace'] if record_events else [])
    logger.info(' '.join(command))
    if record_events and parallel:
        logger.warning(
            'Parallel build (-j): --trace does not report when a recipe ends, '
            'so recorded target timings are approximate (upper bounds).')
    run_id = secrets.token_hex(10)
    directory = make_working_directory(make_args)
    active = {}  # target -> True while its recipes may still be running
    current_log = None
    output = []
    resolved_database_filename = database_filename(db_filename, make_args)

    def record(event, target_name):
        with open(resolved_database_filename, 'a') as database:
            database.write(f'{int(time.time())} {run_id} {event} {target_name}\n')

    def target_directory(target_name):
        # Same spelling parse_timing_db uses to find the log again.
        return os.path.join(directory, 'logs/%s/%s' % (run_id, target_name))

    def finish(target_name, failed=False):
        if failed:
            os.makedirs(target_directory(target_name), exist_ok=True)
            open(os.path.join(target_directory(target_name), 'failed.touch'), 'a').close()
        record('finish', target_name)
        del active[target_name]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=make_environment(keep_recipe_locale=True),
    )
    for line in process.stdout:
        trace_target = TRACE_TARGET.match(line) if record_events else None
        if trace_target:
            if not parallel:
                for previous in list(active):
                    finish(previous)
            target_name = trace_target.group(1)
            active[target_name] = True
            record('start', target_name)
            os.makedirs(target_directory(target_name), exist_ok=True)
            if current_log is not None:
                current_log.close()
            current_log = open(os.path.join(target_directory(target_name), 'log.txt'), 'a')
            latest = os.path.join(directory, 'logs', 'latest')
            try:
                os.makedirs(os.path.dirname(latest), exist_ok=True)
                if os.path.islink(latest):
                    os.remove(latest)
                if not os.path.lexists(latest):
                    os.symlink(run_id, latest)
            except OSError as error:
                logger.warning('Cannot update %s: %s', latest, error)
            continue
        output.append(line)
        print(line, end='')
        if current_log is not None:
            current_log.write(f'[{time.strftime(LOG_TIMESTAMP)}] {line}')
            current_log.flush()

    returncode = process.wait()
    if current_log is not None:
        current_log.close()
    # Whatever was still running when Make exited ends now, successfully or
    # not; on failure it is marked failed so the report does not show it as
    # still running.
    for target_name in list(active):
        finish(target_name, failed=returncode != 0)
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command, output=''.join(output))


def main(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser(
        description='Advanced Makefile processor')
    parser.add_argument(
        '--preprocess_only',
        dest='preprocess_only',
        action='store_true',
        help='Preprocess only')
    parser.add_argument(
        '-f',
        action='store',
        dest='in_filename',
        type=str,
        default='Makefile',
        help='Makefile to read (default %(default)s)')
    parser.add_argument(
        '-db',
        action='store',
        dest='db_filename',
        type=str,
        default='make_profile.db',
        help='Profile with timings')
    parser.add_argument(
        '-p',
        action='store',
        dest='svg_filename',
        type=str,
        default='make.svg',
        help='rendered report image filename (default: make.svg)')
    parser.add_argument(
        '-a',
        action='store',
        dest='after_date',
        type=datetime.fromisoformat,
        default=None,
        help='Render report image with full target time only after the specified date in iso format (other targets will have a time of 1s)')
    parser.add_argument(
        '--analyze',
        dest='analyze',
        metavar='TARGET',
        help='Analyze timing statistics for given target')
    parser.add_argument(
        '--disable_loop_detection',
        dest='disable_loop_detection',
        action='store_false',
        help='Deprecated no-op: GNU Make now resolves includes itself')
    parser.add_argument(
        '--include_depth',
        action='store',
        dest='include_depth',
        type=int,
        default=20,
        help='Deprecated no-op: GNU Make now resolves includes itself')

    args, _ = parser.parse_known_args(argv)
    make_args = with_default_makefile(make_arguments(argv), args.in_filename)
    resolved_database_filename = database_filename(args.db_filename, make_args)

    if args.analyze:
        stats = analyze_target(resolved_database_filename, args.analyze, make_working_directory(make_args))
        print('started:', stats['started'])
        print('finished:', stats['finished'])
        if stats['finished']:
            print('max:', stats['max'])
            print('min:', stats['min'])
            print('avg:', stats['avg'])
            print('median:', stats['median'])
            print('last:', stats['last'])
        return

    if args.preprocess_only:
        out_file = io.StringIO()
        ast = parse_makefile(args.in_filename, make_args)
        generate_makefile(ast, out_file, resolved_database_filename)
        print(out_file.getvalue())
        return

    if make_goals(make_args):
        profile_target(make_args, resolved_database_filename)

    ast = parse_makefile(args.in_filename, make_args)

    docs = {}
    for ttype, data in ast:
        if ttype != 'target':
            continue
        for name in data.get('all_targets', [data['target']]):
            docs[name] = data['docs']
    performance = parse_timing_db(resolved_database_filename, args.after_date, make_working_directory(make_args))
    deps, influences, order_only, indirect_influences = get_dependencies_influences(ast)

    dot_file = io.StringIO()

    export_dot(
        dot_file,
        influences,
        deps,
        order_only,
        performance,
        indirect_influences,
        docs
    )
    dot_file.seek(0)

    render_dot(
        dot_file,
        args.svg_filename
    )

    export_report(
        performance,
        docs,
        influences.keys()
    )


def cli(argv=None):
    """Console entry point: exit with GNU Make's own status when a build fails."""
    try:
        main(sys.argv[1:] if argv is None else argv)
    except subprocess.CalledProcessError as error:
        # A profiled build streamed Make's output already; the report passes
        # carry Make's diagnostic in ``stderr``.
        if error.stderr:
            sys.stderr.write(error.stderr)
        sys.exit(error.returncode)


if __name__ == '__main__':
    cli()
