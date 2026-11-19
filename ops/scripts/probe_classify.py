# -*- coding: utf-8 -*-
"""Probe classifier signals for a set of source images (diagnostics only)."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C


def find_src(name):
    from reference_whitelist import verify_path
    return str(verify_path(name))


def main():
    names = sys.argv[1:]
    print(f"{'file':20s} {'TYPE':12s} {'mode':9s} {'face/bod':8s} "
          f"{'uniqC':>6s} {'grad':>5s} {'edge':>6s} {'flat':>5s} {'lapv':>7s}")
    for n in names:
        p = find_src(n)
        if not p:
            print(f"{n:20s} NOT FOUND"); continue
        r = C.classify(p)
        s = r['signals']
        print(f"{n:20s} {r['input_type']:12s} "
              f"{r['render_mode'].replace('_VECTOR',''):9s} "
              f"{str(s['faces'])+'/'+str(s['bodies']):8s} "
              f"{s['unique_colors']:6d} {s['grad_diff']:5.2f} "
              f"{s['edge_frac']:6.3f} {s['flat_frac']:5.2f} {s['lap_var']:7.1f}")


if __name__ == '__main__':
    main()
