"""merge-p: merge decompiled p_ (player-held) weapon models into one.

GoldSource poses an attached p_ model by bone-NAME merge against the player
model, so a merged p_ model needs only the shared ``Bip01`` arm chain plus one
uniquely-named bone per weapon (two for dual-wield, one per hand) and a
single-frame idle sequence carrying each weapon bone's hand-relative local
transform.
"""
