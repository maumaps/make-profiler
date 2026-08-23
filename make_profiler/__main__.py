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
from make_profiler.parser import get_dependencies_influences, make_command, parse_makefile
from make_profiler.preprocess import generate_makefile
from make_profiler.timing import parse_timing_db, analyze_target
from make_profiler.report_export import export_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('make_profiler')
TRACE_TARGET = re.compile(r"^.*:\d+: update target '([^']+)'(?: due to:|$)")


def make_working_directory(make_args):
    """Return GNU Make's effective working directory after its ``-C`` options."""
    directory = os.getcwd()
    args = iter(make_args)
    for argument in args:
        if argument == '--':
            break
        if argument in ('-C', '--directory'):
            directory = os.path.abspath(os.path.join(directory, next(args)))
        elif argument.startswith('-C') and len(argument) > 2:
            directory = os.path.abspath(os.path.join(directory, argument[2:]))
        elif argument.startswith('--directory='):
            directory = os.path.abspath(os.path.join(directory, argument.split('=', 1)[1]))
    return directory


def database_filename(filename, make_args):
    """Resolve relative profiling data paths like GNU Make resolves ``-C``."""
    if os.path.isabs(filename):
        return filename
    return os.path.join(make_working_directory(make_args), filename)


def make_goals(make_args):
    """Return ordinary GNU Make goals without mistaking option values for goals."""
    goals = []
    option_values = {'-C', '-f', '-I', '--directory', '--file', '--makefile', '--include-dir', '--eval'}
    after_separator = False
    skip_next = False
    for argument in make_args:
        if after_separator:
            goals.append(argument)
        elif skip_next:
            skip_next = False
        elif argument == '--':
            after_separator = True
        elif argument in option_values:
            skip_next = True
        elif argument.startswith(('--directory=', '--file=', '--makefile=', '--include-dir=', '--eval=')):
            continue
        elif argument.startswith('-') or '=' in argument:
            continue
        else:
            goals.append(argument)
    return goals


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


def has_makefile_argument(make_args):
    """Return whether GNU Make receives an explicit Makefile selection."""
    for argument in make_args:
        if argument == '--':
            break
        if argument in ('-f', '--file', '--makefile') or argument.startswith('-f'):
            return True
    return False


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
    """
    command = make_command(make_args, ['--trace'])
    logger.info(' '.join(command))
    run_id = secrets.token_hex(10)
    active_target = None
    output = []
    resolved_database_filename = database_filename(db_filename, make_args)

    def record(event, target_name):
        with open(resolved_database_filename, 'a') as database:
            database.write(f'{int(time.time())} {run_id} {event} {target_name}\n')

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in process.stdout:
        trace_target = TRACE_TARGET.match(line)
        if trace_target:
            if active_target is not None:
                record('finish', active_target)
            active_target = trace_target.group(1)
            record('start', active_target)
            continue
        output.append(line)
        print(line, end='')

    returncode = process.wait()
    if returncode == 0 and active_target is not None:
        record('finish', active_target)
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
        help='Disable cycle detection for include instructions')
    parser.add_argument(
        '--include_depth',
        action='store',
        dest='include_depth',
        type=int,
        default=20,
        help='Depth of the nesting includes')

    args, _ = parser.parse_known_args(argv)
    make_args = with_default_makefile(make_arguments(argv), args.in_filename)
    resolved_database_filename = database_filename(args.db_filename, make_args)

    if args.analyze:
        stats = analyze_target(resolved_database_filename, args.analyze)
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
    performance = parse_timing_db(resolved_database_filename, args.after_date)
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


if __name__ == '__main__':
    main()
