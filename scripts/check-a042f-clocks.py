#!/usr/bin/env python3
from pathlib import Path
import sys


CLOCKS = {
    'scp_clks': 'scpsys',
    'audio_clks': 'audio',
    'img_clks': 'img',
    'cam_clks': 'cam',
    'mipi0a_clks': 'mipi0a',
    'mm_clks': 'mm',
    'venc_clks': 'vcodec',
}


def check_map(text):
    required = {'__init_begin', '__init_end', *CLOCKS}
    required.update('clk_mt6765_' + name + '_probe' for name in CLOCKS.values())
    symbols = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] in required:
            name = fields[2]
            if name in symbols:
                raise ValueError('Duplicate clock-check symbol: ' + name)
            symbols[name] = int(fields[0], 16)
    missing = required - symbols.keys()
    if missing:
        raise ValueError('Missing clock-check symbols: ' + ', '.join(sorted(missing)))
    start, end = symbols['__init_begin'], symbols['__init_end']
    if start >= end:
        raise ValueError('Invalid init-memory bounds')
    for name in sorted(required - {'__init_begin', '__init_end'}):
        if start <= symbols[name] < end:
            raise ValueError('Persistent clock path in freed init memory: ' + name)
    return len(CLOCKS)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: check-a042f-clocks.py System.map')
    try:
        count = check_map(Path(sys.argv[1]).read_text())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
    print(f'Verified {count} clock tables and their probes outside freed init memory.')
