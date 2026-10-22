"""Signed automatic updates with rollback (docs/UPDATES.md).

`UPDATER_PROTOCOL` is the version of the contract between a release and the updater that installs it (manifest
fields, archive layout, `doctor --release`, `db migrate`). A release whose `min_updater_version` is higher is
refused: the user then reinstalls it with `goulot dev bootstrap`.
"""

UPDATER_PROTOCOL = 1
