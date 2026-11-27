# Browser-agent instruction: connect the NAS to the site's "Versions" page

The NAS updates minion2 not with a script from this repository but with a
separate tool, **nas-update** (`/volume1/docker/nas-update/`, DSM task
`nas-update` every 5 minutes, repository github.com/coppermilk/nas-update).
nas-update itself sends the version report when its app config has a
`[report]` section. Everything is done through DSM in the browser. Delete
nothing and do not touch containers by hand; if a step fails, stop and
write down what you saw.

Needed from Tyoma beforehand: the value of the site key `VERSIONS_KEY`
(never name it in chat). The nas-update commit with the report is already in
`main`: `d5ebdc68a3072fb446d91155f4460d7ccd2b7b0a` -- pin exactly that one.

## 1. Update nas-update to the commit with the report

1. Control Panel -> Task Scheduler -> task "nas-update install" (disabled,
   run by hand) -> Edit -> "Task Settings" -> in "Run command" find the line
   `C=...` and replace the sha with
   `d5ebdc68a3072fb446d91155f4460d7ccd2b7b0a`. OK.
2. Select "nas-update install" -> "Run" -> confirm. The task result
   (Action -> View Result) must show the new sha and dry-run lines without
   errors.

## 2. Site key in a root-only file

1. File Station -> `docker/nas-update/` -> create the folder `secrets` (if
   missing).
2. In it create the file `versions.key` (Text Editor -> Create; set the
   encoding to UTF-8), content -- only the key, one line, no quotes or
   spaces. Save.
3. Properties of the file and of the `secrets` folder -> owner `root`,
   permissions: read and write for the owner only (nothing for others).

## 3. The [report] section in the app config

File Station -> `docker/nas-update/apps/minion2.conf` -> open in Text Editor
(UTF-8) -> append at the end:

```
[report]
url             = https://daetotyoma.com/admin/versions/minion2-nas
key_file        = /volume1/docker/nas-update/secrets/versions.key
heartbeat_hours = 24
```

Save. If the file has another name (file name = app name), add it to the one
that has `project = minion2`.

## 4. Check

nas-update logs in Russian; the exact lines are the `log(...)` calls in
nas-update `report.py`. Below they are given by meaning.

1. Task Scheduler -> task `nas-update` -> "Run" once (or wait 5 minutes).
   Its result must contain the version-report line "heartbeat -> site
   accepted" (or "deploy -> site accepted" if an update just happened).
   If it says "site rejected ... (401)", the site gives the cause next to
   it: "secret VERSIONS_KEY is not set" -- the secret is missing in
   Cloudflare -> Workers & Pages -> the site project -> Settings ->
   Variables and Secrets -> Production (add `VERSIONS_KEY` with the key
   value and do Deployments -> Retry deployment; Tyoma does this); "key
   mismatch" -- `versions.key` holds another value (compare, no spaces or
   line breaks). Check the key without the NAS:
   `curl -X PUT -H "X-Board-Key: <key>" https://daetotyoma.com/admin/versions/test -d '{"event":"heartbeat","ok":true}'`
   -- 201 means the key is right (the key does not work for the admin or
   the team board; the header name `X-Board-Key` is unchanged). "key_file
   not read" -- wrong path or file permissions.
2. Open `https://daetotyoma.com/admin/versions/` with the admin password:
   the card "minion2 on NAS" shows the commit, "alive" with today's time
   and "ok".
3. Tell Tyoma what the "Versions" page shows and what the task result said.
