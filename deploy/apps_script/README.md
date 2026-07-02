# kindle bot (Apps Script, off-kernel -- declared, not disguised)

BLUEPRINT 9 marks kindle as the one outlier: it runs in Google Apps
Script against the Drive API, not through the kernel. It shares only
the media tree contract (BLUEPRINT 1.2): it consumes Google Docs from
`_inbox/` (the sorter consumes only image extensions, so the shared
folder stays conflict-free by type), converts each Doc to PDF into
`print/`, and archives the source weekly into `Scripts/` as
`done__<name>`.

Deploy: create an Apps Script project, paste `Config.gs` and
`Kindle.gs`, set the folder ids in `Config.gs`, and add a weekly
time-driven trigger for `weeklyArchive` plus a 5-minute trigger for
`convertInbox`.
