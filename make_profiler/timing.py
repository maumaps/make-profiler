import os
import time


def parse_timing_db(filename, after_date=None, directory=None):
    """Summarize the timing database.

    ``directory`` is the directory GNU Make ran in (``-C``); target files and
    ``logs/`` are looked up there instead of the profiler's own working
    directory. Reported ``log`` paths stay relative to it.
    """
    directory = directory or os.getcwd()
    if not os.path.isfile(filename):
        return {}

    lines = [i.strip().split() for i in open(filename)]
    lines.reverse()
    cur_run_bid = ''
    targets = dict()
    for parts in lines:
        if len(parts) != 4:
            continue
        target = parts[3]
        bid = parts[1]
        action = parts[2]
        timestamp = float(parts[0])

        if not cur_run_bid:
            cur_run_bid = bid

        if target not in targets:
            targets[target] = {
                'current': False,
                'running': False,
                'done': os.path.exists(os.path.join(directory, target)),
                'isdir': os.path.isdir(os.path.join(directory, target))
            }

        logpath = 'logs/%s/%s/log.txt' % (bid, target)
        if 'log' not in targets[target] and os.path.exists(os.path.join(directory, logpath)):
            targets[target]['log'] = logpath

        failpath = 'logs/%s/%s/failed.touch' % (bid, target)
        failpath = os.path.join(directory, failpath)
        if 'failed' not in targets[target]:
            targets[target]['failed'] = os.path.exists(failpath)

        if bid == cur_run_bid:
            targets[target]['current'] = True
            targets[target][action + '_current'] = timestamp
            if 'finish_current' not in targets[target]:
                if targets[target]['failed']:
                    targets[target]['finish_current'] = os.path.getmtime(failpath)
                else:
                    targets[target]['finish_current'] = float(time.time())
                    targets[target]['running'] = True

        if not (bid == cur_run_bid):
            if 'prev' not in targets[target] and action == 'finish':
                targets[target]['prev'] = bid
                targets[target][action + '_prev'] = timestamp
            elif action == 'start' and targets[target].get('prev') == bid:
                targets[target][action + '_prev'] = timestamp

        if 'finish_current' in targets[target] and 'start_current' in targets[target]:
            targets[target]['timing_sec'] = targets[target]['finish_current'] - targets[target]['start_current']
        elif 'start_prev' in targets[target]:
            if after_date and targets[target]['start_prev'] < after_date.timestamp():
                targets[target]['timing_sec'] = 1
            else:
                targets[target]['timing_sec'] = targets[target]['finish_prev'] - targets[target]['start_prev']
    return targets


def analyze_target(filename, target_name, directory=None):
    """Return timing statistics for the given target.

    Parameters
    ----------
    filename: str
        Path to ``make_profile.db`` file.
    target_name: str
        Name of the target to analyse.
    directory: str, optional
        Directory GNU Make ran in; ``logs/`` is looked up there.

    Returns
    -------
    dict
        Dictionary containing ``started`` and ``finished`` counts along with
        ``max``, ``min``, ``avg``, ``median`` and ``last`` duration in seconds.
    """

    directory = directory or os.getcwd()
    if not os.path.isfile(filename):
        return {
            'started': 0,
            'finished': 0,
            'max': 0,
            'min': 0,
            'avg': 0,
            'median': 0,
            'last': 0,
        }

    lines = [i.strip().split() for i in open(filename)]
    runs = {}

    for ts_str, bid, action, tgt in lines:
        if tgt != target_name or action not in ('start', 'finish'):
            continue
        ts = float(ts_str)
        runs.setdefault(bid, {}).update({action: ts})

    started = 0
    finished = 0
    durations = []

    for bid, data in runs.items():
        if 'start' in data:
            started += 1
        if 'start' in data and 'finish' in data:
            failpath = os.path.join(directory, 'logs', bid, target_name, 'failed.touch')
            if not os.path.exists(failpath):
                finished += 1
                durations.append((data['finish'], data['finish'] - data['start']))

    durations.sort(key=lambda d: d[0])
    times = [d[1] for d in durations]

    if times:
        max_d = max(times)
        min_d = min(times)
        avg_d = sum(times) / len(times)
        n = len(times)
        if n % 2:
            median_d = sorted(times)[n // 2]
        else:
            sorted_t = sorted(times)
            median_d = (sorted_t[n // 2 - 1] + sorted_t[n // 2]) / 2
        last_d = times[-1]
    else:
        max_d = min_d = avg_d = median_d = last_d = 0

    return {
        'started': started,
        'finished': finished,
        'max': max_d,
        'min': min_d,
        'avg': avg_d,
        'median': median_d,
        'last': last_d,
    }
