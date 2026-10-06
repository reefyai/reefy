# Firmware build log fallback

The firmware workflow saves output from kernel/image compilation, standalone
AMD module builds, provider staging (including the synthetic module), and debug archive verification in a separate
`reefy-build-logs` artifact (30-day retention). The final upload uses `always()`
so ordinary compilation failures do not skip it. The wrapper runs scripts with
`-e` and `pipefail`; tee does not hide a failed build. Logs are scoped to the
run and attempt to avoid publishing a previous job's files.

Do not add environment dumps, xtrace, credentials or authenticated URLs to
these captured steps. Artifact files do not receive the Actions console's
secret masking. This captures build output only, not runner diagnostic logs.
A killed runner or failed artifact service can still prevent the upload.

## Missing native job logs

An observed failure pattern had complete job/step status and a generic exit
annotation, but the job log endpoint returned BlobNotFound. The whole-run ZIP
included the hosted test job and omitted the self-hosted build job. The same
pattern affected both branch and merged builds. This rules out a problem
limited to the gh CLI log reader. It does not prove whether the runner failed
to upload its logs or the GitHub storage service lost them or made them unavailable.

To investigate with authorized runner access, inspect matching `_diag/Worker_*`
and `_diag/Runner_*` files for log upload errors/retries, HTTP status codes,
runner termination and disk errors. Check runner version, free disk/inodes,
proxy configuration and connectivity to the documented Actions endpoints.
Do not publish raw diagnostic files without checking for credentials or signed
URLs. Do not disable TLS verification to work around upload failures.

GitHub documents the local diagnostic files:
https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/monitor-and-troubleshoot

An upstream runner report demonstrates that log uploads can fail independently
of job completion; it is a similar failure class, not a confirmed match:
https://github.com/actions/runner/issues/2228

The AMD build now uses Buildroot's host bin/sbin PATH, as Buildroot itself does.
With BTF enabled, the kernel invokes pahole during module generation. A missing
host-tool PATH is a plausible integration failure, but the original compiler
message remains unavailable. The new build and fallback logs must establish
whether this fixes it. Similar missing-pahole dependency reports:
https://bugs.archlinux.org/task/69687
https://bugs-devel.debian.org/cgi-bin/bugreport.cgi?bug=1098706

## Interrupted AMD package downloads

An AMD provider build can fail before compilation if its HTTPS package transfer
ends with curl receive error 56 (for example, an SSL bad-record-MAC error).
Default `--retry` does not cover that receive error. The read-only package GET
uses bounded `--retry-all-errors` with connection/transfer timeouts. Output goes
to a temporary file beside the destination and is renamed only after successful
completion; failed partial downloads are removed. TLS verification stays enabled.
The provider's normal package validation and compilation still run afterward.

This handles interrupted downloads; it does not establish whether a particular
TLS failure came from the server, network, proxy, or runner.
See https://curl.se/docs/manpage.html#--retry-all-errors.
