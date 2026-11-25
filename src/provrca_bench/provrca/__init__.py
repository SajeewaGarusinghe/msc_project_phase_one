"""The decision chain: attribution, then the one-to-one matcher, then a rule only if every applicability condition holds, else unknown with a reason.

Built in build-guide phase P3, one link at a time:

* ``attribution`` (P3 item 1, also arm D1) -- claimed writer vs ``sender``; the only reader of
  ``sender``.
* ``matcher`` (P3 item 2) -- one outcome per span; reproduces G0 stage 2's published counts.
* applicability tables, R1, R2, demotion -- later P3 steps.
"""
