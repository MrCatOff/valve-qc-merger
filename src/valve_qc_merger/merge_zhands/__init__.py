"""merge-zhands: merge CSO zombie hand view models into one bodygrouped model.

Every zombie ships a ``v_<zombie>_knife`` (bare claws, sometimes with an
extra like heavy's blade) and a ``v_<zombie>_grenade`` (the same claws holding
the frog grenade). The merge keeps one ``hands`` entry per distinct hand mesh
(knife and grenade hands collapse when they are the same mesh) plus ONE shared
``grenade`` bodygroup, so ``pev_body = grenade_on + 2 * hands``.
"""
