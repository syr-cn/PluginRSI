# OlympiadBench mathematical grading attribution

The comparison conventions in `evaluation/olympiad_grading.py` are adapted from
[OpenBMB/OlympiadBench](https://github.com/OpenBMB/OlympiadBench), retrieved 2026-09-22:

- `eval/auto_scoring_judge.py`: LaTeX normalization, comma-separated answer matching,
  plus/minus expansion and absolute numeric tolerances.
- `inference/judge.py`: use `final_answer[0]` as the canonical reference and parse
  the comma-separated `error` field, with empty values defaulting to 1e-8.

The upstream MIT license is included in [LICENSE](LICENSE).

The local comparator is an adaptation, not an exact reproduction of upstream scores.
It rejects arbitrary factor-of-100 numeric equivalence, requires full tuple length
and ordered tuple elements, uses exact symbolic identities, and bounds work in a
separate CPU process. Matching units may be removed; different units are not converted.
It uses SymPy's strict LaTeX parser. Unsupported answer syntax can be rejected even
if a human would consider the answer valid; concise LaTeX final answers are required.
For constructive tasks this checks the canonical answer, not all possible valid constructions.
