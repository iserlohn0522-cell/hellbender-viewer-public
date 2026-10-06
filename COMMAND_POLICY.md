# Hellbender Viewer 3.0 — read-only command and load policy

Updated 2026-10-06 for release 3.0.0. Design rule: follow Hellbender's published rules, use only ordinary read-only Slurm client commands, and never send requests often — including while developing and testing. (Earlier 2.x versions used separate per-category reads and a log reader; see "Removed in 3.0".)

## Sources checked 2026-10-06

- MU Hellbender guide: [policies](https://docs.itrss.umsystem.edu/pub/hpc/hellbender#policies) and [monitoring your jobs](https://docs.itrss.umsystem.edu/pub/hpc/hellbender#monitoring_your_jobs). It forbids running research code on login nodes and moving data on login nodes, documents `squeue` monitoring, and says fairshare is by far the largest factor in queue placement. It states no limit on scheduler-command frequency and approves no polling interval.
- SchedMD [sinfo](https://slurm.schedmd.com/sinfo.html), [squeue](https://slurm.schedmd.com/squeue.html), [sshare](https://slurm.schedmd.com/sshare.html), [sacct](https://slurm.schedmd.com/sacct.html), [Fair Tree](https://slurm.schedmd.com/fair_tree.html). `sinfo`, `squeue`, `sshare` and `scontrol` each send an RPC to slurmctld and must not be run from loops; `sacct` queries slurmdbd.

This is a conservative reading of the published rules, not administrator approval. No administrator was contacted.

## What runs where

The GUI, parsing, charts, projections, tests and the SQLite store run on the laptop. The cluster side only runs standard, read-only Slurm client commands through the user's existing SSH alias, the same commands a user types to monitor jobs. No program is installed or run on the login node, no file is read there, and no job or node is changed.

## One Refresh = one SSH session

Refresh happens only when the user clicks it (or presses F5 / Ctrl+R). Nothing reads the cluster at startup, while hidden, or on a timer. Each Refresh opens one short SSH session that runs a fixed script; every command has its own `timeout` and output frame, so one failing command leaves the others' results usable and keeps the previous data for itself.

| Section | Command (fixed, read-only) | Scope | Included |
| --- | --- | --- | --- |
| Your jobs | `squeue --user="$USER" --noheader --format=…` (id, state, partition, name, elapsed, limit, CPUs, memory, nodes, reason/nodelist, start or expected start, submit, end, account, requested GRES, work dir) | your jobs only | every Refresh |
| Nodes and GPUs | `sinfo --Node --noheader --Format=NodeList,Partition,StateLong,CPUsState,Memory,AllocMem,Gres,GresUsed` | node metadata, no job or user data | every Refresh |
| Fairshare | `sshare --noheader --parsable2 --Users --format=Account,User,RawShares,NormShares,RawUsage,NormUsage,EffectvUsage,FairShare,LevelFS,TRESRunMins` | your own associations only | at most every 30 min |
| Priority settings | `scontrol show config \| grep -E '^Priority'` | cluster configuration lines | at most every 7 days, only together with fairshare |
| Accounting | `sacct --user="$USER" --allocations --noheader --parsable2 --starttime=<window> --endtime=now --format=…` | your jobs only, explicit finite window | at most every 15 min; after a tracked job leaves the queue, at most every 4 min |

The accounting window starts 2 h before the last successful sync (14 days on the first sync, never more than 30 days); jobs that left the queue since the last sync widen it to cover them. This is what records jobs that started and finished between two Refreshes.

## Limits

- Minimum 300 s between Refreshes (or the saved interval if longer), a shared 30 s gap after any read, and a 95 s in-flight lease; only one read runs at a time. The reservation is stored in SQLite, so it survives restarts and covers a second app instance on the same profile. Pending cooldowns of the 2.x per-category reads (queue, nodes, history, cluster totals, details) are still honoured.
- A failed Refresh backs off (floor × 2^n, at most 1 h). A failed section backs off on its own (fairshare from 1 h, priority settings from 6 h, accounting from 30 min, nodes from 15 min; doubling to at most 24 h) while the other sections keep working.
- Rejected clicks send nothing and do not extend the cooldown; the button shows the remaining time.
- Per-command remote timeouts of 10–25 s, an 80 s remote limit for the whole script and a 95 s local limit. Received output is capped while streaming at 2,000,000 bytes (stdout) and 16,384 bytes (stderr).
- SSH keeps `BatchMode=yes`, `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`, `ConnectTimeout=8`, `ConnectionAttempts=1`, `ClearAllForwardings=yes`, `ControlMaster=no`, `ControlPersist=no`, `PermitLocalCommand=no`, `RemoteCommand=none`. No key material is read, no host trust is changed, no persistent connection is created.

Worst case if the user clicks at every allowed moment: 12 SSH sessions per hour, running at most 12 `squeue`, 12 `sinfo` and 2 `sshare` commands per hour against slurmctld (plus one `scontrol show config` per week) and at most 12 small `sacct` queries per hour against slurmdbd (normally 4). Ordinary use is a few Refreshes per day. Idle load is zero.

## Removed in 3.0 (compared with 2.3.1)

- Log Fetch: no `scontrol show job`, `stat` or `tail` of output files; nothing is read from the cluster file system. The user agreed progress estimation could be dropped; the app instead shows walltime used and, where available, the median runtime of your own similar finished jobs.
- The anonymous whole-cluster `squeue --states=RUNNING,PENDING` (about 16,000 records and 350 KB per read) and its charts.
- Separate buttons and cooldowns per category (queue / nodes / totals / accounting / fetch).

## What the numbers mean

- **Free GPU** = configured GPUs not currently allocated on nodes that accept work (not down, drained, reserved, powered off, not responding). **Usable now** additionally requires the same node to have enough idle CPUs and unallocated memory for your usual GPU job (inferred from your recent jobs, or set in Settings). Nodes the backfill scheduler has planned for a waiting job are counted separately. None of this guarantees an immediate start: priority, reservations, QOS and partition limits still apply. Jobs in `gpu_requeue` run on investor nodes and can be preempted and requeued.
- **Fairshare** is Slurm's `FairShare` factor for your association: under Fair Tree it is your rank among all users (0–1, higher starts sooner) and is not projected. The projection is of `LevelFS` (your share ÷ your usage among siblings): your recorded usage decays with the cluster's `PriorityDecayHalfLife` (7 days assumed if unread), running jobs are counted up to their time limits (an upper bound, using `TRESRunMins`), other users' usage is held constant, and jobs you have not submitted yet are excluded. It is an estimate.
- **Expected start** is Slurm's own backfill estimate (`%S`), shown in local time; it changes as the queue changes. When Slurm gives none, the app shows the median submit-to-start wait of your own jobs in that partition over the last 14 days, labelled as history.
- Times from Slurm are cluster-local (US Central) and are converted to the laptop's time zone for display.

## Verification

Offline: 95 pytest cases, including the exact remote script executed under a local POSIX `sh` against fake Slurm commands (framing, quoting, per-command failure isolation), parsers on realistic synthetic fixtures, the fairshare projection against its closed-form solution, storage transactions, cooldown persistence, and real Tk widgets with scripted collectors.

Live (2026-10-06): one full Refresh through the app path against Hellbender parsed all five sections (about 88 KB in 5 s). An earlier attempt from a shell whose PATH put Git's OpenSSH first was refused at login before any Slurm command ran, because that client cannot reach the Windows ssh-agent; since then the app always calls `%SystemRoot%\System32\OpenSSH\ssh.exe` on Windows. Detailed live records stay in the maintainer's private workspace.
