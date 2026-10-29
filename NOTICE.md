# Third-party components and naming

PhaseTieing provides phase-aware Sandbox quota coordination, Sandbox-scoped
future-benefit-aware CXL placement, SSD placement extensions, Runtime integration,
page-return coordination and the experiment pipeline.

The experimental backend extends Demeter (Junliang Hu et al., SOSP 2025):
https://github.com/vtta/sosp25-demeter at commit
406d42daee7bc857d8f0192486d13763d722f001. Linux 6.10 is reconstructed with the
included patch series and final source overrides. Existing SPDX identifiers,
authorship and copyright notices are retained. See LICENSE, backend/LICENSE
and backend/licenses/GPL-2.0. Third-party files retain their applicable terms.

backend/vmm contains modified Cloud Hypervisor 36.0 sources with their
LICENSE-APACHE and LICENSE-BSD-3-Clause. The original Guest system image is
available through upstream release URLs in backend/downloads.json. Workload
images and ripgrep executables used by the experiments are not bundled here;
applicable ripgrep license texts are retained under backend/licenses/.

Released filenames and source paths use PhaseTieing. Kernel C symbols,
CONFIG_DEMETER, the DEMETER export namespace and /sys/kernel/mm/demeter remain
compatible technical interfaces. Rebuilt modules are named PhaseTieing_placement
and PhaseTieing_balloon; their /sys/module paths follow those names. Upstream
URLs and historical experimental records retain original names and hashes.
