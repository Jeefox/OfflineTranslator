# Pre-release audit — 2026-10-09

Scope: local `dev` checkout of Jeefox/OfflineTranslator. No fetch, commit, push,
merge, workflow dispatch or release publication was performed. Remote branch
freshness and CI results were not verified.

Follow-up before PR: the P2 binding-retention issue below has been fixed with
ManagedScrollableFrame; its five root-owned callbacks are removed selectively.
Tests cover repeated settings closure, repeated destroy, foreign callbacks and
constructor failure after callback registration (including Tcl command cleanup).
The build instructions/pins discrepancy was also corrected in build.bat,
build.py and README. Findings below retain the original audit history.

## Git baseline and preservation

- Branch: `dev`, HEAD `39fb71f02c5a63a2c2a1a764b61bf82c2c80cf94`.
- Local `main`: `f69e7c86e22f78b622639c3e1fa82a2604f30372`.
- Latest commits: `39fb71f` wheel scrolling, `575802f` UI lifecycle,
  `f2dbafe` runtime lifecycle/lossless translation/release checks,
  `9fa05b7` safe weights/local validation, `447bc90` stabilization.
- `main...dev`: 67 files, 4916 insertions, 1528 deletions. The changes include
  extraction of settings UI, bounded loading, protected tokens, safe model
  packaging, tests and CI. This is a substantial release delta.
- Initial worktree: `main.py` and `tests/test_model_lifecycle.py` modified
  (notification-race fix, 71 insertions/2 deletions). Preserved without staging
  or rewriting. This audit adds tests for repeated agent requests and the error
  branch, plus this report; it adds no production changes.

## Critical-path review and evidence

| Area | Code / scenario inspected | Existing validation |
| --- | --- | --- |
| ModelLoader | `model_loader.py`: one daemon thread, Condition-protected latest pending request; callback failures do not kill loader; close drops pending | `test_stabilization.py`: serial latest loading, failure callback failure, close/join |
| Runtime replacement | `main.py:_init_translator_worker/_handle_message`: queued model publication guarded by load sequence; paths captured per load using ContextVar; failed replacement retains previous runtime | `test_model_lifecycle.py`, `test_marian_lifecycle.py` |
| Concurrency | `translation_service.py:_translate_unit`: Future shares identical work; inference lock prevents overlap; cancelled owner does not cancel independent waiter | `test_concurrent_translation.py`: success, failure, cancelled producer/consumer, distinct requests |
| Generations / cancellation | `main.py:translate_thread/_release_stale_worker`: stale messages cannot release another worker's busy flag; service checks cancellation around chunks and units | `test_model_lifecycle.py`, `test_stabilization.py`, GUI suites |
| Coalescing / notification | `_handle_agent_request`, `_start_translation_internal`, `_continue_pending_translation`, done/error handlers: B pending while A owns busy; B notification must survive A success/error | Updated `test_model_lifecycle.py`: A/B race, standalone request, repeated same-text request, duplicate completion, error A |
| Marian | per-direction limits, atomic backend load, explicit local paths, safetensors-only, trust_remote_code=False, local_files_only for frozen/offline mode | `test_backends.py`, `test_marian_lifecycle.py`, `test_safe_models.py`, `test_release_models.py` |
| GGUF | lazy import, fixed Hy-MT2 prompt/direction, CPU runtime, source token budget and output limit | fake runtime in `test_llama_cpp_backend.py`; real smoke skipped |
| Segmentation / whitespace | exact source slices in `sentence_pipeline.py` and chunker; original separators reconstructed; long-word fallback | `test_long_text.py`, `test_sentence_translation.py`, randomized properties in `test_stabilization.py`, `test_clipboard_layout.py` |
| Protected tokens | unique numeric markers, punctuation handling, exactly-once restoration; damaged markers fail without caching | `test_protected_tokens.py`, `test_stabilization.py` |
| Cache | locked LRU, entry/character limits, exact-text/direction/model key; no cancelled partial unit cached | `test_stage13_translation_cache.py`, `test_concurrent_translation.py`, `test_stabilization.py` |
| Settings / damaged JSON | typed defaults, malformed/non-object JSON fallback; atomic fsync/replace save; persistence failure does not apply settings UI changes | `test_settings_integrity.py`, `test_settings_autotranslate.py`, `test_ui_review.py`, `test_dictionary.py` |
| Local models | lightweight shared Marian config/tokenizer/shard validation; traversal/unsafe weights rejected; actual loader handles invalid weight contents | `test_model_validation.py`, `test_model_registry.py`, `test_safe_models.py` |
| UI lifecycle | settings withdraw/build/layout/deiconify; tooltip hidden preparation; Linux widget wheel binds removed on close | `test_ui_review.py`, stages 5/10/12/14; no native compositor/pixel verification |
| Tray / notifications | ready state required before hide; timeout/failure/stale startup guarded; notifier return code/timeout checked; failed notification restores hidden GUI for current generation | `test_system_tray.py`, `test_notifications.py`, `test_model_lifecycle.py`; native delivery not tested |
| Offline / packaging | frozen Marian loads local-only; no model weights bundled; pinned safe conversion is a separate build step; GGUF excluded unless requested | `test_release_models.py`, `test_safe_models.py`; PyInstaller call is mocked in packaging tests |
| CI / release docs | `.github/workflows/build.yml`: Linux full suites, PR Linux/Windows build smoke, release Linux/Windows/macOS, separate models, checksums; README release instructions inspected | Static review only; no CI run or actual release build |

## Confirmed defects

### P1 — notification for pending B consumed by A (already fixed in worktree)

Before the current uncommitted fix: A starts; agent B sets the shared boolean;
B becomes pending without advancing generation; A completes in the current
generation, consumes the boolean and sends result A; B subsequently completes
without its intended notification. A's error also cleared the boolean.

The preserved fix associates the request with `_notification_text` and only
consumes it for matching `_active_text`. Clear/swap/direction resets clear both
fields. Existing generation guards, cancellation and coalescing are unchanged.
New tests also verify no second worker for repeated same active agent text and
that an error from A preserves B's notification. No additional confirmed P0/P1
was found in this audit. This conclusion is limited to inspected code and local
tests, not a guarantee against all runtime failures.

### P2 — CTkScrollableFrame global binding retention

CustomTkinter 5.2.2 constructor installs MouseWheel and Shift `bind_all`
callbacks; its `destroy()` does not unregister them. Isolated Linux/Xvfb
reproduction: create/destroy three frames, then inspect
`root.bind_all('<MouseWheel>')`: script length grows from 0 to 347, retaining
three `_mouse_wheel_all` entries. Settings' local Button-4/5 cleanup cannot remove
these upstream registrations. Accumulation is confirmed; a native visible crash
or incorrect-scroll consequence was not demonstrated. Existing settings test
checks local bindings only. Per audit scope, not fixed automatically.

### P3 — local build instructions are not consistently pinned

`build.bat` installs `requirements.txt pyinstaller`, and README's earlier build
section recommends requirements.txt; the later section and CI use
`requirements-release.txt`. Following the former leaves PyInstaller/transitive
versions unpinned. Static reproducibility issue; no build failure reproduced.
`build.py`/Marian cache docstrings also retain legacy descriptions of bundled
weights despite the current separate-model release policy. Not auto-fixed.

## Validation boundaries

- `xvfb-run -a .venv/bin/python scripts/run_tests.py`: all 27 suites,
  process exit 0; log `/tmp/offline-release-audit-tests.log`. The runner began
  before the two additional test scenarios were added; the updated
  `tests/test_model_lifecycle.py` was subsequently run separately and passed.
- Core import checks and `compileall` on application modules/backends/scripts/
  tests passed; updated lifecycle test syntax checked again after edits.
  `git diff --check` passed; final diff reviewed. Initial user changes remain.
- Local Linux/Xvfb: full standalone suite, core imports, syntax compilation,
  focused updated notification regressions and binding-retention reproduction.
- Tiny real Marian: actual safetensors loading/generation with a generated small
  model; tokenizer mocked. Not real Helsinki end-to-end quality validation.
- GGUF smoke: explicit SKIP because OFFLINE_TRANSLATOR_GGUF is unset. Historical
  benchmark files are not evidence of a current runtime smoke pass.
- Packaging tests use mocked subprocess/PyInstaller and tiny fake artifacts.
  No actual executable was built or launched during this audit.
- No native Windows/macOS test, native Linux tray/notification server test,
  Wayland test, high-DPI visual review, CUDA test or network-isolated packaged
  executable test was performed.
- CI was statically reviewed only. Action revisions, dependency availability
  on clean runners and remote checks were not verified.

## Release decision and dev → main plan

Working-tree candidate: **RELEASE WITH CAVEATS**, contingent on successful local
checks and CI/native smoke gates. Committed HEAD does not contain the P1
notification fix; do not tag the current HEAD as the audited fixed candidate.

1. Review/approve the preserved notification diff, tests and this report.
2. With separate authorization, commit to dev and open the dev → main PR.
3. Require CI full tests and real Linux/Windows build-smoke success; run macOS
   build before tagging.
4. Test native tray/hotkey/notifications and repeated settings lifecycle on each
   supported OS; test installed models with networking disabled in real bundles.
5. Run real Helsinki EN→RU/RU→EN inference; require Hy-MT2 smoke for a GGUF build.
6. Track P2 bindings cleanup and P3 documentation/pinning as explicit follow-ups.
7. After approval and green gates, merge dev → main; separately authorize tag
   and release publication. Verify archive extraction and checksums afterwards.
