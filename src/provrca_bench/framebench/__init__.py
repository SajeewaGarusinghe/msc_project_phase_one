"""FrameBench: (clean case, target, family, threat model, dose, seed) -> overlay bytes + sealed labels.

    case.py      one clean development case and the facts later steps need (server spans, edges)
    target.py    the four-check target rule, check (a) as a BARO rank (K21), seeded draw
    doses.py     symptom-matched doses, as amended 25 Nov (F2/FS pooled; F1 at the floor)
    build.py     one variant: genuine rows byte for byte, a sender column, derive, sealed labels
    generate.py  the development set; test sets are generated only at the G3 freeze

Built so far (25 Nov 2026): T2 variants of F1, F2, F3, FS, F4 and the A1 volume control, at high
and low dose. Not yet: T1 variants and the adaptive families (F2c, F3c, FSc), which need the P3
matcher, and the honest faults AF1/AF2. ``rule_applicable`` is left unset until P3 computes it.
"""
