#!/usr/bin/env python3
"""Daily: save a copy of each council term's strategic plan (terms.json).

The township removes older documents from its site, so the plan PDF is
archived as soon as it's listed. No AI involved; see term_summary.py.
"""
import sys

import term_summary

if __name__ == "__main__":
    sys.argv.append("--archive-only")
    term_summary.main()
