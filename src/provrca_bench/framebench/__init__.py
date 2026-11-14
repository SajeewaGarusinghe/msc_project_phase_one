"""The overlay generator: one deterministic function from (clean case, target, family, threat model, dose, seed) to overlay bytes plus sealed labels.

Filled in build-guide phase P2. Empty on purpose: the build order is a gate, and
nothing here may depend on R1 or R2 before G0 reports (it now has).
"""
