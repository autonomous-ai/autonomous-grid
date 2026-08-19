"""The measurement harness ADR 0034 rests on (issue 35).

Deliberately NOT collected by pytest: the modules here carry no `test_` prefix and the package's
entry point is `tests/measure_non_dev_design.py`, so the default `python_files` never matches one.
A multi-gigabyte, multi-minute, subscription-spending run must not ride along with `pytest tests/`.
The harness's own decisions ARE tested, from `tests/test_measure_non_dev_design.py`.
"""
