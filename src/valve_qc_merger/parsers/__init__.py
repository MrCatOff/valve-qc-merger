"""Readers that turn raw QC/SMD text into :mod:`valve_qc_merger.models` objects.

Each parser is responsible for one on-disk format and produces plain data
structures; it performs no I/O side effects beyond reading its input.
"""
