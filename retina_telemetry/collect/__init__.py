"""Stage 1 — collection.

Everything this service takes from the rest of the node stack. Nothing here
knows the server exists, and nothing here converts units: values are handed on
in the units their source produced them in, under names that say so
(``delay_km``, ``timestamp_ms``, ``rx_alt_m``). Stage 2 emits the spec's names
and the spec's units, so a conversion that goes missing is visible at the call
site rather than on the wire.

Nothing in the node stack pushes to us. Every module here polls or reads.

Most of it is required. :mod:`contact` is the exception: every field is
optional and so is the whole document, because the spec says a node with
nothing to report never calls that endpoint at all.
"""
