"""Operations as callable services: ``run_x(options, reporter) -> ServiceResult``.

The CLI commands and the studio GUI both call these. A service never prints:
it reports through a :class:`~valve_qc_merger.services.base.Reporter` (the
default one prints, which is exactly the CLI's output), checks for
cancellation between models, and returns what it produced.
"""
