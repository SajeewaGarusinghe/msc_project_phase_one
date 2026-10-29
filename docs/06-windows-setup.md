# Running the bench on Windows

## The short version

**Install Docker Desktop with the WSL2 backend, then keep the repo *inside* the
WSL2 filesystem — not on `D:`.**

`D:` is a fine place to keep a copy. It is a bad place to *run* from.

## Why not run from D:

The bench runs k3d, which is k3s inside Docker. On Windows, Docker Desktop runs
inside a WSL2 Linux VM. If the repo sits on `D:` and you run commands from WSL2,
every file access crosses the Windows/Linux filesystem boundary at `/mnt/d/`.
Three things go wrong:

| Problem | What you see |
|---|---|
| **Slow I/O** — `/mnt/d` is a 9p/virtio filesystem bridge | Helm installs and image pulls crawl; `make install` takes many times longer |
| **Permission translation** — NTFS has no POSIX mode bits | `chmod 600 kubeconfig` silently does nothing; scripts may refuse to run |
| **Line endings** — git on Windows may check out CRLF | `bash` scripts fail with `bad interpreter: /bin/bash^M` |

**Correction to an earlier version of this document:** it claimed the k3d
containerd-socket bind mount breaks across `/mnt/`. That was wrong on both
counts — the mount should not have been there at all (k3d runs k3s *inside* the
node container, so the socket is already present and chaos-daemon finds it
without help), and it has been removed from `infra/local/k3d-cluster.yaml`.

So running from `D:` is **slow and awkward, not fatal**. The recommendation to
work inside WSL2 stands, but on performance and permission grounds rather than
because fault injection would fail.

## The setup that works

```
Windows
└── WSL2 (Ubuntu)                 <- everything runs HERE
    ├── Docker Desktop backend
    └── ~/msc_project_phase_one   <- repo lives HERE (native ext4)
```

### 1. Enable WSL2 and install Ubuntu

**Note:** Docker Desktop registers its own `docker-desktop` distro. That is a
helper, not a workspace — it has no `apt` and no login shell, which is why
`wsl -e bash` fails with `execvpe(bash)` when it is the only distro present.
You still need a real distro.

In an **Administrator** PowerShell:

```powershell
wsl --install -d Ubuntu
# reboot if prompted, then set a UNIX username/password when Ubuntu first starts
wsl --set-default-version 2
```

### 2. Install Docker Desktop

Download Docker Desktop for Windows, then in **Settings → General** tick
*Use the WSL 2 based engine*, and in **Settings → Resources → WSL Integration**
enable integration with your Ubuntu distro.

**Raise the memory limit.** This is the single most common cause of a broken
local bench. Create or edit `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=16GB      # 12GB absolute minimum; 20GB if you have 32GB of RAM
processors=6
swap=8GB
```

Then `wsl --shutdown` in PowerShell and reopen Ubuntu for it to take effect.

### 3. Install the toolchain inside Ubuntu

Clone the repo first (step 4), then run the one-shot installer:

```bash
bash scripts/setup-wsl.sh
```

It installs `make`, `git`, `curl`, **k3d**, **kubectl** (checksum-verified) and
**helm**, and — importantly — checks that Docker Desktop's **WSL integration is
enabled for this distro**. That last one is the step people miss: Docker running
on Windows is not the same as `docker` working inside Ubuntu. Enable it under
*Docker Desktop → Settings → Resources → WSL Integration*, Apply & Restart, then
reopen the Ubuntu shell.

The script is safe to re-run; every step is skipped if already satisfied.

### 4. Clone and run

```bash
cd ~
git clone -b feat/p0-bench https://github.com/SajeewaGarusinghe/msc_project_phase_one.git
cd msc_project_phase_one

bash scripts/setup-wsl.sh      # step 3, if you have not run it

make local-up
make install TARGET=local
make verify  TARGET=local
```

### 5. Reaching the UIs from Windows

WSL2 forwards `localhost`, so after `make tunnel-grafana` inside Ubuntu, open
`http://localhost:3000` in your normal Windows browser. Same for the Astronomy
Shop on `:8080`.

## If you want a copy on D: anyway

Keep it as an *archive*, not a working copy. Git already gives you this via
GitHub, so a D: copy is mostly redundant — but if you want one:

```bash
# from inside WSL, after committing your work
cp -r ~/msc_project_phase_one /mnt/d/msc_project_phase_one_backup
```

Do not `cd /mnt/d/...` and run `make` there.

## Memory reality check

| You have | Workable? |
|---|---|
| 8 GB RAM | No. Use the cloud bench. |
| 16 GB RAM | Tight — allocate 12 GB to WSL, close everything else. Expect pressure. |
| 32 GB RAM | Comfortable — allocate 20 GB. |

`make local-up` fails fast with a clear message if Docker reports under 12 GB,
rather than letting you discover it through random pod evictions an hour later.
