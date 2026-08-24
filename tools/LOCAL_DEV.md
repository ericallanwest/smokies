# Running the solver locally and getting the service's answer

Solving on Windows and solving in the deployed container give **different
itineraries at identical cost**. This is not a bug in the solver and not
something a pinned package version can fix. What follows is the evidence, why
the obvious fixes do not work, and the one that does.

## What was measured

`SMOKIES_FINGERPRINT=1` prints hashes of every input to the CP-SAT model, the
serialised model itself, and the solution, at the point the model is built.
Run on Windows and inside the deployed image at 12h, no other arguments:

```
                  Windows py3.14        Linux py3.12
required_order    a6f5a5474050c488  ==  a6f5a5474050c488
arc_order         b970b9d67af9961f  ==  b970b9d67af9961f
node_order        abc2f87325f35f22  ==  abc2f87325f35f22
arc_weights       f8c2329caacfc03f  ==  f8c2329caacfc03f
forced_dirs       4f53cda18c2baa0c  ==  4f53cda18c2baa0c
model_proto       f1ab26d237ee6e4c  ==  f1ab26d237ee6e4c   216,125 bytes both
objective              1,450,845    ==       1,450,845
solution          8c05bff569dc75ea  !=  54cc75dcab82d989
```

The model is byte-identical and so is its optimal value. Only the chosen
member of the optimal set differs. So the input ordering is stable, nothing
about the graph construction is platform-dependent, and there is no ordering
bug to repair -- which was the outcome worth ruling out first, because it
would have been ours to fix.

## Why the cheap fixes do not work

**Matching Python and package versions does not help.** A Windows environment
built to match the image exactly -- python 3.12.13, ortools 9.15.6755,
pandas 3.0.5, networkx 3.6.1 -- returns `8c05bff569dc75ea`, the same answer as
python 3.14 on Windows, and still not the Linux one. Python version is not the
variable.

**The variable is the wheel.** `ortools` ships `win_amd64` and `manylinux`
builds of the same version number; they are different binaries, compiled
differently, and their search takes a different path through an equally
optimal space. `random_seed` and `num_search_workers=1` make a solve
reproducible against a fixed binary, which is all they can do.

**Adding a tie-break to the objective would work but is not free.** Making the
optimum unique requires a secondary objective large enough to order the ties
and small enough never to disturb the primary cost, or a second solve with the
cost fixed. Both are real changes to a model that currently solves in ~12s,
and both risk changing every published itinerary. Not worth it to avoid
installing a Linux userspace.

## The fix: run Linux locally

WSL2 is already enabled on this machine, with no distribution installed.
Installing one gives the same manylinux wheel the service runs, so local
results match production and sweeps cost nothing.

```powershell
wsl --install -d Ubuntu          # needs an elevated prompt; may want a reboot
```

Then inside Ubuntu:

```bash
sudo apt update && sudo apt install -y python3-venv
python3 -m venv ~/smokies && source ~/smokies/bin/activate
pip install -r /mnt/c/Users/Eric/Desktop/Ideas/GSMNP/smokies/backend/requirements.txt

# Copy the repo into the Linux filesystem rather than working under /mnt/c --
# cross-filesystem I/O in WSL2 is slow enough to matter over a long sweep.
cp -r /mnt/c/Users/Eric/Desktop/Ideas/GSMNP ~/gsmnp && cd ~/gsmnp

SMOKIES_FINGERPRINT=1 python smokies_circuit_solver_20260509a.py \
  --max-hours 12 --json-out /tmp/x.json | grep '^FP '
```

Confirmed working on 2026-08-24, on Ubuntu under WSL2:

```
FP platform       Linux x86_64 py3.14.4 ortools9.15.6755 pandas3.0.5 networkx3.6.1
FP model_proto    f1ab26d237ee6e4c (216125 bytes)
FP objective      1450845
FP solution       54cc75dcab82d989
```

Local runs are production runs. Sweeps, preset regeneration and regression
baselines all happen on this machine for nothing, and Cloud Build is needed
only to build the image that gets deployed.

Note the Python version there: **3.14 on Linux gives the same answer as 3.12
on Linux**, while 3.12 and 3.14 on Windows both gave the other one. That
closes the question -- the operating system, or rather which wheel ortools
ships for it, is the only variable. There is no need to match the image's
Python version, only its platform.

### Two things that bite during setup

`Errno 2` from pip usually means the shell's working directory no longer
exists -- `getcwd() failed` in the same session is the tell. `cd ~` and retry
before suspecting the venv.

The solver writes preset JSONs to `./docs/data` relative to the working
directory, so `mkdir -p ~/gsmnp/docs/data` or an otherwise successful solve
dies at the last step.

## Keeping it that way

`backend/requirements.txt` is pinned exactly. An unpinned `ortools>=9.10`
meant the next rebuild could silently start producing a different itinerary at
the same cost, putting the published presets back out of agreement with the
service for no reason anyone would notice. Bumping a pin is deliberate:
rebuild, regenerate presets, and check the fingerprint still matches.
