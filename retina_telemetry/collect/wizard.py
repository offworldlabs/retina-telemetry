"""Whether the owner has finished retina-gui's setup wizard.

``/data/retina-gui/setup-wizard-completed`` is written by retina-gui when the
wizard reaches its final step, which is only reachable after the tower step. Its
presence is therefore the one available proof that the node's configuration is
the owner's rather than the shipped default.

## Why registration waits for it

``retina-node/config/default.yml`` ships a working configuration: Greenwich
Observatory as the receiver, Crystal Palace as the illuminator. The merger
writes it on first boot, so ``config.yml`` is readable and complete long before
anybody has chosen anything.

retina-gui records consent at the *first* wizard step and the location at the
*fifth*, with an OS update in between. Without this gate we register in that
window and tell the server a node is at Greenwich running off Crystal Palace,
which was happening to real nodes. The correction does follow, because a config
change is picked up within ``CONFIG_POLL_S`` — but only for an owner who
finishes the wizard, and accepting the terms then closing the tab is a path
retina-gui deliberately supports.

## Why a flag rather than an inspection of the config

Two tempting alternatives, both wrong:

* **Comparing against the default coordinates.** We read the *merged*
  ``config.yml``, where a default is indistinguishable from a deliberate choice,
  so this would refuse registration for life to a node genuinely sited near
  Greenwich. It would also make a value in someone else's config file
  load-bearing here.
* **Requiring the location to be non-null.** It never is. The merger guarantees
  a complete config, which is the whole reason the defaults are a problem.

The flag is the only signal that distinguishes configured from merely populated,
and it costs one ``stat``.

## Absence is not an error

A node that has not finished setup is in a normal, expected state and says so
through ``NodeState.SETUP_INCOMPLETE``. Same posture as ``consent``: this layer
reports what is on disk and the lifecycle decides what it means.

The flag only arrived in retina-gui aee29a6 (2026-06-24), so nodes configured
before then had none. retina-gui backfills it at startup from a location in
``user.yml``, which it has written since 4afa307 (2026-03-23). **That backfill
must be deployed to the fleet before this gate ships**, or every configured node
still holding an old flagless ``/data`` stops registering.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_WIZARD_FLAG_PATH = Path("/data/retina-gui/setup-wizard-completed")


def setup_complete(path: Path | str = DEFAULT_WIZARD_FLAG_PATH) -> bool:
    """Whether retina-gui has recorded the setup wizard as finished.

    The file's contents are an ISO timestamp, deliberately not read: retina-gui
    owns that format, and needing to parse it would make a second thing to keep
    in step across two repos. Existence is the whole signal.

    ``is_file`` rather than ``exists``: the latter is true of a directory, which
    would open the gate on something retina-gui never wrote.

    Returns:
        True if the flag is present. False if it is absent or cannot be
        stat'ed, which is the ordinary state of a node still in setup. Never
        raises, and never guesses in the permissive direction.
    """
    try:
        return Path(path).is_file()
    except OSError as exc:
        # A broken mount rather than a missing file. Reported, and treated as
        # "not complete": registering a node whose configuration we cannot
        # vouch for is the failure this gate exists to prevent.
        log.warning("cannot check the setup flag at %s: %s", path, exc)
        return False
