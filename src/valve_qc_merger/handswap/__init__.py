"""handswap — pure-Python GoldSrc viewmodel hand replacement.

Replaces the original hands of a decompiled CS 1.6 v_ model with the CSO
hands (extracted once from the rig .blend into assets/cso_hands.json.gz),
retargets every animation, rewrites the QC and compiles with studiomdl.
No Blender in the per-weapon path: everything is numpy on SMD/QC text.
"""
__version__ = "2.0.0"
