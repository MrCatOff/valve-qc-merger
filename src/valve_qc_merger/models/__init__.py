"""In-memory representations of GoldSource model data.

This package holds the data structures the tool works with:

* ``qc``    -- QC script directives (``$model``, ``$sequence``, ``$hbox``, ...)
* ``smd``   -- SMD geometry and animation (nodes, skeleton, triangles)
* ``hitbox`` -- hitbox groups and their bounding volumes

Parsing raw files into these structures lives in
:mod:`valve_qc_merger.parsers`.
"""
