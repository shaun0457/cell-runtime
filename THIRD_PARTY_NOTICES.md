# Third-party notices

cell-runtime is licensed under the Apache License 2.0 (see `LICENSE`). It contains code adapted
from the projects below. Each adapted file starts with a header that names what it took, the
upstream copyright line and the license, and says the code was modified for cell-runtime.

## AIC (AI for Industry Challenge)

- Project: AIC, <https://github.com/intrinsic-dev/aic>
- Source: `aic_example_policies/aic_example_policies/ros/CheatCode.py` (the CheatCode example
  policy), checked at `intrinsic-dev/aic@c318777` and at the fork `shaun0457/aic@3ab0653`
  (the file is the same in both)
- Used in:
  - `adapters/aic/geometry.py`: `gripper_target`, `XYIntegrator`, `I_GAIN` and
    `MAX_INTEGRATOR_WINDUP`, from `calc_gripper_pose`
  - `adapters/aic/skills.py`: the approach, descent and settle steps of `insert_cable`, split
    into the `approach`, `align`, `insert_scripted` and `verify` skills
- Copyright: `Copyright (C) 2026 Intrinsic Innovation LLC`
- License: Apache License 2.0, the same license as this project; the full text is in `LICENSE`.
  The AIC repository has no `NOTICE` file.
- Changes: modified for cell-runtime (rewritten without ROS or numpy, split into skills).

Other files under `adapters/aic/` refer to AIC sources by file and line for interface facts and
parameter values, such as controller gains; the copyright and license above apply to anything
taken from them.

## transforms3d (Christoph Gohlke's transformations.py)

- Project: transforms3d, <https://github.com/matthew-brett/transforms3d>
- Source: `transforms3d/_gohlketransforms.py` (Christoph Gohlke's `transformations.py`):
  `quaternion_multiply`, `quaternion_slerp` and `_EPS`, checked at
  `matthew-brett/transforms3d@6a43a98`
- Used in: `adapters/aic/geometry.py`: `quat_multiply`, `quat_slerp` and `_EPS`
- Copyright:
  - `Copyright (c) 2006-2017, Christoph Gohlke`
  - `Copyright (c) 2006-2017, The Regents of the University of California`
- License: BSD 3-clause, as stated in the source file's header (reproduced below)
- Changes: modified for cell-runtime (rewritten on tuples without numpy; `quat_slerp` keeps only
  the defaults `spin=0`, `shortestpath=True`).

License text from the header of `transforms3d/_gohlketransforms.py`:

```text
Copyright (c) 2006-2017, Christoph Gohlke
Copyright (c) 2006-2017, The Regents of the University of California
Produced at the Laboratory for Fluorescence Dynamics
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright
  notice, this list of conditions and the following disclaimer in the
  documentation and/or other materials provided with the distribution.
* Neither the name of the copyright holders nor the names of any
  contributors may be used to endorse or promote products derived
  from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

transforms3d's own `LICENSE` covers the package as a whole:

```text
Copyright (c) 2009-2024, Matthew Brett and Christoph Gohlke
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
