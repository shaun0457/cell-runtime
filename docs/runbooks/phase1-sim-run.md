# Runbook: one Phase 1 run in the AIC sim (#28)

**Status: followed once, on 2026-11-09 (#28), from a fresh VM.** The fixes that run needed are
folded in below. Results are in `docs/phase1-run28-report.md`. Facts are against
`shaun0457/aic` @ `3ab0653`.

What one session produces: the 3 sample-config tasks run in setting A with no faults, the AIC
scorer's report, our event store with one run per goal, and one cancelled goal.

It needs cell-runtime's `main` with the readiness wait and per-goal scenarios merged (commits
"wait for TF and the F/T stream before a goal starts moving" and "give each goal of an engine
run its own scenario in the cell"). Without them the scenario ids below do not match.

## 1. The VM

- **What #28 used:**
  - GCP **g2-standard-8** (8 vCPU, 32 GB) with one **L4**, in `asia-northeast3-a`, about
    US$0.85–0.9 per hour on demand;
  - a 100 GB boot disk from the image family `ubuntu-accelerator-2404-amd64-with-nvidia-580`
    (the NVIDIA driver is preinstalled);
  - a separate 50 GB persistent disk for results, device name `results`, attached at creation
    with `auto-delete=no`.
- **T4s were out of stock** in every zone tried (`ZONE_RESOURCE_POOL_EXHAUSTED`). Plan to sweep
  zones for a GPU that is in stock: the L4 was found on the 22nd try.
- The project needs a GPU quota first: `GPUS_ALL_REGIONS` starts at 0, and a quota request
  raised it to 1.
- **On-demand for this first run**, not Spot: a preemption in the middle of debugging loses the
  session, and the eval runner's resume does not help here.
- AIC asks for 32 GB of RAM or more (`docs/getting_started.md:25-29`); a g2-standard-8 has
  exactly that. An n1-standard-8 (30 GB) is just under.
- **From Windows**, run `gcloud compute ssh` and `scp` from PowerShell. In Git Bash they fail
  on the space in the SDK's install path. `scp` needs a full remote path
  (`cellrt-sim:/home/<user>/`), not `~`.
- Without a GPU Gazebo still runs, with a much lower real-time factor
  (`docs/troubleshooting.md:18-27`). Keep the GPU: Phase 2's ACT policy reads camera images,
  and turning off global illumination would change them.

## 2. Host setup (once per VM)

As in AIC's `docs/getting_started.md:45-95`, plus what a fresh image lacks:

```bash
# results disk, mounted under $HOME so the eval container sees it (distrobox shares $HOME)
sudo blkid /dev/disk/by-id/google-results || \
  sudo mkfs.ext4 -m 0 /dev/disk/by-id/google-results    # formats only a disk with no filesystem
mkdir -p ~/results && sudo mount /dev/disk/by-id/google-results ~/results
sudo chown "$USER": ~/results && mkdir -p ~/results/aic ~/results/aic_cancel ~/results/cellrt
```

The mount does not survive a stop and start of the VM: after one, run the `mount` line again.

1. NVIDIA driver; check with `nvidia-smi`.
   - The accelerator image's driver is **compute-only**: it lacks the OpenGL/EGL libraries.
     Without them Gazebo crashes with "OpenGL 3.3 is not supported" (`component_container`
     exits -11) and the engine stalls.
   - Install the GL package that matches the driver version `nvidia-smi` prints. For driver
     580.178.04 that is:
     `sudo apt install libnvidia-gl-580-server=580.178.04-0ubuntu0.24.04.1`.
   - Create (or recreate) the distrobox after this, so the container picks the libraries up.
2. Docker Engine, then let the user run it: `sudo usermod -aG docker "$USER"`, and log in again.
3. NVIDIA Container Toolkit:
   `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`.
4. Distrobox (`sudo apt install distrobox`, `getting_started.md:77`), told to use Docker in
   every shell: `echo 'export DBX_CONTAINER_MANAGER=docker' >> ~/.bashrc`.
5. Pixi **0.67.2**: `curl -fsSL https://pixi.sh/install.sh | PIXI_VERSION=v0.67.2 bash`.
   - The fork's lockfile needs it: newer pixi (0.84.1 was tried) fails with a pixi-build API
     mismatch.
   - Then uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), and restart the shell.
6. The cell-runtime repo is public, so section 3 clones it over HTTPS with no credentials.
   #28 ran while it was private and used a git bundle; for a branch not pushed yet that still
   works: `git bundle create cell-runtime.bundle main` on the laptop, copied with
   `gcloud compute scp`, then `git clone --branch main cell-runtime.bundle ~/cell-runtime`. The
   bundle has no HEAD, so without `--branch` the clone checks nothing out.

## 3. Workspaces

```bash
mkdir -p ~/ws_aic/src && cd ~/ws_aic/src
git clone https://github.com/shaun0457/aic && cd aic && git checkout 3ab0653
pixi install --frozen     # without --frozen pixi re-solves and hits opencv/libprotobuf conflicts

git clone https://github.com/shaun0457/cell-runtime ~/cell-runtime   # or the bundle (step 2.6)
git -C ~/cell-runtime log --oneline -1                  # record this commit in #28's report
grep -q '^def session_label' ~/cell-runtime/adapters/aic/run.py &&
  grep -q '^READY_TIMEOUT_S' ~/cell-runtime/adapters/aic/world.py && echo 'merged: ok'
```

No `merged: ok` means the readiness wait or the per-goal scenarios are not on `main` yet: stop
here.

Every host shell below needs the same paths. Write them once:

```bash
cat > ~/cellrt.env <<'EOF'
export AIC_PY=$HOME/ws_aic/src/aic/.pixi/envs/default/bin/python
export CELLRT_STORE=$HOME/results/cellrt/aic.sqlite
EOF
```

Install cell-runtime into aic_model's environment without touching the fork's `pixi.toml`:
that environment has no pip, and pyyaml and pydantic are already in it, so uv adds `py_trees`,
`typer` and cell-runtime itself:

```bash
source ~/cellrt.env
uv pip install --python "$AIC_PY" -e ~/cell-runtime
```

pixi does not track this install: run the line again after any `pixi install`. Every
`pixi run` below takes `--frozen` too, or it re-solves the environment first. Then check that
the policy imports without heavy modules (aic_model has a GetState timeout), which also warms
the environment so the policy starts fast in step 5:

```bash
cd ~/ws_aic/src/aic
pixi run --frozen python -c "import sys, adapters.aic.RuntimePolicy; assert 'torch' not in sys.modules; print('ok')"
```

## 4. Start the sim with ground truth

Have the step 5 shell ready first: the engine waits only 30 s for the policy's node
(`docs/getting_started.md:161`, `model_discovery_timeout_seconds`).

```bash
docker pull ghcr.io/intrinsic-dev/aic/aic_eval:latest   # if denied, log in to ghcr.io (getting_started.md:158)
# #28 ran sha256:9aa2ffdbb946d38edde1bac7b5f02a44cfbea26e3b04a9c74e09f14c97472923 (9.2 GB);
# record the digest of every run: docker inspect --format '{{index .RepoDigests 0}}' <image>
distrobox create -r --nvidia -i ghcr.io/intrinsic-dev/aic/aic_eval:latest aic_eval
distrobox enter -r aic_eval
# inside the container:
export AIC_RESULTS_DIR=$HOME/results/aic        # the scorer writes scoring.yaml here
/entrypoint.sh gazebo_gui:=false launch_rviz:=false ground_truth:=true start_aic_engine:=true
```

The VM has no display: `gazebo_gui:=false launch_rviz:=false` are the flags AIC's own compose
file runs the eval with (`docker/docker-compose.yaml:9`). That file also passes
`shutdown_on_aic_engine_exit:=true`; it is left out here so the sim and the engine terminal stay
up after the last trial, to read the scores. Ctrl-C stops the launch. `ground_truth:=true`
publishes the TF frames the skills read. The official eval runs with `ground_truth:=false`, so
it never publishes them, and its access list denies the model the scoring and simulator topics
(`docker/aic_eval/aic_zenoh_config.json5:293-321` in the AIC repo).

## 5. Start the policy

In a second shell on the host (not in the container):

```bash
cd ~/ws_aic/src/aic
source ~/cellrt.env                       # without CELLRT_STORE the store is runs/aic.sqlite here
export CELLRT_SESSION=sample_config       # names the engine config: change it per config
pixi run --frozen ros2 run aic_model aic_model --ros-args -p use_sim_time:=true \
  -p policy:=adapters.aic.RuntimePolicy
```

Start it when the engine log prints "No node with name 'aic_model' found": the engine waits
30 s from then. The entrypoint has to run first, because it starts the Zenoh router.

The aic_model log should show `Loaded policy module adapters.aic.RuntimePolicy` and `Using
policy: RuntimePolicy` (`aic_model.py:66`, `:73`). Each goal becomes one run:
`sample_config/goal<N>/task_1/A/0/a<attempt>`. Running the same config again adds attempts to
the same scenarios (ADR 010). After the third trial the engine cleans up and shuts aic_model
down (`aic_engine.cpp:618-620`).

## 6. Where the results are

- **Engine terminal:** trial progression and each trial's score.
- **Scorer report:** `~/results/aic/scoring.yaml` on the host (`$AIC_RESULTS_DIR` in the
  container, `aic_engine.cpp:2011`), per trial and tier. Each engine run overwrites it, so every
  run gets its own directory.
- **Scorer insertion result:** when `insert_cable` returns False, the YAML says "Task not
  completed" and scores tiers 2 and 3 as 0, even if the plug is in. The truth is the
  `/scoring/insertion_event` topic in that trial's bag (`bag_trial_*`, mcap). Read it inside
  the eval container, where `rosbag2_py` is installed, from a script file: `distrobox enter`
  with an inline `bash -c` loop failed on a quoting error.
- **Our store:** `$CELLRT_STORE`. The step 5 shell is busy with aic_model, so open a new one;
  list the runs, then replay one with its force curve:

  ```bash
  source ~/cellrt.env
  "$AIC_PY" -c "from pathlib import Path; from cellrt.runtime.store import EventStore; s = EventStore(Path('$CELLRT_STORE')); print(*[(r.run_id, r.finished) for r in s.runs()], sep='\n')"
  "$(dirname "$AIC_PY")/cellrt" replay 'sample_config/goal1/task_1/A/0/a1' --db "$CELLRT_STORE" --signals
  ```

## 7. The cancel check

A second engine run, with a fresh aic_model process (the engine configures only a new,
unconfigured node). Stop aic_model (Ctrl-C) and the launch (Ctrl-C), then, in the container:

```bash
export AIC_RESULTS_DIR=$HOME/results/aic_cancel
/entrypoint.sh gazebo_gui:=false launch_rviz:=false ground_truth:=true start_aic_engine:=true
```

and in the policy shell:

```bash
cd ~/ws_aic/src/aic && source ~/cellrt.env
export CELLRT_SESSION=sample_config_cancel
pixi run --frozen ros2 run aic_model aic_model --ros-args -p use_sim_time:=true \
  -p policy:=adapters.aic.RuntimePolicy
```

During the first goal's descent, abort it from a third shell:

```bash
cd ~/ws_aic/src/aic
pixi run --frozen ros2 service list | grep cancel_task          # aic_model's service (aic_model.py:86-87)
pixi run --frozen ros2 service call /cancel_task std_srvs/srv/Empty
```

#28 called it 27 s of wall time after "Entering insert_cable_execute_callback" (sim t = 14 s,
during align, at a real-time factor of about 0.43).

Expected in the store: that run (`sample_config_cancel/goal1/task_1/A/0/a1`) ends `aborted`
with reason `goal_aborted`, and its `hold_checked` reads `hold_confirmed` (ADR 012). After an
abort aic_model can accept the next goal while the old run is still holding: record in #28's
report whether goal 2 started before goal 1's `run_finished`.

## 8. What #28's report records

The report is `docs/phase1-run28-report.md`. It found that `verify`'s first threshold (a seated
depth of 10 mm) was wrong; #58 replaced it with the seated band below. A later run records the
same list.

For each of the 3 tasks of the first run, kept apart:

- the runtime's own result (`run_finished`), the policy's return value, and the scorer's
  insertion result;
- our time over 20 N (sum of `force_over_limit_s` in the run) against the scorer's force
  penalty for the same trial, with the difference and its cause (ADR 013 lists the known ones);
- every `monitor_verdict`, as a finding;
- the real-time factor (sim time over wall time for one run);
- the height that `verify` reported for each seated plug, against the seated band `SEATED`
  (-3 to +1 mm around the port frame, within 2 mm of its axis; set from #28's seated plugs),
  and whether `insert_scripted` ended on a plug that was still moving (the rest rule compares
  one 50 ms step against `SEATED_STILL_MM`, 0.1 mm).

Also to check against the sim, from the PRs' Might miss lists: the CheatCode tip-offset quirk,
the Monitor's insertion rules near the end of the descent, the 10 s readiness wait, and the
hold check's 5 mm/s and 1.0 s.

## 9. Shut down

Stop the policy (Ctrl-C), leave the container and stop it (`distrobox stop -r aic_eval`), then
`sudo umount ~/results` and stop the VM. The results disk keeps its data while the VM is
stopped, and when the VM is deleted only if the disk is not set to be deleted with it. An
on-demand VM bills while it is running.

## 10. Phase 3 runs: a setting, a planner and a fault (#61)

The policy reads its configuration from the environment when aic_model loads it (README, "The
AIC cell"). In the step 5 shell, before starting aic_model:

```bash
export CELLRT_SESSION=s61                     # one session name per family of runs
export CELLRT_SETTING=C                       # A, B or C
export CELLRT_PLANNER=llm                     # rule, fixed or llm; read in setting C only
export NVIDIA_API_KEY="$(cat ~/.nvidia_key)"  # llm only; the file is mode 600
export CELLRT_FAULT='{"kind": "command_dropout", "onset_height_mm": 20, "duration_s": 15}'
```

A bad `CELLRT_SETTING`, `CELLRT_PLANNER` or `CELLRT_FAULT` refuses every goal before the arm
moves, with one fixed message in the aic_model log; without the key, setting C with `llm`
refuses the same way. One engine run took 4 to 6 minutes of wall time on the L4 VM; the five
faulted runs of #61, one after another, took 25 minutes.

**Recording the cameras.** The engine's bags hold no camera topics, so #61 ran its own rclpy
recorder beside the policy (`pixi run --frozen python rec61.py <dir>`): it subscribes to
`/{left,center,right}_camera/image`, scales each frame to 768 px wide and writes JPEG files named
`<receive wall ns>_<stamp ns>.jpg`, at most 20,000 per camera (about 80 MB per camera per engine
run). A second script lines the frames up with the event log by wall time and composes the three
cameras with a caption panel at 2x speed (ffmpeg, libx264, crf 27). Both run with the pixi
environment's Python and are not in the repository yet.

Results of #61: `docs/sim-results.md`.
