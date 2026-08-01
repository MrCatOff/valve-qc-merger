"""merge-world: merge decompiled w_ (dropped-weapon) models into one.

A w_ model renders at its entity origin with its idle pose; no player skeleton
is involved. The merged model therefore needs only TWO bones total — a
``flash`` root and a ``weapon`` child carrying every vertex — so studiomdl
auto-generates exactly one hitbox that always covers the visible weapon.
Each model's rendered pose (``idle . bind⁻¹``, identity for most models) is
baked into its vertices first, which is exact because every corpus idle is a
static pose.
"""
